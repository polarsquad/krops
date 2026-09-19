#!/usr/bin/env sh
# Sourced (not executed) by the capi-core, capi-providers, and caaph Zarf
# onDeploy actions to set $clusterctl. See docs/airgap.md's "Empirical
# findings" #1.
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)
case "$arch" in
  arm64|aarch64) ;;
  *)
    echo "ERROR: this air-gap component requires an arm64 deploy host; got $arch" >&2
    exit 1
    ;;
esac
clusterctl=/tmp/krops-airgap/bin/clusterctl-"$os"-arm64
[ -x "$clusterctl" ] || { echo "ERROR: bundled clusterctl is missing or not executable: $clusterctl" >&2; exit 1; }

patch_feature_gates() {
  sed -E 's/--feature-gates=.*/--feature-gates=ClusterTopology=true/' "$1" > "$1.patched" && mv "$1.patched" "$1"
}

# Rewrites every `image: name:tag` in a rendered manifest to `name:tag@digest`
# using airgap/images.txt, so pods pull the bundled digest (see docs/airgap.md
# finding 8). Fails if any image is left unpinned.
pin_image_digests() {
  while IFS= read -r ref; do
    case "$ref" in *@sha256:*) ;; *) continue ;; esac
    escaped=$(printf '%s' "${ref%@*}" | sed 's/[.[\*^$#]/\\&/g')
    sed -E "s#(image: *[\"']?)${escaped}([\"']?)\$#\\1${ref}\\2#" "$1" > "$1.pinned" && mv "$1.pinned" "$1" || return 1
  done < "${IMAGES_TXT:-/tmp/krops-airgap/images.txt}"
  unpinned=$(grep -E '^[[:space:]]*image:' "$1" | grep -v '@sha256:')
  [ -z "$unpinned" ] || { echo "ERROR: unpinned images in $1:" >&2; echo "$unpinned" >&2; return 1; }
}

# Zarf only creates the `private-registry` pull secret in namespaces it knows
# from charts/manifests; the provider namespaces are created by the kubectl
# apply in these actions, so copy the secret Zarf made in flux-system into
# them first, or their pods hit "no basic auth credentials" on pull.
copy_registry_secret() {
  auth=$(kubectl -n flux-system get secret private-registry -o 'jsonpath={.data.\.dockerconfigjson}') || return 1
  [ -n "$auth" ] || { echo "ERROR: flux-system/private-registry has no .dockerconfigjson" >&2; return 1; }
  for ns in "$@"; do
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: private-registry\n  namespace: %s\ntype: kubernetes.io/dockerconfigjson\ndata:\n  .dockerconfigjson: %s\n' \
      "$ns" "$ns" "$auth" | kubectl apply -f - || return 1
  done
}
