#!/usr/bin/env python3
"""Scrub cloud-specific literals out of WireMock recordings (issue #355).

Core sanitizer for Phase 2. A recording (stub mappings exported from
/__admin/mappings, or the request journal from /__admin/requests) contains
literals that must not be committed: account/subscription/project IDs,
domain names, tokens, generated resource names. This script replaces every
listed literal with a stable placeholder, in every JSON string value.

The literal lists are PER CLOUD and live in the cloud arm directories as
JSON objects mapping each literal to its placeholder, for example:

    {"123456789012": "${AWS_ACCOUNT_ID}", "AKIAIOSFODNN7EXAMPLE": "${AWS_ACCESS_KEY_ID}"}

Only the replacement engine lives here, shared by every cloud. Replacements
are applied longest-literal-first so overlapping literals cannot shadow each
other. JSON keys (header names, field names) are structural and are NOT
scrubbed; values are.

Usage:
    sanitize_recording.py --literals <cloud>/literals.json recording.json [more.json ...]
    sanitize_recording.py --literals <cloud>/literals.json --check recording.json

Without --check each input file is rewritten in place. With --check nothing
is written and the exit code is 1 when any literal is still present (the CI
guard shape for Phase 2). --check scans the raw file text, JSON keys included, so it is intentionally stricter than the rewrite: a literal left in a key keeps failing the check until it is fixed by hand.
"""

import argparse
import json
import sys
from pathlib import Path


def load_literals(path: Path) -> dict[str, str]:
    """Load the per-cloud literal -> placeholder map."""
    with path.open(encoding="utf-8") as handle:
        literals = json.load(handle)
    if not isinstance(literals, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in literals.items()
    ):
        raise ValueError(f"{path}: expected a JSON object of string -> string")
    empty = [key for key in literals if not key]
    if empty:
        raise ValueError(f"{path}: empty literal keys are not allowed")
    return literals


def sanitize_text(text: str, literals: dict[str, str]) -> str:
    """Replace every literal occurrence in text, longest literal first."""
    for literal in sorted(literals, key=len, reverse=True):
        text = text.replace(literal, literals[literal])
    return text


def sanitize_json(node, literals: dict[str, str]):
    """Recursively scrub string values in a parsed JSON document."""
    if isinstance(node, str):
        return sanitize_text(node, literals)
    if isinstance(node, list):
        return [sanitize_json(item, literals) for item in node]
    if isinstance(node, dict):
        return {key: sanitize_json(value, literals) for key, value in node.items()}
    return node


def remaining_literals(path: Path, literals: dict[str, str]) -> list[str]:
    """Literals still present anywhere in a file's raw text (for --check).

    Deliberately stricter than sanitize_file: the rewrite scrubs only JSON
    string values, but this check scans the whole text, so a literal in a
    JSON key still fails the guard. A cloud literal in a key is a leak the
    engine cannot fix by itself; the recording needs a hand edit before it
    is committed.
    """
    text = path.read_text(encoding="utf-8")
    return [literal for literal in literals if literal in text]


def sanitize_file(path: Path, literals: dict[str, str]) -> None:
    """Rewrite one recording file with all literals scrubbed."""
    document = json.loads(path.read_text(encoding="utf-8"))
    scrubbed = sanitize_json(document, literals)
    path.write_text(
        json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--literals",
        required=True,
        type=Path,
        help="per-cloud JSON object mapping literals to placeholders",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report files still containing literals; write nothing",
    )
    parser.add_argument("files", nargs="+", type=Path, help="recording JSON files")
    args = parser.parse_args()

    literals = load_literals(args.literals)

    if args.check:
        dirty = {
            str(path): hits
            for path in args.files
            if (hits := remaining_literals(path, literals))
        }
        for path, hits in sorted(dirty.items()):
            print(f"{path}: {len(hits)} unsanitized literal(s)", file=sys.stderr)
        if dirty:
            print(
                "note: --check scans raw text including JSON keys, which the "
                "rewrite does not scrub; fix literals in keys by hand",
                file=sys.stderr,
            )
        return 1 if dirty else 0

    for path in args.files:
        sanitize_file(path, literals)
        print(f"{path}: sanitized ({len(literals)} literal rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
