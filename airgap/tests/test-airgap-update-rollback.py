#!/usr/bin/env python3
"""Staged air-gap package update: rejection before cutover and offline rollback (#369).

Default (stub) mode drives update-package.sh with a fake zarf and a fake deploy
command, so it runs anywhere. --zarf-package PATH runs the rejection and restore
scenarios against a real signed package with the real zarf (nightly CI job).
"""

import argparse
import fnmatch
import hashlib
import os
import re
import shutil
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
exit "${STUB_DEPLOY_RC:-0}"
"""


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


def run(cmd, env):
    return subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)


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

    def env(self, tree, deploy_rc=0, extra_env=None):
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        if self.zarf is None:
            env["PATH"] = f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}"
        else:
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

    def update(self, tree, *args, deploy_rc=0, extra_env=None):
        cmd = ["bash", str(tree / "airgap/scripts/update-package.sh"), *args]
        return run(cmd, self.env(tree, deploy_rc, extra_env=extra_env))

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
