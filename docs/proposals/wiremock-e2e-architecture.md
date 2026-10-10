# Virtualized e2e harness architecture (WireMock, issue #355)

- Status: under review
- Tracking issue: [#355](https://github.com/polarsquad/krops/issues/355)

## Background

Phase 0 (spikes) proved that cloud API traffic can be redirected to an
in-cluster WireMock for AWS, Azure, and GCP. Findings live in
[docs/wiremock-e2e-spike-findings-aws.md](https://github.com/polarsquad/krops/blob/main/docs/wiremock-e2e-spike-findings-aws.md),
[docs/wiremock-e2e-spike-findings-azure.md](https://github.com/polarsquad/krops/blob/main/docs/wiremock-e2e-spike-findings-azure.md),
and
[docs/wiremock-e2e-spike-findings-gcp.md](https://github.com/polarsquad/krops/blob/main/docs/wiremock-e2e-spike-findings-gcp.md).

Phase 1 landed the shared harness library and per-cloud arm structure in
[virtualized-e2e/](https://github.com/polarsquad/krops/blob/main/virtualized-e2e/).
Nothing there is Flux-reconciled, and phases 2–5 have no mise tasks or CI
workflow yet.

This proposal records the architectural decisions needed before Phase 2 work
can begin: the cross-cloud interception design, the TLS material contract, the
recording workflow, and the Phase 4 automation shape, plus five open questions
that require a choice before implementation.

## Architecture summary

### Interception mechanisms per cloud

Each cloud uses a different interception approach because the controllers have
different configuration surfaces.

| Cloud | Controllers | Mechanism | Boot stubs required |
|---|---|---|---|
| AWS | CAPA, ACK S3/RDS/IAM | `AWS_ENDPOINT_URL` env var (global + per-service) | STS `GetCallerIdentity` (ACK dies at startup without it) |
| Azure | CAPZ, ASO | `aso-controller-settings` Secret for ASO; CoreDNS `rewrite name exact` of `login.microsoftonline.com` and `management.azure.com` for CAPZ (no env surface) | AAD instance-discovery, openid-configuration, and token endpoint stubs (MSAL validates the authority before any ARM call) |
| GCP | CAPG, KCC | CoreDNS `rewrite name exact` of `*.googleapis.com` hostnames + WIF credential repoint of `capg-wif-credentials` and `kcc-wif-credentials` | WIF token-chain stubs: external_account STS exchange (`POST /v1/token`) and impersonation (`:generateAccessToken`) |

All three clouds use the same TLS trust mechanism: `SSL_CERT_FILE=/certs/ca.crt`
pointing at a throwaway CA, mounted via a `wiremock-ca` ConfigMap. Go's
`crypto/x509` honors this env var, covering all controller binaries (all
distroless Go) without SDK involvement. The ConfigMap must exist in each
consuming namespace because Kubernetes volumes cannot cross namespaces.

### WireMock deployment

WireMock runs in `wiremock-system`, HTTPS-only on port 8443 behind a
ClusterIP Service on port 443. Stub mappings are mounted as files (not
in-memory; in-memory stubs are wiped by pod restarts). The shared Deployment
template lives in
[virtualized-e2e/lib/wiremock/](https://github.com/polarsquad/krops/blob/main/virtualized-e2e/lib/wiremock/);
the per-cloud arm adds its boot stubs via a ConfigMap.

### TLS material contract

One throwaway CA is generated per harness run (never committed). Its
`ca.crt` is distributed as a `wiremock-ca` ConfigMap to `wiremock-system`
and each controller namespace. The server certificate is signed by that CA
and built with the WireMock image's own keytool — a LibreSSL-produced PKCS12
fails to load in the WireMock JRE (Phase 0 finding). Both
`--keystore-password` and `--key-manager-password` must be set explicitly
(the key-manager default is the literal string `password` regardless of the
keystore password).

SANs required on the server certificate:

| Arm | SANs |
|---|---|
| All | `wiremock.<namespace>.svc`, `wiremock.<namespace>.svc.cluster.local`, `*.wiremock.<namespace>.svc`, `*.wiremock.<namespace>.svc.cluster.local` (for S3-style virtual-hosted addressing), `localhost` (for `kubectl port-forward` admin access) |
| Azure addition | `login.microsoftonline.com`, `management.azure.com` (the rewritten hostnames are kept in SNI/Host, so the cert must cover them) |
| GCP addition | `compute.googleapis.com`, `container.googleapis.com`, `oauth2.googleapis.com`, `sts.googleapis.com`, `iamcredentials.googleapis.com` (same reason) |

### Phase roadmap

| Phase | What | Status |
|---|---|---|
| 0 | Spike: prove interception per cloud | Done |
| 1 | Shared harness library + per-cloud arm structure | Done |
| 2 | Live recordings (requires a real cloud run per arm) | Not started |
| 3 | Scenario/state-machine content (`lib/scenario-schema.json` shape) | Not started |
| 4 | `mise run <cloud>-e2e-virtualized` tasks + CI workflow | Not started |
| 5 | Full assertion scripts (shared helpers in `lib/assertions.py`) | Helpers only |

---

## Environment setup flow

This is the ordered sequence a Phase 4 mise task runs for one cloud arm.
The steps are the same across clouds; the cloud-specific differences are in
the patch files and boot stubs applied at each step.

```sh
1. kind create cluster --name wiremock-<cloud>-e2e
   (single control-plane node; one cluster per arm, per run)

2. Install mise-pinned host tools (clusterctl, helm, kubectl)
   via jdx/mise-action in CI; already present in the toolbox container locally

3. Generate TLS material (once per cluster, never committed):
   a. Throwaway root CA (2-day validity) with the WireMock image's own keytool
      via `docker run --rm wiremock/wiremock:<pin> keytool -genkeypair ...`
      (Option A from D1; same JRE that will load the keystore)
   b. Server certificate signed by the CA; SANs per-arm as documented in
      the TLS material contract above
   c. JKS keystore packed with both --keystore-password and --key-manager-password
      set to the same value (the key-manager default of "password" is a Phase 0 trap)

4. Distribute TLS material:
   a. Create namespace wiremock-system
   b. kubectl create secret generic wiremock-keystore -n wiremock-system
      --from-file=wiremock.jks --from-literal=keystore-password=... --from-literal=key-manager-password=...
   c. kubectl create configmap wiremock-ca -n wiremock-system --from-file=ca.crt
   d. kubectl create configmap wiremock-ca in EACH controller namespace
      (capa-system + ack-system for AWS; capz-system for Azure; capg-system + cnrm-system for GCP)
      Namespace must exist before the ConfigMap; create it if absent

5. Render and apply WireMock + boot stubs:
   a. envsubst < lib/wiremock/namespace.yaml.tmpl | kubectl apply -f -
   b. envsubst < lib/wiremock/deployment.yaml.tmpl | kubectl apply -f -
   c. envsubst < lib/wiremock/service.yaml.tmpl   | kubectl apply -f -
   d. kubectl apply -f <cloud>/wiremock/stubs-configmap.yaml
   e. kubectl wait --for=condition=Available deployment/wiremock -n wiremock-system

6. Install cloud controllers:
   AWS:   clusterctl init --infrastructure aws:<pin> (dummy AWS_B64ENCODED_CREDENTIALS)
          helm install ack-s3-controller ... ; helm install ack-rds-controller ...
          helm install ack-iam-controller ...
   Azure: clusterctl init --infrastructure azure:<pin> (no credential variables needed)
   GCP:   clusterctl init --infrastructure gcp:<pin>
          kubectl apply -f mgmt/gcp/infrastructure/kcc-operator/ (KCC bundle)

7. Cloud-specific pre-patch steps (BEFORE patching Deployments):
   Azure: kubectl patch secret/aso-controller-settings -n capz-system ...
          (endpoint keys + dummy SP credentials; must precede the Deployment patch
          because secretKeyRef env vars are read at pod start)
          kubectl apply -f <cloud>/wiremock/patches/coredns.yaml
          kubectl rollout restart deployment/coredns -n kube-system
          kubectl rollout status deployment/coredns -n kube-system
   GCP:   kubectl patch secret/capg-wif-credentials ...
          kubectl patch secret/kcc-wif-credentials ...
          kubectl apply -f <cloud>/wiremock/patches/coredns-rewrites.yaml
          kubectl rollout restart deployment/coredns -n kube-system
          kubectl rollout status deployment/coredns -n kube-system
          (rewrite must be in place BEFORE controllers start; a running manager
          keeps its keep-alive TLS connection to the real host until pod restart)

8. Patch controller Deployments (endpoint overrides + CA trust):
   kubectl patch deployment/<name> -n <ns> --type strategic \
     --patch-file <cloud>/wiremock/patches/<controller>.yaml
   per controller, in parallel where the namespaces are independent
   AWS patch order: capa-controller-manager, then ack-{s3,rds,iam}-controller
   Azure patch order: azureserviceoperator-controller-manager, capz-controller-manager
   GCP patch order: capg-controller-manager, cnrm-controller-manager
   NOTE: patch BEFORE waiting on availability — an unpatched ACK controller
   crash-loops on real-STS auth (AWS Phase 0 trap); Azure CAPZ keeps
   existing connections to real AAD until its pod restarts

9. Wait for all controller Deployments to be Available:
   lib/assertions.py deployment-available <ns> <name>  per controller
   (uses kubectl wait --for=condition=Available with a generous timeout;
   the ACK boot race means the controller becomes Available only after
   the patch triggers its rollout, not immediately after helm install)

10. Apply trigger CRs (Phase 2+, requires stubs to be present):
    AWS:   kubectl apply -f <test-cluster-cr> (AWSCluster + Cluster, eu-north-1)
           kubectl apply -f <test-bucket-cr>  (ACK Bucket)
    Azure: kubectl apply -f <test-azurecluster-cr>
    GCP:   kubectl apply -f <test-gcpcluster-cr>

11. Wait for reconcile activity:
    poll /__admin/requests (kubectl port-forward + curl --cacert ca.crt)
    until at least one request per expected controller has arrived,
    or timeout (60s) -- arrival proves the full chain: env override,
    cluster DNS (for virtual-hosted S3 / CoreDNS rewrites), CA trust, WireMock

12. Run Phase 5 assertions:
    lib/assertions.py unmatched  (fails if /__admin/requests/unmatched is non-empty
    after allowing boot stubs; this is the quality gate — see below)

13. Collect debug artifacts (always, including on failure):
    kubectl logs deployment/<controller> -n <ns>  per controller
    curl --cacert ca.crt https://localhost:<port>/__admin/requests
    kubectl describe deployment/<controller> -n <ns>
    upload as GitHub Actions artifact with retention-days: 3

14. kind delete cluster --name wiremock-<cloud>-e2e
```

## Quality gate

The quality gate is the **zero-unmatched-requests assertion** (step 12 above).

`lib/assertions.py unmatched` calls `GET /__admin/requests/unmatched` on
the WireMock admin API. A non-empty response means a controller called a
cloud API endpoint for which no stub mapping exists. This is a hard failure
because it means either:

- the stub set is incomplete (a reconcile path is unrecorded), or
- controller behavior changed after the recordings were taken (schema drift).

The `--allow` flag lets the task exclude known-unmatched paths that are
intentionally out of scope for the current arm (for example, gRPC calls from
CAPG in the REST-only Phase 2 scope for GCP, per D4).

What the gate does NOT catch: a stub matched but returned the wrong response
body. That requires Phase 3 stateful scenarios and is a separate assertion
layer on top of the unmatched check.

## CI placement

The WireMock e2e run takes an estimated 15–25 minutes per arm (kind spin-up
~3 min, controller install ~5 min, reconcile activity ~5 min, assertions ~1 min,
teardown ~1 min), or roughly 20–30 minutes with all three arms in parallel.
That is too long to block PR merges.

| Trigger | Workflow | Job timeout | Gates merges |
|---|---|---|---|
| `pull_request` + `push: main` (path filter `virtualized-e2e/**`) | `virtualized-e2e.yml` | 30 min per arm | No — informational on PR; required on push to main |
| `schedule: nightly` | `virtualized-e2e.yml` | 30 min per arm | No |
| `workflow_dispatch` | `virtualized-e2e.yml` | 30 min per arm | No |

The path filter (`paths: [virtualized-e2e/**]`) means the workflow only runs
when a PR touches the harness files. It does NOT run on unrelated changes
(Renovate bumps, management cluster manifests, etc.), so it does not add
latency to the normal PR cycle.

On `push: main` (post-merge, not pre-merge), a failure opens or comments on
a tracking issue titled `virtualized-e2e: scheduled workflow is failing`,
following the same `report-status` pattern as
[air-gapped.yml](https://github.com/polarsquad/krops/blob/main/.github/workflows/air-gapped.yml).
Cancelled runs are ignored; a recovery closes the issue.

Phase 1 (structure, no recordings) adds no new workflow. The existing
`validate.yml` already builds the kustomize overlays in `virtualized-e2e/`
on every PR. The `virtualized-e2e.yml` workflow is a Phase 4 deliverable.

### D6: per-arm job structure

| Option | How | Trade-offs |
|---|---|---|
| A. Three parallel jobs in one workflow (`aws-e2e`, `azure-e2e`, `gcp-e2e`) | `strategy: matrix` or explicit job names; all run in parallel; one `report-status` job at the end | Each arm uses one runner; total wall time = max(per-arm time); one workflow file to maintain |
| B. Three separate workflows (`virtualized-e2e-aws.yml`, etc.) | Independent schedules and path filters per cloud | Fine-grained control; easier to disable one arm; more workflow files |
| C. Sequential jobs in one workflow | One runner, arms run one after the other | Simplest; total wall time = sum; unnecessary serialization |

Recommendation: **A** — consistent with the repo's existing single-file
nightly pattern; the path filter on each job (`paths: [virtualized-e2e/aws/**,
virtualized-e2e/lib/**]`) handles independent triggering within the same file.

## Open decisions

### D1: TLS certificate generation tooling (Phase 4)

Every arm README states "Phase 4 generates the throwaway CA and JKS keystore,"
but the Phase 0 spike used manual openssl and keytool steps that are not
yet scripted.

| Option | How | Trade-offs |
|---|---|---|
| A. Shell script using `docker run` with the WireMock image | `docker run --rm wiremock/wiremock:<pin> keytool ...` generates the JKS inside the same JRE that will load it; no host-side Java dependency | Requires Docker on the mise host and during CI; the image pin must match the deployment pin |
| B. `step` CLI (smallstep) | mise-pinned `step` generates the CA and signs the cert; a separate `keytool` invocation (from a JDK) converts to JKS | Two tool dependencies; step is already familiar from similar repos but not in this mise config |
| C. `cfssl` + openssl + keytool | Toolchain already verified to work in Phase 0 macOS run; `cfssl` is lightweight | LibreSSL (macOS default openssl) produced a PKCS12 that failed to load in the WireMock JRE; any CI runner must use a non-LibreSSL openssl or skip the PKCS12 path entirely |
| D. Run generation inside the toolbox container | The toolbox image has both Java and openssl; the Phase 4 mise task runs inside it like other helper tasks | Adds Java to the toolbox image, which currently has none; toolbox image is large already |

Recommendation: **A** — the WireMock image's own keytool was the Phase 0
finding for reliability, and Docker is already a hard dependency for kind.

### D2: Phase 4 automation design

The apply order differs non-trivially per cloud (see each arm README). This
decision shapes whether Phase 4 is a single parameterized script, three
separate scripts, or mise tasks.

| Option | How | Trade-offs |
|---|---|---|
| A. One mise task per arm (`aws-e2e-virtualized`, `azure-e2e-virtualized`, `gcp-e2e-virtualized`) | Each task is a small shell script in `mise.toml`; shared steps (CA gen, WireMock apply) are a helper task they call | Explicit per-cloud; task names match the existing `bootstrap`/`teardown` naming style; easy to run one arm without the others |
| B. Single parameterized script `scripts/e2e-virtualized.sh <cloud>` | One entry point, cloud-specific steps in `case` branches | Consistent with `scripts/toolbox-run.sh`; harder to read the per-cloud apply order |
| C. Extend `bootstrap.sh` / `bootstrap-rs` | The harness setup is part of the bootstrap lifecycle | Couples test infrastructure to the production bootstrap; hard to run independently |

Recommendation: **A** — matches the repo's existing task-per-environment
pattern and keeps each cloud's apply order readable.

The tasks must run inside the toolbox container (same `--entrypoint mise`
pattern as `kubeconfigs`, `sops-*`, etc., with `MISE_AUTO_INSTALL=0`) so the
tool versions are pinned. The one exception: `mise run validate` runs on the
host and already builds the kustomize overlays in `virtualized-e2e/`; that
stays a host task.

### D3: GCP incomplete rewrite list

The current CoreDNS rewrite set covers CAPG's resource APIs and the auth
endpoints but not the KCC services the repo's GCP resources actually use:
`iam.googleapis.com` (IAMServiceAccount, IAMPolicyMember,
WorkloadIdentityPool), `serviceusage.googleapis.com`,
`storage.googleapis.com`, `sqladmin.googleapis.com`, and
`servicenetworking.googleapis.com`. Under the current WIF repoint, KCC
resources authenticate against WireMock but then dial the real hostnames
with a dummy token — so they are neither intercepted nor assertable.

| Option | How | Trade-offs |
|---|---|---|
| A. Extend the rewrite list and SAN set per KCC service in scope | Add each missing host to `coredns-rewrites.yaml` and to the server certificate SANs; add boot stubs per service | Full coverage; the SAN set and recording scope grow with the resource set; must be decided before Phase 2 recordings are collected |
| B. Scope the GCP arm to CAPG + KCC auth only | Rename the arm's stated scope; document that KCC resource APIs are explicitly out of scope | Simpler Phase 2; leaves the KCC resource path untested; may need to revisit when more GCP resources are added |
| C. Two GCP arms: one for CAPG, one for KCC resources | `gcp/capg-wiremock/` and `gcp/kcc-wiremock/` | Clean separation; doubles the setup overhead |

This decision must be made before Phase 2 recordings start, because
recordings collected under Option B cannot be extended to Option A without
re-running against a live environment.

### D4: GCP gRPC gap

WireMock 3.13.2 standalone cannot produce `application/grpc` responses. The
GKE gRPC path (`GCPManagedControlPlane` reconcile calls the Container API via
gRPC) returns a 404, surfaced by the client as `rpc Unimplemented`. Arrival
assertions work; stateful gRPC replay does not.

| Option | How | Trade-offs |
|---|---|---|
| A. Add `wiremock-grpc-extension` | Mount the extension JAR into the WireMock Deployment; evaluate gRPC stub and replay capabilities | Adds a dependency and a pinned JAR; the extension is community-maintained and its replay fidelity is not yet evaluated |
| B. Scope the GCP arm to REST-only replay | Formally document that gRPC calls from CAPG are arrival-assertable but not replay-testable; Phase 5 assertions skip gRPC call counts | Simpler; already the de-facto Phase 0 outcome; limits coverage of the GKE control-plane path |
| C. Replace the gRPC path with a REST-based GKE client in CAPG | Out of scope for krops | Not feasible; controlled by the upstream CAPG project |

Option B is the pragmatic Phase 2 starting point; Option A can be evaluated
in parallel as a separate spike (the extension is not pinned to a Phase).

### D5: Azure credential repoint

The Azure arm substitutes dummy service-principal credentials
(`USE_WORKLOAD_IDENTITY_AUTH=false`) for the workload-identity FIC chain that
the real environment uses. This is the Phase 1 shortcut: a kind cluster has
no OIDC issuer that Azure AD can federate with.

| Option | How | Trade-offs |
|---|---|---|
| A. Stay with SP credentials for the harness | Document that the harness uses SP auth, not WIF; the SP path exercises the same azidentity/azcore client construction for endpoint purposes | Cannot assert the WIF token exchange path; diverges from the production credential model |
| B. Fabricate a minimal OIDC issuer for kind | Stand up a local OIDC server in the kind cluster; point Azure AD (or a WireMock stub of AAD) at it | Complex; AAD federation requires a real registered app even for stubs; only viable if the Azure arm can stub the federation endpoints end-to-end |
| C. Repoint `aso-credentials` similarly to the GCP WIF repoint | If ASO's MSAL client honors an `external_account`-style credential override, repoint the Secret to WireMock's token stub | Requires verifying whether ASO/CAPZ accept a token without validating the issuer against a live AAD; unevaluated |

This is a Phase 2 prerequisite only if the harness aims to assert the
WIF token-exchange path. If assertion coverage stops at ARM/AAD API arrival
(same posture as GCP for now), Option A is sufficient.

---

## Slices

The slices follow the phase order. Each is independently mergeable.

| # | Slice | Depends on | Done when |
|---|---|---|---|
| 1 | This document accepted; `AGENTS.md` and `docs/` updated with a `docs/wiremock-e2e.md` architecture page | none | Docs only; `mise run validate` passes |
| 2 | TLS cert generation script (D1 resolved) | 1 | Script generates a valid CA + server cert + JKS; `mise run validate` builds the arms; a smoke test loads the JKS into the WireMock image |
| 3 | Phase 4 mise tasks skeleton: `aws-e2e-virtualized`, `azure-e2e-virtualized`, `gcp-e2e-virtualized` (D2 resolved); tasks run CA gen + WireMock apply + controller patches | 2 | Each task reaches "WireMock available" on a local kind cluster with no CR triggers yet |
| 4 | Phase 2 recordings: AWS arm (live run required) | 3 | AWS: CAPA + ACK S3/RDS/IAM recording stubs committed; `sanitize_recording.py --check` passes; `mise run aws-e2e-virtualized` reaches "zero unmatched requests" |
| 5 | Phase 2 recordings: Azure arm (live run required; D5 resolved) | 3 | Azure: CAPZ + ASO recording stubs committed; same bar as slice 4 |
| 6 | Phase 2 recordings: GCP arm (live run required; D3 resolved) | 3 | GCP: CAPG + KCC (agreed scope) recording stubs committed; same bar |
| 7 | Phase 5 assertion scripts per arm: Deployment available + unmatched-request check | 4–6 | `mise run <cloud>-e2e-virtualized` exits non-zero on unmatched requests; CI job added |
| 8 | Phase 3 scenario content (stateful multi-step reconcile flows) | 7 | At least one stateful scenario per cloud passes end-to-end in CI |

Slices 4–6 require live cloud access (real AWS/Azure/GCP accounts) and are
render-only in CI until sandbox accounts are available per environment.

## Risks

1. The gRPC limitation (D4) means CAPG's GKE control-plane path is
   arrival-assertable at best; the replay gap is a permanent ceiling unless
   Option A (wiremock-grpc-extension) is evaluated.
2. Recording freshness: WireMock recordings are snapshots of the API at the
   time of capture. Controller upgrades may change request shapes, requiring
   re-recording. A CI job that replays stale recordings and passes provides
   false confidence unless there is a way to detect schema drift.
3. The Phase 0 spike ran on a macOS host; CI runs on Linux. The JKS generation
   path (D1) must be verified on the CI runner architecture and OS, not just
   developer laptops. The LibreSSL vs OpenSSL distinction is the known failure
   mode.
4. Stub completeness: boot stubs cover the minimum needed to bring a controller
   up. Phase 2 must discover all API calls a full reconcile makes; any gap
   surfaces as a requeue loop, not a test failure. The
   `/__admin/requests/unmatched` check is the safety net, but it does not
   distinguish "never happened" from "happened but matched".
