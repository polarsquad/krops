#!/usr/bin/env python3
"""Cross-check the kind bootstrap node image pin against every kindest/node ref.

bootstrap.toml's [bootstrap].kind-node-image is authoritative: the bootstrap
CLI and bootstrap.sh write it into the generated kind config (#647), so the
bootstrap node's k8s version is repo-managed instead of riding kind's
baked-in default (which moved to a new minor in kind 0.33). The
docs/dependencies.md invariant says the workload cluster, the CAPD
management cluster, and the kind bootstrap mgmt node all run the same
kindest/node version; this covers the kind-bootstrap leg that no other gate
watches: the CAPD ClusterClass customImage pins, the Cluster
topology.version pins (#142), and the airgap inventory and scripts. A
mismatch means the live bootstrap and the airgap path stage different k8s
versions.
"""

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TOML = REPO_ROOT / "bootstrap.toml"
BOOTSTRAP_SH = REPO_ROOT / "bootstrap.sh"
CLUSTER_CLASSES = (
    REPO_ROOT / "mgmt/local-host/clusters/docker/cluster-class.yaml",
    REPO_ROOT / "mgmt/local-host/clusters/management/cluster-class.yaml",
)
CLUSTER_YAMLS = (
    REPO_ROOT / "mgmt/local-host/clusters/docker/cluster.yaml",
    REPO_ROOT / "mgmt/local-host/clusters/management/cluster.yaml",
)
AIRGAP_REFS = (
    REPO_ROOT / "airgap/images.txt",
    REPO_ROOT / "airgap/scripts/stage-and-create-cluster.sh",
    REPO_ROOT / "airgap/scripts/build-package.sh",
    REPO_ROOT / "airgap/scripts/build-config-artifact.sh",
)

# tag+digest is mandatory: kind requires digest pinning for reproducibility.
PIN_RE = re.compile(r"^kindest/node:v[0-9.]+@sha256:[a-f0-9]{64}$")
# Any kindest/node image ref (digest optional, so a digest-less drift is
# reported as a mismatch instead of being skipped).
REF_RE = re.compile(r"kindest/node:v[0-9A-Za-z.\-]+(?:@sha256:[a-f0-9]{64})?")


def pin() -> str:
    config = tomllib.loads(BOOTSTRAP_TOML.read_text())
    value = config.get("bootstrap", {}).get("kind-node-image")
    if value is None:
        sys.exit(f"{BOOTSTRAP_TOML}: [bootstrap].kind-node-image is missing")
    if not PIN_RE.match(value):
        sys.exit(
            f"{BOOTSTRAP_TOML}: kind-node-image {value!r} must be "
            "kindest/node:v<tag>@sha256:<64-hex-digest>"
        )
    return value


def topology_version(path: Path) -> str:
    match = re.search(
        r"topology:\n(?:[ \t]+\S[^\n]*\n){0,3}[ \t]+version: (v[0-9.]+)",
        path.read_text(),
    )
    if match is None:
        sys.exit(f"{path}: could not find .spec.topology.version")
    return match.group(1)


def main() -> int:
    expected = pin()
    expected_tag = expected.split("@", 1)[0].rsplit(":", 1)[1]  # bare v1.x.y
    failures = []

    for path in (*CLUSTER_CLASSES, *AIRGAP_REFS):
        for found in REF_RE.findall(path.read_text()):
            if found != expected:
                failures.append(f"{path}: {found} != bootstrap.toml's {expected}")

    for path in CLUSTER_YAMLS:
        tag = topology_version(path)
        if tag != expected_tag:
            failures.append(f"{path}: topology.version {tag} != {expected_tag}")

    bootstrap_sh_text = BOOTSTRAP_SH.read_text()
    # The shell path must derive the pin from bootstrap.toml (parity with
    # the CLI, #647) and render it into the kind config.
    if "kind_node_image=$(sed" not in bootstrap_sh_text:
        failures.append(
            f"{BOOTSTRAP_SH}: missing the kind-node-image derive line"
        )
    if "image: ${kind_node_image}" not in bootstrap_sh_text:
        failures.append(
            f"{BOOTSTRAP_SH}: kind config heredoc missing the image line"
        )

    if failures:
        print("kind node image consistency check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"kind node image consistency check OK ({expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
