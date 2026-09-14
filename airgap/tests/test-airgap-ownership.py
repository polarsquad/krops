#!/usr/bin/env python3
"""Require every zarf.yaml image to exist in images.txt with the same tag and digest.

Ownership model: zarf.yaml is the authoritative artifact listing for the
Zarf package; airgap/images.txt is the superset inventory that the
airgap/scripts preloads derive from. Renovate updates the two files
independently, so a partial update can leave zarf.yaml pinning an image the
inventory no longer carries (or with a different digest). This gate fails
when a zarf.yaml image ref is absent from images.txt or has a different
digest for the same name and tag, printing each offending ref with file:line.
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_EXCEPTIONS = {
    "localhost:5001/krops-airgap:latest": (
        "built immediately before packaging; it is not part of the remote "
        "inventory"
    ),
}
IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?"
)


def zarf_image_refs(zarf_path: Path):
    """Yield (line number, ref text) for entries in per-component images: lists."""
    in_images = False
    for number, line in enumerate(zarf_path.read_text().splitlines(), 1):
        if line == "    images:":
            in_images = True
            continue
        if in_images and line.startswith("      - "):
            yield number, line[len("      - "):].strip()
        elif (
            in_images
            and line.strip()
            and not line.lstrip().startswith("#")
            and len(line) - len(line.lstrip()) <= 4
        ):
            in_images = False


def inventory_entries(inventory_path: Path):
    """Return {(name, tag): {digest, ...}} parsed from the inventory file."""
    entries = {}
    for line in inventory_path.read_text().splitlines():
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        match = IMAGE_REF.fullmatch(code)
        if match is None:
            continue
        entries.setdefault((match.group("name"), match.group("tag")), set()).add(
            match.group("digest")
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="repository root containing airgap/ (default: this repository)",
    )
    args = parser.parse_args()
    root = Path(args.root)

    refs = []
    failures = []
    for number, ref in zarf_image_refs(root / "airgap" / "zarf.yaml"):
        if ref in LOCAL_EXCEPTIONS:
            continue
        match = IMAGE_REF.fullmatch(ref)
        if match is None:
            failures.append(f"airgap/zarf.yaml:{number}: unparseable image ref: {ref}")
            continue
        digest = match.group("digest")
        if digest is None:
            failures.append(
                f"airgap/zarf.yaml:{number}: unpinned image (digest required): {ref}"
            )
            continue
        refs.append((number, match.group("name"), match.group("tag"), digest))

    if not refs and not failures:
        failures.append(
            "airgap/zarf.yaml: no image references found; parser coverage is stale"
        )

    inventory = inventory_entries(root / "airgap" / "images.txt")
    for number, name, tag, digest in refs:
        if digest in inventory.get((name, tag), set()):
            continue
        have = ", ".join(sorted(d for d in inventory.get((name, tag), set()) if d))
        if have:
            failures.append(
                f"airgap/zarf.yaml:{number}: stale digest for {name}:{tag}: "
                f"{digest} (airgap/images.txt has {have})"
            )
        else:
            failures.append(
                f"airgap/zarf.yaml:{number}: {name}:{tag}@{digest} "
                "missing from airgap/images.txt"
            )

    if failures:
        print("Air-gap ownership check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"airgap ownership invariant OK ({len(refs)} zarf refs checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
