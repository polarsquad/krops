#!/usr/bin/env python3
"""Regression test for the daily failure-notification job (#321).

The notify-on-failure job's logic lives inside an actions/github-script
block, which nothing here can execute offline. This is a structural guard
instead: it catches the job being dropped, its trigger condition loosening
(e.g. firing on every event instead of only a failed schedule), or losing
the permission it needs to file/update an issue.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AIR_GAPPED_YML = REPO_ROOT / ".github/workflows/air-gapped.yml"


def main() -> int:
    text = AIR_GAPPED_YML.read_text()
    failures = []

    match = re.search(r"\n  notify-on-failure:\n(.*?)(?=\n  \S|\Z)", text, re.DOTALL)
    if match is None:
        print(f"{AIR_GAPPED_YML}: notify-on-failure job is missing", file=sys.stderr)
        return 1
    block = match.group(1)

    if "build" not in block or "isolated-deploy" not in block:
        failures.append("notify-on-failure must run after 'build' and 'isolated-deploy'")
    if "failure()" not in block or "github.event_name == 'schedule'" not in block:
        failures.append("notify-on-failure must only fire on a failed scheduled run")
    if "issues: write" not in block:
        failures.append("notify-on-failure is missing the issues: write permission")
    if "actions/github-script" not in block:
        failures.append("notify-on-failure no longer uses actions/github-script")

    if failures:
        print("Failure-notification wiring check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("Failure-notification wiring check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
