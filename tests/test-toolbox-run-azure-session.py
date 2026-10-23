#!/usr/bin/env python3
"""Offline regression for Azure cache forwarding and the actual Arc login hook."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AzureSession(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/toolbox-run.sh", self.repo / "scripts/toolbox-run.sh")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "log"
        self.env = dict(os.environ, HOME=str(self.tmp / "home"),
                        PATH=f"{self.bin}:{os.environ['PATH']}",
                        CONTAINER_ENGINE="docker", TOOLBOX_IMAGE="toolbox:test",
                        TEST_LOG=str(self.log), AZURE_SUBSCRIPTION_ID="subscription")
        self.env.pop("AZURE_CONFIG_DIR", None)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\nimport os, sys, json\n"
                        "with open(os.environ['TEST_LOG'], 'a') as f:\n"
                        " f.write(json.dumps([os.path.basename(sys.argv[0]), sys.argv[1:], os.environ.get('AZURE_CONFIG_DIR')]) + '\\n')\n"
                        + body)
        path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def wrapper(self, expected, dotenv=None):
        self.stub("docker", "")
        if dotenv is not None:
            (self.repo / ".env").write_text(dotenv)
        result = subprocess.run(["bash", str(self.repo / "scripts/toolbox-run.sh"),
                                 "bootstrap", "azure", "--example", "two words"],
                                env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, args, forwarded = self.calls()[-1]
        self.assertIn(f"{self.env['HOME']}/.krops-azure:{expected}", args)
        self.assertEqual(forwarded, expected)
        self.assertTrue((Path(self.env['HOME']) / ".krops-azure").is_dir())
        self.assertEqual(args[-4:], ["toolbox:test", "azure", "--example", "two words"])
        self.assertIn("AZURE_CONFIG_DIR", args)

    def test_default_cache(self):
        self.wrapper("/root/.azure")

    def test_shell_directory(self):
        self.env["AZURE_CONFIG_DIR"] = "/custom/azure cache"
        self.wrapper("/custom/azure cache")

    def test_dotenv_wins(self):
        self.env["AZURE_CONFIG_DIR"] = "/shell/cache"
        self.wrapper("/dotenv/azure cache", 'AZURE_CONFIG_DIR="/dotenv/azure cache"\n')

    def test_empty_dotenv_uses_default(self):
        self.env["AZURE_CONFIG_DIR"] = "/shell/cache"
        self.wrapper("/root/.azure", "AZURE_CONFIG_DIR=\n")

    def hook(self, session=False, login_fail=False, subscription_fail=False):
        self.env.update(SESSION=str(int(session)), LOGIN_FAIL=str(int(login_fail)),
                        SUBSCRIPTION_FAIL=str(int(subscription_fail)),
                        SESSION_FILE=str(self.tmp / "session"))
        self.stub("az", """from pathlib import Path
args = sys.argv[1:]
session = os.environ['SESSION'] == '1' or Path(os.environ['SESSION_FILE']).exists()
if args[:2] == ['account', 'show']:
 sys.exit(0 if session else 1)
if args[0] == 'login':
 if os.environ['LOGIN_FAIL'] == '1': sys.exit(1)
 Path(os.environ['SESSION_FILE']).touch()
if args[:2] == ['account', 'set']:
 sys.exit(0 if session and os.environ['SUBSCRIPTION_FAIL'] != '1' else 1)
if args[:2] == ['connectedk8s', 'show']: print('https://issuer.example/')
""")
        # The hook waits for the restarted apiserver on both paths (issue
        # #506): the kubectl stub serves the OIDC discovery document with the
        # Arc issuer the az stub reports (first poll matches, no sleep) and
        # /readyz succeeds by default (the no-op stub), so the stability
        # counter reaches 5.
        self.stub("kubectl", """args = sys.argv[1:]
if args[:2] == ['get', '--raw=/.well-known/openid-configuration']:
 print('{"issuer":"https://issuer.example/"}')
""")
        self.stub("docker", "")  # Already-patched apiserver: grep matches, so no sed mutation.
        self.stub("sleep", "")
        script = tomllib.loads((ROOT / "mise.azure.toml").read_text())["tasks"]["arc-federate"]["run"]
        result = subprocess.run(["bash", "-c", script], env=self.env,
                                capture_output=True, text=True, check=False)
        az = [args for name, args, _ in self.calls() if name == "az"]
        return result, az

    def test_missing_session_logs_in_first(self):
        result, az = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(az[:3], [["account", "show"],
                                 ["login", "--use-device-code", "--output", "none"],
                                 ["account", "set", "--subscription", "subscription"]])
        self.assertTrue(any(args[:2] == ["connectedk8s", "connect"] for args in az))

    def test_empty_dotenv_reload_uses_mounted_default(self):
        # mise's env_file reload happens after the wrapper exports its default.
        # Model that boundary by passing an explicitly empty variable into the
        # actual TOML hook, then inspect the environment seen by every az call.
        self.env["AZURE_CONFIG_DIR"] = ""
        result, _ = self.hook(session=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        azure_calls = [call for call in self.calls() if call[0] == "az"]
        self.assertTrue(azure_calls)
        self.assertTrue(all(call[2] == "/root/.azure" for call in azure_calls))

    def test_existing_session_skips_login(self):
        result, az = self.hook(session=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(args[0] == "login" for args in az))
        self.assertEqual(az[:2], [["account", "show"], ["account", "set", "--subscription", "subscription"]])

    def test_failed_login_stops_before_subscription_and_arc(self):
        result, az = self.hook(login_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[0] for args in az], ["account", "login"])

    def test_failed_subscription_stops_before_arc(self):
        result, az = self.hook(session=True, subscription_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[:2] for args in az], [["account", "show"], ["account", "set"]])


if __name__ == "__main__":
    unittest.main()
