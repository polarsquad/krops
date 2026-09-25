#!/usr/bin/env bash
# bootstrap-common.sh – Shared preflight and Flux-seeding helpers used by
# bootstrap.sh and pivot.sh. Sourced, never executed directly.
#
# Environment variables set by these helpers are plain globals in the
# sourcing shell (bash 3.2 compatible: no namerefs, no associative arrays).

# ── Preflight: environment (aws) ──────────────────────────────────────────────
# Validates the GitHub + age inputs the Flux seed needs on the aws environment.
# Sets: GITHUB_USER, GITHUB_REPO, AGE_KEY_FILE, AGE_PUBKEY.
require_flux_env() {
  : "${GITHUB_TOKEN:?GITHUB_TOKEN must be set (a PAT with read access to the repo)}"
  : "${GIT_REPO_URL:?GIT_REPO_URL must be set}"
  # GITHUB_USER is used in the Flux GitHub secret for repo clone authentication
  GITHUB_USER="${GITHUB_USER:-git}"

  case "$GIT_REPO_URL" in
    https://github.com/*/*) ;;
    *)
      echo "ERROR: GIT_REPO_URL must be an HTTPS GitHub repository URL" >&2
      exit 1
      ;;
  esac
  GITHUB_REPO="${GIT_REPO_URL#https://github.com/}"
  GITHUB_REPO="${GITHUB_REPO%/}"
  GITHUB_REPO="${GITHUB_REPO%.git}"
  GITHUB_AUTH="Authorization: Bearer ${GITHUB_TOKEN}"
  github_branch_path="${GIT_BRANCH//\//%2F}"
  github_branch_status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Accept: application/vnd.github+json' \
    -H "${GITHUB_AUTH}" \
    "https://api.github.com/repos/${GITHUB_REPO}/branches/${github_branch_path}" || true)"
  if [ "$github_branch_status" != 200 ]; then
    echo "ERROR: GitHub repository or branch '${GIT_BRANCH}' is unavailable at '${GIT_REPO_URL}' (HTTP ${github_branch_status})" >&2
    exit 1
  fi

  AGE_KEY_FILE="${AGE_KEY_FILE:-age.agekey}"
  if [ ! -f "$AGE_KEY_FILE" ]; then
    echo "ERROR: age key file not found at '$AGE_KEY_FILE'." >&2
    echo "       Generate one with:  mise run sops-keygen" >&2
    echo "       and add its PUBLIC key to .sops.yaml. See docs/secrets.md." >&2
    exit 1
  fi
  # Validate age key file format first (before attempting to extract the public key).
  # This avoids silent grep failure if the file is malformed.
  AGE_CONTENT=$(cat "${AGE_KEY_FILE}")
  missing_fields=""
  echo "$AGE_CONTENT" | grep -q '^# created:' 2>/dev/null || missing_fields="${missing_fields}# created: header, "
  echo "$AGE_CONTENT" | grep -q '^# public key:' 2>/dev/null || missing_fields="${missing_fields}# public key: comment, "
  echo "$AGE_CONTENT" | grep -q '^AGE-SECRET-KEY-' 2>/dev/null || missing_fields="${missing_fields}AGE-SECRET-KEY- line, "

  if [ -n "$missing_fields" ]; then
    echo "ERROR: '${AGE_KEY_FILE}' is not a valid age key file." >&2
    echo "       Missing: ${missing_fields%, }" >&2
    exit 1
  fi
  # Now safely extract the public key (validation already passed).
  AGE_PUBKEY="${AGE_PUBLIC_KEY:-$(echo "$AGE_CONTENT" | grep '^# public key:' | sed 's/^# public key: //')}"
  if [ -z "$AGE_PUBKEY" ]; then
    echo "ERROR: Cannot determine age public key from '${AGE_KEY_FILE}' or from AGE_PUBLIC_KEY env var." >&2
    echo "       Set AGE_PUBLIC_KEY in .env, or regenerate the key with: mise run sops-keygen" >&2
    exit 1
  fi
}

