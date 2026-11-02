# AGENTS.md: krops

Guidance for AI coding agents working in this repository.

## What this repo is

krops is a working reference implementation, not a packaged product or a
general-purpose framework, for managing Kubernetes clusters and cloud
resources through Kubernetes APIs and Flux. A disposable kind cluster runs
the imperative lifecycle, hands off to Flux, and pivots into a self-managed
management cluster. Cluster API manages cluster lifecycle, cloud operators
on the management cluster manage per-workload cloud resources, and Flux
delivers the declared state. Use upstream resource APIs directly: krops has
no controller or CRD of its own and no separate infrastructure state file or
plan/apply toolchain. The repository also contains a Rust lifecycle CLI,
shell fallback scripts, Python validation and documentation tooling, and CI,
not just declarative YAML.

### Environment scope

| Environment | Implemented scope |
|---|---|
| `aws` | CAPA-managed EKS in two regions, ACK S3/RDS/IAM on the management cluster, workload Flux instances, automated teardown and read-only orphan discovery. |
| `azure` | CAPZ-managed AKS with bundled ASO and workload identity. Cloud resources reconcile on the management cluster. |
| `gcp` | CAPG-managed GKE with Config Connector and Workload Identity Federation. Cloud resources reconcile on the management cluster. |
| `local-host` | CAPD management and workload clusters, a local OCI registry, and a Podinfo workload. This is the no-cloud lifecycle test environment. |
| `local-talos` | A single-node Talos management cluster on operator-provided bare metal through Tinkerbell. Management only: no workload cluster and no CAPI addons. |
| Air-gapped `local-host` | A Zarf package of the `local-host` environment, built and deployed through `airgap/scripts/`. It is not a separate `bootstrap.toml` environment and has no AWS variant. |

Azure and GCP currently refuse automated teardown. Read `bootstrap.toml` and
`docs/known-issues.md` for current acceptance and teardown limitations before
a live run. Crossplane is a decided but unimplemented alternative resource
plane, not a component of the current environments.

### Repository map

- `mgmt/<env>/`: management-cluster desired state. `mgmt/shared/` holds the
  Flux Operator `HelmChartProxy` base consumed by AWS, Azure, GCP, and
  local-host.
- `workload/`: workload-cluster desired state. AWS and GCP roots are empty,
  Azure installs cert-manager, and local-host installs Podinfo. Workload
  clusters run no cloud resource operators and hold no cloud credentials.
- `bootstrap-rs/`: the `krops-bootstrap` Rust CLI, Rust tests, and toolbox
  Dockerfile. The CLI implements bootstrap with default pivot, teardown, and
  read-only AWS orphan discovery.
- `bootstrap.toml`: repository-owned lifecycle configuration and the
  authoritative environment list. `bootstrap.sh`, `pivot.sh`, and
  `teardown.sh` remain native reference and fallback paths.
- `scripts/`: the Docker/Podman lifecycle wrapper and the AWS virtualized-e2e
  entrypoint.
- `airgap/`: offline package manifests, image inventory, build/deploy/update
  scripts, and air-gap tests.
- `virtualized-e2e/`: WireMock harness for controller-to-cloud API tests. It
  is not Flux-reconciled. The AWS boot-scope leg is wired; the Azure and GCP
  arms are buildable but have no wired lifecycle leg.
- `tests/`: repository and workflow regression gates. `tools/`, `docs/`, and
  `website/` implement the documentation site. `.github/workflows/` contains
  path-filtered validation, release, review, and end-to-end automation.
- `mise.toml` and `mise.<env>.toml`: pinned tools and tasks. `renovate.json5`
  discovers dependency pins in their consuming files; there is no central
  version catalog.

## The golden rules (read before changing anything)

1. Put persistent desired state in Git; never hand-edit cluster state. Use
   `kubectl` to inspect. Imperative mutation belongs only in the documented
   bootstrap, pivot, teardown, and credential-seeding paths.
2. AWS, Azure, GCP, and local-talos Flux sources track `main`. Local-host
   instead reconciles an OCI artifact published from the checkout. Do not call
   a change live until the intended source has been updated and Flux has
   converged; merging alone does not republish the local-host artifact.
3. Never commit plaintext secrets, `.env`, or `age.agekey`. Git-owned secrets
   use SOPS + age in `*.sops.yaml`, with only `data` and `stringData`
   encrypted. Use the documented toolbox SOPS tasks. AWS CAPA/ACK credentials
   and the management GitHub PAT are seeded from ambient inputs, not stored in
   Git. Azure and GCP credential manifests contain non-secret workload
   identity configuration; do not replace them with static keys.
