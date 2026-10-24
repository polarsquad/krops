# WireMock e2e spike findings: GCP (issue #355, Phase 0)

Spike date: 2026-09-20. Scope: GCP only. Question: CAPG has no usable
endpoint-override for all its clients, so can network-layer interception
(HTTPS proxy or CoreDNS rewrite) get its Google API traffic to an in-cluster
WireMock with dummy credentials, and is that traffic REST or gRPC?

Short answer: interception works for both REST and gRPC, but only via CoreDNS
rewrite with WireMock TLS-terminating on certificates whose SANs match the
real Google hostnames. HTTPS_PROXY with CA trust covers REST but not gRPC
(grpc-go never completes a CONNECT tunnel through WireMock's browser
proxying). CAPG v1.13.1's new `spec.serviceEndpoints` override (upstream
issue kubernetes-sigs/cluster-api-provider-gcp#1391 is CLOSED, shipped)
works for the REST compute client but not for the gRPC GKE client, and
covers no auth endpoint. Phase 1 mechanism for GCP: CoreDNS rewrite for
every Google API host in scope.

## Environment and pinned versions

| Component | Version | Source |
|---|---|---|
| kind | v0.33.0 (node image kindest/node:v1.37.0) | repo mise pin |
| clusterctl | v1.14.2 | repo mise pin |
| cert-manager | v1.21.1 | installed by clusterctl init |
| cluster-api (core/bootstrap/control-plane) | v1.14.2 | installed by clusterctl init |
| CAPG | v1.13.1 | repo pin in `mgmt/gcp/capi-providers/capg-system/providers.yaml` |
| CAPG feature gates | `GKE=true,MachinePool=true` | repo `capg-variables.yaml` |
| WireMock | `wiremock/wiremock:3.13.2-2` | see the 3.13.2 tag regression below |
| Scratch cluster | `wiremock-gcp-spike` (single control-plane node) | created and deleted for this spike |

Dummy credentials throughout: a service-account JSON with a throwaway
2048-bit RSA key, project `wiremock-spike-dummy`, handed to clusterctl as
`GCP_B64ENCODED_CREDENTIALS`. No real GCP project, no real credentials, no
cloud resources created.

## Dependency analysis: REST vs gRPC in CAPG v1.13.1

From the pinned `go.mod` at tag v1.13.1:

```text
google.golang.org/api v0.284.0          # REST discovery clients (gdcl)
google.golang.org/grpc v1.81.1
cloud.google.com/go/compute v1.64.0     # GAPIC compute
cloud.google.com/go/container v1.53.0   # GAPIC GKE
cloud.google.com/go/iam v1.11.0
cloud.google.com/go/resourcemanager v1.15.0
```

Which client each code path constructs (source at v1.13.1):

- REST (`google.golang.org/api/compute/v1`, gdcl): every service under
  `cloud/services/compute/` (networks, subnets, firewalls, instances,
  instanceTemplates, instanceGroupManagers, loadBalancers, addresses,
  routers), all built on one `compute.NewService` (`cloud/scope/clients.go:151`).
  This covers all `GCPCluster` and `GCPManagedCluster` network reconciles.
- gRPC (GAPIC): `container.NewClusterManagerClient` (`clients.go:169`,
  GKE cluster and node pool reconciles), `resourcemanager.NewTagBindingsClient`
  (`clients.go:227`), `credentials.NewIamCredentialsClient` (`clients.go:187`).
- GAPIC but REST transport: instance group managers via
  `computerest.NewInstanceGroupManagersRESTClient` (`clients.go:205`).

Live user-agent evidence from the WireMock journal matches both stacks:

```text
x-goog-api-client: gl-go/1.25.11 gdcl/0.284.0     (REST compute calls)
user-agent: gcp.cluster.x-k8s.io/... grpc-go/1.81.1   (container gRPC calls)
```

Auth transport nuance that matters for stub planning: with a
service-account-key credential, the gdcl REST client fetched tokens from
`oauth2.googleapis.com/token` (our stub was consumed; compute calls carried
`Bearer dummy-spike-token`), while the gRPC GKE client used self-signed JWT
access tokens (a `Bearer eyJhbG...` value, redacted) and never called the
token endpoint.