# ── Preflight: AWS service quotas ─────────────────────────────────────────────
# Checks that the EC2-VPC Elastic IP quota is sufficient in every region the
# default AWS run touches before provisioning begins. Requires aws CLI in PATH.
preflight_aws_quotas() {
  command -v aws >/dev/null 2>&1 \
    || { echo "ERROR: aws CLI not found in PATH (required for EIP quota preflight)" >&2; exit 1; }

  # KEEP IN SYNC with bootstrap.toml [environments.aws]:
  #   - mgmt-cluster = "eu-north-1-management"  (region = name minus "-management")
  #   - every [[environments.aws.teardown.aws-workloads]] region / cluster-name pair
  # and with docs/operations.md.
  # Any additive change to bootstrap.toml -- a new region OR a new workload cluster
  # in an existing region -- changes the required count and requires updating this list.
  # The Rust binary (bootstrap-rs) derives this list dynamically from bootstrap.toml
  # via derive_eip_requirements(); this shell list is hardcoded, so a new region or
  # a new workload cluster in an existing region added to bootstrap.toml will be
  # silently skipped here until this list is updated.
  # eu-north-1: management + workload cluster = 6 EIPs (3 AZs x 2 clusters)
  # eu-west-1:  workload cluster              = 3 EIPs (3 AZs x 1 cluster)
  local REGION_PLAN="eu-north-1:6:eu-north-1-workload,eu-north-1-management eu-west-1:3:eu-west-1-workload"
  local all_ok=1
  local entry

  for entry in $REGION_PLAN; do
    local region="${entry%%:*}"
    local rest="${entry#*:}"
    local required="${rest%%:*}"
    local clusters="${rest#*:}"

    local limit_raw limit allocated owned count cluster foreign available shortfall
    if ! limit_raw=$(aws service-quotas get-service-quota \
      --service-code ec2 \
      --quota-code L-0263D0A3 \
      --region "$region" \
      --query 'Quota.Value' \
      --output text); then
      echo "ERROR: Failed to query EC2 EIP quota in ${region}" >&2
      echo "       Check AWS credentials and service-quotas:GetServiceQuota permission" >&2
      exit 1
    fi
    # Validate that the quota value is numeric before rounding
    case "${limit_raw:-}" in
      ''|*[!0-9.]*)
        echo "ERROR: Unexpected non-numeric quota value '${limit_raw}' from EC2 in ${region}" >&2
        exit 1 ;;
    esac
    # Quota values come back as floats (e.g. "5.0"); round to an integer.
    limit=$(printf '%.0f' "${limit_raw:-0}" 2>/dev/null) || limit=0

    if ! allocated=$(aws ec2 describe-addresses \
      --region "$region" \
      --filters "Name=domain,Values=vpc" \
      --query 'length(Addresses)' \
      --output text); then
      echo "ERROR: Failed to list EC2 EIPs in ${region}" >&2
      echo "       Check AWS credentials and ec2:DescribeAddresses permission" >&2
      exit 1
    fi
    # Validate that the allocation count is numeric before rounding
    case "${allocated:-}" in
      ''|*[!0-9.]*)
        echo "ERROR: Unexpected non-numeric EIP allocation count '${allocated}' from EC2 in ${region}" >&2
        exit 1 ;;
    esac
    # length() returns an integer; printf is defensive against unexpected decimal output.
    allocated=$(printf '%.0f' "${allocated:-0}" 2>/dev/null) || allocated=0

    owned=0
    for cluster in ${clusters//,/ }; do
      if ! count=$(aws ec2 describe-addresses \
        --region "$region" \
        --filters "Name=tag:sigs.k8s.io/cluster-api-provider-aws/cluster/${cluster},Values=owned" \
        --query 'length(Addresses)' \
        --output text); then
        echo "ERROR: Failed to list EC2 EIPs owned by ${cluster} in ${region}" >&2
        echo "       Check AWS credentials and ec2:DescribeAddresses permission" >&2
        exit 1
      fi
      # Validate that the owned count is numeric before rounding
      case "${count:-}" in
        ''|*[!0-9.]*)
          echo "ERROR: Unexpected non-numeric EIP owned count '${count}' for ${cluster} in ${region}" >&2
          exit 1 ;;
      esac
      count=$(printf '%.0f' "${count:-0}" 2>/dev/null) || count=0
      owned=$(( owned + count ))
    done

    foreign=$(( allocated > owned ? allocated - owned : 0 ))
    available=$(( limit > foreign ? limit - foreign : 0 ))

    if [ "$available" -lt "$required" ]; then
      shortfall=$(( required - available ))
      echo "ERROR: Insufficient EC2 Elastic IP quota in ${region}: ${available} available to krops, ${required} needed (${shortfall} short; limit ${limit}, ${allocated} allocated, ${owned} owned by krops)" >&2
      echo "       Request an increase:" >&2
      printf "         aws service-quotas request-service-quota-increase \\\\\n" >&2
      printf "           --service-code ec2 --quota-code L-0263D0A3 \\\\\n" >&2
      printf "           --desired-value %d --region %s\n" "$(( foreign + required ))" "$region" >&2
      all_ok=0
    else
      echo ">>> EIP quota in ${region}: ${available} available to krops (need ${required}, ${owned} already owned) - OK"
    fi
  done

  [ "$all_ok" -eq 1 ] || exit 1
}

