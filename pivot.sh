#!/usr/bin/env bash
# pivot.sh – Move the CAPI management inventory from the local kind bootstrap
# cluster into the self-managed management cluster (issue #79).
#
# The management cluster definition (mgmt/aws/clusters/eu-north-1/management
# or mgmt/local-host/clusters/management) is created by the providers running
# in the kind bootstrap cluster; this script then:
#
#   1. waits for the management cluster to be provisioned,
#   2. exports its kubeconfig (rewriting the endpoint to localhost for CAPD),
#   3. installs the CAPI operator + provider CRs in the target (imperatively,
#      at the same versions as Git, because HelmReleases need Flux first),
#   4. suspends Flux in kind and runs `clusterctl move`,
#   5. unpauses the moved Clusters and seeds Flux on the target,
#   6. deletes the kind bootstrap cluster.
#
# Run from a checkout of the revision you want self-managed (normally main):
#   mise run pivot                  # aws environment (KROPS_PROFILE=aws)
#   mise -E local-host run pivot    # local-host environment
#
# bootstrap.sh execs this script by default (BOOTSTRAP_PIVOT=0 opts out).
# The move is re-runnable: objects are deleted from the source only after
# they were created successfully on the target, so kind stays authoritative
# until step 6. If the move fails midway, NEVER delete moved Cluster /
# AWSManaged* / MachinePool / Dev* objects on the target to "retry clean" —
# the provider would deprovision the real infrastructure. Re-run the move,
# or delete only pure-config duplicates (e.g. an identity). See
# docs/operations.md "Pivot recovery".
set -euo pipefail

cd "$(dirname "$0")"

source ./bootstrap-common.sh

PROFILE="${KROPS_PROFILE:-${1:-aws}}"
GIT_BRANCH="${GIT_BRANCH:-main}"
REGISTRY_NAME="${REGISTRY_NAME:-krops-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5001}"
MGMT_NS="default"
MGMT_KUBECONFIG="${MGMT_KUBECONFIG:-$HOME/.kube/krops-mgmt.yaml}"
PIVOT_SKIP_DELETE="${PIVOT_SKIP_DELETE:-0}"

# Chart versions installed imperatively in the target before Flux exists.
# Chart versions installed imperatively before Flux exists; keep in sync with
# the HelmReleases in mgmt/<env>/infrastructure/ (cert-manager, capi-operator)
# so Flux adopts these installs without drift. Renovate updates these pins
# together with the manifest chart versions (platform-charts group).
CERT_MANAGER_VERSION="1.21.1"
CAPI_OPERATOR_VERSION="0.28.0"

case "$PROFILE" in
  aws)
    MGMT_CLUSTER="eu-north-1-management"
    MGMT_READY_TIMEOUT="${MGMT_READY_TIMEOUT:-40m}"
    ;;
  local-host)
    MGMT_CLUSTER="local-management"
    MGMT_READY_TIMEOUT="${MGMT_READY_TIMEOUT:-15m}"
    ;;
  *)
    echo "ERROR: unsupported environment '$PROFILE' (expected 'local-host' or 'aws')" >&2
    exit 1
    ;;
esac

# ── Phase 0: preflight ────────────────────────────────────────────────────────
pivot_preflight() {
  for cmd in clusterctl helm kubectl kind mise; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found in PATH" >&2; exit 1; }
  done

  local expected_context="${BOOTSTRAP_KUBECONTEXT:-kind-mgmt}"
  local current_context
  current_context="$(kubectl config current-context 2>/dev/null || true)"
  if [ "$current_context" != "$expected_context" ]; then
    echo "ERROR: current kubectl context is '${current_context}', expected '${expected_context}'." >&2
    echo "       The kind bootstrap cluster must be the move SOURCE." >&2
    echo "       Fix with: kubectl config use-context ${expected_context}" >&2
    exit 1
  fi

  if ! kubectl get cluster "$MGMT_CLUSTER" -n "$MGMT_NS" >/dev/null 2>&1; then
    echo "ERROR: Cluster '$MGMT_CLUSTER' not found in the bootstrap cluster." >&2
    echo "       The management cluster definition must be reconciled first:" >&2
    echo "       aws:        merged to main, Flux-in-kind creates it (~15-25 min)" >&2
    echo "       local-host: mise -E local-host run oci-push, then wait ~2 min" >&2
    exit 1
  fi

  if [ "$PROFILE" = aws ]; then
    require_flux_env
  else
    detect_container_engine
    if ! curl --fail --silent --show-error \
      "http://localhost:${REGISTRY_PORT}/v2/" >/dev/null 2>&1; then
      echo "ERROR: local registry is unavailable at localhost:${REGISTRY_PORT}" >&2
      echo "       The management cluster's Flux syncs from it; it must stay running." >&2
      exit 1
    fi
  fi
}

