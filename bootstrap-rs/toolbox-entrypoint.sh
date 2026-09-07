#!/bin/sh
# toolbox-entrypoint.sh – launcher for the krops toolbox image (issue #104).
#
# The image's ENTRYPOINT runs krops-bootstrap directly; this launcher is the
# documented `docker run`/`podman run` command's inner command when the
# operator wants a shell or a custom argv, and the wiring point for the
# toolbox runtime knobs the binary reads (KROPS_TOOLBOX, ENGINE_SOCK,
# CONTAINER_ENGINE, KUBECONFIG).
#
# Usage inside the image (set by the run invocation):
#   toolbox-entrypoint.sh [profile|--recreate|teardown ...]
#
# The launcher:
#   1. detects the engine the way bootstrap.sh does (docker with podman
#      re-detection, then podman) so kind picks the right provider;
#   2. resolves the ENGINE_SOCK source path the daemon-side kind network
#      needs (macOS Docker Desktop and remote Podman sockets do not live at
#      /var/run/docker.sock);
#   3. exports the toolbox knobs and execs krops-bootstrap (PID 1 semantics:
#      signals reach the CLI directly).
set -eu

# ── 1. Engine detection (bootstrap.sh parity) ─────────────────────────────────
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
    echo "       Is the engine socket mounted into the container?" >&2
    exit 1
  fi
fi
export CONTAINER_ENGINE

case "$CONTAINER_ENGINE" in
  docker)
    docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not reachable" >&2; exit 1; }
    ;;
  podman)
    podman info >/dev/null 2>&1 || { echo "ERROR: Podman not reachable" >&2; exit 1; }
    export KIND_EXPERIMENTAL_PROVIDER=podman
    ;;
  *)
    echo "ERROR: Unsupported CONTAINER_ENGINE '${CONTAINER_ENGINE}' (expected docker or podman)" >&2
    exit 1
    ;;
esac

# ── 2. ENGINE_SOCK: the daemon-side source path for the kind network ─────────
# The toolbox's own API socket is mounted at /var/run/docker.sock; the kind
# node's extraMounts hostPath must be the path AS THE DAEMON SEES IT. The
# host-side wrapper (scripts/toolbox-run.sh) computes it correctly for every
# platform (Docker Desktop, rootful/rootless Podman, podman machine) and
# passes it in; only fill the bootstrap.sh-parity fallbacks when missing.
if [ -z "${ENGINE_SOCK:-}" ]; then
  case "$CONTAINER_ENGINE" in
    docker)
      # The Docker daemon always exposes its socket at the standard path
      # (inside the Docker Desktop VM on macOS).
      ENGINE_SOCK=/var/run/docker.sock
      ;;
    podman)
      # Never query podman info here: inside the toolbox it reports the
      # mounted client socket, not the daemon-side path kind needs. The
      # bootstrap.sh fallback covers Linux rootful and podman-machine VMs.
      ENGINE_SOCK=/run/podman/podman.sock
      ;;
  esac
  export ENGINE_SOCK
fi

# ── 3. Join the kind network (best-effort) ────────────────────────────────────
# Every `docker run` starts a NEW container, so the network attachment must
# happen per container start, not once per cluster. The kind network exists
# after the first bootstrap; joining makes krops-registry and kind-network
# endpoints resolve for ANY invocation (oci-push, kubectl, a shell). Failure
# is expected and silent before the first bootstrap creates the network; the
# bootstrap itself joins explicitly after cluster creation.
HOSTNAME_ID="$(cat /etc/hostname 2>/dev/null || true)"
if [ -n "$HOSTNAME_ID" ]; then
  case "$CONTAINER_ENGINE" in
    docker) docker network connect kind "$HOSTNAME_ID" >/dev/null 2>&1 || true ;;
    podman) podman network connect kind "$HOSTNAME_ID" >/dev/null 2>&1 || true ;;
  esac
fi

# ── 4. Toolbox mode + exec ────────────────────────────────────────────────────
export KROPS_TOOLBOX=1

exec krops-bootstrap "$@"
