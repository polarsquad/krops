#!/usr/bin/env python3
"""Cross-check account ID literals in workload-resources/ against cluster-vars.

The management cluster has no cluster-vars, so the AWS account ID is literal in
workload-resources/ (bucket names, trust-principal ARNs, RDS ARN scope). The
per-region cluster-vars ConfigMaps are the source of truth. A migration that
updates one side and not the other would mismatch silently without this gate.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FLUX_CLUSTER_VARS = [
    REPO_ROOT / "mgmt/aws/addons/flux-apps/regions/eu-north-1/cluster-vars.yaml",
    REPO_ROOT / "mgmt/aws/addons/flux-apps/regions/eu-west-1/cluster-vars.yaml",
]
WORKLOAD_RESOURCES = REPO_ROOT / "mgmt/aws/infrastructure/workload-resources"

ACCOUNT_ID_RE = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")
FLUX_ACCOUNT_RE = re.compile(r'AWS_ACCOUNT_ID:\s*"?([0-9]{12})"?')


def main():
    ids = set()
    for path in FLUX_CLUSTER_VARS:
        found = set(FLUX_ACCOUNT_RE.findall(path.read_text()))
        if not found:
            print(f"FAIL: no AWS_ACCOUNT_ID found in {path}", file=sys.stderr)
            sys.exit(1)
        ids |= found
    if len(ids) > 1:
        print(f"FAIL: multiple distinct AWS_ACCOUNT_IDs across cluster-vars: {ids}", file=sys.stderr)
        sys.exit(1)
    expected = ids.pop()

    failures = []
    total = 0
    for path in sorted(WORKLOAD_RESOURCES.rglob("*.yaml")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for m in ACCOUNT_ID_RE.finditer(line):
                total += 1
                found = m.group(1)
                if found != expected:
                    rel = path.relative_to(REPO_ROOT)
                    failures.append(f"{rel}:{lineno}: found {found}, expected {expected} (from cluster-vars)")

    if total == 0:
        print("FAIL: no account ID literals found in workload-resources/ — discovery guard failed", file=sys.stderr)
        sys.exit(1)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: {total} account ID literals in workload-resources/ match cluster-vars ({expected})")


if __name__ == "__main__":
    main()
