#!/usr/bin/env bash
# bootstrap.sh – One-time imperative bootstrap for the management cluster.
# Everything after this script runs is driven by GitOps (Flux).
set -euo pipefail

source "$(dirname "$0")/bootstrap-common.sh"

PROFILE="${KROPS_PROFILE:-${1:-aws}}"
GIT_BRANCH="main"
REGISTRY_NAME="krops-registry"
REGISTRY_PORT="${REGISTRY_PORT:-5001}"
REGISTRY_READY_RETRIES="${REGISTRY_READY_RETRIES:-120}"
LOCAL_RECONCILE_TIMEOUT="${LOCAL_RECONCILE_TIMEOUT:-15m}"

preflight_checks() {
  case "$PROFILE" in
    local-host|aws) ;;
    *)
      echo "ERROR: unsupported profile '$PROFILE' (expected 'local-host' or 'aws')" >&2
      exit 1
      ;;
  esac

  for cmd in curl kind helm kubectl; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found in PATH"; exit 1; }
  done

  if [ "$PROFILE" = local-host ]; then
    command -v mise >/dev/null 2>&1 \
      || { echo "ERROR: mise not found in PATH (required to publish the initial OCI artifact)"; exit 1; }
  fi

  if [ "$PROFILE" = aws ]; then
    require_flux_env
  fi

  detect_container_engine
}

preflight_checks
echo ">>> Using container engine: ${CONTAINER_ENGINE} (socket: ${ENGINE_SOCK})"

# ── Step 1: Create the kind management cluster ────────────────────────────────
echo ">>> Creating kind cluster 'mgmt'..."
# Check if cluster already exists and delete it (idempotent)
if kind get clusters 2>/dev/null | grep -q "^mgmt$"; then
  echo ">>> Cluster 'mgmt' already exists – recreating..."
  kind delete cluster --name mgmt