# ── Preflight: container engine ───────────────────────────────────────────────
# Detects and selects a running container engine. Sets: CONTAINER_ENGINE,
# ENGINE_SOCK (and exports KIND_EXPERIMENTAL_PROVIDER for podman).
# Note: when using podman via the docker CLI shim (e.g., on macOS),
# `docker --version` reports podman; we check for that case first.
detect_container_engine() {
  if [ -z "${CONTAINER_ENGINE:-}" ]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
      if docker --version 2>/dev/null | grep -qi podman; then
        CONTAINER_ENGINE=podman
      else
        CONTAINER_ENGINE=docker
      fi
    elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
      CONTAINER_ENGINE=podman
    else
      echo "ERROR: No running container engine found (tried docker and podman)" >&2
      exit 1
    fi
  fi

  case "$CONTAINER_ENGINE" in
    docker)
      docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running" >&2; exit 1; }
      ENGINE_SOCK="/var/run/docker.sock"
      ;;
    podman)
      podman info >/dev/null 2>&1 || { echo "ERROR: Podman is not running (is 'podman machine' started?)" >&2; exit 1; }
      export KIND_EXPERIMENTAL_PROVIDER=podman
      ENGINE_SOCK="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null || true)"
      ENGINE_SOCK="${ENGINE_SOCK#unix://}"
      if [ -z "$ENGINE_SOCK" ]; then
        ENGINE_SOCK="/run/podman/podman.sock"
        echo ">>> WARNING: Could not detect the podman API socket path; assuming ${ENGINE_SOCK}" >&2
      fi
      ;;
    *)
      echo "ERROR: Unsupported CONTAINER_ENGINE '${CONTAINER_ENGINE}' (expected 'docker' or 'podman')" >&2
      exit 1
      ;;
  esac
}

# ── Flux seeding ──────────────────────────────────────────────────────────────
# Installs the Flux Operator, the bootstrap secrets (aws), and the FluxInstance
# that syncs the management tree, then waits for the instance to become Ready.
# Used by bootstrap.sh (against the kind cluster) and by pivot.sh (against the
# management cluster). Reads the PROFILE global ("aws" or "local-host").

# Optional target: set to a kubeconfig path to seed a cluster other than the
# current kubectl context. Empty means "current context".
SEED_KUBECONFIG=""
# Temp files created by seed_flux, removed by seed_cleanup (callers wire this
# into their EXIT traps; bootstrap.sh exec'ing pivot.sh relies on pivot.sh's).
SEED_CLEANUP_FILES=""

seed_register_cleanup() {
  SEED_CLEANUP_FILES="${SEED_CLEANUP_FILES}${1} "
}

seed_cleanup() {
  # shellcheck disable=SC2086  # intentional word splitting over registered files
  for f in $SEED_CLEANUP_FILES; do
    rm -f "$f"
  done
  SEED_CLEANUP_FILES=""
}

seed_kubectl() {
  if [ -n "$SEED_KUBECONFIG" ]; then
    kubectl --kubeconfig "$SEED_KUBECONFIG" "$@"
  else
    kubectl "$@"
  fi
}

seed_helm() {
  if [ -n "$SEED_KUBECONFIG" ]; then
    helm --kubeconfig "$SEED_KUBECONFIG" "$@"
  else
    helm "$@"
  fi
}

