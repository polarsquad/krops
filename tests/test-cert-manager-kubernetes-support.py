#!/usr/bin/env python3
"""Warn (never fail) when the cert-manager/Kubernetes pairing is untested.

cert-manager 1.21 officially supports Kubernetes 1.33-1.36, but the repo
pins v1.37.0 and that pairing passed the air-gapped nightly (issue #384).
The upstream-window gate that blocked every bootstrap.toml PR was removed
in #458; this keeps a visible, non-blocking signal instead. See
docs/dependencies.md for how to add a pairing.
"""

import os
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TOML = REPO_ROOT / "bootstrap.toml"
NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/docker/cluster.yaml"

# (cert-manager, Kubernetes, evidence). Append a row only after a green
# air-gapped deploy with that exact pairing.
VALIDATED_PAIRINGS = [
    ("1.21.2", "1.37.0", "air-gapped nightly run 35554802424, 2026-09-21"),
]


def pinned_cert_manager_version() -> str:
    with open(BOOTSTRAP_TOML, "rb") as f:
        data = tomllib.load(f)
    charts = data.get("charts", {})
    version = charts.get("cert-manager")
    if not version:
        print("charts.cert-manager is missing from bootstrap.toml", file=sys.stderr)
        sys.exit(1)
    return version


def pinned_kubernetes_version() -> str:
    text = NODE_IMAGE_SOURCE.read_text()
    m = re.search(r"^\s*version: v(?P<version>\d+\.\d+\.\d+)\s*$", text, re.MULTILINE)
    if not m:
        print(
            f"Could not find 'version: vX.Y.Z' in {NODE_IMAGE_SOURCE}",
            file=sys.stderr,
        )
        sys.exit(1)
    return m.group("version")


def main() -> int:
    cert_manager = pinned_cert_manager_version()
    kubernetes = pinned_kubernetes_version()
    for validated_cm, validated_k8s, evidence in VALIDATED_PAIRINGS:
        if (cert_manager, kubernetes) == (validated_cm, validated_k8s):
            print(
                f"NOTICE: cert-manager {cert_manager} with Kubernetes v{kubernetes}"
                f" is empirically validated ({evidence})"
            )
            return 0
    message = (
        f"cert-manager {cert_manager} has not been empirically tested with"
        f" Kubernetes v{kubernetes}. Verify compatibility before merging."
    )
    print(f"WARNING: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=cert-manager/Kubernetes pairing::{message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
