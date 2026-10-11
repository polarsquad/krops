#!/usr/bin/env python3
"""Check the Cloud SQL instance settings and their documentation.

The instance lives on the management cluster (issue #560: the workload
cloud CRs moved off the workload cluster) and reconciles as krops-capg.
Requires PyYAML (`uv run`). Rationale for each check lives in
docs/workload-resources.md.
"""

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL = REPO_ROOT / "mgmt/gcp/infrastructure/workload-resources/postgres/postgres.yaml"
DOC = REPO_ROOT / "docs/workload-resources.md"
CLAIM_FILES = [DOC, REPO_ROOT / "docs/gcp.md", REPO_ROOT / "docs/architecture.md",
               REPO_ROOT / "mgmt/gcp/infrastructure/flux-ks.yaml",
               REPO_ROOT / "AGENTS.md", REPO_ROOT / "docs/gcp-infra.svg"]


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

    texts = {p: p.read_text() for p in CLAIM_FILES}
    for path, text in texts.items():
        for stale in ("no password exists", "IAM authentication only", "IAM auth only", "IAM-only auth", "requireSsl: true"):
            if stale in text:
                failures.append(f"{path.relative_to(REPO_ROOT)}: stale claim `{stale}`")
    for needed in ("edition: ENTERPRISE", "built-in `postgres` user"):
        if needed not in texts[DOC]:
            failures.append(f"{DOC.relative_to(REPO_ROOT)}: must document `{needed}`")
    gcp_text = texts[REPO_ROOT / "docs/gcp.md"]
    section = re.search(r"^## Known limitations\s*$", gcp_text, re.MULTILINE)
    if section is None:
        failures.append("docs/gcp.md: missing ## Known limitations section")
    else:
        limitations = gcp_text[section.end():]
        next_section = re.search(r"^#{1,2}\s+", limitations, re.MULTILINE)
        if next_section:
            limitations = limitations[:next_section.start()]
        bullets = re.findall(
            r"^[-+*] [^\n]*(?:\n(?:[ \t]+[^\n]*|[ \t]*$))*",
            limitations, re.MULTILINE,
        )
        required = ("Cloud SQL", "built-in `postgres` user with no password", "cannot log in")
        if not any(all(claim in " ".join(bullet.split()) for claim in required)
                   for bullet in bullets):
            failures.append("docs/gcp.md: Known limitations must document in one bullet that "
                            "Cloud SQL creates the built-in `postgres` user with no password "
                            "and it cannot log in")

    if failures:
        print("gcp postgres FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("gcp postgres OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
