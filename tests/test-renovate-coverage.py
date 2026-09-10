#!/usr/bin/env python3
"""Verify Renovate detects the pins that replaced the version catalog.

The catalog drift check was retired; these detection checks are what proves
a bump cannot silently stop being proposed for the non-manifest surfaces
(issue #74 acceptance criteria). Each entry maps a tracked file to the
depNames Renovate must extract from it via custom.regex managers.

The repo-level regression for #187 is checked separately via a full
`--dry-run=full` run: the custom-regex harness only exercises the regex
manager and therefore cannot merge the flux/helm and regex registry inputs
for the same chart, which is where Renovate emits the real warning.
"""

import os
import subprocess
import sys
from pathlib import Path

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
    "mise.toml": {"astral-sh/uv"},
    "mgmt/azure/capi-providers/capz-system/providers.yaml": {
        "kubernetes-sigs/cluster-api-provider-azure",
    },
    "mise.azure.toml": {"azure-cli"},
}

REQUIRED_SINGLE_REGISTRY = {
    "cert-manager": "https://charts.jetstack.io",
    "cluster-api-operator": "https://kubernetes-sigs.github.io/cluster-api-operator",
}


def assert_single_registry_per_helm_package(result) -> list[str]:
    """Check the packageRules shape that avoids the #187 duplicate registry merge.

    This guards the fix even though the custom-regex harness cannot reproduce the
    full dry-run warning by itself because it runs only the regex manager and never
    combines the flux manager with the regex-extracted dep inputs for the same chart.
    """
    errors = []
    for package_file in ["bootstrap.toml", "pivot.sh"]:
        for dep in result.deps_by_file.get(package_file, []):
            dep_name = dep.get("depName")
            if dep_name not in REQUIRED_SINGLE_REGISTRY:
                continue
            expected = REQUIRED_SINGLE_REGISTRY[dep_name]
            registry_urls = dep.get("registryUrls") or []
            registry_url = dep.get("registryUrl")

            if registry_urls:
                normalized = [url.rstrip("/") for url in registry_urls]
            elif registry_url:
                normalized = [registry_url.rstrip("/")]
            else:
                errors.append(
                    f"{package_file}: {dep_name} has no registryUrl/registryUrls (expected {expected})"
                )
                continue

            if len(normalized) != 1 or normalized[0] != expected.rstrip("/"):
                errors.append(
                    f"{package_file}: {dep_name} registry values={normalized!r}, expected [{expected}]"
                )
    return errors


def assert_no_repo_registry_warnings() -> list[str]:
    """Regression for the full repo dry-run that triggered #187.

    The custom-regex harness cannot see the merge warning because it never enables
    the full repo config mode. This check ensures the repo-level dry-run remains
    free of "Excess registryUrls found for datasource lookup".
    """
    env = os.environ.copy()
    env.setdefault("LOG_LEVEL", "debug")
    if "GITHUB_COM_TOKEN" in env and "RENOVATE_TOKEN" not in env:
        env["RENOVATE_TOKEN"] = env["GITHUB_COM_TOKEN"]
    if "RENOVATE_GITHUB_COM_TOKEN" in env and "GITHUB_COM_TOKEN" not in env:
        env["GITHUB_COM_TOKEN"] = env["RENOVATE_GITHUB_COM_TOKEN"]
    if "GITHUB_COM_TOKEN" in env and "RENOVATE_GITHUB_COM_TOKEN" not in env:
        env["RENOVATE_GITHUB_COM_TOKEN"] = env["GITHUB_COM_TOKEN"]

    completed = subprocess.run(
        ["renovate", "--platform=local", "--dry-run=full"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = completed.stdout
    errors = []
    if "Excess registryUrls found for datasource lookup" in output:
        errors.append("full dry-run: Excess registryUrls found for datasource lookup")
    if completed.returncode not in (0, 1):
        errors.append(f"full dry-run: unexpected exit code {completed.returncode}")
    return errors


def main() -> int:
    result = run_renovate(EXPECTED)

    missing = {
        path: sorted(deps - result.dep_names(path))
        for path, deps in EXPECTED.items()
        if deps - result.dep_names(path)
    }
    registry_errors = assert_single_registry_per_helm_package(result)
    repo_registry_errors = assert_no_repo_registry_warnings()
    if result.returncode or missing or registry_errors or repo_registry_errors:
        print("Renovate coverage check failed", file=sys.stderr)
        print(f"exit code: {result.returncode}", file=sys.stderr)
        if missing:
            for path, deps in sorted(missing.items()):
                print(f"{path}: missing detection for {deps}", file=sys.stderr)
        for message in registry_errors:
            print(message, file=sys.stderr)
        for message in repo_registry_errors:
            print(message, file=sys.stderr)
        result.print_diagnostics()
        return 1

    for path in sorted(EXPECTED):
        print(f"{path}: pin(s) detected: {sorted(result.dep_names(path))}")
    print("helm registry URLs: single registry per tracked chart pin")
    print("repo-level dry-run: no duplicate registry warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
