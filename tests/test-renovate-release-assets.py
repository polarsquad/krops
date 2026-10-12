#!/usr/bin/env python3
"""Verify Renovate upgrades Zarf release assets with their checksums."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from renovate_harness import run_renovate

PACKAGE_FILE = "airgap/zarf.yaml"
DEP_NAME = "kubernetes-sigs/cluster-api"
OLD_VERSION = "v1.14.2"
OLD_DIGESTS = {
    "5915523f13949884d29d5254445b8d294ba314b5dacb3e8eff9e42f1c9d2e684": "ea2285445da861b2ec96e948563cf158f4e0fd89a36238267b62e43a1bb00da8",
    "eea3a3ecc64ae155c57eea35378d833285a92cb549c3298a18765bc40a7a3f7b": "83976008aa9ddb81dab01443c646aaa125e4993e17bf24e790e29779f712d79d",
}
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def older_clusterctl_fixture(relative: str, text: str) -> str:
    if relative != PACKAGE_FILE:
        return text
    text = text.replace("/releases/download/v1.14.3/clusterctl-", f"/releases/download/{OLD_VERSION}/clusterctl-")
    for current, old in OLD_DIGESTS.items():
        text = text.replace(current, old)
    return text


def main() -> int:
    result = run_renovate([PACKAGE_FILE], transform=older_clusterctl_fixture)
    dependencies = [
        dep
        for dep in result.deps_by_file[PACKAGE_FILE]
        if dep.get("depName") == DEP_NAME and dep.get("currentValue") == OLD_VERSION
    ]
    errors = []
    # The replacement itself is covered by tests/test-renovate-auto-replace.py.
    if result.returncode:
        errors.append(f"Renovate exited {result.returncode}")
    if len(dependencies) != 2:
        errors.append(f"expected two {DEP_NAME} release assets, found {len(dependencies)}")

    replacement_digests = []
    for dependency in dependencies:
        upgrades = [
            update
            for update in dependency.get("updates", [])
            if update.get("newValue") != OLD_VERSION and update.get("newDigest")
        ]
        if not upgrades:
            errors.append(f"{dependency.get('replaceString')}: no version-and-checksum upgrade")
            continue
        upgrade = upgrades[0]
        if not upgrade["newValue"].startswith("v"):
            errors.append(f"{dependency.get('replaceString')}: release tag lost its v prefix")
        if not DIGEST.fullmatch(upgrade["newDigest"]):
            errors.append(f"{dependency.get('replaceString')}: invalid replacement SHA-256")
        replacement_digests.append(upgrade["newDigest"])

    if len(replacement_digests) == 2 and len(set(replacement_digests)) != 2:
        errors.append("platform-specific clusterctl assets received the same replacement SHA-256")

    if errors:
        print("Renovate release-asset integration check failed", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        result.print_diagnostics()
        return 1

    print("clusterctl release assets: versioned URLs and distinct SHA-256 replacements proposed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
