#!/usr/bin/env bash
# build-package.sh — connected-side package build.
# Version bumps must update HOST_IMAGES, mise.toml's Zarf pin, stage-and-create-cluster.sh's KIND_NODE_IMAGE, and cluster-class.yaml's customImage together.
#
#   1. mise run validate (all overlays still build)
#   2. build-config-artifact.sh (trimmed airgap tree -> configured OCI registry)
#   3. stage the Zarf init package, host-daemon images, workload-node pod
#      images, and OCI charts into archives/
#   4. zarf package create (including per-component Syft SBOMs)
#   5. sign the completed package
#   6. render the dependency graph record (#606) next to the package
#
# Output: zarf-package-krops-airgap-arm64-0.1.0.tar.zst next to airgap/.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

if [ -n "${ZARF_SIGNING_KEY:-}" ] && [ "${ZARF_KEYLESS_SIGNING:-0}" = "1" ]; then
  echo "ERROR: set only one of ZARF_SIGNING_KEY or ZARF_KEYLESS_SIGNING=1" >&2
  exit 1
fi
if [ -z "${ZARF_SIGNING_KEY:-}" ] && [ "${ZARF_KEYLESS_SIGNING:-0}" != "1" ]; then
  echo "ERROR: package signing is required; set ZARF_SIGNING_KEY or ZARF_KEYLESS_SIGNING=1" >&2
  exit 1
fi

echo "==> 1/6 validate"
mise run validate

echo "==> 2/6 config artifact"
"$SCRIPT_DIR/build-config-artifact.sh"

# The package must pull the same config artifact that the preceding step
# published. Keep zarf.yaml's local-registry default for ordinary builds, but
# temporarily rewrite its image reference when OCI_REGISTRY is overridden.
if [ -n "${OCI_REGISTRY:-}" ]; then
  OCI_REPOSITORY="${OCI_REPOSITORY:-krops-airgap}"
  OCI_TAG="${OCI_TAG:-latest}"
  ZARF_CONFIG="$REPO_ROOT/airgap/zarf.yaml"
  ZARF_CONFIG_BACKUP=$(mktemp "${TMPDIR:-/tmp}/krops-zarf.XXXXXX")
  cp "$ZARF_CONFIG" "$ZARF_CONFIG_BACKUP"
  restore_zarf_config() {
    cp "$ZARF_CONFIG_BACKUP" "$ZARF_CONFIG"
    rm -f "$ZARF_CONFIG_BACKUP"
  }
  trap restore_zarf_config EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  EXPECTED_ARTIFACT="localhost:5001/krops-airgap:latest"
  CONFIG_ARTIFACT="${OCI_REGISTRY}/${OCI_REPOSITORY}:${OCI_TAG}"
  MATCH_COUNT=$(grep -Fc "$EXPECTED_ARTIFACT" "$ZARF_CONFIG" || true)
  if [ "$MATCH_COUNT" -ne 1 ]; then
    echo "ERROR: expected exactly one config artifact reference: ${EXPECTED_ARTIFACT}" >&2
    exit 1
  fi
  sed -i.bak "s|${EXPECTED_ARTIFACT}|${CONFIG_ARTIFACT}|" "$ZARF_CONFIG"
  rm -f "${ZARF_CONFIG}.bak"
  echo "    zarf config artifact: ${CONFIG_ARTIFACT}"
fi

echo "==> 3/6 offline host assets, workload-node images, and OCI charts"
mkdir -p airgap/archives

INIT_STAGING=$(mktemp -d "${TMPDIR:-/tmp}/krops-zarf-init.XXXXXX")
mise x -- zarf tools download-init \
  --architecture arm64 \
  --output-directory "$INIT_STAGING"
