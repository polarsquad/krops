#!/usr/bin/env python3
"""Verify Renovate detects the pins that replaced the version catalog.

The catalog drift check was retired; these detection checks are what proves
a bump cannot silently stop being proposed for the non-manifest surfaces
(issue #74 acceptance criteria). Each entry maps a tracked file to the
depNames Renovate must extract from it via custom.regex managers.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from renovate_harness import run_renovate

EXPECTED = {
    "bootstrap.toml": {
        "ghcr.io/controlplaneio-fluxcd/charts/flux-operator",
        "cert-manager",
        "cluster-api-operator",
    },
    "pivot.sh": {"cert-manager", "cluster-api-operator"},
    ".github/workflows/validate.yml": {"renovate"},
    "mgmt/local-talos/capi-providers/capi-system/providers.yaml": {
        "kubernetes-sigs/cluster-api",
    },
    "mgmt/local-talos/capi-providers/cabpt-system/provider.yaml": {
        "sidero-community/cluster-api-bootstrap-provider-talos",
    },
    "mgmt/local-talos/capi-providers/cacppt-system/provider.yaml": {
        "sidero-community/cluster-api-control-plane-provider-talos",
    },
    "mgmt/local-talos/capi-providers/capt-system/provider.yaml": {
        "shrinedogg/cluster-api-provider-tinkerbell",
    },
    "mgmt/local-talos/clusters/management/cluster.yaml": {
        "kubernetes/kubernetes",
        "siderolabs/talos",
    },
    "mise.local-talos.toml": {"siderolabs/talos"},
}


def main() -> int:
    result = run_renovate(EXPECTED)

    missing = {
        path: sorted(deps - result.dep_names(path))
        for path, deps in EXPECTED.items()
        if deps - result.dep_names(path)
    }
    if result.returncode or missing:
        print("Renovate coverage check failed", file=sys.stderr)
        print(f"exit code: {result.returncode}", file=sys.stderr)
        if missing:
            for path, deps in sorted(missing.items()):
                print(f"{path}: missing detection for {deps}", file=sys.stderr)
        result.print_diagnostics()
        return 1

    for path in sorted(EXPECTED):
        print(f"{path}: pin(s) detected: {sorted(result.dep_names(path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