# ── Phase 1: wait for the management cluster ──────────────────────────────────
# CAPI Clusters do not expose a uniform Ready condition across providers, so
# readiness = the kubeconfig secret exists (control plane has an endpoint).
# Node readiness is verified in Phase 2 against the exported kubeconfig.
wait_for_management_cluster() {
  echo ">>> Waiting for the management cluster kubeconfig (timeout: ${MGMT_READY_TIMEOUT})..."
  # Readiness = the kubeconfig secret exists (the control plane has an
  # endpoint). Poll rather than kubectl-wait so a not-yet-created secret is
  # waited on instead of erroring immediately.
  local timeout_s attempts=0
  case "$MGMT_READY_TIMEOUT" in
    *h) timeout_s=$(( $(echo "$MGMT_READY_TIMEOUT" | tr -dc '0-9') * 3600 )) ;;
    *m) timeout_s=$(( $(echo "$MGMT_READY_TIMEOUT" | tr -dc '0-9') * 60 )) ;;
    *s) timeout_s="$(echo "$MGMT_READY_TIMEOUT" | tr -dc '0-9')" ;;
    *)  timeout_s="$MGMT_READY_TIMEOUT" ;;
  esac
  local max_attempts=$(( timeout_s / MGMT_POLL_INTERVAL ))
  until kubectl get "secret/${MGMT_CLUSTER}-kubeconfig" -n "$MGMT_NS" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge "$max_attempts" ]; then
      echo "ERROR: management cluster kubeconfig not available within ${MGMT_READY_TIMEOUT}" >&2
      kubectl describe cluster "$MGMT_CLUSTER" -n "$MGMT_NS" || true
      exit 1
    fi
    sleep "$MGMT_POLL_INTERVAL"
  done
  clusterctl describe cluster "$MGMT_CLUSTER" -n "$MGMT_NS" || true
}

# ── Phase 2: export the target kubeconfig ─────────────────────────────────────
export_mgmt_kubeconfig() {
  echo ">>> Exporting management-cluster kubeconfig to ${MGMT_KUBECONFIG}..."
  mkdir -p "$(dirname "$MGMT_KUBECONFIG")"
  clusterctl get kubeconfig "$MGMT_CLUSTER" -n "$MGMT_NS" > "$MGMT_KUBECONFIG"
  chmod 600 "$MGMT_KUBECONFIG"

  if [ "$PROFILE" = local-host ]; then
    # CAPD records the load balancer's container-network IP, which is not
    # routable from macOS. Point the kubeconfig at the port published on
    # localhost instead (same rewrite as the workload kubeconfigs).
    local endpoint port
    endpoint="$($CONTAINER_ENGINE port "${MGMT_CLUSTER}-lb" 6443/tcp | head -1)"
    port="${endpoint##*:}"
    case "$port" in
      ''|*[!0-9]*)
        echo "ERROR: cannot determine the ${MGMT_CLUSTER} API server port" >&2
        exit 1
        ;;
    esac
    kubectl config set-cluster "$MGMT_CLUSTER" \
      --server="https://127.0.0.1:${port}" \
      --kubeconfig="$MGMT_KUBECONFIG" >/dev/null
  fi

  kubectl --kubeconfig "$MGMT_KUBECONFIG" config rename-context \
    "$(kubectl --kubeconfig "$MGMT_KUBECONFIG" config current-context)" \
    krops-mgmt >/dev/null

  echo ">>> Waiting for management-cluster nodes to be ready..."
  kubectl --kubeconfig "$MGMT_KUBECONFIG" wait --for=condition=Ready node --all --timeout=15m
}

