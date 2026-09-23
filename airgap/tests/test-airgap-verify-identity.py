#!/usr/bin/env python3
"""Regression test for the signature-identity wiring (docs/airgap.md#empirical-findings)."""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_RUN_SH = REPO_ROOT / "airgap/scripts/offline-run.sh"
WORKFLOWS_DIR = REPO_ROOT / ".github/workflows"


def main() -> int:
    failures = []

    script = OFFLINE_RUN_SH.read_text()
    if "${AIRGAP_VERIFY_WORKFLOW_REF:-" not in script:
        failures.append(
            f"{OFFLINE_RUN_SH}: --certificate-identity must take the workflow "
            "identity from ${AIRGAP_VERIFY_WORKFLOW_REF:-...}, not a hardcoded "
            "repo, ref, or workflow filename"
        )

    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = workflow_path.read_text()
        if "offline-run.sh" not in workflow:
            continue
        step_blocks = re.split(r"\n(?=      - name:)", workflow)
        offenders = [
            block for block in step_blocks
            if "offline-run.sh" in block
            and "AIRGAP_VERIFY_WORKFLOW_REF: ${{ github.workflow_ref }}" not in block
        ]
        if offenders:
            failures.append(
                f"{workflow_path}: {len(offenders)} step(s) invoke offline-run.sh "
                "without setting AIRGAP_VERIFY_WORKFLOW_REF: ${{ github.workflow_ref }}"
            )

    if failures:
        print("Signature-identity wiring check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Signature-identity wiring check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
