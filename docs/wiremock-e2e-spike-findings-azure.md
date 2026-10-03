# WireMock e2e spike findings: Azure (issue #355, Phase 0)

Spike date: 2026-09-20. Scope: Azure only. Question: do CAPZ and the bundled
ASO expose an endpoint-override config surface (`AZURE_AUTHORITY_HOST` /
custom cloud) pointed at an in-cluster WireMock, and if not, how far do the
`HTTPS_PROXY`+CA and CoreDNS-rewrite fallbacks get?

Short answer: ASO yes (full config surface, proven to arrival), CAPZ no
(named clouds only, env vars ignored), but the CoreDNS-rewrite fallback
carries CAPZ end to end. `HTTPS_PROXY` is not viable: WireMock 3.13.2
answers CONNECT with a hollow 200 and cannot tunnel TLS. The Phase 1
mechanism is therefore a combination: ASO endpoint configuration plus a
CoreDNS rewrite for CAPZ's own clients and for MSAL's instance-discovery
anchor.

## Environment and pinned versions

| Component | Version | Source |
|---|---|---|
| kind | v0.33.0 (node v1.37.0) | repo mise pin |
| clusterctl | v1.14.2 | repo mise pin |
| cert-manager | v1.21.1 | installed by clusterctl init |
| cluster-api (core/bootstrap/control-plane) | v1.14.2 | installed by clusterctl init |
| CAPZ | v1.27.0 | repo pin in `mgmt/azure/capi-providers/capz-system/providers.yaml` |
| ASO | v2.19.0 (`mcr.microsoft.com/k8s/azureserviceoperator:v2.19.0`) | bundled by CAPZ v1.27.0 into capz-system |
| Azure SDK (CAPZ) | azcore v1.23.1, azidentity v1.14.1 | CAPZ v1.27.0 go.mod |
| Azure SDK (ASO) | azcore v1.21.1, azidentity v1.13.1 | ASO v2.19.0 go.mod |
| WireMock | `wiremock/wiremock:3.13.2` | docker hub, same pin as the AWS spike |

Scratch kind cluster `wiremock-azure-spike` (single control-plane node),
created and deleted for this spike. Dummy GUID credentials throughout
(subscription `00000000-...`, tenant `11111111-...`, client `22222222-...`).
No real Azure subscription, no real credentials, no cloud resources created.

Unlike the AWS provider, `clusterctl init --infrastructure azure:v1.27.0`
requires NO variables at all: it installs with zero credential input
(workload-identity default, `aso-controller-settings` born with empty
values). No helm releases were installed in this spike (CAPZ via clusterctl,
WireMock via raw manifests), so the helm boot race from the AWS spike did
not apply.

## Setup shape

Same harness as the AWS spike (`docs/wiremock-e2e-spike-findings-aws.md`):
throwaway CA, server certificate signed for the WireMock Service names plus
wildcards, JKS built with the image's own keytool, HTTPS on 8443 behind a
ClusterIP Service on 443, `wiremock.wiremock-system.svc`. Azure-specific SAN
additions for the CoreDNS-rewrite fallback: `login.microsoftonline.com` and
`management.azure.com`. Controller trust: `SSL_CERT_FILE=/certs/ca.crt`
plus a `wiremock-ca` ConfigMap in each consuming namespace (capz-system,
default).

Fixtures: an `AzureClusterIdentity` (type ServicePrincipal, dummy secret) +
`Cluster` + `AzureCluster` (`spike`, location swedencentral) for CAPZ, and
an ASO `ResourceGroup` CR (`wiremock-spike-rg`) for ASO. Observation:
WireMock admin API `/__admin/requests` over `kubectl port-forward`.

## ASO v2.19.0: config surface exists and is honored

### The surface

CAPZ's bundled ASO reads three endpoint settings from the
`aso-controller-settings` Secret in capz-system (all mounted as
`optional: true` secretKeyRef env entries on the ASO Deployment):

- `AZURE_AUTHORITY_HOST`
- `AZURE_RESOURCE_MANAGER_ENDPOINT`
- `AZURE_RESOURCE_MANAGER_AUDIENCE`

