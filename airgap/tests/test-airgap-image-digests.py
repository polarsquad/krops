#!/usr/bin/env python3
"""Require immutable digests in air-gap image sources changed by a PR."""

import argparse
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_FILES = {
    "airgap/images.txt",
    "airgap/zarf.yaml",
}
LOCAL_EXCEPTIONS = {
    "localhost:5001/krops-airgap:latest": (
        "built immediately before packaging; the build script substitutes the "
        "selected OCI registry and tag"
    ),
}
NON_IMAGE_REFERENCES = {"127.0.0.1:31999"}
IGNORED_AIRGAP_PREFIXES = (
    "airgap/archives/",
    "airgap/manifests/",
    "airgap/rendered/",
    "airgap/sbom/",
    "airgap/tests/",
)
POTENTIAL_SOURCE_SUFFIXES = {".json", ".sh", ".toml", ".txt", ".yaml", ".yml"}
IMAGE_REF = re.compile(
    # A shell parameter default commonly places the reference after `:-`.
    r"(?:(?<![\w./-])|(?<=:-))"
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?"
    r"(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*)"
    r"(?:"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?"
    r"|@(?P<digest_only>sha256:[a-f0-9]{64})"
    r")"
)


def source_lines(relative: str):
    """Yield (line number, text) containing authoritative image references."""
    lines = (REPO_ROOT / relative).read_text().splitlines()
    if relative == "airgap/images.txt":
        for number, line in enumerate(lines, 1):
            if line.strip() and not line.lstrip().startswith("#"):
                yield number, line
        return

    if relative == "airgap/zarf.yaml":
        in_images = False
        for number, line in enumerate(lines, 1):
            if line == "    images:":
                in_images = True
                continue
            if in_images and line.startswith("      - "):
                yield number, line
            elif (
                in_images
                and line.strip()
                and not line.lstrip().startswith("#")
                and len(line) - len(line.lstrip()) <= 4
            ):
                in_images = False
        return

    for number, line in enumerate(lines, 1):
        code = line.split("#", 1)[0]
        if code.strip():
            yield number, code


def is_image_source(relative: str) -> bool:
    return relative in INVENTORY_FILES or (
        relative.startswith("airgap/scripts/") and relative.endswith(".sh")
    )


def is_potential_image_source(relative: str) -> bool:
    path = Path(relative)
    return (
        relative.startswith("airgap/")
        and not relative.startswith(IGNORED_AIRGAP_PREFIXES)
        and path.suffix in POTENTIAL_SOURCE_SUFFIXES
    )


def contains_image_reference(relative: str) -> bool:
    for line in (REPO_ROOT / relative).read_text().splitlines():
        code = line.split("#", 1)[0]
        for match in IMAGE_REF.finditer(code):
            if match.group(0) not in NON_IMAGE_REFERENCES:
                return True
    return False


def all_image_sources():
    scripts = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "airgap/scripts").glob("*.sh")
    }
    return sorted(INVENTORY_FILES | scripts)


def changed_paths(base):
    if base:
        command = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base}...HEAD",
        ]
        return subprocess.run(
            command, cwd=REPO_ROOT, check=True, text=True, capture_output=True
        ).stdout.splitlines()

    committed = []
    for candidate in ("origin/main", "main"):
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if exists.returncode == 0:
            committed = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMR",
                    f"{candidate}...HEAD",
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            break
    modified = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    return committed + modified + untracked


def main() -> int:
    parser = argparse.ArgumentParser()
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--changed-from",
        metavar="GIT_REF",
        help="check image-source files changed between GIT_REF and HEAD",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="audit every authoritative image source in the cloned repository",
    )
    args = parser.parse_args()
    if args.all:
        sources = all_image_sources()
        changed = []
    else:
        changed = changed_paths(args.changed_from)
        sources = sorted(
            {
                path
                for path in changed
                if is_image_source(path)
            }
        )

    failures = []
    seen = defaultdict(list)

    for relative in sorted(set(changed) - set(sources)):
        if is_potential_image_source(relative) and contains_image_reference(relative):
            failures.append(
                f"{relative}: uncovered image-bearing source; register it in the gate"
            )

    for relative in sources:
        matches = 0
        for number, line in source_lines(relative):
            for match in IMAGE_REF.finditer(line):
                reference = match.group(0)
                if reference in NON_IMAGE_REFERENCES:
                    continue
                matches += 1
                if reference in LOCAL_EXCEPTIONS:
                    continue
                if match.group("digest_only") is not None:
                    failures.append(
                        f"{relative}:{number}: digest-only image (readable tag required): "
                        f"{reference}"
                    )
                    continue
                digest = match.group("digest")
                if digest is None:
                    failures.append(f"{relative}:{number}: unpinned image: {reference}")
                    continue
                key = (match.group("name"), match.group("tag"))
                seen[key].append((digest, relative, number))
        if matches == 0 and relative in INVENTORY_FILES:
            failures.append(f"{relative}: no image references found; parser coverage is stale")

    for (name, tag), occurrences in sorted(seen.items()):
        digests = {digest for digest, _, _ in occurrences}
        if len(digests) > 1:
            locations = ", ".join(
                f"{path}:{number}={digest}" for digest, path, number in occurrences
            )
            failures.append(f"inconsistent digest for {name}:{tag}: {locations}")

    if failures:
        print("Air-gap image digest check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    scope_name = "repository" if args.all else "changed files"
    print(f"Air-gap image digest check OK ({len(sources)} {scope_name} checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
