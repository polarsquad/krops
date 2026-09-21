# Azure arm: WireMock interception for ASO and CAPZ

Second arm of the virtualized e2e harness (issue #355), after the AWS
reference arm. Mechanisms proven by the Phase 0 spike; the full evidence is
[docs/wiremock-e2e-spike-findings-azure.md](../../../docs/wiremock-e2e-spike-findings-azure.md).

## Chosen mechanisms: ASO config surface, CAPZ via CoreDNS rewrite

Unlike AWS (one mechanism for every controller), Azure needs a different
mechanism per component:

- ASO v2.19.0 (bundled by CAPZ v1.27.0 into capz-system): the
  `aso-controller-settings` Secret carries a real endpoint configuration
  surface. `AZURE_AUTHORITY_HOST`, `AZURE_RESOURCE_MANAGER_ENDPOINT`, and
  `AZURE_RESOURCE_MANAGER_AUDIENCE` all point at
  `https://wiremock.wiremock-system.svc`; the token path, token scope
  (`.../.default`), and ARM URL all derive from them. The Secret also gets
  dummy service-principal credentials with `USE_WORKLOAD_IDENTITY_AUTH=false`:
  the harness substitutes SP credentials for the workload-identity FIC chain
  the real environment uses (the issue's planned credential-repoint step),
  and the SP path exercises the same azidentity/azcore client construction
  for endpoint purposes. Per-cluster credentials need no work: CAPZ's
  ASOSecret controller creates per-cluster Secrets without endpoint keys, so
  they inherit the global settings (`ALLOW_MULTI_ENV_MANAGEMENT` defaults to
  false). The whole `AzureASOManaged*` AKS path krops uses is covered by the
  one Secret patch.
- CAPZ v1.27.0: no configuration surface at all. The spike tried three
  (named clouds only in `AzureCluster.spec.azureEnvironment`, endpoint env
  vars ignored by `getSettingsFromEnvironment`, no deploy-time knobs in the
  clusterctl-generated Deployment) and got three negatives. The CoreDNS
  exact-name rewrite of `login.microsoftonline.com` and
  `management.azure.com` to the WireMock Service carries CAPZ end to end.

The CoreDNS rewrite is NOT optional even for ASO: MSAL validates a custom
authority via instance discovery anchored at the hardcoded public
`login.microsoftonline.com`, and ASO v2.19.0 exposes no
disable-instance-discovery knob, so the rewrite plus the auth stubs serve
both controllers. `HTTPS_PROXY` is not viable with either component:
WireMock 3.13.2 answers CONNECT with a hollow 200 and cannot tunnel TLS
(spike evidence).

TLS trust comes from `SSL_CERT_FILE=/certs/ca.crt` pointing at the
throwaway CA, mounted as a `wiremock-ca` ConfigMap volume on both
controller Deployments (all distroless Go binaries; Go's `crypto/x509`
honors `SSL_CERT_FILE`). Both controllers live in capz-system, so ONE
`wiremock-ca` ConfigMap there covers both (volumes cannot cross
namespaces). The Phase 4 script creates the CA and distributes the
ConfigMaps; the keystore Secret contract for WireMock itself is documented
in [../../lib/README.md](../../lib/README.md).

Azure-arm addition to the lib TLS contract: because the rewrite keeps the
real hostnames in SNI/Host, the WireMock server certificate must carry
`login.microsoftonline.com` and `management.azure.com` as SANs on top of
the Service names, wildcards, and `localhost` the lib template already
requires.

## Files

- `kustomization.yaml`: composes the shared WireMock templates from
  `../../lib/wiremock/` with the Azure stub ConfigMap. Buildable with
  `kubectl kustomize` (template placeholders flow through as strings).
- `stubs-configmap.yaml`: the static AAD auth-chain stubs. No ARM traffic
  happens until token acquisition succeeds, so these four mappings must be
  present from the start, before any Phase 2 recordings exist (the same
  boot-stub role STS `GetCallerIdentity` plays in the AWS arm): instance
  discovery (`metadata[].aliases` covering every authority host in play),
  both openid-configuration URL shapes (`/common/...` for ASO's MSAL,
  `/<tenant>/v2.0/...` for CAPZ's), and the token endpoint returning a
  dummy unsigned JWT. Stub mappings ship as files because in-memory stubs
  are wiped by any pod restart.
- `patches/`: kubectl patch input, applied per file (exact commands in each
  file's header). Targets live outside this tree:
  - `aso-controller-settings.yaml`: `secret/aso-controller-settings` in
    `capz-system` (clusterctl-installed), JSON merge patch. The endpoint
    configuration and dummy SP credentials.
  - `azureserviceoperator-controller-manager.yaml`: the ASO Deployment in
    `capz-system`, container `manager`. CA trust only.
  - `capz-controller-manager.yaml`: the CAPZ Deployment in `capz-system`,
    container `manager`. CA trust only; the redirection is the rewrite.
  - `coredns.yaml`: `configmap/coredns` in `kube-system`, JSON merge patch
    carrying the full Corefile (kind v0.33.0 default, verified live, plus
    the two rewrite lines). Re-verify against the live ConfigMap if the
    kind/kubeadm pin moves.

## Apply order (what Phase 4 will automate)

1. Generate the throwaway CA, server certificate (with the Azure SAN
   additions above), and JKS keystore; create the keystore Secret in
   `wiremock-system` and the `wiremock-ca` ConfigMap in `wiremock-system`
   and `capz-system`.
2. Render the shared templates (`../../lib/wiremock/example.env` carries
   the values this arm uses) and apply them with `stubs-configmap.yaml`.
3. Apply the CoreDNS rewrite (`patches/coredns.yaml`). The `reload` plugin
   picks it up; `kubectl rollout restart deployment/coredns -n kube-system`
   is the deterministic path.
4. Patch `secret/aso-controller-settings` BEFORE rolling the ASO
   Deployment: secretKeyRef env is read at pod start, and the Deployment
   patch in the next step triggers the rollout that reads the final values.
5. Patch both controller Deployments. Any manager that was already running
   against real endpoints before the rewrite must be restarted (Go
   transport keep-alive reuses established TLS connections; the spike
   watched a manager keep calling real AAD until its pod restarted).
6. WireMock availability and, later, the Phase 5 assertions ride
   `../../lib/assertions.py`.

## Notes

- Unmatched-request handling matches AWS: controllers surface WireMock's
  unmatched 404 as generic Azure SDK errors and keep requeuing, which the
  Phase 5 `/__admin/requests/unmatched` assertion rides on.
- `clusterctl init --infrastructure azure:v1.27.0` needs no credential
  variables (workload-identity default, `aso-controller-settings` born with
  empty values), so there is no Azure equivalent of the AWS spike's "dummy
  values but variable required" note, and no helm boot race either (CAPZ
  comes from clusterctl, not a Flux HelmRelease).
