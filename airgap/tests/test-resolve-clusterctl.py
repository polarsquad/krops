#!/usr/bin/env python3
"""Exercise resolve-clusterctl.sh, shared by 3 Zarf onDeploy actions (#354).

The script hardcodes the Zarf stage path /tmp/krops-airgap, so the test runs
a copy with that path rewritten to a private temp dir: it never touches (or
skips because of) a real deploy's staged files.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "airgap/scripts/resolve-clusterctl.sh"
ZARF_STAGE_DIR = "/tmp/krops-airgap"

FAKE_UNAME = """#!/usr/bin/env sh
case "$1" in
  -s) echo Linux ;;
  -m) echo "$FAKE_ARCH" ;;
esac
"""


def run(script: Path, script_suffix: str, fake_arch: str, fake_bin_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, FAKE_ARCH=fake_arch, PATH=f"{fake_bin_dir}:{os.environ['PATH']}")
    cmd = f". {script}\n{script_suffix}"
    return subprocess.run(
        ["sh", "-c", cmd], env=env, capture_output=True, text=True, check=False
    )


def main() -> int:
    source = SOURCE.read_text()
    if ZARF_STAGE_DIR not in source:
        print(f"resolve-clusterctl.sh no longer references {ZARF_STAGE_DIR}; update this test", file=sys.stderr)
        return 1

    errors = []
    stage_dir = Path(tempfile.mkdtemp(prefix="krops-airgap-test-"))
    clusterctl = stage_dir / "bin" / "clusterctl-linux-arm64"
    script = stage_dir / "resolve-clusterctl.sh"
    script.write_text(source.replace(ZARF_STAGE_DIR, str(stage_dir)))
    fake_bin_dir = stage_dir / "fake-path-bin"
    try:
        fake_bin_dir.mkdir(parents=True)
        uname = fake_bin_dir / "uname"
        uname.write_text(FAKE_UNAME)
        uname.chmod(uname.stat().st_mode | stat.S_IEXEC)

        # Non-arm64 host: must fail before ever looking for the binary.
        result = run(script, "echo unreachable", "x86_64", fake_bin_dir)
        if result.returncode == 0 or "arm64 deploy host" not in result.stderr:
            errors.append(f"arch gate: expected arm64-only failure, got rc={result.returncode} stderr={result.stderr!r}")

        # arm64 host, binary not staged yet: must fail with the missing-binary message.
        result = run(script, "echo unreachable", "arm64", fake_bin_dir)
        if result.returncode == 0 or "missing or not executable" not in result.stderr:
            errors.append(f"missing-binary gate: expected failure, got rc={result.returncode} stderr={result.stderr!r}")

        # arm64 host, binary staged and executable: must succeed and export $clusterctl.
        clusterctl.parent.mkdir(parents=True)
        clusterctl.write_text("#!/usr/bin/env sh\nexit 0\n")
        clusterctl.chmod(clusterctl.stat().st_mode | stat.S_IEXEC)
        result = run(script, 'echo "clusterctl=$clusterctl"', "arm64", fake_bin_dir)
        if result.returncode != 0:
            errors.append(f"happy path: expected success, got rc={result.returncode} stderr={result.stderr!r}")
        elif f"clusterctl={clusterctl}" not in result.stdout:
            errors.append(f"happy path: $clusterctl not set to {clusterctl}, got stdout={result.stdout!r}")

        # patch_feature_gates must rewrite an existing --feature-gates flag in place.
        rendered = stage_dir / "rendered.yaml"
        rendered.write_text("        - --feature-gates=SomeOtherFlag=false\n")
        result = run(script, f'patch_feature_gates {rendered}', "arm64", fake_bin_dir)
        patched = rendered.read_text() if rendered.exists() else ""
        if result.returncode != 0 or "--feature-gates=ClusterTopology=true" not in patched:
            errors.append(f"patch_feature_gates: expected rewrite, got rc={result.returncode} content={patched!r}")
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    if errors:
        print("resolve-clusterctl.sh check failed", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("resolve-clusterctl.sh: arch gate, missing-binary gate, happy path, and patch_feature_gates OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
