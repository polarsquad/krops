#!/usr/bin/env bash
# stage-and-create-cluster.sh — gap-side staging. Runs BEFORE zarf init.
#
# 1. docker-loads every image archive from airgap/archives/ into the host
#    Docker daemon:
#      - kindest/node v1.37.0 (mgmt kind node and CAPD workload/management
#        nodes) — kind and CAPD `docker run` these directly from the host
#        daemon, outside kubelet, so the Zarf agent cannot rewrite them.
#      - kindest/haproxy (CAPD load balancer) and registry:2 (krops-registry,
#        recreated in Phase 5 for the workload cluster's Flux).
#      - workload-pod-images.tar: flux-operator, flux controllers, podinfo —
#        consumed via preLoadImages by CAPD DevMachineTemplates (Phase 5).
# 2. Creates the kind management cluster (same shape as bootstrap.sh:
#    control-plane node with the Docker socket mounted; NO registry mirror
#    patch — in the gap, pod images reach the node through the Zarf agent's
#    rewrite to the nodeport registry).
#
# Everything here must work with Wi-Fi off.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AIRGAP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ARCHIVES="$AIRGAP_DIR/archives"

CLUSTER_NAME="${CLUSTER_NAME:-mgmt}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.37.0}"
DOCKER_SOCKET_PATH="${DOCKER_SOCKET_PATH:-/var/run/docker.sock}"

if [ ! -S "$DOCKER_SOCKET_PATH" ]; then
  echo "ERROR: Docker socket does not exist: ${DOCKER_SOCKET_PATH}" >&2
  exit 1
fi

echo ">>> Loading image archives into the host Docker daemon..."
for tar in "$ARCHIVES"/*.tar; do
  echo "    docker load -i $(basename "$tar")"
  docker load -i "$tar" >/dev/null
done

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  echo ">>> kind cluster '${CLUSTER_NAME}' already exists; leaving it in place"
else
  echo ">>> Creating kind cluster '${CLUSTER_NAME}' (image ${KIND_NODE_IMAGE})..."
  kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: ${DOCKER_SOCKET_PATH}
        containerPath: /var/run/docker.sock
EOF
fi

kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
kubectl wait --for=condition=Ready node --all --timeout=180s

# Recreate krops-registry (the workload cluster's Flux and CAAPH fetch from it;
# the Zarf internal registry is only reachable inside the mgmt cluster).
REGISTRY_NAME="${REGISTRY_NAME:-krops-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5001}"
registry_failure() {
  echo "ERROR: registry '${REGISTRY_NAME}' $*" >&2
  docker ps -a --filter "name=^${REGISTRY_NAME}$" >&2
  docker logs "$REGISTRY_NAME" >&2 || true
  exit 1
}

if ! docker ps --filter "name=^${REGISTRY_NAME}$" --format '{{.Names}}' | grep -q "$REGISTRY_NAME"; then
  echo ">>> Creating registry container '${REGISTRY_NAME}' (localhost:${REGISTRY_PORT})..."
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$REGISTRY_NAME" --network kind \
    -p "127.0.0.1:${REGISTRY_PORT}:5000" \
    --health-cmd='wget --spider --quiet http://localhost:5000/v2/ || exit 1' \
    --health-interval=1s \
    --health-timeout=2s \
    --health-retries=15 \
    registry:2 >/dev/null
fi

registry_health=$(docker inspect --format \
  '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
  "$REGISTRY_NAME")
if [ "$registry_health" != "none" ]; then
  for attempt in $(seq 1 30); do
    registry_health=$(docker inspect --format '{{.State.Health.Status}}' "$REGISTRY_NAME")
    [ "$registry_health" = "healthy" ] && break
    [ "$registry_health" = "unhealthy" ] && break
    sleep 1
  done
  if [ "$registry_health" != "healthy" ]; then
    registry_failure "health status is ${registry_health}"
  fi
fi

# Verify the published host port as well as the in-container health check.
if ! curl --fail --show-error --silent --connect-timeout 1 --max-time 2 \
  --retry 15 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
  "http://localhost:${REGISTRY_PORT}/v2/" >/dev/null; then
  registry_failure "is not reachable through localhost:${REGISTRY_PORT}"
fi

# Seed krops-registry: the krops config artifact (workload/local-host syncs
# from it) and the two OCI charts (flux-operator for the HelmChartProxy,
# podinfo for the workload HelmRelease).
echo ">>> Seeding ${REGISTRY_NAME} with the config artifact + charts..."
flux push artifact "oci://localhost:${REGISTRY_PORT}/krops:latest" \
  --path="$AIRGAP_DIR/config-artifact" \
  --source="airgap-bundle" \
  --revision="airgap@sha1:$(git -C "$AIRGAP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
  --insecure-registry \
  --reproducible
helm push "$ARCHIVES/charts/flux-operator-0.58.0.tgz" "oci://localhost:${REGISTRY_PORT}/charts" --plain-http
helm push "$ARCHIVES/charts/podinfo-6.14.0.tgz" "oci://localhost:${REGISTRY_PORT}/stefanprodan/charts" --plain-http

echo ">>> Staged. Next:"
echo "      zarf init archives/zarf-init-arm64-v0.83.0.tar.zst --registry-mode=nodeport --components=\"\" --confirm"
echo "      zarf package deploy zarf-package-krops-airgap-arm64-0.1.0.tar.zst --confirm"
