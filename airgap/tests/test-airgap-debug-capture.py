#!/usr/bin/env python3
"""Regression test for the cert-manager debug capture on deploy failure (#329).

Zarf's Helm --wait blocks for its whole timeout with no progress output, so
a "zarf package deploy" failure alone gives no clue what got stuck. This
guards against a future edit silently dropping the debug capture in
offline-run.sh, or a workflow artifact-upload step losing the file path, so
the next real failure isn't a black box again.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_RUN_SH = REPO_ROOT / "airgap/scripts/offline-run.sh"
AIR_GAPPED_YML = REPO_ROOT / ".github/workflows/air-gapped.yml"
DEBUG_FILE = "/tmp/airgap-cert-manager-debug.txt"


def main() -> int:
    failures = []

    script = OFFLINE_RUN_SH.read_text()
    if DEBUG_FILE not in script:
        failures.append(f"{OFFLINE_RUN_SH}: no longer writes {DEBUG_FILE}")
    for probe in ("get pods -n cert-manager", "describe pods -n cert-manager", "get events -A"):
        if probe not in script:
            failures.append(f"{OFFLINE_RUN_SH}: debug capture no longer runs '{probe}'")

    workflow = AIR_GAPPED_YML.read_text()
    upload_blocks = re.split(r"\n(?=      - name:)", workflow)
    upload_steps = [
        block for block in upload_blocks
        if "actions/upload-artifact" in block and "krops-airgap-" in block
        and "-results" in block
    ]
    if not upload_steps:
        failures.append(f"{AIR_GAPPED_YML}: found no deployment-evidence upload steps")
    missing = [
        block.splitlines()[0].strip() for block in upload_steps
        if DEBUG_FILE not in block
    ]
    if missing:
        failures.append(
            f"{AIR_GAPPED_YML}: {DEBUG_FILE} missing from upload path in: {missing}"
        )

    if failures:
        print("Debug-capture regression check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Debug-capture regression check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
