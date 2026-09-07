#!/usr/bin/env bash
# build-config-artifact.sh — build the airgap-trimmed krops config tree and
# push it to the connected-side OCI registry, ready to be baked into the
# Zarf package (zarf.yaml lists it under the krops-config component images).
#
# Connected-side only. Requires: flux CLI and an OCI registry. By default it
# uses the krops-registry container from `mise -E local-host run bootstrap`.
#
# Why a trimmed tree: in the gap the substrate (cert-manager, CAPI, CAAPH,
# flux-operator) is deployed by Zarf, and the capi-operator HelmRelease /
# provider fetches cannot reach the internet. The airgap artifact therefore
# contains only the workload-facing kustomizations (clusters/, addons/) and a
# root kustomization + dependsOn chain that never references the substrate
# Kustomizations.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

REGISTRY_PORT="${REGISTRY_PORT:-5001}"
OCI_REPOSITORY="${OCI_REPOSITORY:-krops-airgap}"
OCI_TAG="${OCI_TAG:-latest}"
OCI_REGISTRY="${OCI_REGISTRY:-localhost:${REGISTRY_PORT}}"
OCI_URL="oci://${OCI_REGISTRY}/${OCI_REPOSITORY}:${OCI_TAG}"

ARTIFACT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/krops-airgap-oci.XXXXXX")
cleanup() { rm -rf "$ARTIFACT_ROOT"; }
trap cleanup EXIT

LH="$ARTIFACT_ROOT/mgmt/local-host"
mkdir -p "$LH/clusters" "$LH/addons/cni" "$LH/addons/flux-apps" "$ARTIFACT_ROOT/workload"

# Verbatim copies: the cluster definition and the addon payloads.
cp -R mgmt/local-host/clusters/docker "$LH/clusters/docker"
cp mgmt/local-host/addons/cni/kindnet.yaml "$LH/addons/cni/kindnet.yaml"
cp mgmt/local-host/addons/cni/kustomization.yaml "$LH/addons/cni/kustomization.yaml"
cp mgmt/local-host/addons/cni/flux-ks.yaml "$LH/addons/cni/flux-ks.yaml"
cp mgmt/local-host/addons/flux-apps/kustomization.yaml "$LH/addons/flux-apps/kustomization.yaml"

# Airgap variant of the per-cluster flux-operator HelmChartProxy: fetch the
# chart from krops-registry (seeded by stage-and-create-cluster.sh) instead of
# ghcr.io, which is unreachable in the gap.
cat > "$LH/addons/flux-apps/flux-operator.yaml" <<'EOF'
apiVersion: addons.cluster.x-k8s.io/v1alpha1
kind: HelmChartProxy
metadata:
  name: flux-operator
  namespace: default
spec:
  clusterSelector:
    matchLabels:
      fluxcd: enabled
      krops.polarsquad.com/environment: local-host
  repoURL: oci://krops-registry:5000/charts
  chartName: flux-operator
  version: "0.58.0"
  # Pinned release name, matching mgmt/*/addons/flux-apps/flux-operator.yaml:
  # keeps the release stable across proxy generations and cluster recreations.
  releaseName: flux-operator
  namespace: flux-system
  options:
    waitForJobs: true
    wait: true
    timeout: 5m
    install:
      createNamespace: true
EOF

# Airgap variant of the workload-cluster FluxInstance: distribution.artifact
# is omitted (embedded distribution manifests; the operator's artifact fetch
# has no insecure-registry option) and the four controllers are pinned back to
# tags (the embedded distribution's manifest-list digests do not resolve in a
# single-arch registry). The tag images are pre-loaded into the CAPD node
# stores via preLoadImages (see the cluster-class patch below), so no workload
# pod ever needs the internet. sync.url keeps pointing at krops-registry, which
# stage-and-create-cluster.sh recreates and seeds in the gap.
cat > "$LH/addons/flux-apps/flux-instance.yaml" <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-workload-flux-instance
  namespace: default
