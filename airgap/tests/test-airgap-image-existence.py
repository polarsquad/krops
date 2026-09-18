#!/usr/bin/env python3
"""Confirm every airgap/images.txt digest pin resolves to a real, pullable image."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_EXCEPTIONS = {
    "localhost:5001/krops-airgap:latest": (
        "built immediately before packaging; not published to a remote registry"
    ),
}
IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"@(?P<digest>sha256:[a-f0-9]{64})"
)


def inventory_refs(inventory_path: Path):
    """Yield (line number, name, tag, digest) for each pinned inventory entry."""
    for number, line in enumerate(inventory_path.read_text().splitlines(), 1):
        code = line.strip()
        if not code or code.startswith("#") or code in LOCAL_EXCEPTIONS:
            continue
        match = IMAGE_REF.fullmatch(code)
        if match is None:
            continue
        yield number, match.group("name"), match.group("tag"), match.group("digest")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="repository root containing airgap/images.txt (default: this repository)",
    )
    args = parser.parse_args()
    inventory_path = Path(args.root) / "airgap" / "images.txt"

    refs = list(inventory_refs(inventory_path))
    failures = []
    for number, name, tag, digest in refs:
        ref = f"{name}@{digest}"
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip().splitlines()
            detail = stderr[-1] if stderr else "unknown error"
            failures.append(
                f"airgap/images.txt:{number}: {name}:{tag}@{digest} not found: {detail}"
            )

    if failures:
        print("Air-gap image existence check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"airgap image existence OK ({len(refs)} digests confirmed pullable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