## Mechanism 1: HTTPS_PROXY + CA trust (WireMock browser proxying)

Setup: WireMock with `--enable-browser-proxying --proxy-pass-through false`
(pass-through disabled so unmatched requests 404 instead of leaking to real
Google), capg-manager patched with
`HTTPS_PROXY=http://wiremock.wiremock-system.svc:8080` and
`SSL_CERT_FILE=/certs/wiremock-ca.crt` (WireMock's MITM CA).

Three findings before any result:

1. The current `wiremock/wiremock:3.13.2` docker tag (republished
   2026-03-11) has broken per-host MITM certificate generation: every
   CONNECT target is answered with the CA certificate itself, and
   `/__admin/certs/wiremock-ca.crt` returns 500 (`Failed to export
   certificate authority cert from /root/.wiremock/ca-keystore.jks`).
   A custom `--ca-keystore` shows the same symptom. This is upstream
   wiremock/wiremock-docker#145; the earlier build of the same version,
   tag `3.13.2-2`, works correctly and produced every result below.
   Phase 1 must pin the WireMock image by digest.
2. client-go in CAPG honors `HTTPS_PROXY`. With
   `NO_PROXY=127.0.0.1,localhost` the manager died at startup:
   `Get "https://10.96.0.1:443/api": x509: cannot validate certificate for
   10.96.0.1 because it doesn't contain any IP SANs` (the MITM cert never
   has IP SANs). `NO_PROXY` must exclude the cluster API
   (`10.96.0.1,kubernetes.default.svc`).
3. WireMock keeps stub mappings and the request journal in memory, and
   regenerates the MITM CA per pod start. Two pod restarts during the
   spike each wiped the stubs and invalidated the mounted client CA
   (`x509: certificate signed by unknown authority`). Phase 1 needs
   file-based stubs and a persisted or pre-distributed CA.

REST result: works. With a stub for `POST /token` returning a dummy access
token, the `GCPCluster` network reconcile's calls arrived:

```json
{
  "method": "POST",
  "host": "oauth2.googleapis.com",
  "url": "/token",
  "browserProxyRequest": true
}
{
  "method": "GET",
  "absoluteUrl": "https://compute.googleapis.com/compute/v1/projects/wiremock-spike-dummy/regions/europe-north1?alt=json&prettyPrint=false",
  "headers": {
    "x-goog-api-client": "gl-go/1.25.11 gdcl/0.284.0",
    "authorization": "Bearer dummy-spike-token"
  },
  "browserProxyRequest": true,
  "protocol": "HTTP/1.1"
}
```

gRPC result: fails. The `GCPManagedControlPlane` reconcile's GKE
`GetCluster` never arrived (zero journal entries) and the client reported
`describing cluster: context canceled`. Controls from a debug pod with
stock grpcurl (grpc-go): a direct dial to `container.googleapis.com:443`
reached real Google in about a second; the same call through the proxy
timed out with zero arrival. WireMock's CONNECT MITM does not carry
grpc-go's HTTP/2.

## Mechanism 2: CoreDNS rewrite + SAN-matched TLS termination

Setup: no proxy env anywhere. WireMock serves HTTPS on 8443 behind the
Service on 443, with a server certificate whose SANs list the real
hostnames (`container.googleapis.com`, `compute.googleapis.com`,
`oauth2.googleapis.com`, `sts.googleapis.com`, `iamcredentials.googleapis.com`,
plus `wiremock.wiremock-system.svc`). The JKS was built in an initContainer
with the image's own keytool (self-signed server cert, used directly as the
clients' trust anchor via `SSL_CERT_FILE`; no CA chain needed, so no
openssl involvement at all). CoreDNS in the kind cluster rewrites each
hostname to the WireMock ClusterIP:

