#!/usr/bin/env python3
"""flux-apps delivers the per-workload Flux bootstrap bag via a ResourceSet,
not a ClusterResourceSet.

Each mgmt/<cloud>/addons/flux-apps build must contain a ResourceSet (no CRS).
The ResourceSet's resourcesTemplate, rendered per input, must produce one
Kustomization per workload cluster in namespace `default`, pointing at
<cluster>-kubeconfig and the per-region bundle. The per-region bundles must
build and carry the cloud's cluster-vars + pull secret (+ FluxInstance);
local-host carries only the FluxInstance.
"""
import re, subprocess, sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

# cloud -> {source kind, region -> (live cluster name, expected cluster-vars keys)}
# local-host is OUT of this PR (airgap generator entanglement): it keeps its
# CRS and must not gain a ResourceSet.
CLOUDS = {
    "aws": {
        "source": "GitRepository",
        "regions": {
            "eu-north-1": ("eu-north-1-workload",
                           ["AWS_REGION", "CLUSTER_NAME", "AWS_ACCOUNT_ID"]),
            "eu-west-1": ("eu-west-1-workload",
                          ["AWS_REGION", "CLUSTER_NAME", "AWS_ACCOUNT_ID"]),
        },
    },
    "azure": {
        "source": "GitRepository",
        "regions": {
            "swedencentral": ("swedencentral-workload",
                              ["AZURE_LOCATION", "CLUSTER_NAME", "AZURE_SUBSCRIPTION_ID",
                               "AZURE_TENANT_ID", "AZURE_ASO_CLIENT_ID",
                               "AZURE_ASO_PRINCIPAL_ID", "STORAGE_ACCOUNT_NAME"]),
        },
    },
    "gcp": {
        "source": "GitRepository",
        "regions": {
            "europe-north1": ("europe-north1-workload",
                              ["GCP_REGION", "CLUSTER_NAME", "GCP_PROJECT",
                               "GCP_PROJECT_NUMBER", "GCP_NETWORK"]),
        },
    },
}
# local-host must stay byte-identical: CRS present, no ResourceSet.
LOCAL_HOST = "mgmt/local-host/addons/flux-apps"


def build(rel: str) -> str:
    return subprocess.run(["kubectl", "kustomize", str(REPO / rel)],
                          check=True, capture_output=True, text=True).stdout


def render_template(tpl: str, inputs: dict) -> list:
    """Mirror the operator: substitute << inputs.KEY >> then split docs."""
    def sub(m):
        return str(inputs[m.group(1)])
    return list(yaml.safe_load_all(re.sub(r"<<\s*inputs\.(\w+)\s*>>", sub, tpl)))


def build_bundle(rel: str) -> str:
    """kubectl kustomize of the bundle (SOPS files render ENC[...] verbatim).

    CI has no age private key, so the pull Secret is checked by FILE PRESENCE
    (the SOPS-encrypted `flux-github-pat.sops.yaml` listed in the bundle's
    kustomization.yaml), not by decrypting it. The raw build passes ENC[...]
    placeholders through, which is enough to verify the manifest structure.
    """
    return subprocess.run(["kubectl", "kustomize", str(REPO / rel)],
                          check=True, capture_output=True, text=True).stdout