data:
  flux-instance.yaml: |
    apiVersion: v1
    kind: Namespace
    metadata:
      name: flux-system
    ---
    apiVersion: fluxcd.controlplane.io/v1
    kind: FluxInstance
    metadata:
      name: flux
      namespace: flux-system
      annotations:
        fluxcd.controlplane.io/reconcileEvery: "1h"
        fluxcd.controlplane.io/reconcileTimeout: "5m"
    spec:
      distribution:
        version: "2.x"
        registry: "ghcr.io/fluxcd"
      components:
        - source-controller
        - kustomize-controller
        - helm-controller
        - notification-controller
      cluster:
        type: kubernetes
        size: small
        multitenant: false
        networkPolicy: true
        domain: cluster.local
      sync:
        kind: OCIRepository
        url: oci://krops-registry:5000/krops
        ref: latest
        path: workload/local-host
      kustomize:
        patches:
          - patch: |
              - op: add
                path: /spec/insecure
                value: true
            target:
              kind: OCIRepository
          - patch: |
              - op: replace
                path: /spec/template/spec/containers/0/image
                value: ghcr.io/fluxcd/source-controller:v1.9.4@sha256:8a8ed0a57b8b86f561d5a4309a69f65e62f0cebe4de8801593c5ff35a3bc3c23
            target:
              kind: Deployment
              name: source-controller
          - patch: |
              - op: replace
                path: /spec/template/spec/containers/0/image
                value: ghcr.io/fluxcd/kustomize-controller:v1.9.4@sha256:2b8bec54ffb6caf421bd2a6c005d27f567d5dd4db7feb55794fb51fcabd69b8f
            target:
              kind: Deployment
              name: kustomize-controller
          - patch: |
              - op: replace
                path: /spec/template/spec/containers/0/image
                value: ghcr.io/fluxcd/helm-controller:v1.6.3@sha256:16ada99456385100698a5d7adf90aba8a2089d987ab541c9566b6d7b0e897038
            target:
              kind: Deployment
              name: helm-controller
          - patch: |
              - op: replace
                path: /spec/template/spec/containers/0/image
                value: ghcr.io/fluxcd/notification-controller:v1.9.3@sha256:071c351a0fb163eeb6a2bb82f1e894f51b6b0734216d2e97d3d99c9ab9d710b9
            target:
              kind: Deployment
              name: notification-controller
---
apiVersion: addons.cluster.x-k8s.io/v1beta1
kind: ClusterResourceSet
metadata:
  name: local-workload-flux-instance
  namespace: default
spec:
  clusterSelector:
    matchLabels:
      fluxcd: enabled
      krops.polarsquad.com/environment: local-host
  strategy: ApplyOnce
  resources:
    - kind: ConfigMap
      name: local-workload-flux-instance
EOF