# seed_flux [kubeconfig]
# Requires (aws only): GITHUB_TOKEN, GIT_REPO_URL, AGE_KEY_FILE, AGE_PUBKEY
# (run require_flux_env first). Requires the PROFILE global.
seed_flux() {
  SEED_KUBECONFIG="${1:-}"

  local registry_name="${REGISTRY_NAME:-krops-registry}"
  local registry_port="${REGISTRY_PORT:-5001}"
  local anon_registry_config
  anon_registry_config="$(mktemp)"
  seed_register_cleanup "$anon_registry_config"
  printf '{}\n' > "$anon_registry_config"

  # ── Install the Flux Operator ───────────────────────────────────────────────
  echo ">>> Installing Flux Operator..."
  # The empty registry config keeps helm from picking up local container
  # registry credentials when pulling the public OCI chart below.
  seed_helm install flux-operator oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator \
    --namespace flux-system \
    --create-namespace \
    --wait \
    --registry-config "$anon_registry_config"

  if [ "$PROFILE" = aws ]; then
    # ── GitHub PAT credentials secret ─────────────────────────────────────────
    # Basic-auth secret consumed by Flux's source-controller to clone the repo.
    # Idempotent (apply) so pivot re-runs do not fail on existing secrets.
    echo ">>> Creating GitHub PAT credentials secret in flux-system..."
    seed_kubectl create secret generic flux-github-pat \
      --namespace flux-system \
      --from-literal=username="${GITHUB_USER}" \
      --from-literal=password="${GITHUB_TOKEN}" \
      --dry-run=client -o yaml | seed_kubectl apply -f -

    # ── SOPS age decryption key secret ────────────────────────────────────────
    # Flux's kustomize-controller uses this key to decrypt *.sops.yaml manifests
    # (such as the CAPA AWS credentials) during reconciliation. Flux scans the
    # Secret for keys matching the pattern `keys.<public-key>.agekey` — each
    # matching key is passed to the age library for decryption.
    # Delete first: kubectl apply merges stringData keys, which would leave
    # stale keys from a previous bootstrap with a different age key behind.
    echo ">>> Creating sops-age decryption secret in flux-system..."
    seed_kubectl delete secret sops-age -n flux-system --ignore-not-found
    seed_kubectl create secret generic sops-age \
      --namespace flux-system \
      --from-file="keys.${AGE_PUBKEY}.agekey=${AGE_KEY_FILE}" \
      --dry-run=client -o yaml | seed_kubectl apply -f -
  fi

  # ── Install the FluxInstance via Helm ───────────────────────────────────────
  echo ">>> Installing FluxInstance via Helm..."
  FLUX_INSTANCE_ARGS=(
    --set instance.cluster.type=kubernetes
    --set instance.cluster.size=small
    --set instance.cluster.multitenant=false
    --set instance.cluster.networkPolicy=true
    --set instance.cluster.domain=cluster.local
    --registry-config "$anon_registry_config"
  )
  if [ "$PROFILE" = aws ]; then
    FLUX_INSTANCE_ARGS+=(
      --set instance.sync.kind=GitRepository
      --set instance.sync.url="${GIT_REPO_URL}"
      --set instance.sync.ref=refs/heads/main
      --set instance.sync.path=mgmt/aws
      --set instance.sync.pullSecret=flux-github-pat
    )
  else
    FLUX_INSTANCE_ARGS+=(
      --set instance.sync.kind=OCIRepository
      --set instance.sync.url="oci://${registry_name}:5000/${OCI_REPOSITORY:-krops}"
      --set instance.sync.ref="${OCI_TAG:-latest}"
      --set instance.sync.path=mgmt/local-host
      --set-json 'instance.kustomize.patches=[{"patch":"- op: add\n  path: /spec/insecure\n  value: true","target":{"kind":"OCIRepository"}}]'
    )
  fi
  # Helm 4's watcher strategy treats the FluxInstance's transient InProgress
  # condition as a terminal failure. Use the legacy chart-resource wait here,
  # then wait explicitly for the operator-owned Ready condition below.
  seed_helm upgrade --install flux \
    oci://ghcr.io/controlplaneio-fluxcd/charts/flux-instance \
    --namespace flux-system \
    --wait=legacy \
    --timeout 10m \
    "${FLUX_INSTANCE_ARGS[@]}"

  echo ">>> Waiting for FluxInstance reconciliation to complete..."
  seed_kubectl wait fluxinstance/flux \
    --namespace flux-system \
    --for=condition=Ready \
    --timeout=10m

  # ── Health check ────────────────────────────────────────────────────────────
  # Verify the Flux controllers are running before declaring success.
  echo ">>> Waiting for Flux controllers to be ready..."
  seed_kubectl wait --namespace flux-system --for=condition=ready pod \
    --selector='app.kubernetes.io/part-of=flux' \
    --timeout=90s || true
}
