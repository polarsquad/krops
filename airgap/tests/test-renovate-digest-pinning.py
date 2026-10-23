#!/usr/bin/env python3
"""Verify Renovate proposes digest pins for authoritative air-gap sources.

A failed registry lookup degrades to "no digest proposed", so it is
indistinguishable from an unpinnable reference without lookup-failure
classification (issue #181). When findings coincide with lookup-failure
diagnostics the dry-run is retried once; persisting failures abort with a
distinct infrastructure message instead of "missing digest pin".
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from renovate_harness import run_renovate

EXPECTED_FILES = [
    "airgap/images.txt",
    "airgap/scripts/build-config-artifact.sh",
    "airgap/scripts/build-package.sh",
    "airgap/scripts/stage-and-create-cluster.sh",
    "airgap/zarf.yaml",
]
DIGEST = re.compile(r"@sha256:[a-f0-9]{64}")
EXCLUDED_DEP_NAMES = {"localhost:5001/krops-airgap"}
REQUIRED_DEP_NAMES = {
    "airgap/images.txt": {
        "ghcr.io/fluxcd/source-controller",
        "registry.k8s.io/pause",
    },
    "airgap/scripts/stage-and-create-cluster.sh": {"kindest/node"},
    "airgap/zarf.yaml": {"quay.io/jetstack/cert-manager-controller"},
}


def compute_findings(result):
    """The finding classes the gate reports, recomputable per run."""
    missing = {}
    for package_file in EXPECTED_FILES:
        dependencies = result.deps_without_pin_digest(
            package_file, EXCLUDED_DEP_NAMES, allowed_datasources={"docker"}
        )
        if dependencies:
            missing[package_file] = dependencies
    tag_changes = {
        package_file: changes
        for package_file in EXPECTED_FILES
        if (
            changes := result.pin_digests_changing_tag(
                package_file, EXCLUDED_DEP_NAMES
            )
        )
    }
    inconsistent = result.inconsistent_pin_digests(EXPECTED_FILES)
    missing_extractions = {}
    for package_file, dep_names in REQUIRED_DEP_NAMES.items():
        dependencies = dep_names - result.dep_names(package_file)
        if dependencies:
            missing_extractions[package_file] = sorted(dependencies)
    return missing, missing_extractions, tag_changes, inconsistent


def main() -> int:
    # Strip existing digests so the only pinDigest proposals Renovate can
    # make are new ones: proves the managers propose pins for every ref.
    result = run_renovate(
        EXPECTED_FILES, transform=lambda _, text: DIGEST.sub("", text)
    )
    missing, missing_extractions, tag_changes, inconsistent = compute_findings(result)

    # A failed registry lookup is infrastructure, not a missing pin (issue
    # #181): retry the transient class once before classifying the outcome.
    if (missing or tag_changes or inconsistent) and result.lookup_failures:
        print(
            "registry lookup failure(s) in Renovate diagnostics; "
            "retrying the dry-run once",
            file=sys.stderr,
        )
        result = run_renovate(
            EXPECTED_FILES, transform=lambda _, text: DIGEST.sub("", text)
        )
        missing, missing_extractions, tag_changes, inconsistent = compute_findings(
            result
        )
        if (missing or tag_changes or inconsistent) and result.lookup_failures:
            print(
                "Renovate digest-pinning check inconclusive: registry lookup "
                "failed (infrastructure). Re-run the job; this is NOT "
                "evidence of missing digest pins.",
                file=sys.stderr,
            )
            dep_names = sorted(result.lookup_failure_dep_names())
            if dep_names:
                print(f"lookup-failed dependencies: {dep_names}", file=sys.stderr)
            result.print_diagnostics()
            return 1

    if (
        result.returncode
        or missing
        or missing_extractions
        or tag_changes
        or inconsistent
    ):
        print("Renovate digest-pinning integration check failed", file=sys.stderr)
        print(f"exit code: {result.returncode}", file=sys.stderr)
        for package_file, dependencies in sorted(missing_extractions.items()):
            print(
                f"{package_file}: missing extraction for {dependencies}",
                file=sys.stderr,
            )
        for package_file, dependencies in sorted(missing.items()):
            print(
                f"{package_file}: missing digest pin for {dependencies}",
                file=sys.stderr,
            )
        for package_file, changes in sorted(tag_changes.items()):
            print(f"{package_file}: pin changes the tag {changes}", file=sys.stderr)
        for reference, by_file in sorted(inconsistent.items()):
            print(f"{reference}: differing digests {by_file}", file=sys.stderr)
        result.print_diagnostics()
        return 1

    for package_file in sorted(EXPECTED_FILES):
        print(
            f"{package_file}: {result.pin_digest_count(package_file)} "
            "digest pin(s) proposed"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
