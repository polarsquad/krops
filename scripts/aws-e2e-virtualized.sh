#!/usr/bin/env bash
# WireMock-virtualized e2e, AWS arm (issue #355, Phase 4 wiring).
#
# Boot scope on a throwaway kind cluster: CAPA (clusterctl) and the three
# ACK controllers (helm, mirroring mgmt/aws/infrastructure/ack-controllers)
# run with their AWS endpoints overridden to an in-cluster WireMock serving
# the STS GetCallerIdentity boot stub. Step 8 proves the interception:
# five Deployments Available, >=3 GetCallerIdentity arrivals, zero
# unmatched requests. No fixture CRs (Phase 2/3), no real credentials:
# only the well-known AWS documentation EXAMPLE key pair, and the harness
# never authenticates.
#
# The apply order follows virtualized-e2e/aws/wiremock/README.md; the
# mechanism evidence is docs/wiremock-e2e-spike-findings-aws.md.
#
# Knobs:
#   KROPS_E2E_KIND_CLUSTER  kind cluster name (default krops-e2e-aws)
#   KROPS_E2E_KEEP=1        leave the cluster and workdir for debugging
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KROPS_E2E_KIND_CLUSTER="${KROPS_E2E_KIND_CLUSTER:-krops-e2e-aws}"
KROPS_E2E_KEEP="${KROPS_E2E_KEEP:-0}"

WIREMOCK_NAMESPACE="wiremock-system"
WIREMOCK_KEYSTORE_SECRET="wiremock-keystore"
WIREMOCK_STUBS_CONFIGMAP="wiremock-stubs"
# Digest pin shared with virtualized-e2e/lib/wiremock/deployment.yaml.tmpl
# (Renovate-tracked there); keytool must run inside the same image build
# (a LibreSSL-produced PKCS12 fails to load in the WireMock JRE).
WIREMOCK_IMAGE="wiremock/wiremock:3.13.2-2@sha256:27c4b74404f58ceedc6899f73abdfe228d8f80d5efa4d8bd42d6c155558b677c"
# Dummy static key pair from the AWS documentation (EXAMPLE); nothing here
# authenticates against real AWS.
DUMMY_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"
DUMMY_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# Mirrors the HelmRelease values in
# mgmt/aws/infrastructure/ack-controllers/helm.yaml.
ACK_REGION="eu-north-1"
WAIT_TIMEOUT="600s"

command -v docker >/dev/null 2>&1 || {
  echo "docker is required (the JKS keystore is built with the WireMock image's own keytool)" >&2
  exit 1
}

WORK="$(mktemp -d)"
CERTS="$WORK/certs"
RENDERED="$WORK/rendered"
mkdir -p "$CERTS" "$RENDERED"

cleanup() {
  local status=$?
  if [ "$KROPS_E2E_KEEP" = "1" ]; then
    echo "KROPS_E2E_KEEP=1: leaving kind cluster '$KROPS_E2E_KIND_CLUSTER' and workdir $WORK"
  else
    if [ "$status" -ne 0 ]; then
      debug_dump
    fi
    kind delete cluster --name "$KROPS_E2E_KIND_CLUSTER" || true
    rm -rf "$WORK"
  fi
  exit "$status"
}
trap cleanup EXIT

log() { echo "==> $*"; }

debug_dump() {
  # Failure diagnostics before teardown: pod states plus the tail of every
  # log in the harness namespaces.
  echo "==> failure diagnostics (cluster teardown follows)"
  kubectl get pods -A -o wide 2>/dev/null || true
  local ns pod
  for ns in wiremock-system capa-system ack-system; do
    for pod in $(kubectl get pods -n "$ns" \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      echo "---- logs: ${ns}/${pod}"
      kubectl logs -n "$ns" "$pod" --tail=50 --all-containers 2>/dev/null || true
    done
  done
}

log "step 1/8: kind cluster '$KROPS_E2E_KIND_CLUSTER'"
KIND_NODE_IMAGE="$(python3 - <<'EOF'
import tomllib

with open("bootstrap.toml", "rb") as f:
    print(tomllib.load(f)["bootstrap"]["kind-node-image"])
EOF
)"
kind create cluster --name "$KROPS_E2E_KIND_CLUSTER" --image "$KIND_NODE_IMAGE"

