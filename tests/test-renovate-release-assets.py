#!/usr/bin/env python3
"""Verify Renovate upgrades Zarf release assets with their checksums."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from renovate_harness import run_renovate

PACKAGE_FILE = "airgap/zarf.yaml"
CONFIG_FILE = "renovate.json5"
DEP_NAME = "kubernetes-sigs/cluster-api"
OLD_VERSION = "v1.14.0"
OLD_DIGESTS = {
    "bce7a27ee9dd3f3cbdd3b463203a706ec31ea6ee98926045fbd9dfbe9c020f4b": "e9e7d54322c4ec6c8749d3315f461767618ee5591182ab7b625937cc9e813686",
    "16ec2d3ab39338e94dfa9ddb070c4f37073683db517fa6052ffc38d6702d7d8b": "78303f126d48b0356d3cbfb61acafcb961ee5792147b8593bd08bb9acb32efe0",
}
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def older_clusterctl_fixture(relative: str, text: str) -> str:
    if relative != PACKAGE_FILE:
        return text
    text = text.replace("/releases/download/v1.14.1/clusterctl-", f"/releases/download/{OLD_VERSION}/clusterctl-")
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
    # JSON5 must decode template newlines and quotes before Renovate passes the
    # template to bare Handlebars. A double-escaped sequence is emitted as a
    # literal backslash sequence and corrupts the YAML on the first update.
    replacement_templates = [
        line
        for line in (Path(__file__).resolve().parents[1] / CONFIG_FILE)
        .read_text()
        .splitlines()
        if '"autoReplaceStringTemplate"' in line
        and ("urlPrefix" in line or "clusterctl" in line)
    ]
    if any("\\\\n" in line or "\\\\\\\"" in line for line in replacement_templates):
        errors.append("release-asset replacement template double-escapes a newline or quote")
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
