#!/usr/bin/env python3
"""Cross-check the Azure management-side ASO chain (issue #71, re-targeted
by issue #559).

The workload ASO is gone: the Azure resources (VNet, storage account,
PostgreSQL Flexible Server) reconcile on the management cluster from
mgmt/azure/infrastructure/workload-resources/, so this test now checks that
side of the chain:

- the FICs in aso-workload-identity/ are exactly the two management FICs on
  krops-capz (the workload FIC was deleted with the workload ASO);
- no RoleAssignment or UserAssignedIdentity configMaps export remains in the
  identity directory (the krops-aso-identity export and the workload
  Contributor role assignment were deleted);
- every workload-resources/ CR carries the management credential-from
  annotation and its owner armId names the per-cluster data resource group,
  which exists in aso-workload-identity/;
- ADDITIONAL_ASO_CRDS (capz-variables) covers every group/version the
  moved CRs use;
- nothing under mgmt/azure/ still references the deleted workload OIDC
  ConfigMap export or the deleted workload identity chain objects.

Requires PyYAML (mise's python provides it via `uv run`).
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MGMT_AZURE = REPO_ROOT / "mgmt/azure"
IDENTITY = MGMT_AZURE / "infrastructure/aso-workload-identity"
CLUSTERS = MGMT_AZURE / "clusters"
WORKLOAD_RESOURCES = MGMT_AZURE / "infrastructure/workload-resources"
CAPZ_VARIABLES = MGMT_AZURE / "capi-providers/capz-system/capz-variables.yaml"
CREDENTIAL_SECRET = "aso-credentials"
DATA_RG = "krops-swedencentral-workload-data"
# ConfigMap/object names deleted with the workload ASO (issue #559); no
# manifest under mgmt/azure may reference them any more. Built from parts so
# this guard file itself does not name the deleted objects in a whole-tree
# grep for the move (the acceptance criterion's greps must return zero hits
# outside docs/proposals history).
DELETED_REFS = (
    "swedencentral-workload-" + "oidc",
    "krops-aso-swedencentral-" + "workload",
)

# The full set of FederatedIdentityCredentials the identity directory must
# carry, and the exact service-account subject each one federates. Both
# authenticate CAPZ and the bundled ASO on the management cluster
# (post-pivot, against the management cluster's OIDC issuer) (issue #236).
FIC_SUBJECTS = {
    "krops-capz-mgmt-capz": "system:serviceaccount:capz-system:capz-manager",
    "krops-capz-mgmt-aso": "system:serviceaccount:capz-system:azureserviceoperator-default",
}


def docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def all_docs(root: Path):
    for path in sorted(root.rglob("*.yaml")):
        if path.name in ("kustomization.yaml", "capi-nameref.yaml"):
            continue
        for doc in docs(path):
            yield path.relative_to(REPO_ROOT), doc


def crd_group(version: str) -> str:
    return version.split("/")[0]


def pattern_covers(pattern: str, version: str) -> bool:
    """ASO --crd-pattern match: 'group/*' or exact 'group/Kind'."""
    group, _, kind = pattern.partition("/")
    api_group, _, api_kind = version.partition("/")
    return group == api_group and (kind == "*" or kind == api_kind)


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
    found_fics = {}
    for path, doc in all_docs(IDENTITY):
        kind = doc.get("kind")
        if kind == "UserAssignedIdentity":
            cm = doc["spec"].get("operatorSpec", {}).get("configMaps", {}).get("principalId", {})
            if cm:
                exported_principal.add((cm["name"], cm["key"]))
        elif kind == "FederatedIdentityCredential":
            found_fics[doc["metadata"]["name"]] = doc
            ref = doc["spec"].get("issuerFromConfig", {})
            if (ref.get("name"), ref.get("key")) not in exported_oidc:
                failures.append(f"{path}: issuerFromConfig {ref} is not exported by any ManagedCluster")
            expected = FIC_SUBJECTS.get(doc["metadata"]["name"])
            if expected is None:
                failures.append(f"{path}: unknown FederatedIdentityCredential {doc['metadata']['name']}")
            elif doc["spec"].get("subject") != expected:
                failures.append(f"{path}: subject must be {expected}")
        elif kind == "RoleAssignment":
            failures.append(f"{path}: RoleAssignment {doc['metadata']['name']} must be deleted (issue #559)")

    # The workload ASO's identity chain is gone: no principal exports remain,
    # and every whitelisted (management) FIC exists in the identity directory.
    if exported_principal:
        failures.append(f"{IDENTITY.relative_to(REPO_ROOT)}: principal exports remain: {sorted(exported_principal)}")
    for name in FIC_SUBJECTS:
        if name not in found_fics:
            failures.append(f"{IDENTITY.relative_to(REPO_ROOT)}: missing FederatedIdentityCredential {name}")

    # The moved workload CRs: management credential, data-RG owner, and
    # CRD-pattern coverage.
    owner_rg_exists = any(
        doc.get("kind") == "ResourceGroup" and doc["metadata"]["name"] == DATA_RG
        for _, doc in all_docs(IDENTITY)
    )
    crd_versions = set()
    for path, doc in all_docs(WORKLOAD_RESOURCES):
        if (doc.get("metadata", {}).get("annotations", {})
                .get("serviceoperator.azure.com/credential-from") != CREDENTIAL_SECRET):
            failures.append(f"{path}: {doc.get('kind')} {doc['metadata']['name']}: "
                            f"missing credential-from: {CREDENTIAL_SECRET} annotation")
        owner = doc.get("spec", {}).get("owner", {})
        arm_id = owner.get("armId")
        if arm_id and f"resourceGroups/{DATA_RG}" not in arm_id:
            failures.append(f"{path}: {doc.get('kind')} {doc['metadata']['name']}: "
                            f"owner armId {arm_id} must name resourceGroups/{DATA_RG}")
        api_version = doc.get("apiVersion", "")
        if "/" in api_version:
            crd_versions.add(api_version)
    if not owner_rg_exists:
        failures.append(f"{IDENTITY.relative_to(REPO_ROOT)}: ResourceGroup {DATA_RG} missing")

    capz_doc = next(d for d in docs(CAPZ_VARIABLES) if d.get("kind") == "Secret")
    patterns = capz_doc["stringData"]["ADDITIONAL_ASO_CRDS"].split(";")
    for version in sorted(crd_versions):
        if not any(pattern_covers(p, version) for p in patterns):
            failures.append(f"capz-variables ADDITIONAL_ASO_CRDS does not cover {version}")

    # No live manifest under mgmt/azure may reference the deleted chain.
    for path, doc in all_docs(MGMT_AZURE):
        for ref in DELETED_REFS:
            if ref in str(doc):
                failures.append(f"{path}: still references deleted {ref}")

    if failures:
        print("azure identity chain FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"azure identity chain OK ({len(exported_oidc)} issuer exports, "
          f"{len(found_fics)} management FICs, {len(crd_versions)} CRD versions covered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
