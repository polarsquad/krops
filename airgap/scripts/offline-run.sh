#!/usr/bin/env bash
# offline-run.sh — autonomous Wi-Fi-off validation of the krops airgap bundle.
#
# This script is designed to run with NO operator and NO LLM/agent attached:
# it waits until the internet is unreachable, runs the full deploy, verifies,
# and writes a PASS/FAIL summary. Everything is logged to $LOG.
#
# Usage (operator):
#   1. an agent (or you) starts this script in the background while online:
#        nohup airgap/scripts/offline-run.sh >/dev/null 2>&1 &
#        nohup airgap/scripts/offline-run.sh /path/to/package.tar.zst >/dev/null 2>&1 &
#   2. toggle Wi-Fi OFF within 6 minutes.
#   3. watch:  tail -f /tmp/airgap-offline-run.log
#   4. when the log shows "OFFLINE RUN COMPLETE", toggle Wi-Fi back ON and
#      tell the agent to collect the results.
# Set SKIP_OFFLINE_CHECK=1 only when the caller enforces network isolation and
# monitors external traffic; by default this script waits to confirm isolation.
set -uo pipefail

LOG=/tmp/airgap-offline-run.log
SUMMARY=/tmp/airgap-offline-summary.txt
AIRGAP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ARCHIVES="$AIRGAP_DIR/archives"
PACKAGE_INPUT="${1:-$ARCHIVES/zarf-package-krops-airgap-arm64-0.1.0.tar.zst}"
PACKAGE_DIR=$(cd "$(dirname "$PACKAGE_INPUT")" && pwd)
PACKAGE="$PACKAGE_DIR/$(basename "$PACKAGE_INPUT")"
if [ ! -f "$PACKAGE" ]; then
  echo "offline-run.sh: package not found: $PACKAGE" >&2
  exit 1
fi

# Resolve the already-installed tools before going offline. This supports both
# the macOS operator workflow and Linux CI without invoking mise in the gap.
ZARF=$(command -v zarf)
FLUX=$(command -v flux)
HELM=$(command -v helm)
KIND=$(command -v kind)
KUBECTL=$(command -v kubectl)
DOCKER=$(command -v docker)
export KUBECONFIG="$HOME/.kube/config"

VERIFY_ARGS=()
if [ -n "${ZARF_VERIFY_KEY:-}" ]; then
  VERIFY_ARGS+=(--key "$ZARF_VERIFY_KEY")
else
  VERIFY_ARGS+=(
    --certificate-identity
    'https://github.com/polarsquad/krops/.github/workflows/air-gapped.yml@refs/heads/main'
    --certificate-oidc-issuer
    'https://token.actions.githubusercontent.com'
  )
fi

export CLUSTER_NAME=airgap-mgmt
export REGISTRY_NAME=krops-registry-airgap
export REGISTRY_PORT=5002
MGMT_CTX="kind-airgap-mgmt"
WL_KCFG=/tmp/airgap-wl.kubeconfig

step() { echo ""; echo "===== [$(date +%H:%M:%S)] $* ====="; }
pass() { echo "PASS: $*" | tee -a "$SUMMARY"; }
fail() { echo "FAIL: $*" | tee -a "$SUMMARY"; }

