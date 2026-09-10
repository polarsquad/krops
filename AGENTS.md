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
    controllers, pod-identity roles, account-global IAM, konflate.
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
  release there includes it. Scope fence:
  management-only; no `addons/` (Talos ships its own CNI, no
  HelmChartProxy consumers). The wiring landed in #169 and the docs in
  #171; the remaining #105 item is the hardware acceptance run.
- `mgmt/azure/`: the Azure management variant (issue #71). Same component
  layout as `mgmt/aws/` (`infrastructure/`, `capi-providers/`, `addons/`,
  `clusters/`), synced from GitHub. CAPZ v1.27.0 (`capi-providers/capz-system/`)
  bundles Azure Service Operator (ASO) into `capz-system`; clusters are
  `AzureASOManaged*` (AKS) with the ASO resources inline. The bundled ASO also
  reconciles the identity plumbing in `infrastructure/aso-workload-identity/`
  (user-assigned identity, per-cluster data resource group, role assignment,
  federated credential), which replaces ACK pod identity. Credentials: one
  service principal in `infrastructure/azure-identity/aso-credentials.sops.yaml`,
  read by CAPZ (`AzureClusterIdentity`) and by ASO (`credential-from`
  annotation). Non-secret IDs live in `azure-vars` (flux-system) and in the
  workload `cluster-vars`. Upgrade CAPZ one minor at a time (ASO CRD
  migrations). Teardown is manual until the live acceptance run.
- `workload/`: synced by each WORKLOAD cluster's Flux.
  - `base/`: ACK controllers and S3/RDS/IAM custom resources.
  - `<region>-01/`: per-cluster overlays pointing at `../base`.
- `airgap/`: Zarf offline transfer bundle for the local-host profile.
  `zarf.yaml` + `images.txt` define the packages; `scripts/` builds,
  renders, and stages the bundle (`build-*`, `render-*`, `stage-*`,
  `offline-run.sh`); `archives/` and `rendered/` are gitignored outputs.
  Every build is signed and contains Zarf-generated per-component Syft SBOMs.
  `offline-run.sh` verifies the signature, checksums, and extracted SBOMs
  before staging; operator builds use `ZARF_SIGNING_KEY` / `ZARF_VERIFY_KEY`,
  while upstream CI uses GitHub OIDC keyless signing.
  The `air-gapped` workflow (upstream main only, nightly at 02:17 UTC or
  manual dispatch) builds the ARM64 bundle on an arm64 runner, then runs
  two comparison deployments in parallel: one with public traffic monitored
  and one with external egress blocked (fails if any public traffic was
  attempted). Both deploy evidence artifacts are uploaded. Running both is
  a temporary comparison of validation accuracy and performance; the less
  effective job will be removed after enough runs are evaluated.
- `bootstrap-rs/`: `krops-bootstrap`, the Rust CLI that ports the imperative
  lifecycle (bootstrap + pivot; teardown under issue #100). Behavioral port:
  same step order, messages, and env interface as the scripts, plus
  rerun-safe-by-default semantics. Chart versions it installs imperatively
  are Renovate-annotated constants in `src/main.rs`. CI (bootstrap-rs
  workflow) runs fmt/clippy/build/test; the toolchain is pinned in
  `rust-toolchain.toml`. Two config-driven knobs added for azure:
  `pivot-sops-secrets` (SOPS manifests applied in the pivot target before
  the move) and `teardown.manual` (refuse with operator text).
- `bootstrap.sh` / `pivot.sh` / `teardown.sh`: the shell equivalents of the
  CLI's phases. Kept until the binary completes full parity runs per
  environment, then retired (issues #92/#95/#100). The lifecycle mise tasks
  (`bootstrap`/`pivot`/`teardown`) run the krops-toolbox container via
  `scripts/toolbox-run.sh` (issue #104); the scripts remain the native path
  for development.
- `docs/`: detailed documentation (see the table in README.md).
- `mise.toml`: pinned tool versions and all task entrypoints.
  `mise.aws.toml` is the AWS tool layer (aws-cli, clusterawsadm),
  activated with `MISE_ENV=aws`.
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
mise install            # install pinned tools (kubectl, kind, flux, sops, age, ...)
mise run validate       # build every kustomize overlay; mirrors CI
mise run bootstrap      # toolbox container: kind cluster + Flux handoff + pivot to self-managed mgmt
mise -E aws run kubeconfigs  # export AWS workload-cluster kubeconfigs
mise run teardown       # toolbox container: full teardown (EKS, AWS resources, kind)
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
locally: `mise x node@24 -- python3 tests/test-renovate-coverage.py`. They
do not cover lookup liveness or the replacement path; only the
dry-run and the handlebars simulation cover those.

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
- `docs/extending.md`: adding a workload cluster, adding apps, adding other providers (Azure, Talos, k0smotron).
- `docs/secrets.md`: SOPS + age setup, credential rotation.
- `docs/konflate.md`: rendered PR review, CI gate, tokens, write-back.
- `docs/aws-iam.md`: EKS Pod Identity, ACK controller roles, reader user.
- `docs/operations.md`: quotas, configuration, bootstrap, verification.
- `docs/workload-resources.md`: S3/RDS posture, known limitations.
- `docs/airgap.md`: Zarf offline bundle for the local-host profile.
