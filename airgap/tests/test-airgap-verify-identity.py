#!/usr/bin/env python3
"""Regression test for the signature-identity wiring (docs/airgap.md#empirical-findings)."""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_RUN_SH = REPO_ROOT / "airgap/scripts/offline-run.sh"
UPDATE_PACKAGE_SH = REPO_ROOT / "airgap/scripts/update-package.sh"
VERIFY_PACKAGE_SH = REPO_ROOT / "airgap/scripts/verify-package.sh"
WORKFLOWS_DIR = REPO_ROOT / ".github/workflows"
UPDATE_ROLLBACK_MARKER = "test-airgap-update-rollback.py --zarf-package"
IDENTITY_ENV = "AIRGAP_VERIFY_WORKFLOW_REF: ${{ github.workflow_ref }}"


def main() -> int:
    failures = []

    library = VERIFY_PACKAGE_SH.read_text()
    if "${AIRGAP_VERIFY_WORKFLOW_REF:-" not in library:
        failures.append(
            f"{VERIFY_PACKAGE_SH}: --certificate-identity must take the workflow "
            "identity from ${AIRGAP_VERIFY_WORKFLOW_REF:-...}, not a hardcoded "
            "repo, ref, or workflow filename"
        )

    for script_path in (OFFLINE_RUN_SH, UPDATE_PACKAGE_SH):
        if not re.search(r"^\s*(source|\.)\s+.*verify-package\.sh", script_path.read_text(), re.M):
            failures.append(f"{script_path}: must source verify-package.sh")

    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = workflow_path.read_text()
        for marker in ("offline-run.sh", UPDATE_ROLLBACK_MARKER):
            if marker not in workflow:
                continue
            step_blocks = re.split(r"\n(?=      - name:)", workflow)
            offenders = [
                block for block in step_blocks
                if marker in block and IDENTITY_ENV not in block
            ]
            if offenders:
                failures.append(
                    f"{workflow_path}: {len(offenders)} step(s) invoke {marker} "
                    f"without setting {IDENTITY_ENV}"
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