fi
# Mount the host's container engine socket into the kind node at the standard Docker
# socket path. This ensures all in-cluster components can access a Docker-compatible API
# at /var/run/docker.sock, whether the backend is Docker or Podman (which exposes a
# Docker-compatible socket). This is essential for building and loading container images.
KIND_REGISTRY_PATCH=""
if [ "$PROFILE" = local-host ]; then
  KIND_REGISTRY_PATCH="containerdConfigPatches:
  - |-
    [plugins.\"io.containerd.grpc.v1.cri\".registry.mirrors.\"localhost:${REGISTRY_PORT}\"]
      endpoint = [\"http://${REGISTRY_NAME}:5000\"]"
fi
kind create cluster --name mgmt --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
${KIND_REGISTRY_PATCH}
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: ${ENGINE_SOCK}
        containerPath: /var/run/docker.sock
EOF

echo ">>> Waiting for cluster node to be ready..."
# Explicitly switch kubectl to use the kind cluster context
kubectl config use-context kind-mgmt
kubectl wait --for=condition=Ready node --all --timeout=120s

# ── Step 1.5: Bootstrap local container registry (local-host profile only) ────
if [ "$PROFILE" = local-host ]; then
  echo ">>> Bootstrapping local container registry..."

  # Check if registry already exists
  if ! $CONTAINER_ENGINE ps -a --filter "name=^${REGISTRY_NAME}$" | grep -q "${REGISTRY_NAME}"; then
    # Create registry container
    echo "    Creating registry container '${REGISTRY_NAME}'..."
    $CONTAINER_ENGINE run -d \
      --name "$REGISTRY_NAME" \
      --network kind \
      -p "127.0.0.1:${REGISTRY_PORT}:5000" \
      registry:2 >/dev/null
    echo "    Registry created and running: localhost:${REGISTRY_PORT}"
  else
    # Check if registry is running, restart if needed
    if ! $CONTAINER_ENGINE ps --filter "name=^${REGISTRY_NAME}$" | grep -q "${REGISTRY_NAME}"; then
      echo "    Restarting stopped registry..."
      $CONTAINER_ENGINE start "$REGISTRY_NAME" >/dev/null
      echo "    Registry restarted: localhost:${REGISTRY_PORT}"
    else
      echo "    Registry already running: localhost:${REGISTRY_PORT}"
    fi
  fi

  echo ">>> Waiting for local registry API at localhost:${REGISTRY_PORT}..."
  if ! curl --fail --silent --show-error \
    --retry "$REGISTRY_READY_RETRIES" \
    --retry-connrefused \
    --retry-delay 1 \
    "http://localhost:${REGISTRY_PORT}/v2/" >/dev/null; then
    echo "ERROR: local registry did not become ready at localhost:${REGISTRY_PORT}" >&2
    exit 1
  fi

  # Configure kind nodes to access the registry via the hostname
  # Add a configmap to tell the cluster about the local registry
  kubectl create configmap local-registry-config \
    --from-literal=registry-url="${REGISTRY_NAME}:5000" \
    --namespace kube-system

  echo ">>> Publishing initial OCI artifact from the local Git checkout..."
  mise -E local-host run oci-push
  echo ">>> Initial OCI artifact is available at oci://localhost:${REGISTRY_PORT}/${OCI_REPOSITORY:-krops}:${OCI_TAG:-latest}"
fi

# ── Steps 2–4: Seed Flux (operator, secrets, FluxInstance) ────────────────────
workload_flux_log_pid=""
workload_kubeconfig=""
cleanup_workload_reconciliation() {
  if [ -n "$workload_flux_log_pid" ]; then
    kill "$workload_flux_log_pid" >/dev/null 2>&1 || true
    wait "$workload_flux_log_pid" >/dev/null 2>&1 || true
    workload_flux_log_pid=""
  fi
  if [ -n "$workload_kubeconfig" ]; then
    rm -f "$workload_kubeconfig"
    workload_kubeconfig=""
  fi
}
cleanup_bootstrap() {
  cleanup_workload_reconciliation
  seed_cleanup
}
trap cleanup_bootstrap EXIT
seed_flux

# ── Step 5: Watch local-host reconciliation ──────────────────────────────────
# Stream the GitOps handoff in the bootstrap terminal for the local profile.
# The final Kustomization is created by the OCI root, so wait for it to appear
# before starting the progress watcher or asking kubectl to wait for readiness.
# Matching the watch timeout to the authoritative readiness timeout prevents
# the progress display from reporting a misleading early timeout.
if [ "$PROFILE" = local-host ]; then
  echo ""
  echo ">>> Step 5: Flux reconciliation progress"
  reconcile_discovery_attempts=0
  until kubectl get kustomization flux-apps \
      --namespace flux-system >/dev/null 2>&1; do
    reconcile_discovery_attempts=$((reconcile_discovery_attempts + 1))
    if [ "$reconcile_discovery_attempts" -ge 60 ]; then
      echo "ERROR: flux-apps Kustomization was not created within 2 minutes" >&2
      flux get kustomizations
      exit 1
    fi
    sleep 2
  done

  echo ">>> Waiting until the local workload cluster and Flux addons are ready..."

  if ! kubectl wait kustomization/flux-apps \
      --namespace flux-system \
      --for=condition=Ready \
      --timeout="$LOCAL_RECONCILE_TIMEOUT"; then
    echo "ERROR: local-host reconciliation did not complete within ${LOCAL_RECONCILE_TIMEOUT}" >&2
    flux get kustomizations
    exit 1
  fi
  echo ""
  echo ">>> Workload cluster Flux reconciliation errors"
  workload_kubeconfig="$(mktemp)"

  clusterctl get kubeconfig local-workload > "$workload_kubeconfig"
  workload_endpoint="$($CONTAINER_ENGINE port local-workload-lb 6443/tcp | head -1)"
  workload_port="${workload_endpoint##*:}"
  case "$workload_port" in
    ''|*[!0-9]*)
      echo "ERROR: cannot determine the local-workload API server port" >&2
      exit 1
      ;;
  esac
  kubectl config set-cluster local-workload \
    --server="https://127.0.0.1:${workload_port}" \
    --kubeconfig="$workload_kubeconfig" >/dev/null

  workload_flux_discovery_attempts=0
  until kubectl --kubeconfig "$workload_kubeconfig" get kustomization flux-system \
      --namespace flux-system >/dev/null 2>&1; do
    workload_flux_discovery_attempts=$((workload_flux_discovery_attempts + 1))
    if [ "$workload_flux_discovery_attempts" -ge 60 ]; then
      echo "ERROR: workload Flux Kustomization was not created within 2 minutes" >&2
      kubectl --kubeconfig "$workload_kubeconfig" get pods \
        --namespace flux-system || true
      exit 1
    fi
    sleep 2
  done

  echo ">>> Waiting for workload Flux controllers to be ready..."
  kubectl --kubeconfig "$workload_kubeconfig" wait pod \
    --namespace flux-system \
    --selector='app.kubernetes.io/part-of=flux' \
    --for=condition=Ready \
    --timeout="$LOCAL_RECONCILE_TIMEOUT"

  flux logs \
    --kubeconfig "$workload_kubeconfig" \
    --all-namespaces \
    --follow \
    --level=error \
    --since=10m &
  workload_flux_log_pid=$!

  if ! kubectl --kubeconfig "$workload_kubeconfig" wait kustomization/flux-system \
      --namespace flux-system \
      --for=condition=Ready \
      --timeout="$LOCAL_RECONCILE_TIMEOUT"; then
    echo "ERROR: workload reconciliation did not complete within ${LOCAL_RECONCILE_TIMEOUT}" >&2
    flux get kustomizations \
      --kubeconfig "$workload_kubeconfig" \
      --all-namespaces
    exit 1
  fi
  cleanup_workload_reconciliation
fi

# ── Done ──────────────────────────────────────────────────────────────────────
# Everything else is driven by GitOps. The FluxInstance above syncs the
# mgmt/aws/ directory, whose top-level kustomization.yaml wires in the
# infrastructure, capi-providers, addons, and clusters Kustomizations with
# the correct dependsOn ordering. No further imperative steps are required.
echo ""
if [ "$PROFILE" = aws ]; then
  echo ">>> Bootstrap complete! Flux is now reconciling from ${GIT_REPO_URL}"
  echo ">>> Watch progress with: flux get kustomizations --watch"
else
  echo ">>> Local-host profile complete: Flux is reconciling from the local OCI artifact"
  echo ">>> Local registry: localhost:${REGISTRY_PORT} (cluster endpoint: ${REGISTRY_NAME}:5000)"
  echo ">>> OCI source: oci://${REGISTRY_NAME}:5000/${OCI_REPOSITORY:-krops}:${OCI_TAG:-latest} (path: mgmt/local-host)"
  echo ">>> Watch progress with: flux get sources oci --watch"
  echo ">>> No AWS resources were provisioned"
fi

# ── Pivot to the self-managed management cluster ──────────────────────────────
# This is the DEFAULT exit of bootstrap: the Flux instance seeded above
# reconciles the management-cluster definition (creating the management
# cluster via CAPA/CAPD), then pivot.sh moves the CAPI inventory into it and
# deletes the kind cluster. Opt out with BOOTSTRAP_PIVOT=0 to keep the kind
# bootstrap cluster as the management cluster (pre-#79 behavior).
export KROPS_PROFILE="$PROFILE"
if [ "${BOOTSTRAP_PIVOT:-1}" = 1 ]; then
  # EXIT traps do not fire across exec; clean up the seed temp files first.
  cleanup_bootstrap
  exec "$(dirname "$0")/pivot.sh"
fi
