# GCP arm: WireMock interception for CAPG and KCC

Second arm of the virtualized e2e harness (issue #355), after the AWS
reference arm. Mechanism proven by the Phase 0 spike; the full evidence is
[docs/wiremock-e2e-spike-findings-gcp.md](../../../docs/wiremock-e2e-spike-findings-gcp.md).

## Chosen mechanism: CoreDNS rewrite + SAN-matched TLS, plus the WIF credential repoint

CAPG v1.13.1 has no usable endpoint-override surface, so interception is
network-layer:

- CoreDNS `rewrite name exact` stanzas map the Google API hostnames the
  spike enumerated (`compute`, `container`, `oauth2`, `sts`,
  `iamcredentials` `.googleapis.com`) to the WireMock Service, which
  TLS-terminates with a server certificate whose SANs match the real
  hostnames. This is the exact spike-validated posture.
- HTTPS_PROXY + CA trust was rejected as the single mechanism: it covers
  the REST compute clients, but grpc-go never completes a CONNECT tunnel
  through WireMock's browser proxying, so the gRPC GKE client
  (GCPManagedControlPlane reconciles) would escape interception. No proxy
  env is set on any controller.
- CAPG's `spec.serviceEndpoints` override (shipped in v1.13.1) was
  rejected as the single mechanism: it works for the REST compute client
  only, the gRPC GKE client cannot dial an `https://` override, and it
  covers no auth endpoint. It remains a REST-only complement for compute;
  note for Phase 2 that the override replaces the whole base URL (drops
  `/compute/v1` from request paths), so recordings made under DNS
  interception will not match override-mode traffic.
- The WIF credential repoint (the #355 Phase 1 GCP auth shortcut):
  `capg-wif-credentials` and `kcc-wif-credentials` get `token_url` and
  `service_account_impersonation_url` pointed at the WireMock Service
  directly, so the external_account STS exchange and the impersonation
  call land at WireMock without DNS interception of the auth hosts. Only
  the resource APIs strictly need the DNS rewrite; the three auth-host
  rewrites stay in the CoreDNS patch (inert under the repoint) so the arm
  also covers the un-repointed credential flavor, and the boot stubs
  match by path, not host, so they serve both.

TLS trust comes from `SSL_CERT_FILE=/certs/ca.crt` pointing at the
throwaway CA, mounted as a `wiremock-ca` ConfigMap volume, the same
contract as the AWS arm. Go's `crypto/x509` honors `SSL_CERT_FILE`, which
covers capg-controller-manager and the Terraform provider processes
cnrm-controller-manager spawns (child processes inherit the environment).
The ConfigMap must exist in EACH consuming namespace (`capg-system`,
`cnrm-system`): volumes cannot cross namespaces. The Phase 4 script
creates the CA and distributes the ConfigMaps; the keystore Secret
contract for WireMock itself is documented in
[../../lib/README.md](../../lib/README.md).

### Server certificate shape

Beyond the lib-mandated SANs (`wiremock.<namespace>.svc` and its
cluster.local form, the wildcard forms of both, and `localhost`), the GCP
arm's server certificate carries the five rewritten hostnames as SANs:
`compute.googleapis.com`, `container.googleapis.com`,
`oauth2.googleapis.com`, `sts.googleapis.com`, and
`iamcredentials.googleapis.com`, the exact hostname set of the
spike-validated certificate. The rest of the TLS material contract
(throwaway CA generated once per harness run, JKS keystore built with the
WireMock image's own keytool, both `--keystore-password` and
`--key-manager-password` set explicitly) is the lib contract, unchanged.

## Files

- `kustomization.yaml`: composes the shared WireMock templates from
  `../../lib/wiremock/` with the GCP stub ConfigMap. Buildable with
  `kubectl kustomize` (template placeholders flow through as strings).
- `stubs-configmap.yaml`: the WIF token-chain boot stubs (the
  external_account STS exchange `POST /v1/token` and the impersonation
  `:generateAccessToken` call). Every reconcile exchanges credentials
  before its first resource API call, so these static mappings must be
  present from the start, before any Phase 2 recordings exist; this is
  the GCP equivalent of the AWS arm's STS GetCallerIdentity boot stub.
  Stub mappings ship as files because in-memory stubs are wiped by any
  pod restart.
- `patches/`: kubectl patch input (exact commands in each file's header).
  Targets live outside this tree:
  - `coredns-rewrites.yaml`: `configmap/coredns` in `kube-system`; the
    full Corefile with the five rewrite stanzas inserted.
  - `capg-wif-credentials-repoint.yaml`: `secret/capg-wif-credentials` in
    `capg-system`; replaces `credentials.json` with a dummy
    external_account configuration repointed at WireMock.
  - `kcc-wif-credentials-repoint.yaml`: `secret/kcc-wif-credentials` in
    `cnrm-system`; the same repoint for `key.json`.
  - `capg-controller-manager.yaml`: `deployment/capg-controller-manager`
    in `capg-system`, container `manager`; CA trust only, no endpoint or
    proxy env.
  - `cnrm-controller-manager.yaml`: `statefulset/cnrm-controller-manager`
    in `cnrm-system` (cluster mode renders a StatefulSet), container
    `manager`; CA trust only.

## Apply order (what Phase 4 will automate)

1. Generate the throwaway CA, server certificate (SAN set above), and JKS
   keystore; create the keystore Secret in `wiremock-system` and the
   `wiremock-ca` ConfigMap in `wiremock-system`, `capg-system`, and
   `cnrm-system`.
2. Render the shared templates (`../../lib/wiremock/example.env` carries
   the values this arm uses) and apply them with `stubs-configmap.yaml`.
3. Apply the CoreDNS rewrite and wait out its rollout (minutes on a
   loaded host, spike caveat). The rewrite must be in place BEFORE the
   controllers come up: a manager that already dialed a real endpoint
   keeps the keep-alive connection until its pod restarts.
4. Apply the two WIF credential repoint patches.
5. Install CAPG and KCC, then apply the two controller CA-trust patches
   before any CR triggers a reconcile: the first thing a reconcile does
   is exchange credentials, and that call must land at WireMock.
6. WireMock availability and, later, the Phase 5 assertions ride
   `../../lib/assertions.py`.

## Known gaps deferred to Phase 2

- WireMock 3.13.2 standalone cannot produce `application/grpc` responses:
  the GKE gRPC calls get a 404, which the client surfaces as rpc
  `Unimplemented`. Arrival and method/URI assertions work; stateful gRPC
  replay does not, at least not without the unevaluated
  wiremock-grpc-extension. The alternative is the #355 scope-down clause:
  replay covers REST-based CAPG calls plus KCC's Terraform-provider-backed
  resources. Journal-based assertions should expect unary calls only; the
  spike saw a reflection probe hang without ever appearing in the
  journal.
- Under a service-account-key credential (the spike's dummy-credential
  flavor, not the repo's posture) the gdcl REST clients fetch
  `oauth2.googleapis.com/token` and need a `POST /token` stub before any
  compute call. That stub is not shipped: the harness runs the repo's WIF
  posture, where no oauth2 token call is made.
