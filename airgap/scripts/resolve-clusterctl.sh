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
