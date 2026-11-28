#!/usr/bin/env python3
"""SBOM decode in verify-package.sh runs with syft's update check disabled (docs/airgap.md#sequence).

Stub mode only: a fake zarf records each call, and `tools sbom convert` succeeds
only when SYFT_CHECK_FOR_APP_UPDATE=false, so a regression fails the run.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "airgap/scripts/verify-package.sh"
UPDATE_VAR = "SYFT_CHECK_FOR_APP_UPDATE"

STUB_ZARF = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(os.environ.get("SYFT_CHECK_FOR_APP_UPDATE", "<unset>") + " " + " ".join(argv) + "\n")

if argv[:2] == ["package", "verify"]:
    sys.exit(0)
if argv[:3] == ["package", "inspect", "sbom"]:
    out = Path(argv[argv.index("--output") + 1])
    for name in ("a.json", "b.json"):
        out.joinpath(name).write_text('{"artifacts":[],"schema":{"version":"10.0.0"}}')
    sys.exit(0)
if argv[:3] == ["tools", "sbom", "convert"]:
    if os.environ.get("SYFT_CHECK_FOR_APP_UPDATE") != "false":
        print("stub: update check not disabled, would fetch toolbox-data.anchore.io", file=sys.stderr)
        sys.exit(97)
    sys.exit(0)
print("UNEXPECTED ZARF CALL: " + " ".join(argv), file=sys.stderr)
sys.exit(99)
'''

# The variable is set as a per-command prefix inside verify-package.sh, so the
# calling shell must still see it unset after the call returns.
DRIVER = (
    'source "$VERIFY_SH"; airgap_verify_package "$pkg" "$sbom_dir"; rc=$?; '
    'printf "after=%s\\n" "${SYFT_CHECK_FOR_APP_UPDATE-unset}"; exit $rc'
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="airgap-verify-offline-") as tmp:
        base = Path(tmp)
        zarf = base / "zarf"
        zarf.write_text(STUB_ZARF)
        zarf.chmod(0o755)
        log = base / "calls.log"
        sbom_dir = base / "sbom"
        sbom_dir.mkdir()
        pkg = base / "package.tar.zst"
        pkg.write_bytes(b"")

        # Drop any outer setting so the result cannot come from the caller's environment.
        env = {k: v for k, v in os.environ.items() if k != UPDATE_VAR}
        env.update(
            ZARF=str(zarf),
            ZARF_VERIFY_KEY="dummy-key",
            STUB_LOG=str(log),
            VERIFY_SH=str(VERIFY_SH),
            pkg=str(pkg),
            sbom_dir=str(sbom_dir),
        )
        result = subprocess.run(
            ["bash", "-c", DRIVER], env=env, check=False, capture_output=True, text=True
        )

        check(
            result.returncode == 0,
            f"airgap_verify_package returned {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
        check(
            result.stdout.strip().splitlines()[-1:] == ["after=unset"],
            f"{UPDATE_VAR} leaked into the calling shell: {result.stdout!r}",
        )

        calls = log.read_text().splitlines()
        converts = [line for line in calls if " tools sbom convert " in line]
        check(
            len(converts) == 2,
            f"expected 2 tools sbom convert calls, got {len(converts)}: {calls}",
        )
        for line in converts:
            check(
                line.startswith("false "),
                f"convert call without {UPDATE_VAR}=false: {line}",
            )
    print("PASS: SBOM decode runs with syft update check disabled")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
