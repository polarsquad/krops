# AGENTS.md: krops

Guidance for AI coding agents working in this repository.

## What this repo is

A working reference implementation of a GitOps pattern for managing AWS
infrastructure through the Kubernetes API: no Terraform, no state files, no
second toolchain. A local kind cluster bootstraps Flux, which reconciles
everything else: CAPA-managed EKS workload clusters, per-cluster Flux
instances (CAPI addons), and ACK operators (S3, RDS, IAM) managing cloud
resources. There is no app source code here, only declarative infrastructure.

- `mgmt/aws/`: synced by the MANAGEMENT cluster's Flux.
  - `infrastructure/`: cert-manager, CAPI operator, CAPA identity, ACK
    controllers (S3, RDS, IAM), the per-cluster Bucket/DBInstance/reader
    Role CRs (`workload-resources/`), account-global IAM, konflate.
  - `capi-providers/`: capi-system, capa-system, caaph-system.
  - `addons/flux-apps/`: installs Flux on each workload cluster
    (HelmChartProxy + ClusterResourceSets).
  - `clusters/`: EKS cluster definitions per region (`eu-north-1`,
    `eu-west-1`); `eu-north-1` also defines the self-managed management
    cluster (`clusters/management/`).
- `mgmt/local-host/`: the local-host management variant (kind-based).
  Same layout as `mgmt/aws/` (`clusters/docker`, `capi-providers/`,
  `addons/`, `infrastructure/`) with no cloud dependencies.
