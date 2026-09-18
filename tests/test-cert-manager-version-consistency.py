#!/usr/bin/env python3
"""Cross-check cert-manager's chart version against every place it must match.

bootstrap.toml's charts.cert-manager pin is authoritative. The five
HelmRelease manifests are already cross-checked against it by
test-bootstrap-config.py; this covers the other places that pin didn't
protect: pivot.sh's imperative install, airgap/zarf.yaml's embedded chart
version, and the actual container image tags in airgap/images.txt and
airgap/zarf.yaml's images list. A mismatch between the chart version and
its image tags is exactly what left cert-manager in ImagePullBackOff for
two weeks (issue #322): Renovate's "platform-charts" group now keeps these
from drifting apart going forward, but this check catches it regardless of
how the drift happens (manual edit, partial merge, a rebase dropping one
file's change, ...).
"""

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TOML = REPO_ROOT / "bootstrap.toml"
PIVOT_SH = REPO_ROOT / "pivot.sh"
ZARF_YAML = REPO_ROOT / "airgap/zarf.yaml"
IMAGES_TXT = REPO_ROOT / "airgap/images.txt"

IMAGE_NAMES = [
    "quay.io/jetstack/cert-manager-controller",
    "quay.io/jetstack/cert-manager-cainjector",
    "quay.io/jetstack/cert-manager-webhook",
    "quay.io/jetstack/cert-manager-startupapicheck",
]


def chart_version() -> str:
    config = tomllib.loads(BOOTSTRAP_TOML.read_text())
    version = config.get("charts", {}).get("cert-manager")
    if version is None:
        sys.exit(f"{BOOTSTRAP_TOML}: charts.cert-manager is missing")
    return version


def pivot_version() -> str:
    match = re.search(r'CERT_MANAGER_VERSION="([^"]+)"', PIVOT_SH.read_text())
    if match is None:
        sys.exit(f"{PIVOT_SH}: could not find CERT_MANAGER_VERSION=\"...\"")
    return match.group(1)


def zarf_chart_version() -> str:
    text = ZARF_YAML.read_text()
    # Tolerates an optional `# renovate: ...` annotation line between `url:`
    # and `version:` so this doesn't break whichever order this and the
    # customManager that adds that annotation merge in.
    match = re.search(
        r"name: cert-manager\n"
        r"\s+namespace: cert-manager\n"
        r"\s+url: [^\n]+\n"
        r"(?:\s+#[^\n]*\n)*"
        r"\s+version: v([^\s]+)",
        text,
    )
    if match is None:
        sys.exit(f"{ZARF_YAML}: could not find the cert-manager chart's version: line")
    return match.group(1)


def image_tags() -> dict:
    """{file: {image_name: tag}} for the four cert-manager images in each source."""
    tags = {}
    for source in (IMAGES_TXT, ZARF_YAML):
        found = {}
        for name in IMAGE_NAMES:
            match = re.search(rf"{re.escape(name)}:v([^\s@]+)", source.read_text())
            if match is not None:
                found[name] = match.group(1)
        tags[source] = found
    return tags


def main() -> int:
    expected = chart_version()
    failures = []

    pivot = pivot_version()
    if pivot != expected:
        failures.append(
            f"{PIVOT_SH}: CERT_MANAGER_VERSION={pivot!r} != bootstrap.toml's {expected!r}"
        )

    zarf_chart = zarf_chart_version()
    if zarf_chart != expected:
        failures.append(
            f"{ZARF_YAML}: chart version {zarf_chart!r} != bootstrap.toml's {expected!r}"
        )

    for source, found in image_tags().items():
        missing = set(IMAGE_NAMES) - set(found)
        if missing:
            failures.append(f"{source}: missing image tags for {sorted(missing)}")
        for name, tag in found.items():
            if tag != expected:
                failures.append(
                    f"{source}: {name}:v{tag} does not match chart version "
                    f"v{expected} (bootstrap.toml)"
                )

    if failures:
        print("cert-manager version consistency check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"cert-manager version consistency check OK (chart and images both v{expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