# cluster-class.yaml: add preLoadImages to both DevMachineTemplates. Two
# offline-only gaps found by Wi-Fi-off runs (connected runs mask them by
# pulling silently):
#   - registry.k8s.io/pause:3.10.1: leftover from the v1.35.0 generation,
#     whose kubeadm required 3.10.1; v1.37.0 kubeadm pulls 3.10.2 (already
#     in images.txt). Kept until the pause-pin follow-up removes it.
#   - docker.io/kindest/kindnetd:v20260528-9350166c: the vendored CNI pins
#     this tag but the v1.37.0 node bakes v20260820; the CNI pull hangs
#     offline so nodes never become Ready.
# The flux controllers + podinfo are pre-loaded so the per-cluster Flux needs
# no internet. All entries load from the host Docker daemon
# (stage-and-create-cluster.sh loads workload-pod-images.tar).
python3 - "$LH/clusters/docker/cluster-class.yaml" <<'PY'
import sys
path = sys.argv[1]
txt = open(path).read()
preload = """          preLoadImages:
            - registry.k8s.io/pause:3.10.1@sha256:278fb9dbcca9518083ad1e11276933a2e96f23de604a3a08cc3c80002767d24c
            - docker.io/kindest/kindnetd:v20260528-9350166c@sha256:92f49a1b2c9242058481fc3e13412c19a62cfeb090717dad4598719d32351f1f
            - ghcr.io/controlplaneio-fluxcd/flux-operator:v0.58.0@sha256:1c919ce1e28716f817ded65c06df0b7a8269542387d5a2ce50212450473c6209
            - ghcr.io/fluxcd/source-controller:v1.9.4@sha256:8a8ed0a57b8b86f561d5a4309a69f65e62f0cebe4de8801593c5ff35a3bc3c23
            - ghcr.io/fluxcd/kustomize-controller:v1.9.4@sha256:2b8bec54ffb6caf421bd2a6c005d27f567d5dd4db7feb55794fb51fcabd69b8f
            - ghcr.io/fluxcd/helm-controller:v1.6.3@sha256:16ada99456385100698a5d7adf90aba8a2089d987ab541c9566b6d7b0e897038
            - ghcr.io/fluxcd/notification-controller:v1.9.3@sha256:071c351a0fb163eeb6a2bb82f1e894f51b6b0734216d2e97d3d99c9ab9d710b9
            - ghcr.io/stefanprodan/podinfo:6.14.0@sha256:0a8aa037137c010a75aed8d3fe56931d0edd3bcd0e55acfb96db11e1e96397b1
"""
anchor = "          customImage: kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5\n"
count = txt.count(anchor)
if count != 2:
    sys.exit(f"ERROR: expected 2 DevMachineTemplate customImage anchors, found {count}")
txt = txt.replace(anchor, anchor + preload)
open(path, "w").write(txt)
print(f"patched {path}: preLoadImages added to {count} DevMachineTemplates")
PY

# Workload tree: rewrite the podinfo chart OCI URL to krops-registry (seeded in
# the gap) and mark the OCIRepository insecure (plain HTTP). The FluxInstance
# insecure patch only covers the operator-generated sync source, not
# tree-defined OCIRepositories. Everything else ships verbatim.
cp -R workload/local-host "$ARTIFACT_ROOT/workload/local-host"
python3 - "$ARTIFACT_ROOT/workload/local-host/podinfo/helm.yaml" <<'PY'
import sys
path = sys.argv[1]
txt = open(path).read()
txt = txt.replace(
    "oci://ghcr.io/stefanprodan/charts/podinfo",
    "oci://krops-registry:5000/stefanprodan/charts/podinfo",
)
if "insecure: true" not in txt:
    txt = txt.replace("spec:\n  interval: 1h\n  url:", "spec:\n  interval: 1h\n  insecure: true\n  url:", 1)
open(path, "w").write(txt)
assert "insecure: true" in txt and "krops-registry" in txt
print(f"patched {path}: podinfo chart -> krops-registry, insecure: true")
PY

# Optional registry override: WORKLOAD_REGISTRY_HOST (default krops-registry)
# rewrites the workload-side registry references. Use a distinct name when
# rehearsing on a host that already runs a live baseline, so the rehearsal's
# seeded registry never touches the baseline's krops:latest.
if [ -n "${WORKLOAD_REGISTRY_HOST:-}" ] && [ "$WORKLOAD_REGISTRY_HOST" != "krops-registry" ]; then
  echo "==> Rewriting workload registry references to '${WORKLOAD_REGISTRY_HOST}'"
  grep -rl "krops-registry" "$ARTIFACT_ROOT" | while IFS= read -r f; do
    sed -i.bak "s|krops-registry|${WORKLOAD_REGISTRY_HOST}|g" "$f"
    rm -f "${f}.bak"
  done
fi


# Trimmed root kustomization: no infrastructure/ or capi-providers/ entries.
cat > "$LH/kustomization.yaml" <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
# Airgap root of the local-host OCI artifact. The substrate (cert-manager,
# CAPI core/providers, CAAPH, flux-operator) is deployed by Zarf, so this root
# only wires the workload-facing Kustomizations. DependsOn chains never
# reference substrate Kustomizations (they do not exist in the gap).
resources:
  - clusters/flux-ks.yaml
  - addons/cni/flux-ks.yaml
  - addons/flux-apps/flux-ks.yaml
