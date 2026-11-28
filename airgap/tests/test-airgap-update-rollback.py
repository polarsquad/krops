#!/usr/bin/env python3
"""Staged air-gap package update: rejection before cutover and offline rollback (#369).

Default (stub) mode drives update-package.sh with a fake zarf and a fake deploy
command, so it runs anywhere. --zarf-package PATH runs the rejection and restore
scenarios against a real signed package with the real zarf (nightly CI job).
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "airgap/scripts"
UPDATE_SH = SCRIPTS / "update-package.sh"
VERIFY_SH = SCRIPTS / "verify-package.sh"
OFFLINE_RUN_SH = SCRIPTS / "offline-run.sh"


def read_active_name():
    match = re.search(r"^ACTIVE_NAME=(\S+)$", UPDATE_SH.read_text(), re.M)
    if match is None:
        raise SystemExit("update-package.sh: ACTIVE_NAME not found")
    return match.group(1)


ACTIVE_NAME = read_active_name()
STRIPPED_ENV = ("ZARF", "ZARF_VERIFY_KEY", "AIRGAP_VERIFY_WORKFLOW_REF", "AIRGAP_DEPLOY_CMD")

STUB_ZARF = r'''#!/usr/bin/env python3
import hashlib
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(argv) + "\n")


def valid(path):
    try:
        lines = Path(path).read_bytes().split(b"\n")
    except OSError:
        return False
    if len(lines) != 4 or lines[0] != b"MAGIC" or lines[3] != b"":
        return False
    return hashlib.sha256(lines[1]).hexdigest().encode() == lines[2]


if argv[:1] == ["init"] or argv[:2] == ["package", "deploy"]:
    print("UNEXPECTED ZARF CALL: " + " ".join(argv), file=sys.stderr)
    sys.exit(99)
if argv[:2] == ["package", "verify"]:
    if os.environ.get("STUB_LITTER_STAGING"):
        Path(argv[2]).parent.joinpath("litter").touch()
    sys.exit(0 if valid(argv[2]) else 1)
if argv[:3] == ["package", "inspect", "sbom"]:
    if not valid(argv[3]):
        sys.exit(1)
    out = argv[argv.index("--output") + 1]
    Path(out, "test.json").write_text('{"artifacts":[],"schema":{"version":"10.0.0"}}')
    sys.exit(0)
if argv[:3] == ["tools", "sbom", "convert"]:
    sys.exit(0)
print("UNEXPECTED ZARF CALL: " + " ".join(argv), file=sys.stderr)
sys.exit(99)
'''

STUB_DEPLOY = """#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$STUB_DEPLOY_LOG"
if [ -n "${STUB_DEPLOY_DROP_NEXT:-}" ]; then rm -f "$(dirname "$1")/.update-state.next"; fi
exit "${STUB_DEPLOY_RC:-0}"
"""


SHIMMED = ("df", "cp", "ln", "mv", "rm", "sync", "mkdir", "mktemp", "sha256sum", "shasum")

# One generic shim per command, configured through SHIM_CONFIG. Anything a
# scenario does not target is exec'd into the real binary.
SHIM_TEMPLATE = r'''#!__PYTHON__
import json
import os
import signal
import subprocess
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
cfg = json.loads(os.environ["SHIM_CONFIG"])
# Run the real command, then kill the update script: an interruption right after it.
kill = cfg.get("kill_after")
if kill and name in kill["cmds"] and any(a.endswith(kill["suffix"]) for a in args):
    rc = subprocess.run([cfg["real"][name], *args]).returncode
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(rc)
DF_HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on"


def passthrough():
    real = cfg["real"][name]
    os.execv(real, [real, *args])


def enospc(label):
    sys.stderr.write(f"{label}: No space left on device\n")
    sys.exit(1)


def fail(label):
    sys.stderr.write(f"{label}: simulated failure\n")
    sys.exit(1)


if name == "df":
    if cfg.get("df_fail"):
        fail("df")
    target = args[-1].rstrip("/")
    if target.endswith("/airgap/archives"):
        free = cfg.get("df_free_kib")
        if cfg.get("df_free_kib_with_bak") is not None and any(f.endswith(".bak") for f in os.listdir(target)):
            free = cfg["df_free_kib_with_bak"]
        if free is not None:
            print(DF_HEADER)
            print(f"shim dev 1000000 999999 {free} 99% /shim")
            sys.exit(0)
    elif cfg.get("df_other_kib") is not None:
        print(DF_HEADER)
        print(f"{cfg.get('df_other_dev', 'shimtmp')} 1000000 999999 {cfg['df_other_kib']} 99% /other")
        sys.exit(0)
    passthrough()

if name == "cp":
    match = cfg.get("cp_fail_in")
    if match is not None and match in args[-1]:
        if cfg.get("cp_partial"):
            data = open(args[-2], "rb").read()
            with open(args[-1], "wb") as fh:
                fh.write(data[: len(data) // 2])
        enospc("cp")
    passthrough()

if name == "ln":
    if cfg.get("ln_fake_in") and cfg["ln_fake_in"] in args[-1]:
        # A regular file where a symlink was asked for: the lock must not accept it.
        open(args[-1], "w").close()
        sys.exit(0)
    match = cfg.get("ln_fail_in")
    if match is not None and match in args[-1]:
        enospc("ln")
    passthrough()

if name == "mv":
    if cfg.get("mv_fail_to") and args[-1].endswith(cfg["mv_fail_to"]):
        fail("mv")
    passthrough()

if name == "sync":
    if cfg.get("sync_fail_in") and cfg["sync_fail_in"] in args[-1]:
        fail("sync")
    passthrough()

if name == "mkdir":
    if cfg.get("mkdir_fail_in") and args[-1].endswith(cfg["mkdir_fail_in"]):
        fail("mkdir")
    passthrough()

if name == "mktemp":
    if cfg.get("mktemp_fail_in") and cfg["mktemp_fail_in"] in " ".join(args):
        fail("mktemp")
    passthrough()

if name in ("sha256sum", "shasum"):
    archives_dir = os.path.dirname(args[-1])
    if cfg.get("plant_state_tmp"):
        # A dangling symlink makes the state temp write fail with ENOENT.
        os.symlink(os.path.join(archives_dir, "missing-dir", "x"), os.path.join(archives_dir, ".update-state.tmp"))
    if cfg.get("plant_state_next"):
        os.symlink(os.path.join(archives_dir, "missing-dir", "y"), os.path.join(archives_dir, ".update-state.next"))
    if cfg.get("sha_garbage"):
        print("not-a-checksum  file")
        sys.exit(0)
    if cfg.get("sha_fail_exit"):
        sys.stdout.write(subprocess.run([cfg["real"][name], *args], capture_output=True, text=True).stdout)
        sys.exit(1)
    passthrough()

passthrough()
'''



def make_valid_package(path, body=b"release-1"):
    digest = hashlib.sha256(body).hexdigest().encode()
    path.write_bytes(b"MAGIC\n" + body + b"\n" + digest + b"\n")
    return path


def make_truncated_package(path, data, keep=None):
    path.write_bytes(data[: keep if keep is not None else len(data) // 2])
    return path


def make_corrupted_package(path, data):
    flipped = bytearray(data)
    flipped[len(flipped) // 2] ^= 0x01
    path.write_bytes(bytes(flipped))
    return path


def limit_fsize():
    # Every file write fails with EFBIG, as on a full filesystem (macOS and Linux).
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def run(cmd, env, fsize_zero=False):
    return subprocess.run(
        cmd, env=env, check=False, capture_output=True, text=True,
        preexec_fn=limit_fsize if fsize_zero else None,
    )


def check(condition, message):
    if not condition:
        raise AssertionError(message)


class Harness:
    def __init__(self, base, zarf, fixtures, pristine=None):
        self.base = base
        self.zarf = zarf
        self.v1, self.v2, self.truncated, self.corrupted = fixtures
        self.pristine = pristine
        self.trees = 0
        self.bin_dir = base / "bin"
        self.bin_dir.mkdir()
        self.deploy_stub = base / "deploy-stub.sh"
        self.deploy_stub.write_text(STUB_DEPLOY)
        self.deploy_stub.chmod(0o755)
        if zarf is None:
            stub = self.bin_dir / "zarf"
            stub.write_text(STUB_ZARF)
            stub.chmod(0o755)

    def tree(self):
        self.trees += 1
        root = self.base / f"tree-{self.trees}"
        (root / "airgap/scripts").mkdir(parents=True)
        (root / "airgap/archives").mkdir()
        shutil.copy(UPDATE_SH, root / "airgap/scripts/update-package.sh")
        shutil.copy(VERIFY_SH, root / "airgap/scripts/verify-package.sh")
        return root

    def write_shims(self):
        # Real paths are resolved from the original PATH, before any shim dir is prepended.
        shim_dir = self.base / "shims"
        shim_dir.mkdir(exist_ok=True)
        real = {name: shutil.which(name) for name in SHIMMED}
        real = {name: path for name, path in real.items() if path}
        for name in real:
            path = shim_dir / name
            path.write_text(SHIM_TEMPLATE.replace("__PYTHON__", sys.executable))
            path.chmod(0o755)
        return shim_dir, real

    def env(self, tree, deploy_rc=0, extra_env=None, shims=None):
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        path_dirs = []
        if shims is not None:
            shim_dir, real = self.write_shims()
            path_dirs.append(str(shim_dir))
            env["SHIM_CONFIG"] = json.dumps({"real": real, **shims})
        if self.zarf is None:
            path_dirs.append(str(self.bin_dir))
        if path_dirs:
            env["PATH"] = os.pathsep.join([*path_dirs, os.environ["PATH"]])
        if self.zarf is not None:
            env["ZARF"] = self.zarf
            ref = os.environ.get("AIRGAP_VERIFY_WORKFLOW_REF")
            if ref:
                env["AIRGAP_VERIFY_WORKFLOW_REF"] = ref
        env["AIRGAP_DEPLOY_CMD"] = str(self.deploy_stub)
        env["STUB_DEPLOY_LOG"] = str(tree / "deploy.log")
        env["STUB_DEPLOY_RC"] = str(deploy_rc)
        env["STUB_LOG"] = str(tree / "zarf-calls.log")
        if extra_env:
            env.update(extra_env)
        return env

    def update(self, tree, *args, deploy_rc=0, extra_env=None, shims=None, fsize_zero=False):
        cmd = ["bash", str(tree / "airgap/scripts/update-package.sh"), *args]
        env = self.env(tree, deploy_rc, extra_env=extra_env, shims=shims)
        return run(cmd, env, fsize_zero=fsize_zero)

    def write_package(self, tree, name, data):
        path = tree / name
        if self.pristine is not None and data is self.v1:
            try:
                os.link(self.pristine, path)
                return path
            except OSError:
                pass  # cross-device or unsupported fs: fall back to a copy
        path.write_bytes(data)
        return path


def archives(tree):
    return tree / "airgap/archives"


def active(tree):
    return archives(tree) / ACTIVE_NAME


def backup(tree):
    return archives(tree) / (ACTIVE_NAME + ".bak")


def state(tree):
    return archives(tree) / ".update-state"


def staging(tree):
    return archives(tree) / ".staging"


def deploy_calls(tree):
    log = tree / "deploy.log"
    if not log.exists():
        return []
    return [Path(line).name for line in log.read_text().splitlines() if line]


def clear_deploy_log(tree):
    (tree / "deploy.log").unlink(missing_ok=True)


def check_rc(result, expected, label):
    check(
        result.returncode == expected,
        f"{label}: expected exit {expected}, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}",
    )


def check_clean(tree, label):
    check(not backup(tree).exists(), f"{label}: .bak must not exist")
    check(not (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).exists(), f"{label}: .bak.sha256 must not exist")
    check(not staging(tree).exists(), f"{label}: .staging must not exist")
    check(not state(tree).exists(), f"{label}: .update-state must not exist")
    check(not os.path.lexists(archives(tree) / ".update-state.next"), f"{label}: .update-state.next must not exist")


def install_v1(h, tree):
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "install v1")
    check_rc(h.update(tree, "confirm"), 0, "confirm v1")
    return active(tree).stat().st_ino


def scenario_first_install(h):
    tree = h.tree()
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "first install")
    check(active(tree).read_bytes() == h.v1, "first install: active must equal v1")
    check(not backup(tree).exists(), "first install: .bak must not exist")
    check(not staging(tree).exists(), "first install: .staging must not exist")
    check(
        state(tree).read_text() == "phase=awaiting-confirm\nfirst_install=1\n",
        f"first install: state must await confirm, got {state(tree).read_text()!r}",
    )
    check(deploy_calls(tree) == [ACTIVE_NAME], f"first install: deploy calls {deploy_calls(tree)}")
    check_rc(h.update(tree, "confirm"), 0, "first install confirm")
    check_clean(tree, "first install confirm")


def scenario_first_install_rollback(h):
    tree = h.tree()
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "first install for rollback")
    check_rc(h.update(tree, "rollback"), 0, "first install rollback")
    check(not active(tree).exists(), "first install rollback: active must be removed")
    check_clean(tree, "first install rollback")


def rejected_before_cutover(h, data):
    tree = h.tree()
    inode = install_v1(h, tree)
    clear_deploy_log(tree)
    pkg = h.write_package(tree, "bad.tar.zst", data)
    check_rc(h.update(tree, "apply", str(pkg)), 2, "rejected apply")
    check(active(tree).read_bytes() == h.v1, "rejected: active must still equal v1")
    check(active(tree).stat().st_ino == inode, "rejected: active must keep its inode")
    check_clean(tree, "rejected")
    check(deploy_calls(tree) == [], f"rejected: deploy must not run, got {deploy_calls(tree)}")


def scenario_truncated_rejected(h):
    rejected_before_cutover(h, h.truncated)


def scenario_corrupted_rejected(h):
    rejected_before_cutover(h, h.corrupted)


def scenario_deploy_failure_restores(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    clear_deploy_log(tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg), deploy_rc=1), 3, "deploy failure")
    check(len(deploy_calls(tree)) == 1, "deploy failure: deploy must have been invoked")
    check(active(tree).read_bytes() == h.v1, "deploy failure: active must be restored to v1")
    check(active(tree).stat().st_ino == inode, "deploy failure: restore must reuse the v1 inode")
    check_clean(tree, "deploy failure")


def scenario_full_cycle(h):
    tree = h.tree()
    install_v1(h, tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "cycle apply v2")
    check(active(tree).read_bytes() == h.v2, "cycle: active must equal v2")
    check(backup(tree).read_bytes() == h.v1, "cycle: .bak must hold v1")
    check("phase=awaiting-confirm" in state(tree).read_text(), "cycle: phase must be awaiting-confirm")

    check_rc(h.update(tree, "apply", str(pkg)), 1, "cycle second apply")

    check_rc(h.update(tree, "rollback"), 0, "cycle rollback")
    check(active(tree).read_bytes() == h.v1, "cycle: rollback must restore v1")
    check(not backup(tree).exists(), "cycle: rollback must remove .bak")

    check_rc(h.update(tree, "apply", str(pkg)), 0, "cycle re-apply v2")
    check_rc(h.update(tree, "confirm"), 0, "cycle confirm")
    check(active(tree).read_bytes() == h.v2, "cycle: confirm must keep v2")
    check_clean(tree, "cycle confirm")


def simulate_cutover_crash(h, tree):
    install_v1(h, tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    os.link(active(tree), backup(tree))
    (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).write_text(hashlib.sha256(h.v1).hexdigest() + "\n")
    state(tree).write_text("phase=cutover\n")
    os.replace(pkg, active(tree))


def scenario_stale_staging_discarded(h):
    tree = h.tree()
    install_v1(h, tree)
    staging(tree).mkdir()
    (staging(tree) / "junk").write_text("leftover")
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "stale staging apply")
    check(active(tree).read_bytes() == h.v2, "stale staging: active must equal v2")
    check(not staging(tree).exists(), "stale staging: .staging must be gone")


def scenario_cutover_blocks_apply_and_rollback_restores(h):
    tree = h.tree()
    simulate_cutover_crash(h, tree)
    pkg = h.write_package(tree, "v3.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg)), 1, "cutover apply")
    check(state(tree).exists(), "cutover apply: state must be left alone")
    check_rc(h.update(tree, "rollback"), 0, "cutover rollback")
    check(active(tree).read_bytes() == h.v1, "cutover rollback: active must be v1")
    check_clean(tree, "cutover rollback")


def scenario_cutover_blocks_confirm(h):
    tree = h.tree()
    simulate_cutover_crash(h, tree)
    check_rc(h.update(tree, "confirm"), 1, "cutover confirm")
    check("phase=cutover" in state(tree).read_text(), "cutover confirm: state must stay cutover")
    check(backup(tree).exists(), "cutover confirm: .bak must stay")


def scenario_tampered_backup_blocks_rollback(h):
    tree = h.tree()
    install_v1(h, tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "tamper apply v2")
    backup(tree).write_bytes(b"tampered")
    check_rc(h.update(tree, "rollback"), 4, "tampered rollback")
    check(active(tree).read_bytes() == h.v2, "tampered rollback: active must stay v2")
    check(backup(tree).exists(), "tampered rollback: .bak must stay in place")


def scenario_naming_invariants(h):
    update_text = UPDATE_SH.read_text()
    offline_text = OFFLINE_RUN_SH.read_text()
    active_match = re.search(r"^ACTIVE_NAME=(\S+)$", update_text, re.M)
    check(active_match is not None, "naming: update-package.sh ACTIVE_NAME not found")
    active_name = active_match.group(1)
    check(
        fnmatch.fnmatch(active_name, "*.tar.zst"),
        f"naming: ACTIVE_NAME {active_name} must match *.tar.zst",
    )
    match = re.search(r'PACKAGE_INPUT="\$\{1:-\$ARCHIVES/([^}"]+)\}"', offline_text)
    check(match is not None, "naming: offline-run.sh PACKAGE_INPUT default not found")
    check(
        active_name == match.group(1),
        f"naming: ACTIVE_NAME {active_name} != offline-run.sh default {match.group(1)}",
    )
    artifacts = (
        active_name + ".bak",
        active_name + ".bak.sha256",
        ".staging",
        ".update-state",
        ".update.lock",
    )
    for name in artifacts:
        check(
            not fnmatch.fnmatch(name, "*.tar"),
            f"naming: {name} must not match *.tar (stage-and-create-cluster.sh docker-load glob)",
        )


def scenario_explicit_zarf_honored(h):
    """ZARF env var is used even when a decoy zarf is first on PATH."""
    t = h.tree()
    decoy_dir = h.base / "decoy-bin"
    decoy_dir.mkdir()
    decoy = decoy_dir / "zarf"
    decoy.write_text("#!/bin/sh\necho DECOY >&2; exit 98\n")
    decoy.chmod(0o755)
    pkg = h.write_package(t, "v1.tar.zst", h.v1)
    result = h.update(t, "apply", str(pkg), extra_env={
        "PATH": f"{decoy_dir}{os.pathsep}{os.environ['PATH']}",
        "ZARF": str(h.bin_dir / "zarf"),
    })
    check_rc(result, 0, "explicit zarf")
    check(active(t).read_bytes() == h.v1, "explicit zarf: active must equal v1")
    check("DECOY" not in result.stderr, f"explicit zarf: decoy zarf was used: {result.stderr}")


def scenario_real_mode_forwards_workflow_ref(h):
    """In real-zarf mode, AIRGAP_VERIFY_WORKFLOW_REF from the outer env is forwarded."""
    t = h.tree()
    fake_ref = "example/fork/.github/workflows/air-gapped.yml@refs/heads/feature"
    # Build a real-mode harness (zarf= points at stub binary so it still runs offline)
    real_dir = h.base / "real-mode"
    real_dir.mkdir()
    real = Harness(real_dir, str(h.bin_dir / "zarf"), (h.v1, h.v2, h.truncated, h.corrupted))
    real_t = real.tree()
    real_pkg = real.write_package(real_t, "v1.tar.zst", real.v1)
    with mock.patch.dict(os.environ, {"AIRGAP_VERIFY_WORKFLOW_REF": fake_ref}):
        # Real-mode harness forwards the ref via auto-injection (no extra_env)
        check(real.env(real_t).get("AIRGAP_VERIFY_WORKFLOW_REF") == fake_ref, "real mode: ref must be forwarded")
        # Stub-mode harness strips it
        check("AIRGAP_VERIFY_WORKFLOW_REF" not in h.env(t), "stub mode: ref must be stripped")
        # The ref reaches the zarf subprocess via auto-injection, not extra_env
        check_rc(real.update(real_t, "apply", str(real_pkg)), 0, "forward ref apply")
    calls_log = real_t / "zarf-calls.log"
    check(calls_log.exists() and fake_ref in calls_log.read_text(), "real mode: ref must reach zarf verify")


def scenario_unknown_phase_blocks_apply(h):
    """apply() rejects any state file with an unknown or corrupted phase."""
    t = h.tree()
    install_v1(h, t)
    clear_deploy_log(t)
    # Write a corrupted phase
    state(t).write_text("phase=bogus\n")
    pkg = h.write_package(t, "v2.tar.zst", h.v2)
    result = h.update(t, "apply", str(pkg))
    check_rc(result, 1, "unknown phase apply")
    check(active(t).read_bytes() == h.v1, "unknown phase: active must stay v1")
    check(deploy_calls(t) == [], f"unknown phase: deploy must not run, got {deploy_calls(t)}")
    check(state(t).read_text() == "phase=bogus\n", "unknown phase: state must be left alone")
    check(not backup(t).exists(), "unknown phase: .bak must not exist")
    check("unexpected state" in result.stderr, f"unknown phase: stderr {result.stderr}")
    # Also test with empty state file
    state(t).write_text("")
    result2 = h.update(t, "apply", str(pkg))
    check_rc(result2, 1, "empty state apply")
    check("unexpected state" in result2.stderr, f"empty state: stderr {result2.stderr}")


def scenario_littered_staging_still_deploys(h):
    """apply() succeeds even when staging is non-empty after the swap (rm -rf, not rmdir)."""
    t = h.tree()
    install_v1(h, t)
    pkg = h.write_package(t, "v2.tar.zst", h.v2)
    result = h.update(t, "apply", str(pkg), extra_env={"STUB_LITTER_STAGING": "1"})
    check_rc(result, 0, "littered staging apply")
    check(active(t).read_bytes() == h.v2, "littered staging: active must equal v2")
    check(not staging(t).exists(), "littered staging: .staging must be gone")
    check(len(deploy_calls(t)) == 2, f"littered staging: expected 2 deploy calls, got {deploy_calls(t)}")


def reset_logs(tree):
    clear_deploy_log(tree)
    (tree / "zarf-calls.log").unlink(missing_ok=True)


def check_untouched(h, tree, inode, label):
    check(active(tree).read_bytes() == h.v1, f"{label}: active must still equal v1")
    check(active(tree).stat().st_ino == inode, f"{label}: active must keep its inode")
    check(not (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).exists(), f"{label}: .bak.sha256 must not exist")
    check(not os.path.lexists(archives(tree) / ".update-state.tmp"), f"{label}: .update-state.tmp must not exist")
    check(deploy_calls(tree) == [], f"{label}: deploy must not run, got {deploy_calls(tree)}")


def rerun_after_failure(h, tree, pkg, label):
    check_rc(h.update(tree, "apply", str(pkg)), 0, f"{label} rerun")
    check(active(tree).read_bytes() == h.v2, f"{label} rerun: active must equal v2")
    check(deploy_calls(tree) == [ACTIVE_NAME], f"{label} rerun: deploy calls {deploy_calls(tree)}")


def scenario_preflight_refuses_low_space(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    result = h.update(tree, "apply", str(pkg), shims={"df_free_kib": 1})
    check_rc(result, 5, "preflight apply")
    check("insufficient storage" in result.stderr, f"preflight: stderr {result.stderr}")
    check_clean(tree, "preflight")
    check(not (tree / "zarf-calls.log").exists(), "preflight: zarf must not be called")
    check_untouched(h, tree, inode, "preflight")
    rerun_after_failure(h, tree, pkg, "preflight")


def scenario_disk_full_during_staging_copy(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    result = h.update(tree, "apply", str(pkg), shims={"cp_fail_in": "/.staging/", "cp_partial": True})
    check_rc(result, 2, "staging disk full")
    check("No space left on device" in result.stderr, f"staging disk full: stderr {result.stderr}")
    check_clean(tree, "staging disk full")
    check_untouched(h, tree, inode, "staging disk full")
    rerun_after_failure(h, tree, pkg, "staging disk full")


def scenario_disk_full_during_backup_copy(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    shims = {"ln_fail_in": ACTIVE_NAME + ".bak", "cp_fail_in": ACTIVE_NAME + ".bak", "cp_partial": True}
    result = h.update(tree, "apply", str(pkg), shims=shims)
    check_rc(result, 2, "backup disk full")
    check_clean(tree, "backup disk full")
    check_untouched(h, tree, inode, "backup disk full")
    rerun_after_failure(h, tree, pkg, "backup disk full")


def scenario_state_write_failure(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    result = h.update(tree, "apply", str(pkg), shims={"mv_fail_to": ".update-state"})
    check_rc(result, 2, "state write failure")
    check_clean(tree, "state write failure")
    check_untouched(h, tree, inode, "state write failure")
    rerun_after_failure(h, tree, pkg, "state write failure")


def lock_present(tree):
    return os.path.lexists(archives(tree) / ".update.lock")


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def apply_v2(h, tree, **kwargs):
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    return h.update(tree, "apply", str(pkg), **kwargs)


def apply_failure_case(h, label, shims, rc):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    result = apply_v2(h, tree, shims=shims)
    check_rc(result, rc, label)
    check_clean(tree, label)
    check_untouched(h, tree, inode, label)
    rerun_after_failure(h, tree, h.write_package(tree, "v2.tar.zst", h.v2), label)


def scenario_stale_backup_without_state(h):
    # Provably redundant leftovers (same inode, identical, interrupted copy) are removed.
    seeds = (
        ("hard link to active", lambda tree: os.link(active(tree), backup(tree))),
        ("identical copy", lambda tree: backup(tree).write_bytes(h.v1)),
        ("interrupted copy", lambda tree: backup(tree).write_bytes(h.v1[: len(h.v1) // 2])),
        ("empty file", lambda tree: backup(tree).write_bytes(b"")),
    )
    for label, seed in seeds:
        tree = h.tree()
        install_v1(h, tree)
        seed(tree)
        (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).write_text("0" * 64 + "\n")
        check_rc(apply_v2(h, tree), 0, f"stale backup ({label}) apply")
        check(backup(tree).read_bytes() == h.v1, f"stale backup ({label}): .bak must hold the previous active")
        check(active(tree).read_bytes() == h.v2, f"stale backup ({label}): active must equal v2")
        check_rc(h.update(tree, "rollback"), 0, f"stale backup ({label}) rollback")
        check(active(tree).read_bytes() == h.v1, f"stale backup ({label}): rollback must restore v1")


def scenario_differing_backup_is_kept(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    leftover = b"the only copy of an older archive"
    backup(tree).write_bytes(leftover)
    reset_logs(tree)
    result = apply_v2(h, tree)
    check_rc(result, 1, "differing backup refused")
    check("move it aside" in result.stderr, f"differing backup: stderr {result.stderr}")
    check(backup(tree).read_bytes() == leftover, "differing backup: .bak must be kept byte for byte")
    check_untouched(h, tree, inode, "differing backup")
    corrupt = h.write_package(tree, "corrupt.tar.zst", h.corrupted)
    check_rc(h.update(tree, "apply", str(corrupt)), 1, "differing backup with a bad package")
    check(backup(tree).read_bytes() == leftover, "differing backup: bad package must keep .bak")
    low = apply_v2(h, tree, shims={"df_free_kib": 1})
    check_rc(low, 1, "differing backup with a refused preflight")
    check(backup(tree).read_bytes() == leftover, "differing backup: refused preflight must keep .bak")
    check(active(tree).read_bytes() == h.v1, "differing backup: active must stay v1")


def scenario_apply_backup_path_refused(h):
    for label, make in (
        ("separate copy", lambda tree: backup(tree).write_bytes(h.v2)),
        ("hard link to active", lambda tree: os.link(active(tree), backup(tree))),
    ):
        tree = h.tree()
        inode = install_v1(h, tree)
        make(tree)
        before = backup(tree).read_bytes()
        result = h.update(tree, "apply", str(backup(tree)))
        check_rc(result, 1, f"apply of the backup path ({label})")
        check("is the backup" in result.stderr, f"apply of the backup path ({label}): stderr {result.stderr}")
        check(backup(tree).exists() and backup(tree).read_bytes() == before, f"apply of the backup path ({label}): .bak must be kept")
        check(active(tree).read_bytes() == h.v1 and active(tree).stat().st_ino == inode, f"apply of the backup path ({label}): active must stay v1")


def scenario_first_install_state_failure_keeps_orphan(h):
    tree = h.tree()
    backup(tree).write_bytes(b"orphan")
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    result = h.update(tree, "apply", str(pkg), shims={"mv_fail_to": ".update-state"})
    check_rc(result, 2, "orphan backup with first install state failure")
    check(backup(tree).read_bytes() == b"orphan", "orphan backup: first install failure must keep .bak")
    check(not active(tree).exists(), "orphan backup: active must not exist")
    check(not state(tree).exists(), "orphan backup: .update-state must not exist")


def scenario_state_next_write_failure(h):
    apply_failure_case(h, "state next write failure", {"plant_state_next": True}, 2)


def scenario_lock_without_symlinks(h):
    tree = h.tree()
    install_v1(h, tree)
    no_links = {"ln_fail_in": ".update.lock"}
    check_rc(apply_v2(h, tree, shims=no_links), 0, "lock fallback apply")
    check(active(tree).read_bytes() == h.v2, "lock fallback: active must equal v2")
    check(not lock_present(tree), "lock fallback: lock must be released")
    check_rc(h.update(tree, "rollback", shims=no_links), 0, "lock fallback rollback")
    check(active(tree).read_bytes() == h.v1, "lock fallback rollback: active must be v1")


def scenario_lock_link_not_symlink_refused(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    result = apply_v2(h, tree, shims={"ln_fake_in": ".update.lock"})
    check_rc(result, 1, "lock that is not a symlink")
    check("cannot create" in result.stderr, f"lock not a symlink: stderr {result.stderr}")
    check_untouched(h, tree, inode, "lock not a symlink")


def scenario_rollback_interrupted_after_backup_removed(h):
    tree = h.tree()
    install_v1(h, tree)
    os.link(active(tree), backup(tree))
    (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).write_text(hashlib.sha256(h.v1).hexdigest() + "\n")
    state(tree).write_text("phase=cutover\n")
    (archives(tree) / ".update-state.next").write_text("phase=awaiting-confirm\n")
    interrupted = h.update(tree, "rollback", shims={"kill_after": {"cmds": ["rm"], "suffix": ".bak"}})
    check(interrupted.returncode == -signal.SIGKILL, f"same-inode rollback: interrupt did not happen, rc {interrupted.returncode}")
    check(not backup(tree).exists(), "same-inode rollback: .bak must be consumed before the interrupt")
    check(state(tree).exists(), "same-inode rollback: state must survive the interrupt")
    check_rc(h.update(tree, "rollback"), 0, "same-inode rollback rerun")
    check(active(tree).read_bytes() == h.v1, "same-inode rollback rerun: active must be v1")
    check_clean(tree, "same-inode rollback rerun")


def scenario_rollback_interrupted_after_checksum_removed(h):
    # State is removed before the checksum, so an interrupt after the checksum leaves no
    # state behind to rerun against; the rerun reports that nothing is in progress.
    tree = h.tree()
    install_v1(h, tree)
    os.link(active(tree), backup(tree))
    (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).write_text(hashlib.sha256(h.v1).hexdigest() + "\n")
    state(tree).write_text("phase=cutover\n")
    interrupted = h.update(tree, "rollback", shims={"kill_after": {"cmds": ["rm"], "suffix": ".sha256"}})
    check(interrupted.returncode == -signal.SIGKILL, f"checksum rollback: interrupt did not happen, rc {interrupted.returncode}")
    check(not state(tree).exists(), "checksum rollback: state must be removed before the checksum")
    check(active(tree).read_bytes() == h.v1, "checksum rollback: active must be v1")
    check_rc(h.update(tree, "rollback"), 1, "checksum rollback rerun reports nothing in progress")
    check(active(tree).read_bytes() == h.v1, "checksum rollback rerun: active must be v1")


def scenario_rollback_with_missing_backup_fails(h):
    # A missing .bak whose checksum does not match the active archive is not a finished
    # restore: rollback must exit 4 and keep the state and the recorded checksum.
    tree = h.tree()
    install_v1(h, tree)
    check_rc(apply_v2(h, tree), 0, "missing backup setup apply")
    backup(tree).unlink()
    result = h.update(tree, "rollback")
    check_rc(result, 4, "missing backup rollback")
    check(state(tree).exists(), "missing backup rollback: state must be kept")
    check((archives(tree) / (ACTIVE_NAME + ".bak.sha256")).exists(), "missing backup rollback: checksum must be kept")
    check(active(tree).read_bytes() == h.v2, "missing backup rollback: active must stay v2")


def scenario_rollback_interrupted_after_swap(h):
    tree = h.tree()
    install_v1(h, tree)
    check_rc(apply_v2(h, tree), 0, "post-swap setup apply")
    interrupted = h.update(tree, "rollback", shims={"kill_after": {"cmds": ["mv"], "suffix": ".bak"}})
    check(interrupted.returncode == -signal.SIGKILL, f"post-swap rollback: interrupt did not happen, rc {interrupted.returncode}")
    check(active(tree).read_bytes() == h.v1, "post-swap rollback: the swap must have happened before the interrupt")
    check(state(tree).exists(), "post-swap rollback: state must survive the interrupt")
    check_rc(h.update(tree, "rollback"), 0, "post-swap rollback rerun")
    check(active(tree).read_bytes() == h.v1, "post-swap rollback rerun: active must be v1")
    check_clean(tree, "post-swap rollback rerun")


def scenario_stale_full_copy_does_not_block_preflight(h):
    tree = h.tree()
    install_v1(h, tree)
    backup(tree).write_bytes(h.v1)
    result = apply_v2(h, tree, shims={"df_free_kib": 2048, "df_free_kib_with_bak": 100})
    check_rc(result, 0, "stale full copy apply")
    check(active(tree).read_bytes() == h.v2, "stale full copy: active must equal v2")


def scenario_first_install_keeps_orphan_backup(h):
    tree = h.tree()
    backup(tree).write_bytes(b"orphan")
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "orphan backup first install")
    check(backup(tree).read_bytes() == b"orphan", "orphan backup: first install must keep .bak")
    check(active(tree).read_bytes() == h.v1, "orphan backup: active must equal v1")
    check_rc(h.update(tree, "confirm"), 0, "orphan backup confirm")
    check_clean(tree, "orphan backup confirm")


def scenario_preflight_negative_space(h):
    tree = h.tree()
    inode = install_v1(h, tree)
    reset_logs(tree)
    result = apply_v2(h, tree, shims={"df_free_kib": -5})
    check_rc(result, 5, "negative free space")
    check("have 0 KiB" in result.stderr, f"negative free space: stderr {result.stderr}")
    check_untouched(h, tree, inode, "negative free space")


def scenario_unreadable_df_refuses(h):
    apply_failure_case(h, "unreadable df", {"df_fail": True}, 5)


def scenario_tmpdir_warning(h):
    cases = (("shim dev", 8000, True), ("shimtmp", 8000, False), ("shim dev", 1000, True))
    for device, tmp_free, warns in cases:
        tree = h.tree()
        pkg = make_valid_package(tree / "big.tar.zst", b"x" * (4 * 1024 * 1024))
        result = h.update(tree, "apply", str(pkg), shims={
            "df_free_kib": 10**6, "df_other_kib": tmp_free, "df_other_dev": device,
        })
        check_rc(result, 0, f"tmpdir warning ({device}, {tmp_free} KiB)")
        check(
            ("zarf extraction may need" in result.stderr) == warns,
            f"tmpdir warning ({device}, {tmp_free} KiB): warning={warns}, stderr {result.stderr}",
        )
        check(active(tree).read_bytes() == pkg.read_bytes(), "tmpdir warning: active must be the package")


def scenario_lock_handling(h):
    tree = h.tree()
    install_v1(h, tree)
    lock = archives(tree) / ".update.lock"
    os.symlink(str(os.getpid()), lock)
    result = apply_v2(h, tree)
    check_rc(result, 1, "live lock")
    check("another update is running" in result.stderr, f"live lock: stderr {result.stderr}")
    check(os.path.islink(lock), "live lock: lock must be left alone")

    tree = h.tree()
    install_v1(h, tree)
    lock = archives(tree) / ".update.lock"
    os.symlink(str(dead_pid()), lock)
    check_rc(apply_v2(h, tree), 0, "dead symlink lock")
    check(not os.path.lexists(lock), "dead symlink lock: lock must be released after the run")

    tree = h.tree()
    install_v1(h, tree)
    lock = archives(tree) / ".update.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{dead_pid()}\n")
    check_rc(apply_v2(h, tree), 0, "old lock directory with dead pid")
    check(not os.path.lexists(lock), "old lock directory: lock must be released after the run")

    tree = h.tree()
    install_v1(h, tree)
    lock = archives(tree) / ".update.lock"
    lock.mkdir()
    result = apply_v2(h, tree)
    check_rc(result, 1, "old lock directory without pid")
    check("no readable owner" in result.stderr, f"old lock directory: stderr {result.stderr}")
    check(lock.is_dir(), "old lock directory without pid: must be left alone")


def scenario_full_disk_lock_and_rollback(h):
    tree = h.tree()
    install_v1(h, tree)
    check_rc(apply_v2(h, tree), 0, "full disk setup apply")
    reset_logs(tree)
    check_rc(h.update(tree, "rollback", fsize_zero=True), 0, "full disk rollback")
    check(active(tree).read_bytes() == h.v1, "full disk rollback: active must be v1")
    check_clean(tree, "full disk rollback")
    check(not lock_present(tree), "full disk rollback: lock must be released")

    inode = active(tree).stat().st_ino
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg), fsize_zero=True), 2, "full disk apply")
    check_clean(tree, "full disk apply")
    check_untouched(h, tree, inode, "full disk apply")
    check(not lock_present(tree), "full disk apply: lock must be released")
    rerun_after_failure(h, tree, pkg, "full disk apply")
    check_rc(h.update(tree, "confirm", fsize_zero=True), 0, "full disk confirm")
    check_clean(tree, "full disk confirm")
    check(active(tree).read_bytes() == h.v2, "full disk confirm: active must stay v2")


def scenario_pre_swap_crash_rollback(h):
    tree = h.tree()
    install_v1(h, tree)
    os.link(active(tree), backup(tree))
    (archives(tree) / (ACTIVE_NAME + ".bak.sha256")).write_text(hashlib.sha256(h.v1).hexdigest() + "\n")
    state(tree).write_text("phase=cutover\n")
    (archives(tree) / ".update-state.next").write_text("phase=awaiting-confirm\n")
    check_rc(h.update(tree, "rollback"), 0, "pre-swap rollback")
    check(active(tree).read_bytes() == h.v1, "pre-swap rollback: active must be v1")
    check_clean(tree, "pre-swap rollback")


def scenario_post_deploy_state_failure(h):
    tree = h.tree()
    install_v1(h, tree)
    reset_logs(tree)
    result = apply_v2(h, tree, extra_env={"STUB_DEPLOY_DROP_NEXT": "1"})
    check_rc(result, 1, "post-deploy state failure")
    check("state is still phase=cutover" in result.stderr, f"post-deploy: stderr {result.stderr}")
    check(state(tree).read_text().strip() == "phase=cutover", f"post-deploy: state {state(tree).read_text()!r}")
    check(active(tree).read_bytes() == h.v2, "post-deploy: the deploy ran, so active must be v2")
    check(not os.path.lexists(archives(tree) / ".update-state.next"), "post-deploy: .update-state.next must be gone")
    check_rc(h.update(tree, "confirm"), 1, "post-deploy confirm refused")
    check_rc(h.update(tree, "rollback"), 0, "post-deploy rollback")
    check(active(tree).read_bytes() == h.v1, "post-deploy rollback: active must be v1")
    check_clean(tree, "post-deploy rollback")


def scenario_state_temp_write_failure(h):
    apply_failure_case(h, "state temp write failure", {"plant_state_tmp": True}, 2)


def scenario_checksum_garbage(h):
    apply_failure_case(h, "garbage checksum", {"sha_garbage": True}, 2)


def scenario_checksum_tool_failure(h):
    apply_failure_case(h, "checksum tool failure", {"sha_fail_exit": True}, 2)


def scenario_sync_failure(h):
    apply_failure_case(h, "sync failure", {"sync_fail_in": "/.staging/"}, 2)


def scenario_sbom_tmpdir_failure(h):
    apply_failure_case(h, "sbom tmpdir failure", {"mktemp_fail_in": "airgap-update-sbom"}, 2)


def scenario_staging_mkdir_failure(h):
    apply_failure_case(h, "staging mkdir failure", {"mkdir_fail_in": ".staging"}, 2)


def scenario_first_install_state_failure(h):
    tree = h.tree()
    pkg = h.write_package(tree, "v1.tar.zst", h.v1)
    result = h.update(tree, "apply", str(pkg), shims={"mv_fail_to": ".update-state"})
    check_rc(result, 2, "first install state failure")
    check_clean(tree, "first install state failure")
    check(not active(tree).exists(), "first install state failure: active must not exist")
    check(deploy_calls(tree) == [], f"first install state failure: deploy must not run, got {deploy_calls(tree)}")
    check_rc(h.update(tree, "apply", str(pkg)), 0, "first install state failure rerun")


def scenario_rollback_needs_no_space(h):
    tree = h.tree()
    install_v1(h, tree)
    pkg = h.write_package(tree, "v2.tar.zst", h.v2)
    check_rc(h.update(tree, "apply", str(pkg)), 0, "no-space rollback apply")
    result = h.update(tree, "rollback", shims={"df_fail": True, "cp_fail_in": ""})
    check_rc(result, 0, "no-space rollback")
    check(active(tree).read_bytes() == h.v1, "no-space rollback: active must be v1")
    check_clean(tree, "no-space rollback")


def stub_scenarios():
    return [
        ("first install succeeds", scenario_first_install),
        ("first install rollback removes the archive", scenario_first_install_rollback),
        ("truncated package rejected before cutover", scenario_truncated_rejected),
        ("corrupted package rejected before cutover", scenario_corrupted_rejected),
        ("deploy failure restores prior archive", scenario_deploy_failure_restores),
        ("full update cycle: apply, refuse, rollback, re-apply, confirm", scenario_full_cycle),
        ("stale staging is discarded", scenario_stale_staging_discarded),
        ("cutover crash: apply refused, rollback restores", scenario_cutover_blocks_apply_and_rollback_restores),
        ("cutover crash: confirm refused", scenario_cutover_blocks_confirm),
        ("tampered backup: rollback exits 4 and keeps active", scenario_tampered_backup_blocks_rollback),
        ("naming invariants", scenario_naming_invariants),
        ("explicit ZARF honored over decoy on PATH", scenario_explicit_zarf_honored),
        ("real-mode harness forwards AIRGAP_VERIFY_WORKFLOW_REF", scenario_real_mode_forwards_workflow_ref),
        ("unknown or empty state phase blocks apply", scenario_unknown_phase_blocks_apply),
        ("littered staging after swap still deploys", scenario_littered_staging_still_deploys),
        ("preflight refuses low archives space before any write", scenario_preflight_refuses_low_space),
        ("disk full during staging copy leaves active untouched", scenario_disk_full_during_staging_copy),
        ("disk full during backup copy leaves no partial files", scenario_disk_full_during_backup_copy),
        ("state write failure leaves no partial files", scenario_state_write_failure),
        ("stale backup without state: provably redundant copies removed", scenario_stale_backup_without_state),
        ("differing backup without state is kept; bad or refused package changes nothing", scenario_differing_backup_is_kept),
        ("apply of the backup path is refused and keeps the backup", scenario_apply_backup_path_refused),
        ("first install state failure keeps an orphan backup", scenario_first_install_state_failure_keeps_orphan),
        ("state next write failure leaves no partial files", scenario_state_next_write_failure),
        ("lock falls back to a directory without symlinks", scenario_lock_without_symlinks),
        ("lock that is not a symlink is refused", scenario_lock_link_not_symlink_refused),
        ("rollback interrupted after the backup was consumed (same inode) is finished", scenario_rollback_interrupted_after_backup_removed),
        ("rollback with a missing backup exits 4 and keeps state", scenario_rollback_with_missing_backup_fails),
        ("rollback interrupted after the swap is finished", scenario_rollback_interrupted_after_swap),
        ("rollback interrupted after the checksum is removed leaves nothing stuck", scenario_rollback_interrupted_after_checksum_removed),
        ("stale full copy does not block the preflight", scenario_stale_full_copy_does_not_block_preflight),
        ("first install keeps an orphan backup", scenario_first_install_keeps_orphan_backup),
        ("preflight refuses negative free space as zero", scenario_preflight_negative_space),
        ("unreadable df on apply exits 5", scenario_unreadable_df_refuses),
        ("TMPDIR warning uses 2.6x on a shared filesystem", scenario_tmpdir_warning),
        ("lock: live, dead, and old-format holders", scenario_lock_handling),
        ("full disk: rollback, confirm and lock work; apply exits 2", scenario_full_disk_lock_and_rollback),
        ("pre-swap crash: rollback removes a link-only backup", scenario_pre_swap_crash_rollback),
        ("post-deploy state failure: exit 1 at cutover, rollback recovers", scenario_post_deploy_state_failure),
        ("state temp write failure leaves no partial files", scenario_state_temp_write_failure),
        ("garbage checksum leaves no partial files", scenario_checksum_garbage),
        ("checksum tool failure leaves no partial files", scenario_checksum_tool_failure),
        ("sync failure leaves no partial files", scenario_sync_failure),
        ("SBOM tmpdir failure exits 2", scenario_sbom_tmpdir_failure),
        ("staging mkdir failure exits 2", scenario_staging_mkdir_failure),
        ("first install state failure leaves no archive", scenario_first_install_state_failure),
        ("rollback needs no free space", scenario_rollback_needs_no_space),
    ]


def real_scenarios():
    return [
        ("truncated package rejected before cutover (real zarf)", scenario_truncated_rejected),
        ("corrupted package rejected before cutover (real zarf)", scenario_corrupted_rejected),
        ("deploy failure restores prior archive (real zarf)", scenario_deploy_failure_restores),
    ]


def run_scenarios(scenarios, h_factory, base):
    failed = 0
    for index, (name, fn) in enumerate(scenarios):
        scenario_dir = base / f"scenario-{index}"
        scenario_dir.mkdir()
        h = h_factory(scenario_dir)
        try:
            fn(h)
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {name}\n  {exc}")
        except Exception as exc:  # a crash inside a scenario is a failure, not a pass
            failed += 1
            print(f"FAIL: {name}\n  {type(exc).__name__}: {exc}")
        else:
            print(f"PASS: {name}")
        finally:
            shutil.rmtree(scenario_dir, ignore_errors=True)
    return failed


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zarf-package", type=Path, help="real signed package for real-zarf mode")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="airgap-update-rollback-") as tmp:
        base = Path(tmp)
        if args.zarf_package is None:
            zarf = None
            pristine = None
            v1 = make_valid_package(base / "fixture-v1", b"release-1").read_bytes()
            v2 = make_valid_package(base / "fixture-v2", b"release-2").read_bytes()
            truncated = make_truncated_package(base / "fixture-truncated", v1).read_bytes()
            corrupted = make_corrupted_package(base / "fixture-corrupted", v1).read_bytes()
            scenarios = stub_scenarios()
            print("mode: stub (fake zarf and deploy command)")
        else:
            zarf = os.environ.get("ZARF") or shutil.which("zarf")
            if not zarf:
                print("FAIL: real-zarf mode needs ZARF or zarf on PATH", file=sys.stderr)
                return 1
            # Keep the pristine package as the single on-disk copy: scenarios
            # that need the full bytes hardlink it (write_package), and the
            # truncated/corrupted variants are derived in RAM. The runner
            # cannot hold several 1.7 GB copies alongside the workspace.
            real = args.zarf_package.read_bytes()
            v1 = real
            v2 = real
            truncated = real[:1000]
            corrupted_buf = bytearray(real)
            corrupted_buf[len(corrupted_buf) // 2] ^= 0x01
            corrupted = bytes(corrupted_buf)
            pristine = args.zarf_package.resolve()
            scenarios = real_scenarios()
            print(f"mode: real zarf ({zarf}) on {args.zarf_package.name}")
            print(f"disk: {shutil.disk_usage(base).free / 2**30:.1f} GiB free under {base}")

        def h_factory(scenario_dir):
            return Harness(scenario_dir, zarf, (v1, v2, truncated, corrupted), pristine=pristine)

        failed = run_scenarios(scenarios, h_factory, base)

    if failed:
        print(f"{failed} scenario(s) failed")
        return 1
    print("all update-rollback scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
