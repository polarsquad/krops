#!/usr/bin/env python3
"""Guard the air-gapped workflow's triggers, guards, and timeouts (docs/airgap.md finding 11)."""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / ".github/workflows/air-gapped.yml").read_text()
OFFLINE_RUN = (REPO_ROOT / "airgap/scripts/offline-run.sh").read_text()
ZARF = (REPO_ROOT / "airgap/zarf.yaml").read_text()


def job_block(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", WORKFLOW, re.S | re.M)
    return match.group(1) if match else ""


def main() -> int:
    failures = []

    on_block = re.search(r"^on:\n(.*?)(?=^\w)", WORKFLOW, re.S | re.M).group(1)
    triggers = set(re.findall(r"^  (\w+):", on_block, re.M))
    if triggers != {"schedule", "workflow_dispatch"}:
        failures.append(f"triggers must be schedule and workflow_dispatch only, got {sorted(triggers)}")

    build = job_block("build")
    if re.search(r"^    if:", build, re.M):
        failures.append("the build job must not carry an if: guard (it must run on forks and any branch)")
    if "timeout-minutes: 15" not in build:
        failures.append("the build job timeout must be 15 minutes")
    if "timeout-minutes: 30" not in job_block("isolated-deploy"):
        failures.append("the deploy job timeout must be 30 minutes")

    if "/tmp/airgap-workload-debug.txt" not in WORKFLOW:
        failures.append("the workflow must upload /tmp/airgap-workload-debug.txt")

    for needle, why in (
        ('--registry-mode=nodeport --components="" --timeout 1m --confirm', "zarf init timeout"),
        ('package deploy "$PACKAGE" --timeout 5m --confirm', "zarf deploy timeout"),
        ("seq 1 80", "20m workload cluster wait"),
    ):
        if needle not in OFFLINE_RUN:
            failures.append(f"offline-run.sh: missing {why}: {needle}")
    if "fluxinstance/flux --timeout=1m" not in ZARF:
        failures.append("zarf.yaml: the FluxInstance wait must time out after 1m")

    if failures:
        print("air-gapped workflow config check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("air-gapped workflow config OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