```text
rewrite name exact container.googleapis.com wiremock.wiremock-system.svc.cluster.local
rewrite name exact compute.googleapis.com wiremock.wiremock-system.svc.cluster.local
rewrite name exact oauth2.googleapis.com wiremock.wiremock-system.svc.cluster.local
```

REST result: works (same arrivals as mechanism 1, now over HTTP/2.0 since
the direct listener negotiates h2).

gRPC result: works at the arrival level. With `container.googleapis.com`
rewritten, the `GCPManagedControlPlane` reconcile's calls arrived (15 in
the first window):

```json
{
  "method": "POST",
  "url": "/google.container.v1.ClusterManager/GetCluster",
  "absoluteUrl": "https://container.googleapis.com/google.container.v1.ClusterManager/GetCluster",
  "headers": {
    "content-type": "application/grpc",
    "user-agent": "gcp.cluster.x-k8s.io/... grpc-go/1.81.1",
    "te": "trailers",
    "authorization": "Bearer eyJhbG...ZqfG"
  },
  "protocol": "HTTP/2.0"
}
```

Serving-side limitation: WireMock 3.13.2 standalone cannot produce
`application/grpc` responses. The unmatched gRPC POST gets a 404
`text/plain` page, which the client surfaces as
`rpc error: code = Unimplemented desc = unexpected HTTP status code
received from server: 404`. Arrival and method/URI assertions work; stateful
gRPC replay does not, at least not without the unevaluated
wiremock-grpc-extension. A related observation: grpcurl's reflection probe
(a bidirectional stream) hung without ever appearing in the journal, while
unary calls (a hand-crafted `GetCluster` frame over curl, and CAPG's real
`GetCluster`) logged immediately. Journal-based assertions should expect
unary calls only.

## Mechanism 3: ServiceEndpoints override (shipped in v1.13.1)

The premise that CAPG has zero endpoint-override support is outdated for
the repo's pin: upstream issue #1391 (Custom Endpoint Support) is CLOSED
(completed 2025-03-07), and v1.13.1 ships `spec.serviceEndpoints` on both
`GCPCluster` (`api/v1beta1`) and `GCPManagedCluster` (`exp/api/v1beta1`),
with per-service URI fields for compute, container, iam, and
resourceManager (all validated `^https://`).

Tested by patching both CRs (DNS rewrites for compute and container
removed first; oauth2 left rewritten for the token path):

- compute override works for the REST client: the region GET arrived at
  `https://wiremock.wiremock-system.svc/projects/wiremock-spike-dummy/regions/europe-north1`
  with `Bearer dummy-spike-token`. Note the override replaces the whole
  base URL: the `/compute/v1` prefix disappears from request paths, which
  stub authoring must account for.
- container override fails for the gRPC client:
  `describing cluster: context canceled`, zero arrivals, with the endpoint
  as `https://wiremock.wiremock-system.svc` and with an explicit `:443`.
  The GAPIC gRPC transport cannot dial an `https://` endpoint URL in
  v1.13.1.