CAPZ's `config/aso/settings.yaml` (what clusterctl applies) templates all
three from same-named clusterctl variables, so they can be set at
`clusterctl init` time. The standalone ASO Helm chart exposes the same three
as values (`azureAuthorityHost`, `azureResourceManagerEndpoint`,
`azureResourceManagerAudience`). ASO's config package
(`v2/pkg/common/config/config.go`) documents all three.

### Evidence

Patched the secret with dummy SP credentials
(`USE_WORKLOAD_IDENTITY_AUTH=false`) and all three endpoints set to
`https://wiremock.wiremock-system.svc`, mounted the throwaway CA, restarted
the Deployment, and created a `ResourceGroup` CR. Arrivals at WireMock
(redacted samples from `/__admin/requests`):

```json
{
  "method": "POST",
  "url": "/11111111-1111-1111-1111-111111111111/oauth2/v2.0/token",
  "body": "client_id=22222222-...&grant_type=client_credentials&scope=https%3A%2F%2Fwiremock.wiremock-system.svc%2F.default+openid+offline_access+profile"
}
```

```json
{
  "method": "PUT",
  "url": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/wiremock-spike-rg?api-version=2020-06-01",
  "headers": {
    "host": "wiremock.wiremock-system.svc",
    "authorization": "Bearer eyJhbG...lIn0 (the dummy stub token)",
    "user-agent": "azsdk-go-generic/v2.19.0 (go1.26.2; linux) aso-controller/v2.19.0 cluster-api-provider-azure/v1.27.0"
  },
  "response_status": 404
}
```

All three settings are exercised in that chain: the ARM endpoint in the PUT
URL, the audience in the token scope
(`https://wiremock.wiremock-system.svc/.default`), and the authority host in
the token path. The 404 is WireMock's unmatched-request page; arrival is
what the spike needed to prove.

### Caveat: MSAL instance discovery anchors at the real public cloud

With a custom `AZURE_AUTHORITY_HOST`, token acquisition first failed
against REAL Azure without anything reaching WireMock:

```
ClientSecretCredential authentication failed.
GET https://login.microsoftonline.com/common/discovery/instance
RESPONSE 400: "error": "invalid_instance",
"error_description": "AADSTS50049: Unknown or invalid instance. Trace ID: 12a52fb9-a4..."
```

azidentity/MSAL validates a non-public authority via instance discovery
anchored at the hardcoded public endpoint
`https://login.microsoftonline.com/common/discovery/instance`; real AAD
rejects the in-cluster hostname (`AADSTS50049` proves the custom authority
WAS applied, since the canonical host validates fine). ASO exposes no
disable-instance-discovery knob (no `DisableInstanceDiscovery` anywhere in
the ASO source at v2.19.0). The fix is DNS, not configuration: rewrite
`login.microsoftonline.com` to WireMock and stub the discovery chain (stub
list below). With the rewrite plus stubs the full auth chain became
hermetic, with the custom authority host still configured.

### Caveat: static auth stubs are a Phase 1 prerequisite

Like ACK's STS `GetCallerIdentity` dependency in the AWS spike, no ARM
traffic happens until token acquisition succeeds, so the WireMock stub set
must ship these static mappings from the start:

- `GET /common/discovery/instance` returning `metadata[].aliases` covering
  every authority host in play (both `login.microsoftonline.com` and
  `wiremock.wiremock-system.svc` were listed in this spike).