# ── Phase 3: install CAPI in the target ───────────────────────────────────────
# Imperative but Git-identical: the target has no Flux yet, so the HelmReleases
# cannot be reconciled. After Phase 5 Flux adopts these releases and provider
# CRs without drift (same charts, same versions, same CRs).
install_capi_in_target() {
  echo ">>> Installing cert-manager ${CERT_MANAGER_VERSION} in the target..."
  local anon_registry_config
  anon_registry_config="$(mktemp)"
  seed_register_cleanup "$anon_registry_config"
  printf '{}\n' > "$anon_registry_config"
  # Values mirror mgmt/<env>/infrastructure/cert-manager/helmrelease.yaml
  helm install cert-manager cert-manager \
    --repo https://charts.jetstack.io \
    --version "$CERT_MANAGER_VERSION" \
    --namespace cert-manager --create-namespace --wait \
    --set crds.enabled=true \
    --registry-config "$anon_registry_config" \
    --kubeconfig "$MGMT_KUBECONFIG"

  echo ">>> Installing CAPI operator ${CAPI_OPERATOR_VERSION} in the target..."
  # Values mirror mgmt/<env>/infrastructure/capi-operator/helmrelease.yaml
  helm install capi-operator cluster-api-operator \
    --repo https://kubernetes-sigs.github.io/cluster-api-operator \
    --version "$CAPI_OPERATOR_VERSION" \
    --namespace capi-operator-system --create-namespace --wait \
    --set cert-manager.enabled=false \
    --registry-config "$anon_registry_config" \
    --kubeconfig "$MGMT_KUBECONFIG"

  echo ">>> Applying provider CRs in the target..."
  # Apply the plain manifests verbatim (NOT the kustomization dirs: capa-system
  # contains the SOPS-encrypted aws-credentials which only Flux can decrypt).
  local infra_ns infra_name
  if [ "$PROFILE" = aws ]; then
    infra_ns="capa-system"; infra_name="aws"
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/capi-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/capi-system/providers.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/capa-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/capa-system/providers.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/caaph-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/capi-providers/caaph-system/addon-provider.yaml

    # CAPA credentials: the InfrastructureProvider above references the
    # aws-credentials secret (configSecret.name). On the bootstrap cluster
    # Flux decrypts aws-credentials.sops.yaml; here it is created directly
    # with the same shape (stringData.AWS_B64ENCODED_CREDENTIALS).
    # Do NOT pre-apply mgmt/aws/infrastructure/aws-identity/: the
    # AWSClusterControllerIdentity carries a move hook and comes over with
    # the Phase 4 move.
    echo ">>> Creating CAPA credentials secret in capa-system..."
    local aws_b64_credentials
    aws_b64_credentials="$(mise -E aws run aws-credentials)"
    kubectl --kubeconfig "$MGMT_KUBECONFIG" create secret generic aws-credentials \
      --namespace capa-system \
      --from-literal="AWS_B64ENCODED_CREDENTIALS=${aws_b64_credentials}" \
      --dry-run=client -o yaml | kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f -
  else
    infra_ns="capd-system"; infra_name="docker"
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/capi-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/capi-system/providers.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/capd-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/capd-system/provider.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/caaph-system/namespace.yaml
    kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/local-host/capi-providers/caaph-system/provider.yaml
  fi

  echo ">>> Waiting for providers in the target..."
  kubectl --kubeconfig "$MGMT_KUBECONFIG" wait --for=condition=Ready \
    coreprovider/cluster-api -n capi-system --timeout=15m
  kubectl --kubeconfig "$MGMT_KUBECONFIG" wait --for=condition=Ready \
    bootstrapprovider/kubeadm controlplaneprovider/kubeadm -n capi-system --timeout=15m
  kubectl --kubeconfig "$MGMT_KUBECONFIG" wait --for=condition=Ready \
    "infrastructureprovider/${infra_name}" -n "$infra_ns" --timeout=15m
  kubectl --kubeconfig "$MGMT_KUBECONFIG" wait --for=condition=Ready \
    addonprovider/helm -n caaph-system --timeout=15m

  # `clusterctl move` requires every source provider to exist in the target
  # at >= its source version. Same files + same catalog = same versions; the
  # listings below make the comparison visible in the pivot log.
  echo ">>> Source providers:"
  kubectl get coreproviders,bootstrapproviders,controlplaneproviders,infrastructureproviders,addonproviders -A || true
  echo ">>> Target providers:"
  kubectl --kubeconfig "$MGMT_KUBECONFIG" get \
    coreproviders,bootstrapproviders,controlplaneproviders,infrastructureproviders,addonproviders -A || true
}

