#!/usr/bin/env python3
"""Check the workload Cloud SQL instance settings and their documentation.

Requires PyYAML (`uv run`). Rationale for each check lives in
docs/workload-resources.md.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL = REPO_ROOT / "workload/gcp-base/postgres/postgres.yaml"
DOC = REPO_ROOT / "docs/workload-resources.md"
CLAIM_FILES = [DOC, REPO_ROOT / "docs/gcp.md", REPO_ROOT / "docs/architecture.md",
               REPO_ROOT / "workload/gcp-base/postgres/flux-ks.yaml"]


def main() -> int:
    failures = []
    instances = [d for d in yaml.safe_load_all(SQL.read_text()) if d and d.get("kind") == "SQLInstance"]
    if len(instances) != 1:
        print(f"expected 1 SQLInstance in {SQL.relative_to(REPO_ROOT)}, got {len(instances)}")
        return 1
    spec = instances[0]["spec"]
    settings = spec["settings"]
    ip = settings["ipConfiguration"]

    if settings.get("edition") != "ENTERPRISE":
        failures.append(f"settings.edition must be ENTERPRISE (db-f1-micro is Enterprise only), got {settings.get('edition')}")
    if "requireSsl" in ip:
        failures.append("ipConfiguration.requireSsl is deprecated; use sslMode alone")
    if ip.get("sslMode") != "ENCRYPTED_ONLY":
        failures.append(f"ipConfiguration.sslMode must be ENCRYPTED_ONLY, got {ip.get('sslMode')}")
    if "rootPassword" in spec:
        failures.append("rootPassword must not be set")

    doc = DOC.read_text()
    for path in CLAIM_FILES:
        text = path.read_text()
        for stale in ("no password exists", "IAM authentication only", "IAM auth only", "requireSsl: true"):
            if stale in text:
                failures.append(f"{path.relative_to(REPO_ROOT)}: stale claim `{stale}`")
    for needed in ("edition: ENTERPRISE", "built-in `postgres` user"):
        if needed not in doc:
            failures.append(f"{DOC.relative_to(REPO_ROOT)}: must document `{needed}`")

    if failures:
        print("gcp postgres FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("gcp postgres OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