EOF

# clusters/flux-ks.yaml: drop the dependsOn on capd-system (Zarf-managed).
cat > "$LH/clusters/flux-ks.yaml" <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: docker-workload-cluster
  namespace: flux-system
spec:
  interval: 5m
  retryInterval: 2m
  timeout: 5m
  prune: true
  wait: false
  sourceRef:
    kind: OCIRepository
    name: flux-system
  path: ./mgmt/local-host/clusters/docker
  healthChecks:
    - apiVersion: cluster.x-k8s.io/v1beta2
      kind: Cluster
      name: local-workload
      namespace: default
EOF

# addons/flux-apps/flux-ks.yaml: dependsOn local-workload-cni only
# (caaph-system is Zarf-managed in the gap).
cat > "$LH/addons/flux-apps/flux-ks.yaml" <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: flux-apps
  namespace: flux-system
spec:
  interval: 5m
  retryInterval: 2m
  timeout: 10m
  prune: true
  wait: true
  sourceRef:
    kind: OCIRepository
    name: flux-system
  path: ./mgmt/local-host/addons/flux-apps
  dependsOn:
    - name: local-workload-cni
EOF

# Optional rehearsal rename: AIRGAP_CLUSTER_NAME=airgap-wl rewrites every
# local-workload reference inside the assembled mgmt tree (copied files AND
# the generated flux-ks files above, so dependsOn references stay consistent).
# Use when rehearsing on a host whose Docker daemon already runs a live
# local-workload baseline: CAPD matches containers by the
# cluster.x-k8s.io/cluster-name label, so two clusters with the same name on
# one daemon would collide/adopt.
if [ -n "${AIRGAP_CLUSTER_NAME:-}" ]; then
  echo "==> Renaming workload cluster to '${AIRGAP_CLUSTER_NAME}' in the artifact copy"
  grep -rl "local-workload" "$LH" | while IFS= read -r f; do
    sed -i.bak "s|local-workload|${AIRGAP_CLUSTER_NAME}|g" "$f"
    rm -f "${f}.bak"
  done
fi

GIT_SHA=$(git rev-parse HEAD)
GIT_REF=$(git branch --show-current)
GIT_REF="${GIT_REF:-detached}"
SOURCE_URL=$(git config --get remote.origin.url || true)
SOURCE_URL="${SOURCE_URL:-file://${REPO_ROOT}}"

echo "==> Pushing airgap config artifact ${OCI_URL}"
PUSH_ARGS=(
  "$OCI_URL"
  --path="$ARTIFACT_ROOT"
  --source="$SOURCE_URL"
  --revision="${GIT_REF}@sha1:${GIT_SHA}"
  --reproducible
)
if [[ "$OCI_REGISTRY" == localhost:* || "$OCI_REGISTRY" == 127.0.0.1:* ]]; then
  PUSH_ARGS+=(--insecure-registry)
fi
if [ -n "${OCI_USERNAME:-}" ] && [ -n "${OCI_PASSWORD:-}" ]; then
  PUSH_ARGS+=(--creds "${OCI_USERNAME}:${OCI_PASSWORD}")
fi
flux push artifact "${PUSH_ARGS[@]}"

# Keep a plain-directory copy of the tree in the bundle: the gap-side stage
# script re-pushes it into krops-registry as krops:latest for the workload
# cluster's Flux (which does not talk to the Zarf registry).
rm -rf "$REPO_ROOT/airgap/config-artifact"
cp -R "$ARTIFACT_ROOT" "$REPO_ROOT/airgap/config-artifact"
echo "==> Bundle copy at airgap/config-artifact/ ($(du -sh "$REPO_ROOT/airgap/config-artifact" | cut -f1))"

echo "==> Done. Config artifact: ${OCI_REGISTRY}/${OCI_REPOSITORY}:${OCI_TAG}"