log "step 2/8: throwaway CA, server certificate, JKS keystore"
# Full CA extensions: Python >= 3.13 enables VERIFY_X509_STRICT (RFC 5280)
# by default in ssl.create_default_context and rejects a CA without
# keyUsage keyCertSign ("CA cert does not include key usage extension",
# X509_V_ERR_CA_CERT_MISSING_KEY_USAGE); the assertions (mise python 3.15)
# verify the admin TLS against this CA. `openssl req -x509` adds no
# keyUsage, so the extensions go through a config file (portable across
# OpenSSL/LibreSSL, where -addext forms differ).
cat > "$CERTS/ca-ext.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no

[dn]
CN = krops-virtualized-e2e-ca

[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
  -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" \
  -config "$CERTS/ca-ext.cnf"

# One password, three consumers: a JKS key entry has a single protection
# password, and each consumer can only be satisfied when all are equal
# (both failure modes verified against the pinned image):
#   - keytool -certreq unlocks the entry with the STORE password (it has
#     no -keypass option); keypass != storepass makes it prompt for the
#     key password and die non-interactively with
#     "Too many failures - try later".
#   - WireMock's KeyManagerFactory unlocks the entry with
#     --key-manager-password; keypass != key-manager-password crashes the
#     pod at boot with UnrecoverableKeyException (Phase 0 spike finding).
KEYSTORE_PASSWORD="$(openssl rand -hex 16)"
KEY_MANAGER_PASSWORD="$KEYSTORE_PASSWORD"

# keytool runs inside the pinned WireMock image (see WIREMOCK_IMAGE note).
keytool() {
  docker run --rm -v "$CERTS:/certs" -w /certs --entrypoint keytool \
    "$WIREMOCK_IMAGE" "$@"
}

# Server key lives inside the JKS from the start: genkeypair, certreq,
# CA sign, import chain (Phase 0 finding). -keypass equals the store
# password (and thus the key manager password): see the password block
# above for why the three must be one value.
keytool -genkeypair -alias wiremock -keyalg RSA -keysize 2048 \
  -sigalg SHA256withRSA -storetype JKS -keystore wiremock.jks \
  -storepass "$KEYSTORE_PASSWORD" -keypass "$KEYSTORE_PASSWORD" \
  -validity 2 -dname "CN=wiremock.${WIREMOCK_NAMESPACE}.svc"
keytool -certreq -alias wiremock -keystore wiremock.jks \
  -storepass "$KEYSTORE_PASSWORD" -file server.csr

# SANs per the lib contract (lib/wiremock/deployment.yaml.tmpl): both
# Service name forms, the wildcard of both (S3 virtual-hosted bucket
# addressing dials the unqualified host form), and localhost for admin
# access over kubectl port-forward. SKID/AKID/keyUsage/EKU make the leaf
# pass the same strict RFC 5280 checks (VERIFY_X509_STRICT, Python >= 3.13
# default): a v3 leaf without an Authority Key Identifier fails with
# "Missing Authority Key Identifier".
cat > "$CERTS/san.cnf" <<EOF
subjectAltName=DNS:wiremock.${WIREMOCK_NAMESPACE}.svc,DNS:wiremock.${WIREMOCK_NAMESPACE}.svc.cluster.local,DNS:*.wiremock.${WIREMOCK_NAMESPACE}.svc,DNS:*.wiremock.${WIREMOCK_NAMESPACE}.svc.cluster.local,DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF
openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.crt" \
  -CAkey "$CERTS/ca.key" -CAcreateserial -out "$CERTS/server.crt" \
  -days 2 -sha256 -extfile "$CERTS/san.cnf"

keytool -importcert -noprompt -alias ca -file ca.crt \
  -keystore wiremock.jks -storepass "$KEYSTORE_PASSWORD"
keytool -importcert -noprompt -alias wiremock -file server.crt \
  -keystore wiremock.jks -storepass "$KEYSTORE_PASSWORD"

log "step 3/8: render the shared WireMock templates and apply"
python3 - "$RENDERED" <<'EOF'
import sys
from pathlib import Path

rendered = Path(sys.argv[1])
params = {}
for line in Path("virtualized-e2e/lib/wiremock/example.env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    params[key.strip()] = value.strip()

for name in ("namespace", "service", "deployment"):
    template = Path(f"virtualized-e2e/lib/wiremock/{name}.yaml.tmpl").read_text()
    for key, value in params.items():
        template = template.replace("${" + key + "}", value)
    rendered.joinpath(f"{name}.yaml").write_text(template)
EOF

# Namespace, keystore Secret, and stubs ConfigMap before the Deployment:
# a missing Secret or ConfigMap keeps the pod in ContainerCreating.
kubectl apply -f "$RENDERED/namespace.yaml"
kubectl create secret generic "$WIREMOCK_KEYSTORE_SECRET" -n "$WIREMOCK_NAMESPACE" \
  --from-file=wiremock.jks="$CERTS/wiremock.jks" \
  --from-literal=keystore-password="$KEYSTORE_PASSWORD" \
  --from-literal=key-manager-password="$KEY_MANAGER_PASSWORD"
kubectl create configmap wiremock-ca -n "$WIREMOCK_NAMESPACE" \
  --from-file=ca.crt="$CERTS/ca.crt"
kubectl apply -f "$RENDERED/service.yaml" -f "$RENDERED/deployment.yaml" \
  -f virtualized-e2e/aws/wiremock/stubs-configmap.yaml

log "step 4/8: clusterctl init (CAPA)"
# CAPA pin read from the repo's provider manifest (single source of truth;
# the spike validated v2.13.0, each run re-validates the current pin).
CAPA_VERSION="$(python3 - <<'EOF'
import re
from pathlib import Path

text = Path("mgmt/aws/capi-providers/capa-system/providers.yaml").read_text()
match = re.search(r'^  version: "(v[^"]+)"', text, re.MULTILINE)
if not match:
    raise SystemExit("cannot find the CAPA version pin in providers.yaml")
print(match.group(1))
EOF
)"
# Dummy profile file: clusterctl requires the variable to be set and does
# not validate the values against AWS; the ACK Secret reuses the same file.
cat > "$WORK/aws-credentials" <<EOF
[default]
aws_access_key_id = $DUMMY_ACCESS_KEY_ID
aws_secret_access_key = $DUMMY_SECRET_ACCESS_KEY
region = $ACK_REGION
EOF
AWS_B64ENCODED_CREDENTIALS="$(base64 < "$WORK/aws-credentials" | tr -d '\n')" \
  clusterctl init --infrastructure "aws:${CAPA_VERSION}"
log "    CAPA ${CAPA_VERSION} installed"

log "step 5/8: ACK controllers (helm, mirroring mgmt/aws/infrastructure/ack-controllers)"
ack_chart_version() {
  python3 - "$1" <<'EOF'
import re
import sys
from pathlib import Path

chart = sys.argv[1]
text = Path("mgmt/aws/infrastructure/ack-controllers/helm.yaml").read_text()
match = re.search(
    rf"url: oci://public\.ecr\.aws/aws-controllers-k8s/{re.escape(chart)}\n  ref:\n    tag: \"([^\"]+)\"",
    text,
)
if not match:
    raise SystemExit(f"cannot find the {chart} tag pin in helm.yaml")
print(match.group(1))
EOF
}

cat > "$WORK/ack-values.yaml" <<EOF
aws:
  region: $ACK_REGION
  credentials:
    secretName: aws-credentials
    secretKey: credentials
    profile: default
EOF

kubectl create namespace ack-system
kubectl create secret generic aws-credentials -n ack-system \
  --from-file=credentials="$WORK/aws-credentials"

# ECR Public throttles anonymous pulls from the shared GitHub-hosted
# runner IP pool ("429: toomanyrequests: Data limit exceeded"). A
# runner drawn to a throttled IP 429s on every fetch until the limit
# resets, so the fetches retry with backoff on that signature (#725).
# A FetchReference failure happens before release creation, so a plain
# re-run is safe (no "already exists" collision).
retry_on_429() {
  local desc="$1" attempt=1 delay=10 output rc=0
  shift
  while :; do
    # `rc=$?` after the && || idiom captures the command's exit status;
    # a bare `rc=$?` after an `if` compound would capture the if's own
    # status (0) instead.
    output="$("$@" 2>&1)" && rc=0 || rc=$?
    if [ "$rc" -eq 0 ]; then
      printf '%s\n' "$output"
      return 0
    fi
    if [ "$attempt" -ge 5 ] || ! grep -q '429: toomanyrequests' <<<"$output"; then
      printf '%s\n' "$output" >&2
      return "$rc"
    fi
    log "    $desc: ECR Public 429 (rate limit); retry ${attempt}/4 in ${delay}s"
    sleep "$delay"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
}

# The workflow's "Cache ACK chart tarballs" step pre-populates
# KROPS_E2E_CHART_CACHE with the pinned chart tarballs; a warm cache
# keeps this step off ECR Public entirely. Tarball names embed the
# pinned tag (helm pull: <chart>-<tag>.tgz), so a restore-keys
# fallback can never substitute a different version: an exact-name
# miss just pulls the pinned version.
ACK_CHART_CACHE="${KROPS_E2E_CHART_CACHE:-}"
install_ack_chart() {
  local chart="$1" tag repo tarball
  tag="$(ack_chart_version "$chart")"
  repo="oci://public.ecr.aws/aws-controllers-k8s/${chart}"
  tarball="${chart}-${tag}.tgz"
  if [ -n "$ACK_CHART_CACHE" ]; then
    # actions/cache restores nothing (and creates no directory) on a miss,
    # and helm's downloader opens <dest>/<tarball><random> without creating
    # the directory. On a cold cache that is an immediate
    # "open ...: no such file or directory" instead of a chart pull
    # (observed on the self-hosted runner, krops run 40896512846).
    mkdir -p "$ACK_CHART_CACHE"
    # Validate the cached tarball: a run killed mid-download can persist
    # a partial file through the cache save, and restore hands it back
    # to every later run. `helm show chart` is local-only and fails on
    # a truncated archive, so a corrupt entry re-pulls instead of
    # failing every install.
    if [ -f "$ACK_CHART_CACHE/$tarball" ] && \
      helm show chart "$ACK_CHART_CACHE/$tarball" >/dev/null 2>&1; then
      log "    ${chart} ${tag} from chart cache"
    else
      # rm first: a fetch that dies mid-download must not leave a
      # partial file a later cache hit would install.
      rm -f "$ACK_CHART_CACHE/$tarball"
      retry_on_429 "pull ${chart} ${tag}" \
        helm pull "$repo" --version "$tag" --destination "$ACK_CHART_CACHE" \
        || return $?
    fi
    retry_on_429 "install ${chart}" \
      helm install "ack-${chart%-chart}-controller" "$ACK_CHART_CACHE/$tarball" \
        -n ack-system -f "$WORK/ack-values.yaml"
  else
    retry_on_429 "install ${chart}" \
      helm install "ack-${chart%-chart}-controller" "$repo" --version "$tag" \
        -n ack-system -f "$WORK/ack-values.yaml"
  fi
}

# No --wait: the unpatched controllers crash loop on real-STS auth until
# the step 6 patch lands (spike caveat).
install_ack_chart s3-chart
install_ack_chart rds-chart
install_ack_chart iam-chart

log "step 6/8: distribute the CA and patch the controller Deployments"
kubectl create configmap wiremock-ca -n capa-system \
  --from-file=ca.crt="$CERTS/ca.crt"
kubectl create configmap wiremock-ca -n ack-system \
  --from-file=ca.crt="$CERTS/ca.crt"

# Patch BEFORE waiting on availability: an unpatched ACK controller crash
# loops on real-STS auth, so readiness would race the patch.
kubectl patch deployment capa-controller-manager -n capa-system \
  --type strategic --patch-file virtualized-e2e/aws/wiremock/patches/capa-controller-manager.yaml
kubectl patch deployment ack-s3-controller-s3-chart -n ack-system \
  --type strategic --patch-file virtualized-e2e/aws/wiremock/patches/ack-s3-controller.yaml
kubectl patch deployment ack-rds-controller-rds-chart -n ack-system \
  --type strategic --patch-file virtualized-e2e/aws/wiremock/patches/ack-rds-controller.yaml
kubectl patch deployment ack-iam-controller-iam-chart -n ack-system \
  --type strategic --patch-file virtualized-e2e/aws/wiremock/patches/ack-iam-controller.yaml

log "step 7/8: wait for the Deployments to be Available (interception in effect)"
ASSERTIONS="virtualized-e2e/lib/assertions.py"
python3 "$ASSERTIONS" deployment-available wiremock-system wiremock --timeout "$WAIT_TIMEOUT"
python3 "$ASSERTIONS" deployment-available capa-system capa-controller-manager --timeout "$WAIT_TIMEOUT"
python3 "$ASSERTIONS" deployment-available ack-system ack-s3-controller-s3-chart --timeout "$WAIT_TIMEOUT"
python3 "$ASSERTIONS" deployment-available ack-system ack-rds-controller-rds-chart --timeout "$WAIT_TIMEOUT"
python3 "$ASSERTIONS" deployment-available ack-system ack-iam-controller-iam-chart --timeout "$WAIT_TIMEOUT"

log "step 8/8: assertions (arrival proof + zero unmatched)"
KROPS_E2E_CA_CERT="$CERTS/ca.crt" \
  python3 virtualized-e2e/aws/tests/test-aws-virtualized-e2e.py
