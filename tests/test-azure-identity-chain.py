#!/usr/bin/env python3
"""Cross-check the Azure workload-identity chain (issue #71).

The management-side ASO resources and the workload-cluster ASO release are
coupled by literal names that no renderer validates: the ConfigMap the
ManagedCluster exports its OIDC issuer into must be the one the
FederatedIdentityCredential reads; the ConfigMap the UserAssignedIdentity
exports its principalId into must be the one each RoleAssignment reads; and
the federated subject must name the ASO service account on the workload
cluster. Requires PyYAML (mise's python provides it via `uv run`).
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = REPO_ROOT / "mgmt/azure/infrastructure/aso-workload-identity"
CLUSTERS = REPO_ROOT / "mgmt/azure/clusters"
ASO_NAMESPACE = "azureserviceoperator-system"
ASO_SERVICE_ACCOUNT = "azureserviceoperator-default"


def docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def all_docs(root: Path):
    for path in sorted(root.rglob("*.yaml")):
        if path.name in ("kustomization.yaml", "capi-nameref.yaml"):
            continue
        for doc in docs(path):
            yield path.relative_to(REPO_ROOT), doc


def main() -> int:
    failures = []
    exported_oidc = set()
    for _, doc in all_docs(CLUSTERS):
        if doc.get("kind") != "AzureASOManagedControlPlane":
            continue
        for res in doc["spec"].get("resources", []):
            cm = (res.get("spec", {}).get("operatorSpec", {}).get("configMaps", {})
                  .get("oidcIssuerProfile", {}))
            if cm:
                exported_oidc.add((cm["name"], cm["key"]))

    exported_principal = set()
    for _, doc in all_docs(IDENTITY):
        if doc.get("kind") == "UserAssignedIdentity":
            cm = doc["spec"].get("operatorSpec", {}).get("configMaps", {}).get("principalId", {})
            if cm:
                exported_principal.add((cm["name"], cm["key"]))

    for path, doc in all_docs(IDENTITY):
        kind = doc.get("kind")
        if kind == "FederatedIdentityCredential":
            ref = doc["spec"].get("issuerFromConfig", {})
            if (ref.get("name"), ref.get("key")) not in exported_oidc:
                failures.append(f"{path}: issuerFromConfig {ref} is not exported by any ManagedCluster")
            expected = f"system:serviceaccount:{ASO_NAMESPACE}:{ASO_SERVICE_ACCOUNT}"
            if doc["spec"].get("subject") != expected:
                failures.append(f"{path}: subject must be {expected}")
        if kind == "RoleAssignment":
            ref = doc["spec"].get("principalIdFromConfig", {})
            if (ref.get("name"), ref.get("key")) not in exported_principal:
                failures.append(f"{path}: principalIdFromConfig {ref} is not exported by any UserAssignedIdentity")

    ns_doc = docs(REPO_ROOT / "workload/azure-base/aso/namespace.yaml")[0]
    if ns_doc["metadata"]["name"] != ASO_NAMESPACE:
        failures.append(f"workload/azure-base/aso/namespace.yaml: namespace must be {ASO_NAMESPACE}")

    if failures:
        print("azure identity chain FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"azure identity chain OK ({len(exported_oidc)} issuer exports, {len(exported_principal)} principal exports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