- `mgmt/local-talos/`: the single-node Talos management variant (issue
  #105). Same component layout as `mgmt/aws/` minus addons
  (`infrastructure/`, `capi-providers/`, `clusters/management/`), synced
  from the GitHub GitRepository source like `mgmt/aws`, NOT the laptop OCI
  registry (a physical machine cannot reach krops-registry). Providers are
  Talos + Tinkerbell (CABPT/CACPPT from sidero-community releases, CAPT)
  instead of CAPD; the cluster definition is imperative (explicit
  controlPlaneRef, no ClusterClass) with committed site-specific values
  (control plane endpoint IP, Tinkerbell Hardware name). CAPT is pinned to
  the shrinedogg fork release v0.7.1 (upstream main plus the
  installer-image annotation mirror, PR tinkerbell#604; see
  `capi-providers/capt-system/provider.yaml`); re-point at upstream once a
  release there includes it. The installer image is declared on the
  TalosConfig through `spec.imageFactory` (CABPT v0.8.x resolves it against
  the Image Factory API); the committed definition declares no block, so no
  override is rendered, and the CAPT fork's annotation mirror is no longer
  consumed by CABPT v0.8.x. The `spec.imageFactory` path is not yet
  validated live: the #105 hardware acceptance run (done, closed) predates
  the CABPT bump to v0.8.2, and the PXE/Tinkerbell-Workflow provisioning
  transport still needs a run (issue #225). Fork retirement is tracked in
  issue #266, blocked on upstream PR
  tinkerbell/cluster-api-provider-tinkerbell#604. Scope fence:
  management-only; no `addons/` (Talos ships its own CNI, no
  HelmChartProxy consumers). The wiring landed in #169 and the docs in
  #171; the remaining #105 item is the hardware acceptance run.
- `mgmt/azure/`: the Azure management variant (issue #71). Same component
  layout as `mgmt/aws/` (`infrastructure/`, `capi-providers/`, `addons/`,
  `clusters/`), synced from GitHub. CAPZ v1.27.0 (`capi-providers/capz-system/`)
  bundles Azure Service Operator (ASO) into `capz-system`; clusters are
  `AzureASOManaged*` (AKS) with the ASO resources inline. The bundled ASO also
  reconciles the identity plumbing in `infrastructure/aso-workload-identity/`
  (user-assigned identities, per-cluster data resource group, role assignment,
  federated credentials), which replaces ACK pod identity. Credentials: none at
  rest; workload identity via the `krops-capz` UAMI (AzureClusterIdentity
  `type: WorkloadIdentity` + `credential-from` aso-credentials, a plain
  Secret). Federation is set up by the `arc-federate` mise task
  (`post-kind-create-task`): Arc OIDC issuer for kind, then the management
  cluster's own OIDC issuer post-pivot (issue #236). Non-secret IDs live in
  `azure-vars` (flux-system) and in the workload `cluster-vars`. Upgrade CAPZ
  one minor at a time (ASO CRD migrations). Teardown is manual until the live
  acceptance run.
- `mgmt/gcp/`: the GCP management variant (issue #72). Same component
  layout as `mgmt/aws/` (`infrastructure/`, `capi-providers/`, `addons/`,
  `clusters/`), synced from GitHub. CAPG v1.13.1
  (`capi-providers/capg-system/`) provisions GKE clusters
  (`GCPManaged*`); the Config Connector operator ships as a pinned verbatim
  release bundle (`infrastructure/kcc-operator/`, version comment
  `kcc-operator-version:`) and `tests/test-kcc-operator-pin.py` (in
  `mise run validate` and CI) keeps every committed copy byte-identical and
  matching the version comment: Renovate bumps the comment and the operator
  image tag, and the gate then goes red until the whole release bundle is
  re-downloaded (same "Renovate opens, human completes" posture as the
  Talos images). Credentials: none at rest; Workload Identity Federation
  through the `krops` pool with plain `external_account` Secrets
  (`capg-wif-credentials`, `kcc-wif-credentials`), provider
  `${GCP_WIF_PROVIDER:=mgmt}` (kind bootstrap overrides to `kind` via the
  `gcp-wif` ConfigMap the `wif-federate` post-kind-create task creates; the
  pivot pins `mgmt` via `pivot-manifest-vars`). Non-secret IDs live in
  `gcp-vars` (flux-system) and the workload `cluster-vars`. Teardown is
  manual until the live acceptance run.
- `workload/`: synced by each WORKLOAD cluster's Flux.
  - `base/`: intentionally empty since issue #346 (the ACK controllers and
    the S3/RDS/IAM custom resources moved to `mgmt/aws/infrastructure/`);
    the workload Flux instance stays ready for a future application
    workload.
  - `azure-base/`: cert-manager, ASO (workload identity), and the Azure
    resources (VNet + delegated subnet + private DNS, storage account +
    container, PostgreSQL Flexible Server). `swedencentral-01/` points at it.
    `tests/test-azure-identity-chain.py` (in `mise run validate` and CI)
    cross-checks the ConfigMap/subject couplings between these and
    `mgmt/azure/infrastructure/aso-workload-identity/`.
  - `gcp-base/` (PR 2, issue #72): Config Connector (the same
    pinned operator bundle as the management side; it ships its own webhook
    certs, so no cert-manager) and the GCP resources
    (PSA range + peering, storage bucket, Cloud SQL with IAM-only auth,
    per-cluster reader GSA). `europe-north1-01/` points at it;
    `tests/test-gcp-identity-chain.py` cross-checks the WIF
    pool/provider/subject couplings against `mgmt/gcp/`.
  - `<region>-01/`: per-cluster overlays pointing at `../base`.
- `airgap/`: Zarf offline transfer bundle for the local-host profile.
  `zarf.yaml` is the authoritative image listing for the package and
  `images.txt` is the superset inventory (the `scripts/` preloads derive from
  the same pins); `airgap/tests/test-airgap-ownership.py` (in `mise run
  validate` and CI) enforces that every `zarf.yaml` image appears in
  `images.txt` with the identical tag and digest, guarding against partial
  air-gap updates (issue #228); the CI-only
  `airgap/tests/test-airgap-kubeadm-images.py` checks the k8s component pins in
  `images.txt` against real `kubeadm config images list`. `scripts/` builds,
  renders, and stages the bundle (`build-*`, `render-*`, `stage-*`,
  `offline-run.sh`); `archives/` and `rendered/` are gitignored outputs.
  Zarf fetches SHA-256-pinned CAAPH release assets and bundles arm64
  `clusterctl`; its bounded deploy action renders and applies CAAPH from those
  staged assets (the supported kind-cluster teardown, not `zarf package remove`,
  removes those resources).
  Every build is signed and contains Zarf-generated per-component Syft SBOMs.
  `offline-run.sh` verifies the signature, checksums, and extracted SBOMs
  before staging; operator builds use `ZARF_SIGNING_KEY` / `ZARF_VERIFY_KEY`,
  while upstream CI uses GitHub OIDC keyless signing.
  The `air-gapped` workflow (nightly at 02:17 UTC or manual dispatch, on
  any repository that carries it) builds the ARM64 bundle on an arm64 runner,
  then deploys it with external egress blocked (fails if any public traffic
  was attempted). The deploy evidence artifact is uploaded.
- `virtualized-e2e/`: WireMock-virtualized e2e harness (issue #355), not
  Flux-reconciled and not wired into a mise task yet (Phase 4). `lib/`
  carries the shared components (WireMock manifest templates under
  `lib/wiremock/`, the `scenario-schema.json` Phase 3 shape,
  `sanitize_recording.py`, `assertions.py`); `<cloud>/wiremock/` carries one
  arm per cloud with only what differs (interception patches, boot stubs,
  arm README). `aws/` is the reference arm; `azure/` adds the second arm
  (ASO endpoint configuration via `aso-controller-settings`, plus a
  CoreDNS rewrite covering CAPZ and MSAL instance discovery); `gcp/`
  mirrors the reference with the CoreDNS-rewrite + SAN-cert interception
  and WIF credential repoint from its Phase 0 spike. The kustomize
  overlays here are built by `mise run validate` like the `mgmt`/`workload`
  ones.
- `bootstrap-rs/`: `krops-bootstrap`, the Rust CLI that ports the imperative
  lifecycle (bootstrap + pivot; teardown under issue #100). Behavioral port:
  same step order, messages, and env interface as the scripts, plus
  rerun-safe-by-default semantics. Chart versions it installs imperatively
  are Renovate-annotated constants in `src/main.rs`. CI (bootstrap-rs
  workflow) runs fmt/clippy/build/test; the toolchain is pinned in
  `rust-toolchain.toml`. Five config-driven knobs added for azure and gcp:
  `pivot-sops-secrets` (SOPS manifests applied in the pivot target before
  the move), `teardown.manual` (refuse with operator text),
  `post-kind-create-task` (mise task run after kind creation) and
  `pivot-manifests` (plain manifests applied in the target before the move),
  plus `pivot-manifest-vars` (key/value overrides merged onto the
  flux-system ConfigMap data before `pivot-manifests` substitution; also
  supports `${VAR:=default}` placeholders in those manifests, issue #72).
- `bootstrap.sh` / `pivot.sh` / `teardown.sh`: the shell equivalents of the
  CLI's phases. Kept until the binary completes full parity runs per
  environment, then retired (issues #92/#95/#100). The lifecycle mise tasks
  (`bootstrap`/`pivot`/`teardown`) run the krops-toolbox container via
  `scripts/toolbox-run.sh` (issue #104); the scripts remain the native path
  for development.
- `docs/`: detailed documentation (see the table in README.md).
- `mise.toml`: pinned tool versions and all task entrypoints.
  `mise.aws.toml` is the AWS tool layer (aws-cli, clusterawsadm),
  activated with `MISE_ENV=aws`. `mise.azure.toml` (azure-cli) and
  `mise.gcp.toml` (gcloud, plus the `gcp-bootstrap`, `wif-federate` and
  `kubeconfigs` tasks; gcloud state lives in the gitignored `.gcloud/`
  shared with the toolbox) are the other per-environment layers.
  Helper tasks run inside the toolbox image via `--entrypoint mise`
  (issue #423); `validate` and `podinfo-port-forward` stay host tasks by
  design; `MISE_AUTO_INSTALL=0` is mandatory for in-toolbox runs and mise's
  `env_file` makes `/workspace/.env` override `-e` values.
- `renovate.json5`: Renovate config. Dependency versions live in the native
  files that consume them (mise configs, manifests, workflows, airgap
  inventory); Renovate discovers and updates them weekly and tracks pending
  updates in the dependency dashboard issue. See `docs/dependencies.md`.
  Edit it only with the dry-run workflow in "Editing renovate.json5" below.

## The golden rules (read before changing anything)

1. Edit YAML in Git; never mutate the clusters. Use kubectl to inspect live
   state, but make every persistent change here and let Flux converge.
2. Flux tracks `main`. Nothing reconciles until merged to `main`. Do not
   promise a fix is "live" until then.
3. Secrets via SOPS + age. Encrypted manifests are named `*.sops.yaml` and
   only `data`/`stringData` fields are encrypted (per `.sops.yaml`). Never
   commit plaintext secrets; `age.agekey` and `.env` are gitignored and must
   stay that way. Encrypt with `mise run sops-encrypt <file>`.
4. Run `mise run validate` before pushing. PRs are reviewed as rendered Flux
   diffs by the konflate GitHub Actions workflow (backed by an in-cluster
   instance), so what you push is what gets reviewed.

## Keeping this file current

When making changes that affect repository structure, architecture,
development workflows, build or test procedures, deployment workflows, or
other information used to navigate and understand the repository, update
`AGENTS.md` as part of the same pull request.

Do not update `AGENTS.md` for changes that do not affect repository
understanding or agent workflows.

## App layout convention

Each component pairs a plain kustomize root with a Flux `Kustomization`:

```
<scope>/<component>/
  kustomization.yaml   # kustomize.config.k8s.io: lists the manifests (+ flux-ks.yaml)
  flux-ks.yaml         # Flux Kustomization(s): path, dependsOn, wait
  ...                  # raw manifests (HelmRelease, CRs, ...)
```

- Register new components in the parent `kustomization.yaml` (the
  `flux-ks.yaml` entry) and use `dependsOn` / `wait: true` for ordering.
- Per-cluster values come from `postBuild.substituteFrom: cluster-vars`
  (`${AWS_REGION}`, `${CLUSTER_NAME}`), not from hardcoding.
- Adding a workload cluster or app is a documented multi-step procedure:
  follow `docs/extending.md` exactly rather than improvising.

## Common tasks (mise)

```sh
mise install            # host: pinned tools for validate and the docs tasks
mise run validate       # host: build every kustomize overlay; mirrors CI
scripts/toolbox-run.sh bootstrap [profile]   # toolbox: kind + Flux handoff + pivot
scripts/toolbox-run.sh teardown  [profile]   # toolbox: full teardown
# helper tasks (sops-*, *-bootstrap, kubeconfigs, oci-push) run the same image:
docker run --rm -it -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E aws run kubeconfigs      # see docs/operations.md
```

## Editing renovate.json5

Renovate configs fail silently in ways `renovate-config-validator` cannot
see (it is a syntax gate only). Before pushing any change to
`renovate.json5`, prove extraction with a local dry-run:

```sh
GITHUB_COM_TOKEN=$(gh auth token) RENOVATE_TOKEN=$(gh auth token) \
  LOG_LEVEL=debug npx --yes -p renovate@44.50.1 \
  renovate --platform=local --dry-run=full > /tmp/rv.log 2>&1
```

Pin the CLI version (unversioned npx resolves "latest" inconsistently) and
run it on Node >= 24.11 (renovate's `engines` field; the CI renovate job
pins node 24). On older Node the dry-run logs an `unhandledRejection`
(`RegExp.escape is not a function`) and still exits 0: a green-looking
silent no-op.
Without `GITHUB_COM_TOKEN` every GitHub datasource lookup skips, hiding
dead depNames. Read the "Dependency extraction complete" stats and compare
`fileCount`/`depCount` per manager against what the change claims to
cover; grep for "Failed to look up" and the `skipReason` histogram.

Traps that have bitten this repo (each caught in a live review):

- `matchStrings` compile with only the `g` flag: `^`/`$` anchors silently
  extract zero dependencies from multi-line files. Keep patterns
  unanchored with literal context, and never end a pattern by consuming
  `\n` (skips every second line).
- JSON5 eats single backslashes: `\s` inside a matchString class parses to
  plain `s`, and `\\n` / `\\\"` in an `autoReplaceStringTemplate` are
  emitted verbatim by the bare-handlebars replacement path, corrupting
  the bumped file. Verify loaded patterns and simulate replacements
  through handlebars; check file bytes with `repr()`, never the diff's
  appearance.
- Custom `depNameTemplate`s must resolve to live repos and datasources
  (`gh api repos/<owner>/<repo>`); three 404 depNames have shipped.
  `github-releases` returns nothing for tag-only repos (golang/go,
  python/cpython); use `github-tags` or `golang-version`.

Repo gates: `tests/test-renovate-coverage.py` (every managed pin is
discovered) and the digest-pinning test run in CI only (the validate.yml
`renovate-digest-pinning` job), not in `mise run validate`. Both run on the
shared harness `tests/renovate_harness.py` (issue #97): a new Renovate test
is a file list plus assertions, never a copy of the subprocess/parsing
logic. The harness needs Node >= 24.11 (renovate's `engines` field);
locally: `mise x node@24 -- python3 tests/test-renovate-coverage.py`.
The offline unit test `tests/test-renovate-actions-grouping.py` also runs in
that CI job and uses the shared harness to apply Renovate's real package-rule
engine, checking that only action dependencies join the GitHub Actions group.
Run locally with Renovate on PATH and Node >= 24.11. These tests
do not cover lookup liveness or the replacement path; only the
dry-run and the handlebars simulation cover those.

The digest-pinning, coverage, and release-assets tests all run sequentially
in the same CI job and their fixtures overlap on `airgap/zarf.yaml`'s
`kubernetes-sigs/cluster-api*` depNames, so the harness points every run at
a shared `RENOVATE_CACHE_DIR` (defaulting to a fixed path under the OS temp
dir): repeat datasource lookups hit Renovate's on-disk cache instead of the
GitHub API again.

The offline `airgap/tests/test-airgap-image-digests.py` gate is separate from
Renovate: it scans air-gap inventories and scripts changed by the PR, requires
readable tags plus SHA-256 digests, and rejects inconsistent repeated
references within the changed files. Untouched legacy files are not checked.
It runs in both `mise run validate` and CI and performs no registry lookups.
Use `python3 airgap/tests/test-airgap-image-digests.py --all` for a full-clone
audit, including untouched legacy files.

## Where to look next

Load these only when the task touches their domain:

- `docs/architecture.md`: reconciliation order, how workload apps are delivered.
- `docs/bootstrap-cli.md`: the `krops-bootstrap` Rust CLI: interface, env knobs, pivot, parity status.
- `docs/azure.md`: the Azure environment: subscription prep, credentials, AKS clusters, ASO on workload clusters, upgrade rules.
- `docs/gcp.md`: the GCP environment: project prep, WIF credentials (no keys), GKE clusters, Config Connector on the workload cluster, upgrade rules.
- `docs/extending.md`: adding a workload cluster, adding apps, adding other providers (Azure, Talos, k0smotron).
- `docs/secrets.md`: SOPS + age setup, credential rotation.
- `docs/konflate.md`: rendered PR review, CI gate, tokens, write-back.
- `docs/aws-iam.md`: management-cluster ACK controllers (static SOPS credentials, union scope), reader roles, reader user.
- `docs/operations.md`: quotas, configuration, bootstrap, verification.
- `docs/workload-resources.md`: S3/RDS posture, known limitations.
- `docs/airgap.md`: Zarf offline bundle for the local-host profile.
