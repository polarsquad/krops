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
  TOOLBOX_IMAGE   image reference (default: ghcr.io/polarsquad/krops-toolbox:latest;
                  build locally with: docker build -f bootstrap-rs/Dockerfile \\
                    -t krops-toolbox:dev . && TOOLBOX_IMAGE=krops-toolbox:dev)
                  Remote-registry references are pulled (refreshed) before
                  every run; a failed pull warns and falls back to the local
                  cache. Local builds (bare name[:tag]) are used as-is.
  KROPS_PROFILE aws | azure | gcp | local-host | local-talos
                  (default: the mise environment in use)
EOF
  exit 2
}

[ $# -ge 1 ] || usage
LIFECYCLE="$1"
shift

# ── .env passthrough, parsed like mise's env_file ─────────────────────────────
# Loaded before engine/socket resolution below; .env wins over the process
# environment, as it does under mise. Never log these values.
ltrim() { printf '%s' "${1#"${1%%[![:space:]]*}"}"; }
rtrim() { printf '%s' "${1%"${1##*[![:space:]]}"}"; }
# Parse one physical line without shell evaluation. The result stays in a
# variable (not command substitution), preserving escaped trailing newlines.
parse_dotenv_value() {
  local input="$1" state=unquoted c next name replacement pending="" i=0 n=${#1}
  DOTENV_VALUE=""
  while [ "$i" -lt "$n" ]; do
    c="${input:i:1}"
    i=$((i + 1))
    # Only unquoted, unescaped whitespace is syntactic. Keep it pending
    # until another value character arrives; discard it at end or a comment.
    if [ "$state" = unquoted ]; then
      if [[ "$c" = [[:space:]] ]]; then
        pending+="$c"
        continue
      fi
      if [ "$c" = '#' ] && { [ -n "$pending" ] || [ "$i" -eq 1 ] || [[ "${input:i-2:1}" = [[:space:]] ]]; }; then
        break
      fi
      DOTENV_VALUE+="$pending"
      pending=""
    fi
    case "$c" in
      "\\")
        # mise changed single-quote escape semantics in 2026.10.8.
        # Reject that unsupported form instead of changing its value silently.
        [ "$state" != single ] || return 1
        if [ "$i" -lt "$n" ]; then
          next="${input:i:1}"
          case "$next" in
            n) DOTENV_VALUE+=$'\n' ;;
            r) DOTENV_VALUE+=$'\r' ;;
            t) DOTENV_VALUE+=$'\t' ;;
            "\\"|\"|\'|'$'|' ') DOTENV_VALUE+="$next" ;;
            *) DOTENV_VALUE+="\\$next" ;;
          esac
          i=$((i + 1))
        else
          DOTENV_VALUE+="\\"
        fi
        ;;
      \")
        case "$state" in
          unquoted) state=double ;;
          double) state=unquoted ;;
          single) DOTENV_VALUE+="$c" ;;
        esac
        ;;
      \')
        case "$state" in
          unquoted) state=single ;;
          single) state=unquoted ;;
          double) DOTENV_VALUE+="$c" ;;
        esac
        ;;
      '$')
        if [ "$state" = single ]; then
          DOTENV_VALUE+="$c"
          continue
        fi
        name=""
        if [ "${input:i:1}" = '{' ]; then
          i=$((i + 1))
          while [ "$i" -lt "$n" ] && [ "${input:i:1}" != '}' ]; do
            name+="${input:i:1}"
            i=$((i + 1))
          done
          [ "$i" -lt "$n" ] || return 1
          i=$((i + 1))
        else
          while [ "$i" -lt "$n" ]; do
            case "${input:i:1}" in
              [A-Za-z0-9_]) name+="${input:i:1}"; i=$((i + 1)) ;;
              *) break ;;
            esac
          done
        fi
        case "$name" in
          '') DOTENV_VALUE+='$' ;;
          [0-9]*|*[!A-Za-z0-9_]*) return 1 ;;
          *) replacement="${!name:-}"; DOTENV_VALUE+="$replacement" ;;
        esac
        ;;
      *) DOTENV_VALUE+="$c" ;;
    esac
  done
  [ "$state" = unquoted ]
}
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(ltrim "$line")"
    case "$line" in
      ''|'#'*) continue ;;
      export[[:space:]]*) line="$(ltrim "${line#export}")" ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="$(rtrim "${line%%=*}")"
    case "$key" in
      ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
    esac
    value="$(ltrim "${line#*=}")"
    if parse_dotenv_value "$value"; then
      export "$key=$DOTENV_VALUE"
    else
      printf 'WARNING: Unsupported or malformed dotenv value for %s; skipped\n' "$key" >&2
    fi
  done < .env
fi

# Resolve checkout metadata on the host: linked worktree .git pointers refer
# outside /workspace and cannot be followed inside the toolbox. Refresh after
# .env loading so stale OCI metadata cannot describe a different checkout.
# Failure is nonfatal for lifecycle operations that do not publish OCI artifacts.
unset KROPS_OCI_GIT_SHA KROPS_OCI_GIT_REF KROPS_OCI_SOURCE_URL
if KROPS_OCI_GIT_SHA=$(git rev-parse HEAD 2>/dev/null); then
  KROPS_OCI_GIT_REF=$(git branch --show-current 2>/dev/null) || KROPS_OCI_GIT_REF=""
  KROPS_OCI_GIT_REF="${KROPS_OCI_GIT_REF:-detached}"
  KROPS_OCI_SOURCE_URL=$(git config --get remote.origin.url 2>/dev/null) || KROPS_OCI_SOURCE_URL=""
  KROPS_OCI_SOURCE_URL="${KROPS_OCI_SOURCE_URL:-file://${REPO_ROOT}}"
  export KROPS_OCI_GIT_SHA KROPS_OCI_GIT_REF KROPS_OCI_SOURCE_URL
else
  unset KROPS_OCI_GIT_SHA
fi

# Share the Azure login cache used by subscription preparation. Resolve after
# .env so the mount and the forwarded variable agree with mise precedence.
export AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR:-/root/.azure}"
mkdir -p "$HOME/.krops-azure"

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

# ── Image refresh: revalidate remote-registry tags (#645) ────────────────────
# docker/podman run treat a locally-present mutable tag (e.g. :latest) as
# valid and never revalidate it against the registry, so a fresh release
# never reaches a host that already pulled it. Pull remote-registry refs
# explicitly, best-effort: a failed pull must not abort a lifecycle
# operation that has a usable local cache (offline host, registry outage),
# so it only warns. Local builds have no registry source and are used as-is.
# Host rule: the first "/"-component of the ref is a registry host when it
# contains "." or ":", or equals "localhost"; a bare name[:tag] is local.
# Hostless docker.io-namespace refs (user/image) classify as local: worst
# case is today's behavior (no auto-pull), and no repo doc uses that form.
if [ "${TOOLBOX_IMAGE%%/*}" != "$TOOLBOX_IMAGE" ]; then
  case "${TOOLBOX_IMAGE%%/*}" in
    *[.:]*|localhost)
      if ! "$CONTAINER_ENGINE" pull "$TOOLBOX_IMAGE"; then
        printf 'WARNING: could not pull %s; continuing with the locally cached image (it may be stale)\n' "$TOOLBOX_IMAGE" >&2
      fi
      ;;
  esac
fi

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
  KROPS_RUN_ID
  KROPS_RUN_TTL
  KROPS_REVISION
  KROPS_RUN_KIND
  REGISTRY_PORT
  OCI_REPOSITORY
  OCI_TAG
  KROPS_OCI_GIT_SHA
  KROPS_OCI_GIT_REF
  KROPS_OCI_SOURCE_URL
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
  -v "$HOME/.krops-azure:$AZURE_CONFIG_DIR" \
  -w /workspace \
  "${IMMUTABLE_ENV_ARGS[@]}" \
  "${MUTABLE_ENV_ARGS[@]}" \
  "$TOOLBOX_IMAGE" \
  ${CLI_ARGS[@]+"${CLI_ARGS[@]}"}
