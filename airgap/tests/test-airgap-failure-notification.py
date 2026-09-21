#!/usr/bin/env python3
"""Regression test for the daily failure-notification job (#321).

The job's logic lives inside an actions/github-script block, which nothing here
can execute offline. This is a structural guard on the parsed workflow: it
catches the job being dropped, its trigger condition loosening (e.g. firing on
every event instead of only a scheduled run), a lost `needs`, or a permission
scope other than issues: write.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AIR_GAPPED_YML = REPO_ROOT / ".github/workflows/air-gapped.yml"

REQUIRED_NEEDS = {"build", "isolated-deploy"}


def check_job(jobs: dict, name: str) -> list[str]:
    job = jobs.get(name)
    if job is None:
        return [f"{name} job is missing"]

    failures = []
    needs = job.get("needs", [])
    needs = {needs} if isinstance(needs, str) else set(needs)
    if not REQUIRED_NEEDS <= needs:
        failures.append(f"{name} must need {sorted(REQUIRED_NEEDS)}, has {sorted(needs)}")
    condition = "".join(str(job.get("if", "")).split())
    if condition != "always()&&github.event_name=='schedule'":
        failures.append(f"{name} must run always() but only on a scheduled run, has if: {job.get('if')!r}")
    if job.get("permissions") != {"issues": "write"}:
        failures.append(f"{name} permissions must be exactly issues: write, has {job.get('permissions')!r}")
    uses = [step.get("uses", "") for step in job.get("steps", [])]
    if not any(u.startswith("actions/github-script@") for u in uses):
        failures.append(f"{name} no longer uses actions/github-script")
    return failures


def main() -> int:
    jobs = yaml.safe_load(AIR_GAPPED_YML.read_text()).get("jobs", {})
    failures = check_job(jobs, "report-status")

    if failures:
        print("Failure-notification wiring check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("Failure-notification wiring check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
