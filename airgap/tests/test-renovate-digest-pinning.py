#!/usr/bin/env python3
"""Verify Renovate proposes digest pins for authoritative air-gap sources."""

from pathlib import Path
import re
import sys

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
    "airgap/images.txt": {"ghcr.io/fluxcd/source-controller"},
    "airgap/scripts/build-config-artifact.sh": {"registry.k8s.io/pause"},
    "airgap/scripts/build-package.sh": {"registry.k8s.io/pause"},
    "airgap/scripts/stage-and-create-cluster.sh": {"kindest/node"},
    "airgap/zarf.yaml": {"quay.io/jetstack/cert-manager-controller"},
}


def main() -> int:
    # Strip existing digests so the only pinDigest proposals Renovate can
    # make are new ones: proves the managers propose pins for every ref.
    result = run_renovate(
        EXPECTED_FILES, transform=lambda _, text: DIGEST.sub("", text)
    )

    missing = {}
    for package_file in EXPECTED_FILES:
        dependencies = result.deps_without_pin_digest(
            package_file, EXCLUDED_DEP_NAMES
        )
        if dependencies:
            missing[package_file] = dependencies
    missing_extractions = {}
    for package_file, dep_names in REQUIRED_DEP_NAMES.items():
        dependencies = dep_names - result.dep_names(package_file)
        if dependencies:
            missing_extractions[package_file] = sorted(dependencies)
    if result.returncode or missing or missing_extractions:
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
