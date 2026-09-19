#!/usr/bin/env python3
"""Regression test for the branch-dispatch signature-identity fix (#328).

zarf package verify used to hardcode --certificate-identity to
refs/heads/main, so a workflow_dispatch run off any other branch always
failed verification even for a package it built and signed itself. The fix
threads the actual triggering ref through as AIRGAP_VERIFY_REF; this guards
against a future edit silently reintroducing a hardcoded ref, or a new step
invoking offline-run.sh without wiring that env var through.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_RUN_SH = REPO_ROOT / "airgap/scripts/offline-run.sh"
AIR_GAPPED_YML = REPO_ROOT / ".github/workflows/air-gapped.yml"


def main() -> int:
    failures = []

    script = OFFLINE_RUN_SH.read_text()
    if "${AIRGAP_VERIFY_REF:-refs/heads/main}" not in script:
        failures.append(
            f"{OFFLINE_RUN_SH}: --certificate-identity must use "
            "${AIRGAP_VERIFY_REF:-refs/heads/main}, not a hardcoded ref"
        )

    if "${AIRGAP_VERIFY_REPO:-polarsquad/krops}" not in script:
        failures.append(
            f"{OFFLINE_RUN_SH}: --certificate-identity must take the repository from "
            "${AIRGAP_VERIFY_REPO:-polarsquad/krops}"
        )

    workflow = AIR_GAPPED_YML.read_text()
    step_blocks = re.split(r"\n(?=      - name:)", workflow)
    offenders = [
        block for block in step_blocks
        if "offline-run.sh" in block
        and not (
            "AIRGAP_VERIFY_REF: ${{ github.ref }}" in block
            and "AIRGAP_VERIFY_REPO: ${{ github.repository }}" in block
        )
    ]
    if offenders:
        failures.append(
            f"{AIR_GAPPED_YML}: {len(offenders)} step(s) invoke offline-run.sh "
            "without setting AIRGAP_VERIFY_REF and AIRGAP_VERIFY_REPO"
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
