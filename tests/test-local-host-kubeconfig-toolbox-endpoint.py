#!/usr/bin/env python3
"""The local-host endpoint rewrite to 127.0.0.1 is host-only (#423): inside the
toolbox (KROPS_TOOLBOX=1, on the kind network) the CAPD-recorded endpoint
already resolves and 127.0.0.1 would be the container itself. Both
mgmt-kubeconfig (mise.toml) and the local-host kubeconfigs task must skip the
rewrite and never call the engine when KROPS_TOOLBOX=1."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDED = "https://172.18.0.9:6443"

STUB_CLUSTERCTL = """#!/usr/bin/env sh
# clusterctl get kubeconfig <name> ... -> a minimal kubeconfig with the recorded endpoint
name="$3"
cat <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: RECORDED
  name: $name
contexts:
- context:
    cluster: $name
    user: $name-admin
  name: $name-admin@$name
current-context: $name-admin@$name
users:
- name: $name-admin
  user:
    token: dummy
EOF
""".replace("RECORDED", RECORDED)

STUB_ENGINE = """#!/usr/bin/env sh
echo "ENGINE CALLED: $*" >> "$ENGINE_LOG"
case "$1" in
  port) echo "0.0.0.0:41234" ;;
  info) exit 0 ;;
esac
exit 0
"""


def sandbox(tmp: Path, config_name: str) -> dict:
    (tmp / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "mise.toml", tmp / "mise.toml")
    shutil.copy(REPO_ROOT / "mise.local-host.toml", tmp / "mise.local-host.toml")
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    (bin_dir / "clusterctl").write_text(STUB_CLUSTERCTL)
    (bin_dir / "docker").write_text(STUB_ENGINE)
    for f in bin_dir.iterdir():
        os.chmod(f, 0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        HOME=str(tmp),
        ENGINE_LOG=str(tmp / "engine.log"),
        MISE_AUTO_INSTALL="0",
        MISE_TRUSTED_CONFIG_PATHS=str(tmp),
        CONTAINER_ENGINE="docker",
    )
    for leaky in ("KROPS_TOOLBOX", "KUBECONFIG", "MGMT_KUBECONFIG", "KUBECONFIG_FILE"):
        env.pop(leaky, None)
    (tmp / ".kube").mkdir()
    subprocess.run(["mise", "trust", "-q", str(tmp / "mise.toml")], env=env, check=False)
    subprocess.run(["mise", "trust", "-q", str(tmp / config_name)], env=env, check=False)
    return env


def server_of(kubeconfig: Path, cluster: str) -> str:
    out = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "config", "view", "-o",
         f"jsonpath={{.clusters[?(@.name==\"{cluster}\")].cluster.server}}"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def run_task(tmp: Path, env: dict, argv: list[str], extra: dict) -> subprocess.CompletedProcess:
    e = dict(env)
    e.update(extra)
    return subprocess.run(["mise", *argv], cwd=tmp, env=e, capture_output=True, text=True, check=False)


def test_mgmt_kubeconfig_host_rewrites() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = sandbox(tmp, "mise.toml")
        r = run_task(tmp, env, ["run", "mgmt-kubeconfig"], {"KROPS_PROFILE": "local-host"})
        assert r.returncode == 0, r.stderr
        assert server_of(tmp / ".kube/krops-mgmt.yaml", "local-management") == "https://127.0.0.1:41234"
        assert "port local-management-lb 6443/tcp" in (tmp / "engine.log").read_text()


def test_mgmt_kubeconfig_toolbox_keeps_recorded_endpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = sandbox(tmp, "mise.toml")
        r = run_task(tmp, env, ["run", "mgmt-kubeconfig"],
                     {"KROPS_PROFILE": "local-host", "KROPS_TOOLBOX": "1"})
        assert r.returncode == 0, r.stderr
        assert server_of(tmp / ".kube/krops-mgmt.yaml", "local-management") == RECORDED
        assert not (tmp / "engine.log").exists(), "engine must not be called in the toolbox"


def test_local_host_kubeconfigs_host_rewrites() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = sandbox(tmp, "mise.local-host.toml")
        r = run_task(tmp, env, ["-E", "local-host", "run", "kubeconfigs"], {})
        assert r.returncode == 0, r.stderr
        assert server_of(tmp / "local-workload.kubeconfig", "local-workload") == "https://127.0.0.1:41234"


def test_local_host_kubeconfigs_toolbox_keeps_recorded_endpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = sandbox(tmp, "mise.local-host.toml")
        r = run_task(tmp, env, ["-E", "local-host", "run", "kubeconfigs"], {"KROPS_TOOLBOX": "1"})
        assert r.returncode == 0, r.stderr
        assert server_of(tmp / "local-workload.kubeconfig", "local-workload") == RECORDED
        assert not (tmp / "engine.log").exists(), "engine must not be called in the toolbox"


def main() -> int:
    tests = [
        test_mgmt_kubeconfig_host_rewrites,
        test_mgmt_kubeconfig_toolbox_keeps_recorded_endpoint,
        test_local_host_kubeconfigs_host_rewrites,
        test_local_host_kubeconfigs_toolbox_keeps_recorded_endpoint,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok - {test.__name__}")
        except AssertionError as exc:
            print(f"FAILED: {test.__name__}: {exc}", file=sys.stderr)
            failed += 1
    if failed:
        return 1
    print("local-host kubeconfig toolbox endpoint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
