#!/usr/bin/env bash
# toolbox-run.sh – thin wrapper that runs the krops toolbox container.
# See docs/operations.md ("Toolbox container") for what it adds over the
# raw `docker run`/`podman run` invocation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat >&2 <<EOF
Usage: scripts/toolbox-run.sh <bootstrap|pivot|teardown> [extra krops-bootstrap args]

Env:
  TOOLBOX_IMAGE   image reference (default: ${TOOLBOX_IMAGE:-ghcr.io/polarsquad/krops-toolbox:latest};
                  build locally with: docker build -f bootstrap-rs/Dockerfile \\
                    -t krops-toolbox:dev . && TOOLBOX_IMAGE=krops-toolbox:dev)
  KROPS_PROFILE aws | azure | gcp | local-host | local-talos
                  (default: the mise environment in use)
EOF
  exit 2
}

[ $# -ge 1 ] || usage
LIFECYCLE="$1"
shift

# ── .env passthrough with quote stripping ─────────────────────────────────────
# Loaded before engine/socket resolution below; process env wins over .env.
# Never log these values.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    # Only accept valid, non-empty identifiers not starting with a digit;
    # skip garbage lines instead of exporting them.
    case "$key" in
      ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < .env
fi

TOOLBOX_IMAGE="${TOOLBOX_IMAGE:-ghcr.io/polarsquad/krops-toolbox:latest}"

# ── Engine detection (bootstrap.sh parity) ────────────────────────────────────
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

# ── Socket resolution: mount source (host side) vs ENGINE_SOCK (daemon side) ──
case "$CONTAINER_ENGINE" in
  docker)
    SOCK_SOURCE="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}' \
      2>/dev/null | sed 's|^unix://||')"
    [ -S "$SOCK_SOURCE" ] || SOCK_SOURCE=/var/run/docker.sock
    ENGINE_SOCK_IN=/var/run/docker.sock
    ;;
  podman)
    SOCK_SOURCE="$(podman info --format '{{.Host.RemoteSocket.Path}}' \
      2>/dev/null | sed 's|^unix://||')"
    [ -S "$SOCK_SOURCE" ] || SOCK_SOURCE=/run/podman/podman.sock
    # kind's extraMounts hostPath resolves inside the podman VM/rootful
    # namespace, where the socket lives at the standard path. (On Linux
    # rootless the daemon-side path IS the reported one; keep it then.)
    case "$(uname -s):$SOCK_SOURCE" in
      Darwin:*) ENGINE_SOCK_IN=/run/podman/podman.sock ;;
      *)       ENGINE_SOCK_IN="$SOCK_SOURCE" ;;
    esac
    ;;
  *)
    echo "ERROR: Unsupported CONTAINER_ENGINE '${CONTAINER_ENGINE}' (expected docker or podman)" >&2
    exit 1
    ;;
esac

# Repo-local persistent kubeconfig state (gitignored): the toolbox's internal
# kind kubeconfig and the exported management kubeconfig live here.
mkdir -p .kube
KUBECONFIG_IN=/workspace/.kube/kind.yaml

# Repo-local .gcloud/ directory, shared with the host gcp session (docs/gcp.md).
CLOUDSDK_CONFIG_IN=/workspace/.gcloud

# Splits TOOLBOX_ENV_SPEC into mutable/immutable -e flags; see
# docs/operations.md ("Toolbox container").
build_env_args() {
  MUTABLE_ENV_ARGS=()
  IMMUTABLE_ENV_ARGS=()
  local spec key value
  for spec in "$@"; do
    case "$spec" in
      *=*)
        key="${spec%%=*}"
        value="${spec#*=}"
        if [ -n "${!key:-}" ] && [ "${!key}" != "$value" ]; then
          echo "WARNING: $key is set to '${!key}' but toolbox-run.sh always overrides it with '$value'; see docs/operations.md (\"Toolbox container\")." >&2
        fi
        IMMUTABLE_ENV_ARGS+=(-e "$spec")
        ;;
      *)
        MUTABLE_ENV_ARGS+=(-e "$spec")
        ;;
    esac
  done
}

TOOLBOX_ENV_SPEC=(
  CONTAINER_ENGINE
  "ENGINE_SOCK=$ENGINE_SOCK_IN"
  KROPS_PROFILE
  REGISTRY_PORT
  OCI_REPOSITORY
  OCI_TAG
  BOOTSTRAP_PIVOT
  PIVOT_SKIP_DELETE
  GIT_REPO_URL
  GITHUB_TOKEN
  GITHUB_USER
  AGE_KEY_FILE
  AGE_PUBLIC_KEY
  AWS_REGION
  AWS_PROFILE
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_SESSION_TOKEN
  AZURE_SUBSCRIPTION_ID
  AZURE_LOCATION
  AZURE_CONFIG_DIR
  GCP_PROJECT
  GCP_REGION
  "KUBECONFIG=$KUBECONFIG_IN"
  "CLOUDSDK_CONFIG=$CLOUDSDK_CONFIG_IN"
)
build_env_args "${TOOLBOX_ENV_SPEC[@]}"

# Interactive TTY when run from a terminal (pivot/teardown prompts, ctrl-c).
TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then
  TTY_ARGS=(-it)
fi

# The lifecycle command: bootstrap/pivot rerun the CLI (reruns are safe and
# resume; bootstrap's default exit pivots), teardown uses the subcommand.
case "$LIFECYCLE" in
  bootstrap) CLI_ARGS=("$@") ;;
  pivot)     CLI_ARGS=("$@") ;;
  teardown)  CLI_ARGS=(teardown "$@") ;;
  *)         usage ;;
esac

exec "$CONTAINER_ENGINE" run --rm ${TTY_ARGS[@]+"${TTY_ARGS[@]}"} \
  -v "$REPO_ROOT:/workspace" \
  -v "$REPO_ROOT/.kube:/root/.kube" \
  -v "$SOCK_SOURCE:/var/run/docker.sock" \
  -w /workspace \
  "${IMMUTABLE_ENV_ARGS[@]}" \
  "${MUTABLE_ENV_ARGS[@]}" \
  "$TOOLBOX_IMAGE" \
  ${CLI_ARGS[@]+"${CLI_ARGS[@]}"}