shopt -s nullglob
init_outputs=("$INIT_STAGING"/*)
shopt -u nullglob
if [ "${#init_outputs[@]}" -ne 1 ]; then
  echo "ERROR: expected exactly one init package from zarf tools download-init, found ${#init_outputs[@]}" >&2
  exit 1
fi
mv "${init_outputs[0]}" airgap/archives/zarf-init-arm64.tar.zst
rm -rf "$INIT_STAGING"

# Saves each image under its digest-less tag: docs/airgap.md finding 9.
save_host_images() {
  out=$1
  shift
  aliases=()
  for ref in "$@"; do
    docker tag "$ref" "${ref%@*}"
    aliases+=("${ref%@*}")
  done
  docker save -o "$out" "${aliases[@]}"
  for alias in "${aliases[@]}"; do
    short=${alias#docker.io/}
    short=${short#library/}
    tar -xOf "$out" manifest.json | grep -q -e "\"$alias\"" -e "\"$short\"" -e "\"docker.io/$short\"" || {
      echo "ERROR: $out does not carry the tag $alias" >&2
      exit 1
    }
  done
}

HOST_IMAGES=(
  kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
  kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
  kindest/haproxy:v20230606-42a2262b@sha256:001a06433666046dea44567c7d7c6adfc2ac0edb556576f6da507ff0b0f063d3
  docker.io/library/registry:3.1.2@sha256:01cd312e67026f06f5d89c3e133a4a9adc7cb9eeb9b1194e0b2954dfd770efd2
)
for img in "${HOST_IMAGES[@]}"; do
  docker pull --platform linux/arm64 "$img" >/dev/null
done
save_host_images airgap/archives/kindest_node_v1.37.0_mgmt.tar kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
save_host_images airgap/archives/kindest_node_v1.37.0.tar kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
save_host_images airgap/archives/kindest_haproxy_v20230606-42a2262b.tar kindest/haproxy:v20230606-42a2262b@sha256:001a06433666046dea44567c7d7c6adfc2ac0edb556576f6da507ff0b0f063d3
save_host_images airgap/archives/docker.io_library_registry_3.1.2.tar docker.io/library/registry:3.1.2@sha256:01cd312e67026f06f5d89c3e133a4a9adc7cb9eeb9b1194e0b2954dfd770efd2
echo "    saved Zarf init package and host-daemon image archives"

WORKLOAD_IMAGES=(
  registry.k8s.io/pause:3.10.2@sha256:f548e0e8e3dc1896ca956272154dde3314e8cc4fde0a57577ee9fa1c63f5baf4
  docker.io/kindest/kindnetd:v20260528-9350166c@sha256:92f49a1b2c9242058481fc3e13412c19a62cfeb090717dad4598719d32351f1f
  ghcr.io/controlplaneio-fluxcd/flux-operator:v0.62.0@sha256:061eff8bf782a14f9ea4770f1ec820da0a3bc895532096b8ade3b2e7569c34dc
  ghcr.io/fluxcd/source-controller:v1.10.1@sha256:611b6bca3eae95bcff471414d1fb14a4ab83d7f14e609cccef2bd389c121f37e
  ghcr.io/fluxcd/kustomize-controller:v1.10.1@sha256:37db29b25dae35b881a5e064acedb0cc954442da453a39eaa0eab7a90dbecc4e
  ghcr.io/fluxcd/helm-controller:v1.7.1@sha256:54526cc6ed1202d71d44c310676448065664f3255ad671d9a69ac26f904e617a
  ghcr.io/fluxcd/notification-controller:v1.10.0@sha256:9f58b9532ec9d3ccf539cd3ccdf283477a5dcb7777b922db5477d8bbd4d3767c
  ghcr.io/stefanprodan/podinfo:6.17.0@sha256:fa2f27c1a8561097e5144bfc0853eba6054d743b68b03de4cad4675616ddefb0
)
for img in "${WORKLOAD_IMAGES[@]}"; do
  docker pull --platform linux/arm64 "$img" >/dev/null
done
save_host_images airgap/archives/workload-pod-images.tar "${WORKLOAD_IMAGES[@]}"
echo "    saved airgap/archives/workload-pod-images.tar"

# OCI charts the workload cluster needs in the gap (seeded into krops-registry
# by the stage script): the per-cluster flux-operator chart (HelmChartProxy)
# and the podinfo chart (workload HelmRelease).
mkdir -p airgap/archives/charts
flux_chart_version=$(sed -nE 's/^flux-operator = "(.*)"/\1/p' bootstrap.toml | head -1)
  [ -n "$flux_chart_version" ] || { echo "ERROR: no flux-operator chart version in bootstrap.toml [charts]" >&2; exit 1; }
helm pull oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator --version "$flux_chart_version" -d airgap/archives/charts
podinfo_chart_version=$(sed -nE 's/^ *tag: "?([0-9][^"]*)"?.*/\1/p' workload/local-host/podinfo/helm.yaml | head -1)
[ -n "$podinfo_chart_version" ] || { echo "ERROR: no podinfo chart tag in workload/local-host/podinfo/helm.yaml" >&2; exit 1; }
helm pull oci://ghcr.io/stefanprodan/charts/podinfo --version "$podinfo_chart_version" -d airgap/archives/charts
echo "    staged charts: $(ls airgap/archives/charts/)"

echo "==> 4/6 zarf package create (SBOM generation enabled)"
cd airgap
mise x -- zarf package create . --confirm

PACKAGE="$PWD/zarf-package-krops-airgap-arm64-0.1.0.tar.zst"
if [ ! -f "$PACKAGE" ]; then
  echo "ERROR: expected Zarf package was not created: $PACKAGE" >&2
  exit 1
fi

echo "==> 5/6 sign package"
if [ "${ZARF_KEYLESS_SIGNING:-0}" = "1" ]; then
  mise x -- zarf package sign "$PACKAGE" --keyless --confirm
else
  sign_args=(--signing-key "$ZARF_SIGNING_KEY")
  if [ -n "${ZARF_SIGNING_KEY_PASS:-}" ]; then
    sign_args+=(--signing-key-pass "$ZARF_SIGNING_KEY_PASS")
  fi
  mise x -- zarf package sign "$PACKAGE" "${sign_args[@]}"
fi

echo "==> 6/6 dependency graph record"
# Declared-source render of the exact dependency graph behind this build
# (#606): image refs with digests, chart versions, tool versions, and build
# metadata. Stored next to the package archive and retained as its own CI
# artifact. The renderer reads Git sources only, so it is fully offline.
if command -v sha256sum >/dev/null 2>&1; then
  PACKAGE_SHA256=$(sha256sum "$PACKAGE" | awk '{print $1}')
else
  PACKAGE_SHA256=$(shasum -a 256 "$PACKAGE" | awk '{print $1}')
fi
mise x -- uv run python scripts/render-dependency-graph.py \
  --out "$REPO_ROOT/airgap/dependency-graph.json" \
  --package-name "$(basename "$PACKAGE")" \
  --package-sha256 "$PACKAGE_SHA256"

echo "==> Built:"
ls -lh "$PACKAGE" "$REPO_ROOT/airgap/dependency-graph.json"