: > "$SUMMARY"
{
step "0. establish offline isolation"
if [ "${SKIP_OFFLINE_CHECK:-0}" = "1" ]; then
  echo "isolation mode: caller-monitored (offline connectivity check skipped)"
else
  echo "isolation mode: self-checked (waiting for internet to become unreachable)"
  online_deadline=$(( $(date +%s) + ${OFFLINE_WAIT_SECONDS:-900} ))
  while true; do
    if ! curl -s --max-time 3 https://ghcr.io >/dev/null 2>&1 && \
       ! curl -s --max-time 3 https://registry.k8s.io >/dev/null 2>&1; then
      echo "internet unreachable -> OFFLINE confirmed"
      break
    fi
    if [ "$(date +%s)" -ge "$online_deadline" ]; then
      echo "TIMEOUT waiting for Wi-Fi off; aborting."
      fail "never went offline within ${OFFLINE_WAIT_SECONDS:-900}s"
      exit 1
    fi
    sleep 5
  done
fi

step "1. verify package signature, checksums, and embedded SBOMs"
SBOM_OUTPUT=$(mktemp -d "${TMPDIR:-/tmp}/airgap-sbom.XXXXXX")
# zarf verifies the signature and the checksum manifest (sboms.tar included)
# and extracts the SBOM archive, but never parses the documents: a package
# whose SBOM entries are not valid Syft JSON would extract cleanly. Decode
# each document with syft's own decoder (vendored inside the zarf binary)
# and fail unless at least one document was found and all decoded.
sbom_found=0
if "$ZARF" package verify "$PACKAGE" "${VERIFY_ARGS[@]}" && \
   "$ZARF" package inspect sbom "$PACKAGE" \
     --output "$SBOM_OUTPUT" --verify=always "${VERIFY_ARGS[@]}"; then
  while IFS= read -r -d '' doc; do
    sbom_found=1
    "$ZARF" tools sbom convert "$doc" -o syft-json >/dev/null || {
      fail "SBOM document failed to decode as Syft: $doc"
      exit 1
    }
  done < <(find "$SBOM_OUTPUT" -type f -name '*.json' -print0)
else
  fail "package signature, checksums, or SBOM extraction"
  exit 1
fi
if [ "$sbom_found" -eq 0 ]; then
  fail "no SBOM documents extracted to $SBOM_OUTPUT"
  exit 1
fi
pass "package signature, checksums, and embedded SBOMs verified offline ($SBOM_OUTPUT)"

step "2. stage: docker load + kind create + seed registry"
if CLUSTER_NAME=$CLUSTER_NAME REGISTRY_NAME=$REGISTRY_NAME REGISTRY_PORT=$REGISTRY_PORT \
     "$AIRGAP_DIR/scripts/stage-and-create-cluster.sh"; then
  pass "stage (docker load, kind create, registry seed)"
else
  fail "stage-and-create-cluster.sh"
  exit 1
fi

step "3. zarf init"
# The init package filename embeds the zarf CLI version (it is produced by
# `zarf tools download-init` in build-package.sh), so it tracks the mise pin
# in mise.toml. Resolve by glob instead of hardcoding a version that
# Renovate bumps out from under this script.
shopt -s nullglob
init_packages=("$ARCHIVES"/zarf-init-arm64-v*.tar.zst)
shopt -u nullglob
if [ "${#init_packages[@]}" -ne 1 ]; then
  fail "expected exactly one zarf init package matching $ARCHIVES/zarf-init-arm64-v*.tar.zst, found ${#init_packages[@]}"
  exit 1
fi
if ( cd "$AIRGAP_DIR" && "$ZARF" init "${init_packages[0]}" \
       --registry-mode=nodeport --components="" --confirm ); then
  pass "zarf init"
else
  fail "zarf init"
  exit 1
fi

step "4. zarf package deploy"
if ( cd "$AIRGAP_DIR" && "$ZARF" package deploy "$PACKAGE" --confirm ); then
  pass "zarf package deploy"
else
  fail "zarf package deploy"
  exit 1
fi

step "5. verify mgmt substrate (all non-baked images from 127.0.0.1:31999)"
sleep 20
nonzarf=$("$KUBECTL" --context "$MGMT_CTX" get pods -A -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' 2>/dev/null \
  | grep -v "127.0.0.1:31999" \
  | grep -vE "registry.k8s.io/(coredns|kube-|etcd|pause)|docker.io/kindest/" \
  | sort -u)
if [ -z "$nonzarf" ]; then
  pass "all mgmt workload images resolve from the Zarf internal registry"
else
  fail "mgmt images not from Zarf registry: $nonzarf"
fi

step "6. verify config artifact sync (OCIRepository Ready from Zarf registry)"
ociready=$("$KUBECTL" --context "$MGMT_CTX" -n flux-system get ocirepository flux-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
ociurl=$("$KUBECTL" --context "$MGMT_CTX" -n flux-system get ocirepository flux-system -o jsonpath='{.spec.url}' 2>/dev/null)
if [ "$ociready" = "True" ] && echo "$ociurl" | grep -q "zarf-docker-registry"; then
  pass "OCIRepository Ready from internal registry ($ociurl)"
else
  fail "OCIRepository not Ready/internal (ready=$ociready url=$ociurl)"
fi

step "7. verify mgmt Flux kustomizations"
"$KUBECTL" --context "$MGMT_CTX" -n flux-system wait kustomization/flux-system \
  --for=condition=Ready --timeout=10m >/dev/null 2>&1 \
  && pass "flux-system kustomization Ready" || fail "flux-system kustomization"

step "8. verify CAPD workload cluster provisions offline"
wl_ready=$("$KUBECTL" --context "$MGMT_CTX" get clusters.cluster.x-k8s.io airgap-wl -n default \
  -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)
# wait up to 10m for Available
for i in $(seq 1 40); do
  [ "$wl_ready" = "True" ] && break
  sleep 15
  wl_ready=$("$KUBECTL" --context "$MGMT_CTX" get clusters.cluster.x-k8s.io airgap-wl -n default \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)
done
if [ "$wl_ready" = "True" ]; then
  pass "workload cluster airgap-wl Available"
else
  fail "workload cluster airgap-wl not Available"
fi

step "9. verify workload nodes Ready + per-cluster Flux + podinfo"
port=$("$DOCKER" port airgap-wl-lb 6443/tcp 2>/dev/null | head -1 | sed 's/.*://')
if [ -n "$port" ]; then
  "$KUBECTL" --context "$MGMT_CTX" get secret -n default airgap-wl-kubeconfig \
    -o jsonpath='{.data.value}' 2>/dev/null | base64 -d > "$WL_KCFG"
  "$KUBECTL" config set-cluster airgap-wl --server="https://127.0.0.1:${port}" --kubeconfig="$WL_KCFG" >/dev/null 2>&1
  nodes_ready=$("$KUBECTL" --kubeconfig="$WL_KCFG" get nodes --no-headers 2>/dev/null | grep -c " Ready ")
  [ "${nodes_ready:-0}" -ge 2 ] && pass "workload nodes Ready ($nodes_ready)" || fail "workload nodes Ready=$nodes_ready"

  wlf=$("$KUBECTL" --kubeconfig="$WL_KCFG" -n flux-system get ocirepository flux-system \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  # The workload API can become reachable before its Flux source has been
  # created and reconciled. Allow that asynchronous handoff to complete.
  for i in $(seq 1 20); do
    [ "$wlf" = "True" ] && break
    sleep 15
    wlf=$("$KUBECTL" --kubeconfig="$WL_KCFG" -n flux-system get ocirepository flux-system \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  done
  [ "$wlf" = "True" ] && pass "workload Flux sync Ready from krops-registry" || fail "workload Flux sync ready=$wlf"

  podinfo=$("$KUBECTL" --kubeconfig="$WL_KCFG" -n podinfo get pods --no-headers 2>/dev/null | grep -c " Running ")
  for i in $(seq 1 20); do
    [ "${podinfo:-0}" -ge 1 ] && break
    sleep 15
    podinfo=$("$KUBECTL" --kubeconfig="$WL_KCFG" -n podinfo get pods --no-headers 2>/dev/null | grep -c " Running ")
  done
  [ "${podinfo:-0}" -ge 1 ] && pass "podinfo Running on workload cluster" || fail "podinfo not Running"
else
  fail "could not determine airgap-wl LB port"
fi

step "OFFLINE RUN COMPLETE"
if grep -q "^FAIL" "$SUMMARY"; then
  echo "RESULT: FAIL"
  cat "$SUMMARY"
  exit 1
else
  result_suffix=""
  if [ "${SKIP_OFFLINE_CHECK:-0}" = "1" ]; then
    result_suffix=" (connectivity check skipped; caller-monitored)"
  fi
  echo "RESULT: PASS - full airgap deploy verified with no internet${result_suffix}"
  cat "$SUMMARY"
  exit 0
fi
} 2>&1 | tee "$LOG"
