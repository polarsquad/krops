#!/usr/bin/env python3
"""Exercise resolve-clusterctl.sh, shared by 3 Zarf onDeploy actions (#354).

The script hardcodes /tmp/krops-airgap/bin/clusterctl-<os>-arm64, mirroring
where Zarf actually stages it at deploy time, so this test stages fakes at
that same path rather than an isolated temp dir. It always cleans up, and
skips instead of failing if that path is already in real use.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "airgap/scripts/resolve-clusterctl.sh"
STAGE_DIR = Path("/tmp/krops-airgap")
BIN_DIR = STAGE_DIR / "bin"
CLUSTERCTL = BIN_DIR / "clusterctl-linux-arm64"

FAKE_UNAME = """#!/usr/bin/env sh
case "$1" in
  -s) echo Linux ;;
  -m) echo "$FAKE_ARCH" ;;
esac
"""


def run(script_suffix: str, fake_arch: str, fake_bin_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, FAKE_ARCH=fake_arch, PATH=f"{fake_bin_dir}:{os.environ['PATH']}")
    cmd = f". {SCRIPT}\n{script_suffix}"
    return subprocess.run(
        ["sh", "-c", cmd], env=env, capture_output=True, text=True, check=False
    )


def main() -> int:
    if STAGE_DIR.exists():
        print(f"SKIP: {STAGE_DIR} already exists (looks like a real deploy); not touching it")
        return 0

    errors = []
    fake_bin_dir = STAGE_DIR / "fake-path-bin"
    try:
        fake_bin_dir.mkdir(parents=True)
        uname = fake_bin_dir / "uname"
        uname.write_text(FAKE_UNAME)
        uname.chmod(uname.stat().st_mode | stat.S_IEXEC)

        # Non-arm64 host: must fail before ever looking for the binary.
        result = run("echo unreachable", "x86_64", fake_bin_dir)
        if result.returncode == 0 or "arm64 deploy host" not in result.stderr:
            errors.append(f"arch gate: expected arm64-only failure, got rc={result.returncode} stderr={result.stderr!r}")

        # arm64 host, binary not staged yet: must fail with the missing-binary message.
        result = run("echo unreachable", "arm64", fake_bin_dir)
        if result.returncode == 0 or "missing or not executable" not in result.stderr:
            errors.append(f"missing-binary gate: expected failure, got rc={result.returncode} stderr={result.stderr!r}")

        # arm64 host, binary staged and executable: must succeed and export $clusterctl.
        BIN_DIR.mkdir(parents=True)
        CLUSTERCTL.write_text("#!/usr/bin/env sh\nexit 0\n")
        CLUSTERCTL.chmod(CLUSTERCTL.stat().st_mode | stat.S_IEXEC)
        result = run('echo "clusterctl=$clusterctl"', "arm64", fake_bin_dir)
        if result.returncode != 0:
            errors.append(f"happy path: expected success, got rc={result.returncode} stderr={result.stderr!r}")
        elif f"clusterctl={CLUSTERCTL}" not in result.stdout:
            errors.append(f"happy path: $clusterctl not set to {CLUSTERCTL}, got stdout={result.stdout!r}")

        # patch_feature_gates must rewrite an existing --feature-gates flag in place.
        rendered = STAGE_DIR / "rendered.yaml"
        rendered.write_text("        - --feature-gates=SomeOtherFlag=false\n")
        result = run(f'patch_feature_gates {rendered}', "arm64", fake_bin_dir)
        patched = rendered.read_text() if rendered.exists() else ""
        if result.returncode != 0 or "--feature-gates=ClusterTopology=true" not in patched:
            errors.append(f"patch_feature_gates: expected rewrite, got rc={result.returncode} content={patched!r}")
    finally:
        import shutil

        shutil.rmtree(STAGE_DIR, ignore_errors=True)

    if errors:
        print("resolve-clusterctl.sh check failed", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("resolve-clusterctl.sh: arch gate, missing-binary gate, happy path, and patch_feature_gates OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