4. Treat lifecycle operations as real infrastructure changes. Before any
   deletion, teardown, forced recreation, or scale-to-zero, verify the live
   context, environment, exact target, and ownership. AWS provisioning costs
   money, and `AWS_ONLY=1` teardown still deletes real AWS resources. Never
   run cloud teardown unless the user explicitly requested that operation.
5. Preserve the project scope: direct upstream APIs, one GitOps reconciliation
   model, cloud resource operators on the management cluster, and minimal
   abstraction. Open an issue before adding a provider, changing the bootstrap
   sequence or `bootstrap.toml` shape, or editing `renovate.json5`.
6. Run `mise run validate` plus the targeted checks for every changed surface
   before pushing. A green local run is not proof of a live deployment or of
   CI-only gates.
7. Keep one logical change per PR. Follow `CONTRIBUTING.md` for branch, commit,
   review, and issue conventions, and update documentation in the same PR as
   the behavior or workflow change.

## App layout convention

The management tree separates Flux entrypoints from plain kustomize roots:

```text
mgmt/<env>/kustomization.yaml            # lists the Flux entrypoint files
<category>/flux-ks.yaml                  # Flux Kustomization objects
<category>/<component>/kustomization.yaml  # plain manifests or child roots
```

- A Flux `Kustomization` uses `spec.path` to select a plain kustomize root and
  a `sourceRef` appropriate to the environment: `GitRepository` for
  GitHub-synced environments, `OCIRepository` for local-host.
- Register each new root in its parent `kustomization.yaml`, then register its
  Flux entrypoint in the environment root. Entrypoints can be category-level
  or component-level, and some categories have no grouping kustomization.
  Mirror the nearest sibling instead of imposing a uniform tree.
- Use `dependsOn` for ordering. Use `wait: true` when later resources require
  readiness or installed CRDs, not indiscriminately on long-running cloud
  resources.
- Per-cluster values flow through `cluster-vars` and run metadata through
  `krops-run` with `postBuild.substituteFrom`. Preserve
  `substituteStrategy: Always` where optional values have documented defaults.
- Workload Flux delivery uses the shared `HelmChartProxy` plus a per-environment
  `ResourceSet` and regional inputs. Changes under `mgmt/shared/` affect every
  consuming environment and the air-gap package.
- Adding a workload cluster or application touches several roots and sometimes
  teardown configuration and tests. Follow `docs/extending.md`; do not invent
  a shortened procedure.

## Lifecycle and operational sharp edges

Use the toolbox for lifecycle and helper tasks. Host mise is for validation,
documentation tasks, and the deliberate host-side Podinfo port-forward.

```sh
scripts/toolbox-run.sh bootstrap <env>
scripts/toolbox-run.sh pivot <env>       # resume the default bootstrap/pivot path
scripts/toolbox-run.sh teardown <env>
```

- Environment names are `[environments.*]` sections in `bootstrap.toml`.
  `KROPS_PROFILE` overrides the positional argument; the checked-in default is
  `aws`. Bootstrap pivots by default. The CLI has `teardown` and `orphans`
  subcommands, but no `pivot` subcommand; the wrapper's `pivot` verb reruns the
  normal resume path.
- Build `krops-toolbox:dev` and set `TOOLBOX_IMAGE` only for unreleased toolbox
  changes. Helper tasks run in that same image with `--entrypoint mise` and
  `MISE_AUTO_INSTALL=0`; use the exact invocations in `docs/operations.md`.
- Local-host publishes only `mgmt/local-host/` and `workload/local-host/` into
  its OCI artifact. Republish through the documented toolbox `oci-push` helper
  after local changes. Keep `KROPS_OCI_GIT_SHA`, `KROPS_OCI_GIT_REF`, and
  `KROPS_OCI_SOURCE_URL` out of `.env`; the wrapper derives them so linked
  worktrees publish the right checkout.
- `.env` overrides the process environment in both the wrapper and toolbox
  mise tasks. Do not put secrets in command arguments or logs.
- Concurrent lifecycle runs need distinct host-local kind and registry names.
  The wrapper does not forward all override knobs, so follow the raw-container
  procedure in `docs/bootstrap-cli.md`. Native shell runs are not multiplex-safe.
- Do not move ACK, ASO, or Config Connector resource CRs back under `workload/`.
  The management-cluster placement is intentional and guarded by tests.
- `mise -E aws run aws-e2e-virtualized` creates a throwaway kind cluster and
  intercepts AWS APIs with WireMock. It uses no real cloud credentials, but it
  is still a lifecycle test, not a read-only check.

## Validation

Start from a clean checkout and install the pinned host tools:

```sh
mise trust
mise install
mise run validate
git diff --check
```

`mise run validate` runs shell and offline regression checks, Python ruff,
configuration cross-checks, and every kustomize build under `mgmt/`,
`workload/`, and `virtualized-e2e/`. It does not cover every CI job.

Add the checks for the surfaces you changed:

| Changed surface | Required local checks |
|---|---|
| `bootstrap-rs/` | From `bootstrap-rs/`: `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, `cargo build --locked`, `cargo test --locked` |
| `bootstrap.toml` or referenced files | `python3 tests/test-bootstrap-config.py`, then `mise run validate` |
| `README.md`, `docs/`, or docs assembler | `mise run docs-test` and `mise run docs-build` |
| Markdown | `mise run markdown-lint`; note that `.markdownlint-cli2.jsonc` deliberately excludes `AGENTS.md` |
| `airgap/` | `python3 airgap/tests/test-airgap-image-digests.py --all`; run the documented offline package drill for lifecycle changes |
| Lifecycle scripts or CLI behavior | Full local-host bootstrap and teardown through the toolbox |
| `renovate.json5` or dependency discovery | The dry-run and regression checks below |

CI additionally runs YAML lint, external link checks, rendered Flux diffs,
CI-only Renovate and air-gap gates, and path-selected release or end-to-end
workflows. Verify checks against the PR head SHA, not an earlier run.

## Editing renovate.json5

Keep dependency versions in their consuming files and add Renovate coverage
for every new pin. `renovate-config-validator` checks syntax only; prove real
extraction and replacement behavior before pushing.

Use Node 24.11 or newer and the Renovate version pinned in
`.github/workflows/validate.yml` (currently `44.181.1`):

```sh
GITHUB_COM_TOKEN=$(gh auth token) RENOVATE_TOKEN=$(gh auth token) \
  LOG_LEVEL=debug mise x node@24 -- npx --yes -p renovate@44.181.1 \
  renovate --platform=local --dry-run=full > /tmp/krops-renovate.log 2>&1
```

Read the extraction counts, lookup failures, and `skipReason` histogram even
when the command exits zero. Missing GitHub tokens hide failed datasource
lookups. Renovate uses RE2 for `matchStrings`, so JavaScript-only constructs
such as lookbehind are invalid. Check multiline matching, JSON5 escaping, and
replacement templates against actual output bytes, not just the diff.

With the pinned Renovate CLI on PATH, also run:

```sh
mise x node@24 -- python3 tests/test-renovate-coverage.py
python3 airgap/tests/test-renovate-digest-pinning.py
python3 tests/test-renovate-auto-replace.py
```

See `docs/dependencies.md` for grouping, pinning policy, and update ownership.

## Where to look next

Load only the references relevant to the task:

- `docs/architecture.md`: reconciliation pillars and per-environment topology.
- `docs/operations.md`: toolbox contracts, prerequisites, pivot recovery,
  teardown, releases, and AWS operations.
- `docs/bootstrap-cli.md`: CLI interface, `bootstrap.toml`, runtime controls,
  concurrency, teardown, and orphan discovery.
- `docs/extending.md`: adding clusters, workload apps, and providers.
- `docs/secrets.md`: SOPS + age, bootstrap-owned secrets, rotation, and
  compromise recovery.
- `docs/aws.md`, `docs/azure.md`, `docs/gcp.md`: environment setup and
  credentials.
- `docs/dependencies.md`: dependency ownership, Renovate grouping, and update
  procedures.
- `docs/airgap.md`: offline bundle build, deployment, verification, and update
  drills.
- `docs/konflate.md`: rendered PR review and its CI gate.
- `docs/known-issues.md`: current limitations and acceptance status. Consult
  it before promising or running a live environment.
- `docs/documentation.md`: documentation ownership, placement, links, and
  strict-site rules.
- `virtualized-e2e/README.md`: implemented WireMock arms and unwired phases.
- `docs/crossplane.md`: frozen design record for an unimplemented alternative;
  never cite it as current deployed architecture.

## Keeping docs current

When a change affects repository structure, workflows, or how to navigate the
repository, update `AGENTS.md` and the relevant `docs/` page in the same PR.
Keep this file and `CONTRIBUTING.md` consistent when contributor workflows
change.

The same applies when the repository moves forward in a way the docs describe:
a provider or operator pin bump, a lifecycle change, a new environment or
component, or a discovered or resolved limitation updates the affected `docs/`
pages in the same PR. Record a discovered or resolved limitation, provider
constraint, or platform constraint in `docs/known-issues.md` with linked
sources. Remove a registry entry only when the fix is merged or confirmed gone
in a live run, and note what closed it.
