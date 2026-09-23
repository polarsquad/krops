#!/usr/bin/env python3
"""Offline check that every regex manager's update rewrites only the version/digest.

Renovate hands `autoReplaceStringTemplate` the dependency's standard fields
only; custom capture groups (indent, header, urlPrefix, ...) render empty.
Extraction-only tests cannot see that, so this runs Renovate's real
`doAutoReplace` (including its re-extraction check) on every dependency the
regex managers extract from tracked files.
"""

import subprocess
import sys

from renovate_harness import REPO_ROOT, simulate_auto_replace


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout.split()
    records = simulate_auto_replace(files)
    failures = [r for r in records if not r["ok"]]
    if not records:
        print("no regex-manager dependencies extracted", file=sys.stderr)
        return 1
    if failures:
        print("Renovate autoReplace check failed", file=sys.stderr)
        for r in failures:
            print(f"  - {r['packageFile']}: {r['manager']}: {r['error']}", file=sys.stderr)
            print(f"      {r['replaceString']!r}", file=sys.stderr)
        return 1
    print(f"autoReplace: {len(records)} regex-manager updates rewrite only their version/digest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
