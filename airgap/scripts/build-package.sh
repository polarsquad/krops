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

echo "==> 1/5 validate"
mise run validate

echo "==> 2/5 config artifact"
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

echo "==> 3/5 offline host assets, workload-node images, and OCI charts"
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
  kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
  kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
  kindest/haproxy:v20230606-42a2262b@sha256:001a06433666046dea44567c7d7c6adfc2ac0edb556576f6da507ff0b0f063d3
  docker.io/library/registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
)
for img in "${HOST_IMAGES[@]}"; do
  docker pull --platform linux/arm64 "$img" >/dev/null
done
save_host_images airgap/archives/kindest_node_v1.36.4_mgmt.tar kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
save_host_images airgap/archives/kindest_node_v1.36.4.tar kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
save_host_images airgap/archives/kindest_haproxy_v20230606-42a2262b.tar kindest/haproxy:v20230606-42a2262b@sha256:001a06433666046dea44567c7d7c6adfc2ac0edb556576f6da507ff0b0f063d3
save_host_images airgap/archives/docker.io_library_registry_2.tar docker.io/library/registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
echo "    saved Zarf init package and host-daemon image archives"

WORKLOAD_IMAGES=(
  registry.k8s.io/pause:3.10.2@sha256:f548e0e8e3dc1896ca956272154dde3314e8cc4fde0a57577ee9fa1c63f5baf4
  docker.io/kindest/kindnetd:v20260528-9350166c@sha256:92f49a1b2c9242058481fc3e13412c19a62cfeb090717dad4598719d32351f1f
  ghcr.io/controlplaneio-fluxcd/flux-operator:v0.61.0@sha256:71041d9fff7f7b05f1a8123ebe73c73b7b156f4aacfe1e8e19b5aa953a98892c
  ghcr.io/fluxcd/source-controller:v1.9.6@sha256:6a6693172589f8ff26123a231d5fa6ceb194a6efb4dc647cdf057c959f76a2e3
  ghcr.io/fluxcd/kustomize-controller:v1.9.6@sha256:2ebeaa341da77d52b6abbbba5efcee0450d47f8b42f0e6f33b08f9020262d606
  ghcr.io/fluxcd/helm-controller:v1.6.5@sha256:0d52fff5c4d476277b8fcb6beb9041e269adb5db943fe69f5a806ea0c92b1511
  ghcr.io/fluxcd/notification-controller:v1.9.4@sha256:840f318265ee26f0d2c48a158bf7896b22aa4e998e320a18646309f0e40b15da
  ghcr.io/stefanprodan/podinfo:6.15.0@sha256:ec73780a8425f59ea49f5bc8cdff0d598805a224fbaa1f86c67a244f250fa9da
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

echo "==> 4/5 zarf package create (SBOM generation enabled)"
cd airgap
mise x -- zarf package create . --confirm

PACKAGE="$PWD/zarf-package-krops-airgap-arm64-0.1.0.tar.zst"
if [ ! -f "$PACKAGE" ]; then
  echo "ERROR: expected Zarf package was not created: $PACKAGE" >&2
  exit 1
fi

echo "==> 5/5 sign package"
if [ "${ZARF_KEYLESS_SIGNING:-0}" = "1" ]; then
  mise x -- zarf package sign "$PACKAGE" --keyless --confirm
else
  sign_args=(--signing-key "$ZARF_SIGNING_KEY")
  if [ -n "${ZARF_SIGNING_KEY_PASS:-}" ]; then
    sign_args+=(--signing-key-pass "$ZARF_SIGNING_KEY_PASS")
  fi
  mise x -- zarf package sign "$PACKAGE" "${sign_args[@]}"
fi

echo "==> Built:"
ls -lh "$PACKAGE"