def main() -> None:
    fails: list[str] = []
    for cloud, spec in CLOUDS.items():
        rel = f"mgmt/{cloud}/addons/flux-apps"
        try:
            rendered = build(rel)
        except subprocess.CalledProcessError as e:
            fails.append(f"{rel}: kustomize build failed: {e.stderr.strip()}")
            continue

        if "kind: ClusterResourceSet" in rendered:
            fails.append(f"{rel}: ClusterResourceSet still present (must be removed)")

        rset = None
        for doc in yaml.safe_load_all(rendered):
            if doc and doc.get("kind") == "ResourceSet":
                rset = doc
        if rset is None:
            fails.append(f"{rel}: ResourceSet missing")
            continue
        if rset["metadata"].get("namespace") != "default":
            fails.append(f"{rel}: ResourceSet must be in namespace `default`")

        inputs = rset["spec"]["inputs"]
        tmpl = rset["spec"]["resourcesTemplate"]

        # Render for each real input and validate the generated Kustomization.
        for inp in inputs:
            region = inp["region"]
            cluster = inp["cluster"]
            if region not in spec["regions"]:
                fails.append(f"{rel}: input region {region} not expected")
                continue
            _, want_vars = spec["regions"][region]
            docs = render_template(tmpl, inp)
            ks = [d for d in docs if d and d.get("kind") == "Kustomization"]
            if len(ks) != 1:
                fails.append(f"{rel}: input {region} rendered {len(ks)} Kustomization(s), want 1")
                continue
            k = ks[0]
            if k["metadata"]["namespace"] != "default":
                fails.append(f"{rel}: {region} Kustomization not in `default`")
            if k["spec"].get("kubeConfig", {}).get("secretRef", {}).get("name") != f"{cluster}-kubeconfig":
                fails.append(f"{rel}: {region} kubeConfig.secretRef.name != {cluster}-kubeconfig")
            if k["spec"]["sourceRef"].get("kind") != spec["source"]:
                fails.append(f"{rel}: {region} sourceRef.kind != {spec['source']}")
            want_path = f"./mgmt/{cloud}/addons/flux-apps/regions/{region}"
            if k["spec"].get("path") != want_path:
                fails.append(f"{rel}: {region} path != {want_path}")
            if k["spec"].get("prune") is not True:
                fails.append(f"{rel}: {region} Kustomization must set prune: true")
            if not k["spec"].get("decryption"):
                fails.append(f"{rel}: {region} Kustomization must SOPS-decrypt (pull secret)")

            # The per-region bundle must build and carry the right objects.
            brel = f"mgmt/{cloud}/addons/flux-apps/regions/{region}"
            try:
                bundle = build_bundle(brel)
            except subprocess.CalledProcessError as e:
                fails.append(f"{brel}: kustomize build failed: {e.stderr.strip()}")
                continue
            kinds = [d["kind"] for d in yaml.safe_load_all(bundle) if d]
            if "FluxInstance" not in kinds:
                fails.append(f"{brel}: FluxInstance missing")
            if "ClusterResourceSet" in kinds:
                fails.append(f"{brel}: ClusterResourceSet must not be in a bundle")
            for var in want_vars:
                if var not in bundle:
                    fails.append(f"{brel}: cluster-var {var} missing")
            # Pull secret: SOPS-encrypted, so CI has no age key to decrypt it.
            # Check file presence + kustomization membership instead.
            secret_file = REPO / brel / "flux-github-pat.sops.yaml"
            kustomization = (REPO / brel / "kustomization.yaml").read_text()
            if not secret_file.exists():
                fails.append(f"{brel}: flux-github-pat.sops.yaml missing")
            elif "flux-github-pat.sops.yaml" not in kustomization:
                fails.append(f"{brel}: flux-github-pat.sops.yaml not listed in kustomization.yaml")

    # local-host is out of this PR: its flux-apps tree must be unchanged
    # (CRS still present, no ResourceSet introduced).
    try:
        lh = build(LOCAL_HOST)
    except subprocess.CalledProcessError as e:
        fails.append(f"{LOCAL_HOST}: kustomize build failed: {e.stderr.strip()}")
    else:
        if "kind: ClusterResourceSet" not in lh:
            fails.append(f"{LOCAL_HOST}: local-host is out of this PR; its CRS must stay")
        if "kind: ResourceSet" in lh:
            fails.append(f"{LOCAL_HOST}: local-host must not gain a ResourceSet in this PR")

    if fails:
        for f in fails:
            print("FAIL:", f, file=sys.stderr)
        sys.exit(1)
    print("OK: flux-apps delivers the workload Flux bag via ResourceSet (no CRS)")


if __name__ == "__main__":
    main()