- There is no field for any auth endpoint, so the token path still needs
  the credential-level repoint (the #355 GCP auth shortcut) or DNS.

## Decision gate outcome

| Mechanism | REST compute | gRPC container | token endpoint |
|---|---|---|---|
| HTTPS_PROXY + CA trust (MITM) | works | fails (no arrival) | works (stubbed) |
| CoreDNS rewrite + SAN cert | works | works (arrival) | works (stubbed) |
| ServiceEndpoints override | works | fails (no arrival) | not covered |

Gate result: REST is observed and interceptable, gRPC is interceptable
only via the DNS rewrite path. The Phase 1 mechanism choice for GCP is
CoreDNS rewrite rules mapping Google API hostnames to the WireMock Service
IP, TLS-terminating with certs whose SANs match the real hostnames, exactly
the spike plan's fallback branch. HTTPS_PROXY is not viable as the single
mechanism (gRPC), and the ServiceEndpoints override is not viable as the
single mechanism (gRPC + no auth coverage); keep it in mind as a REST-only
complement for compute.

Scope-down decision (gRPC serving): for the GKE gRPC calls, Phase 5-style
assertions can check arrival and request shape only, until replay
technology is chosen. Options for Phase 2/3: evaluate the
wiremock-grpc-extension for stateful gRPC replay, or scope replay down to
REST-based CAPG calls plus KCC's Terraform-provider-backed resources
(KCC/Config Connector traffic is REST through the Terraform providers),
matching the scope-down clause in #355.

## Implications for Phase 1

- Interception: CoreDNS `rewrite name exact` entries for
  `compute`, `container`, `oauth2`, and, when the repo's real WIF
  credentials are in play, `sts` and `iamcredentials` `.googleapis.com`,
  all pointing at the WireMock Service. No proxy env on capg-manager.
- WireMock: HTTPS on 8443 behind Service 443, server cert with SANs for
  every rewritten hostname plus the service DNS name, JKS built with the
  image's own keytool in an initContainer, `--keystore-password` and
  `--key-manager-password` set explicitly (the AWS spike's boot finding
  applies unchanged).
- Pin the WireMock image by digest: the mutable `3.13.2` tag was
  republished with the MITM cert-generation regression (`3.13.2-2` is the
  known-good build of the same version).
- File-based stub mappings: pod restarts wipe in-memory stubs and the
  journal (bit twice during this spike), and with browser proxying also
  regenerate the CA (invalidating client trust).
- Server-cert distribution: the DNS mode's server cert is the clients'
  trust anchor via `SSL_CERT_FILE`; regenerate-and-redistribute per pod is
  fine for a spike, but Phase 1 should generate the throwaway key pair
  once per harness run and mount the same PEM everywhere.
- Auth stubs: the REST (gdcl) clients need a `POST /token` stub before any
  compute call; the gRPC GAPIC clients self-sign JWTs and need no token
  stub. Under the repo's WIF posture, either repoint `token_url` and
  `service_account_impersonation_url` in the external_account JSON at
  WireMock (the #355 auth shortcut) or rewrite `sts`/`iamcredentials` via
  the same DNS+SAN mechanism (both SANs were on the spike cert and are
  REST-shaped).
- Unmatched behavior: REST clients surface WireMock's 404 as
  `googleapi: got HTTP response code 404`; gRPC clients surface it as rpc
  `Unimplemented`. Controllers keep requeuing either way, which is what
  arrival assertions ride on.
- The ServiceEndpoints compute override, if ever used, changes request
  paths (drops `/compute/v1`); recordings made under DNS interception will
  not directly match override-mode traffic.

## Environment caveats observed during the spike

- The host ran five other kind clusters (including a sibling spike's), and
  the scratch API server was slow enough that both CAPI's and CAPG's
  leader election (5s lease) flapped; both managers CrashLoopBackOffed
  repeatedly and webhooks intermittently refused connections. The workable
  spike mitigation is `--leader-elect=false` (single replica). CAPG
  v1.13.1 does not accept `--leader-election-lease-duration` style flags
  (`unknown flag` at boot).
- Applying to the kind CoreDNS configmap prints the missing
  last-applied-configuration warning; harmless. CoreDNS rollouts on the
  loaded host took minutes.
- CAPI v1.14's `Cluster` is v1beta2: `spec.infrastructureRef` now takes
  `apiGroup`, not `apiVersion`.

## Real-credential safety audit

- Dummy service-account JSON with a throwaway RSA key throughout; the key
  never existed in any GCP project.
- No CRs existed before the proxy patch, so the unpatched manager made no
  cloud calls. After patching, every controller call terminated at
  WireMock: pass-through disabled in proxy mode, DNS rewritten to the
  in-cluster Service in DNS mode, and the ServiceEndpoints override
  pointing at the in-cluster Service in override mode.
- The only packets that reached real Google were the author's own
  unauthenticated control probes (grpcurl/curl direct dials from debug
  pods, no credentials attached), used to prove that direct egress worked
  when judging the proxy-mode gRPC failure.
- No GCP resources exist to clean up.

## Cleanup

`kind delete cluster --name wiremock-gcp-spike` after the runs. All kind
clusters remaining on the host pre-date this spike.