- `GET /common/.well-known/openid-configuration` (ASO's MSAL path) and
  `GET /<tenant>/v2.0/.well-known/openid-configuration` (CAPZ's MSAL path;
  different URL shape, both must exist).
- `POST /<tenant>/oauth2/v2.0/token` returning a dummy JWT. Both vendored
  azidentity versions accept an unsigned JWT-shaped string without
  complaint.

### Bonus: per-cluster credentials inherit the endpoints

CAPZ's ASOSecret controller creates a per-cluster ASO credential Secret
(`spike-aso-secret`, keys: AZURE_SUBSCRIPTION_ID/TENANT_ID/CLIENT_ID/
CLIENT_SECRET, no endpoint keys), and CAPZ's ASOAPI path creates real ASO
CRs for the `AzureCluster` (a `ResourceGroup` named `spike` appeared,
owned by the AzureCluster). Those reconciles flow through the global
endpoint settings automatically (`ALLOW_MULTI_ENV_MANAGEMENT` defaults to
false, so per-credential endpoint overrides are rejected and the global
ones apply). The whole `AzureASOManaged*` path krops uses for AKS is
covered by the one Secret patch.

## CAPZ v1.27.0: no config surface (three surfaces tried, three negatives)

### Helm values / deploy-time knobs

krops deploys CAPZ via clusterctl, not Helm. The generated
`capz-controller-manager` Deployment wires exactly one Azure env var
(`AZURE_SUBSCRIPTION_ID` from `capz-manager-bootstrap-credentials`) plus
downward-API fields; args are leader election, diagnostics, feature gates,
verbosity. No endpoint flags or env wiring exist.

### CR fields

`AzureCluster.spec.azureEnvironment` accepts only the four named clouds
(AzurePublicCloud, AzureChinaCloud, AzureGermanCloud,
AzureUSGovernmentCloud). The CRD schema has no enum (any string passes API
validation, and the field is webhook-immutable after creation), but the
controller's switch in `azure/scope/clients.go`
(`getSettingsFromEnvironment`) rejects anything else at reconcile time.
Creating an `AzureCluster` with `azureEnvironment: AzureStackCloud`
produced:

```
failed to create scope: failed to configure azure settings and credentials
for Identity: invalid cloud environment name ""
```

The empty `%q` is an upstream error-message bug (the switch's default
branch prints the not-yet-assigned field, not the rejected input), but the
rejection is real: no AzureStackCloud case, no custom endpoint fields
anywhere in the CRD.

### Environment variables

`getSettingsFromEnvironment` reads ONLY `AZURE_SUBSCRIPTION_ID` from the
process environment; endpoints come from hardcoded `cloud.Configuration`
presets, and `azure/scope/identity.go` always constructs the credential
with an explicit `Cloud` block, so azidentity's `AZURE_AUTHORITY_HOST`
environment fallback never engages either.

Empirical confirmation: patched the capz-controller-manager with
`AZURE_AUTHORITY_HOST=https://wiremock.wiremock-system.svc` and
`AZURE_RESOURCE_MANAGER_ENDPOINT=https://wiremock.wiremock-system.svc`
(DNS rewrite OFF) and let the `AzureCluster` reconcile. CAPZ called REAL
AAD with the dummy tenant:

```
failed to get availability zones: failed to get zones for location
swedencentral: failed to refresh resource sku cache: could not iterate
resource skus: ClientSecretCredential authentication failed.
GET https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0/.well-known/openid-configuration
"error_description": "AADSTS90002: Tenant '11111111-...' not found. ...
Trace ID: 941754d7-... Timestamp: 2026-09-20 15:24:35Z"
```

A real AAD trace ID proves real-endpoint contact; the WireMock journal
stayed empty of CAPZ traffic for the whole window (only ASO's entries from
the earlier test). Both env vars are ignored.

## CAPZ fallback 1, CoreDNS rewrite: works end to end

Corefile additions (exact-name rewrites, real hostnames only):

```
rewrite name exact login.microsoftonline.com wiremock.wiremock-system.svc.cluster.local.
rewrite name exact management.azure.com wiremock.wiremock-system.svc.cluster.local.
```

With the WireMock server certificate carrying the real hostnames as SANs
(signed by the throwaway CA, mounted via `SSL_CERT_FILE`) and the auth
stubs from the ASO section in place, CAPZ's full reconcile arrived at
WireMock:

```
GET  /common/discovery/instance
GET  /11111111-1111-1111-1111-111111111111/v2.0/.well-known/openid-configuration
POST /11111111-1111-1111-1111-111111111111/oauth2/v2.0/token
GET  /subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Compute/skus   (x12)
```

Redacted ARM sample (`/__admin/requests`):

```json
{
  "method": "GET",
  "absoluteUrl": "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Compute/skus?$filter=location+eq+'swedencentral'&api-version=2021-07-01",
  "headers": {
    "authorization": "Bearer eyJhbG...lIn0 (the dummy stub token)",
    "user-agent": "azsdk-go-armcompute/v5.7.0 (go1.26.7; linux) cluster-api-provider-azure/v1.27.0-dirty"
  }
}
```

The Go runtime in this user-agent (`go1.26.7`) differs from ASO's
(`go1.26.2`, see ASO evidence above). That is expected: CAPZ and ASO are
separately compiled binaries built from different upstream repositories. The
azcore/azidentity pins in the version table come from each project's go.mod
and are unaffected.

The absoluteUrl shows the real ARM hostname while TLS terminated at
WireMock, which is exactly the intended rewrite posture. After stubbing the
skus list with `{"value": []}`, the reconcile walked deeper and its
ASO-managed resources (the `ResourceGroup` CR named `spike`) appeared and
were reconciled by ASO through the ASO endpoint configuration, also at
WireMock.

One operational gotcha: a manager that established TLS connections to the
real endpoints BEFORE the rewrite keeps reusing them (Go transport
keep-alive). It kept placing real AAD calls with fresh trace IDs until the
pod was restarted. Restart the controller after applying the rewrite.

## CAPZ fallback 2, HTTPS_PROXY + CA: fails at the proxy

azcore's default transport honors proxy environment variables
(`Proxy: http.ProxyFromEnvironment` in azcore v1.23.1
`runtime/transport_default_http_client.go`), so `HTTPS_PROXY` is respected
by both CAPZ and ASO in principle. In practice WireMock 3.13.2 cannot
terminate a CONNECT tunnel. From a test pod with
`HTTPS_PROXY=http://wiremock.wiremock-system.svc:8080`:

```
> CONNECT management.azure.com:443 HTTP/1.1
< HTTP/1.1 200 OK
* CONNECT tunnel established, response 200
* TLSv1.3 (OUT), TLS handshake, Client hello (1):
* TLSv1.3 (OUT), TLS alert, record overflow (534):
* OpenSSL/3.3.2: error:0A0000C6:SSL routines::packet length too long
curl exit 35
```

The CONNECT gets a hollow 200 (and never even appears in the request
journal), the tunnel carries nothing, and the TLS ClientHello is answered
with non-TLS bytes. A bare 200 is worse than a clean refusal because
clients proceed into the handshake before failing. `HTTPS_PROXY` is not a
viable interception mechanism with WireMock as the TLS terminator.

## Decision gate outcome

| Controller | Config-surface override | Fallback result |
|---|---|---|
| ASO v2.19.0 (bundled) | yes: `aso-controller-settings` Secret (or clusterctl variables / ASO Helm values) with `AZURE_AUTHORITY_HOST` + `AZURE_RESOURCE_MANAGER_ENDPOINT` + `AZURE_RESOURCE_MANAGER_AUDIENCE` | CoreDNS rewrite additionally required for MSAL's instance-discovery anchor when the authority host is customized |
| CAPZ v1.27.0 | no: named clouds only, endpoint env vars ignored, no CR fields | CoreDNS rewrite of `login.microsoftonline.com` + `management.azure.com` works end to end (proven to arrival); `HTTPS_PROXY` fails (WireMock cannot tunnel CONNECT) |

The Azure Phase 0 gate passes with a combined mechanism: ASO endpoint
configuration plus a CoreDNS rewrite covering CAPZ's own SDK clients and
the MSAL discovery anchor. No real Azure hostname is contacted once both
are in place, and no network-layer interception beyond in-cluster DNS is
needed.

## Implications for Phase 1

- WireMock deployment: identical to the AWS harness (throwaway CA, JKS via
  the image's keytool, both password flags, HTTPS 8443 behind ClusterIP
  443), with Azure SAN additions `login.microsoftonline.com` and
  `management.azure.com`.
- ASO patch: `aso-controller-settings` Secret with dummy SP credentials,
  `USE_WORKLOAD_IDENTITY_AUTH=false`, and the three endpoint settings
  pointed at WireMock; `SSL_CERT_FILE` + CA ConfigMap in capz-system. No
  per-cluster credential work needed (CAPZ's ASOSecret secrets inherit the
  global settings).
- CAPZ patch: none. CoreDNS exact-name rewrites for
  `login.microsoftonline.com` and `management.azure.com`, plus
  `SSL_CERT_FILE` + CA ConfigMap on the capz-controller-manager Deployment,
  plus a manager restart after the rewrite lands (keep-alive).
- Static auth stubs required before any controller traffic:
  `/common/discovery/instance` (aliases covering every authority host in
  play), `/common/.well-known/openid-configuration`,
  `/<tenant>/v2.0/.well-known/openid-configuration`,
  `/<tenant>/oauth2/v2.0/token` (a dummy unsigned-JWT token is accepted).
  Because both vendored azidentity versions accept an unsigned JWT-shaped
  string (see "Caveat: static auth stubs are a Phase 1 prerequisite"
  above), this pattern proves endpoint routing only: no token signature or
  real AAD auth validation is exercised by the harness.
- The krops Azure environment's real clusters are `AzureASOManaged*`
  (ASO-API) with workload identity; the virtualized harness substitutes
  dummy service-principal credentials for the FIC chain, mirroring the
  issue's planned credential-repoint step. The SP path exercises the same
  azidentity/azcore client construction for endpoint purposes.
- Unmatched-request handling: controllers surface WireMock's unmatched 404
  as generic Azure SDK errors (`ERROR CODE UNAVAILABLE`) and keep
  requeuing, which the Phase 5 `/__admin/requests/unmatched` assertion
  rides on, same as AWS.

## Harness findings beyond the AWS list

- WireMock 3.13.2 standalone rejects the 2.x `--http-port` flag; the HTTP
  port flag is `--port`. Any invalid flag kills the jar with a bare
  `MissingResourceException` and no usage text (the error-reporting bundle
  is missing from the classpath), so flag typos are undiagnosable from the
  log alone.
- The keytool-built keystore is PKCS12 even when named `.jks`; it boots
  fine with the default `--keystore-type=JKS` (OpenJDK's DualFormatJKS
  reads both).
- Clearing the request journal is `DELETE /__admin/requests` in 3.13.2
  (`POST /__admin/requests/reset` returns 404).
- WireMock answers CONNECT with 200 but never journals the request and
  never tunnels bytes.

## Environment caveats observed during the spike

- The loaded-host leader-election flap from the AWS spike hit capi-system
  AND capz-system. For capz-controller-manager the real killer was the
  liveness probe (`timeoutSeconds: 1`, `failureThreshold: 3`): kubelet
  SIGTERMs a CPU-starved manager, which then exits 0 after a graceful
  "leader election lost" shutdown, looking like an election problem.
  Raising leader-election timeouts (60s/30s/5s) alone did not stabilize
  it; relaxing the probes (`timeoutSeconds: 10`, `failureThreshold: 12`)
  did. CI runners with a single kind cluster should need neither.
- `clusterctl init --infrastructure azure:v1.27.0` needs no credential
  variables at all, so the "dummy values but variable required" note from
  the AWS spike has no Azure equivalent.
- `AzureClusterIdentity.spec.clientSecret` is an ObjectReference
  `{name, namespace}` (no key field; the key is fixed to `clientSecret`)
  and the namespace must be set explicitly even for a same-namespace
  secret, or every reconcile fails with "an empty namespace may not be
  set when a resource name is provided".

## Cleanup

`kind delete cluster --name wiremock-azure-spike` after the runs. No Azure
resources exist to clean (dummy credentials never authenticated anywhere;
the only real Azure calls ever placed were CAPZ's/ASO's unassisted token
probes against the real `login.microsoftonline.com`, all rejected with
AADSTS errors). No local clusters left running by this spike.
