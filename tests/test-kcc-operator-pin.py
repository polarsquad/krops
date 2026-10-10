#!/usr/bin/env python3
"""Cross-check the Config Connector operator pin (issue #72).

The operator manifest is committed verbatim from Google's versioned release
bundle, in the one copy that exists (management cluster; issue #560 removed
the workload copy). Renovate bumps the annotated version and the image tag
but cannot rewrite the 3500-line manifest, so this gate fails deliberately
when an image tag drifts from its annotated version: a human must
re-download the release bundle and replace the file (see docs/gcp.md).
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_FILES = [
    REPO_ROOT / "mgmt/gcp/infrastructure/kcc-operator/configconnector-operator.yaml",
]
VERSION_RE = re.compile(r"^# kcc-operator-version: (?P<version>[0-9.]+)\s*$", re.MULTILINE)
IMAGE_RE = re.compile(
    r"^\s*image: gcr\.io/gke-release/cnrm/operator:(?P<version>[0-9.]+)\s*$", re.MULTILINE
)


def body_below_header(path: Path) -> str:
    lines = path.read_text().splitlines()
    idx = next((i for i, l in enumerate(lines) if not l.startswith("#")), len(lines))
    return "\n".join(lines[idx:])


def main() -> int:
    failures = []
    existing = [p for p in OPERATOR_FILES if p.is_file()]
    if not existing:
        print("kcc-operator pin FAILED: no operator manifest found")
        return 1
    versions = {}
    bodies = {}
    for path in existing:
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text()
        vm = VERSION_RE.search(text)
        im = IMAGE_RE.search(text)
        if not vm:
            failures.append(f"{rel}: no '# kcc-operator-version:' annotation")
            continue
        if not im:
            failures.append(f"{rel}: no gcr.io/gke-release/cnrm/operator image line")
            continue
        versions[rel] = (vm.group("version"), im.group("version"))
        bodies[rel] = body_below_header(path)
        if vm.group("version") != im.group("version"):
            failures.append(
                f"{rel}: image tag {im.group('version')} != annotated version "
                f"{vm.group('version')} (re-download the release bundle, see docs/gcp.md)"
            )

    if len(existing) == len(OPERATOR_FILES):
        annotated = {a for a, _ in versions.values()}
        if len(annotated) != 1:
            failures.append(f"annotated versions disagree across copies: {versions}")
        if len(bodies) == 2 and len(set(bodies.values())) != 1:
            failures.append("the mgmt and workload operator manifests differ below the header")

    if failures:
        print("kcc-operator pin FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    sample = next(iter(versions.values()))[0] if versions else "?"
    print(f"kcc-operator pin OK (v{sample}, {len(existing)} copy{'ies' if len(existing) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