# ── Phase 4: suspend Flux in kind, then move ──────────────────────────────────
suspend_and_move() {
  # clusterctl move pauses Clusters on the source and deletes the moved
  # objects after creating them on the target. Flux-in-kind must not
  # reconcile mid-move (Git carries no spec.paused, so it would unpause the
  # Clusters and recreate deleted objects). kind is abandoned after the
  # pivot, so the suspension is never lifted there.
  echo ">>> Suspending Flux Kustomizations in the bootstrap cluster..."
  local ks
  for ks in $(kubectl get kustomizations -n flux-system -o name); do
    kubectl patch "$ks" -n flux-system --type merge -p '{"spec":{"suspend":true}}'
  done

  echo ">>> Moving the CAPI inventory to the management cluster..."
  if ! clusterctl move --to-kubeconfig "$MGMT_KUBECONFIG" -n "$MGMT_NS"; then
    echo "" >&2
    echo "ERROR: clusterctl move failed." >&2
    echo "       The move is re-runnable: objects are deleted from the source only" >&2
    echo "       after they were created on the target, so kind stays authoritative." >&2
    echo "       NEVER delete moved Cluster / AWSManaged* / MachinePool / Dev* objects" >&2
    echo "       on the target to work around a failure — the provider would" >&2
    echo "       deprovision the real infrastructure. See docs/operations.md" >&2
    echo "       'Pivot recovery'." >&2
    exit 1
  fi

  # Moved Clusters are created on the target with spec.paused=true (the move
  # pauses them); Git carries no paused field, so Flux would never clear it.
  # Flux is not seeded on the target yet, so unpausing here is race-free.
  echo ">>> Unpausing moved Clusters on the target..."
  local cluster
  for cluster in $(kubectl --kubeconfig "$MGMT_KUBECONFIG" get clusters -n "$MGMT_NS" -o name); do
    kubectl --kubeconfig "$MGMT_KUBECONFIG" patch "$cluster" -n "$MGMT_NS" \
      --type merge -p '{"spec":{"paused":false}}' || true
  done

  echo ">>> Clusters on the management cluster after the move:"
  kubectl --kubeconfig "$MGMT_KUBECONFIG" get clusters -n "$MGMT_NS"

  if [ "$PROFILE" = aws ]; then
    # The AWSClusterControllerIdentity carries a clusterctl move hook and
    # comes over with the inventory; verify, and fall back to the Git
    # manifest only if it is missing.
    if ! kubectl --kubeconfig "$MGMT_KUBECONFIG" get \
        awsclustercontrolleridentities.infrastructure.cluster.x-k8s.io default \
        -n "$MGMT_NS" >/dev/null 2>&1; then
      echo ">>> AWSClusterControllerIdentity missing after the move; applying from Git..."
      kubectl --kubeconfig "$MGMT_KUBECONFIG" apply -f mgmt/aws/infrastructure/aws-identity/identity.yaml
    fi
  fi
}

# ── Phase 5: seed Flux on the target ──────────────────────────────────────────
seed_target() {
  # aws:       syncs mgmt/aws from GitHub main.
  # local-host: syncs mgmt/local-host from the local OCI registry; the
  #            artifact must contain the management manifests.
  if [ "$PROFILE" = local-host ]; then
    echo ">>> Publishing the current checkout as the OCI artifact..."
    mise -E local-host run oci-push
  fi

  seed_flux "$MGMT_KUBECONFIG"

  echo ">>> Kustomizations on the management cluster:"
  kubectl --kubeconfig "$MGMT_KUBECONFIG" get kustomizations -n flux-system
}

# ── Phase 6: delete the bootstrap cluster ─────────────────────────────────────
delete_bootstrap_cluster() {
  if [ "$PIVOT_SKIP_DELETE" = 1 ]; then
    echo ">>> PIVOT_SKIP_DELETE=1: keeping the kind bootstrap cluster for inspection"
    return
  fi

  # Guard: only delete kind once the management cluster demonstrably owns
  # everything (all clusters present; local-host: registry still serving).
  local clusters
  clusters="$(kubectl --kubeconfig "$MGMT_KUBECONFIG" get clusters -n "$MGMT_NS" -o name)"
  if [ -z "$clusters" ]; then
    echo "ERROR: no Clusters on the management cluster; refusing to delete kind." >&2
    exit 1
  fi
  if [ "$PROFILE" = local-host ]; then
    if ! curl --fail --silent --show-error \
      "http://localhost:${REGISTRY_PORT}/v2/" >/dev/null 2>&1; then
      echo "ERROR: local registry unavailable; the management cluster's Flux depends on it." >&2
      echo "       Refusing to delete kind until it is serving." >&2
      exit 1
    fi
  fi

  echo ">>> Deleting the kind bootstrap cluster..."
  kind delete cluster --name mgmt

  echo ""
  echo ">>> Pivot complete: the management cluster is self-managed."
  echo ">>> Management kubeconfig: ${MGMT_KUBECONFIG}"
  echo ">>> Use with: KUBECONFIG=${MGMT_KUBECONFIG} kubectl get clusters"
  if kubectl config use-context krops-mgmt >/dev/null 2>&1; then
    echo ">>> kubectl context switched to krops-mgmt"
  else
    echo ">>> To use it by default: export KUBECONFIG=${MGMT_KUBECONFIG}"
  fi
}

MGMT_POLL_INTERVAL="${MGMT_POLL_INTERVAL:-10}"

trap seed_cleanup EXIT

pivot_preflight
wait_for_management_cluster
export_mgmt_kubeconfig
install_capi_in_target
suspend_and_move
seed_target
delete_bootstrap_cluster
