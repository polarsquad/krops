# Crossplane on the management cluster, selectable per environment

- Status: draft, for review
- Tracking issue: [#313](https://github.com/polarsquad/krops/issues/313)
- Next step once accepted: split the slices below into sub-issues and implement them one at a time.

## Background

- #346 landed (#377): on AWS, ACK now runs on the management cluster only (`mgmt/aws/infrastructure/ack-controllers/`, `workload-resources/`). So a management-side Crossplane no longer contradicts the current placement, it matches it.
- The direction discussed on #313 is that cloud resources are authored from the management cluster, and each environment uses exactly one resource controller for them: either the cloud-specific operator (ACK on AWS, ASO on Azure, KCC on GCP) or Crossplane. Never both for the same resources (one resource, one controller).
- This supersedes two lines in "Explicitly out of scope" of the #313 description: "Replacing ACK with Upbound provider-aws (or ASO with provider-azure)" and "Crossplane on the management cluster". Both become in scope, as an alternative to the native operator, not an addition to it. Cluster lifecycle stays with CAPI, and Crossplane still does not compose CAPI `Cluster` objects.

## Decision 1: how the choice is expressed

The choice has to live in Git and be structural. Flux `Kustomization.spec.path` is not substituted (`postBuild` runs after the build), and kustomize cannot include or exclude a directory conditionally, so a variable such as `RESOURCE_PLANE=crossplane` in `cluster-vars` cannot switch anything. Options:

| Option | How | Verdict |
|---|---|---|
| A. Selector directory | Each environment gets `mgmt/<env>/infrastructure/resource-plane/kustomization.yaml`, whose only meaningful line names `native` or `crossplane`. Both plane directories exist beside it. Choosing = a one-line PR that konflate renders as a diff. | Recommended |
| B. One sync path per choice | `mgmt/aws` and `mgmt/aws-crossplane` | Duplicates the whole tree. Reject. |
| C. Flux `suspend` or patches | Toggle Kustomizations at runtime | Does not remove what is installed, unclear prune behavior. Reject. |
| D. Config file rewrites Git | The CLI edits manifests from `bootstrap.toml` | Breaks golden rule 1 (edit YAML in Git, never mutate). Reject. |

With A, add a `resource-plane = "native" | "crossplane"` key per environment in `bootstrap.toml` (default `native`, today's behavior), as the operator-facing place to see the choice. Git stays the source of truth; `tests/test-bootstrap-config.py` cross-checks the key against the selector directory so they cannot disagree.

The key is per environment, not global: aws, azure and gcp are already separate environments, each with one cloud. Mixing planes across clouds in one management cluster is out of scope.

Exclusivity guard: a new test renders each environment and fails if both planes' resources are present, or if CRDs of both the native operator and its Crossplane provider for the same cloud would be installed (for example `*.services.k8s.aws` alongside `*.aws.upbound.io`).

## Decision 2: switching a live environment

First version: the choice is made when an environment is set up. Switching a running environment is a separate guarded procedure (last slice), because with `prune: true` removing a plane deletes its CRs, and ACK, ASO and KCC delete the cloud object when the CR is deleted. A switch needs retain or orphan deletion policies and adoption by the new controller before anything is pruned. Until that slice exists, the docs say so explicitly.

## What Crossplane on the management cluster consists of

- Core: `crossplane-system`, HelmRelease `install.crds: CreateReplace`, one or more composition functions (`Function` packages).
- Cloud provider packages (`Provider`) per cloud, with credentials that follow each environment's existing model: AWS static SOPS credentials on the management cluster (as `ack-controllers/aws-credentials.sops.yaml` does today), GCP Workload Identity Federation (as `kcc-wif-credentials`), Azure workload identity (as `aso-workload-identity`). No new secret-at-rest model.
- A platform API layer: XRD, Composition and XRs that reproduce what `mgmt/aws/infrastructure/workload-resources/` creates today (bucket, DB instance, IAM role), so the two planes are functionally equivalent for the same inputs, including the `managed-by` tag lineage and per-cluster naming.
- Ownership rule (same as #224): Flux applies and prunes only what is in Git (Crossplane, functions, providers, XRDs, Compositions, XRs); Crossplane creates and deletes only composed resources; Flux never applies a composed resource.

## Slices (each mergeable and verifiable on its own)

| # | Slice | Depends on | Done when |
|---|---|---|---|
| 1 | Decision record: `docs/crossplane.md`, the selector design, ownership rules, updated scope of this issue and README wording | none | Docs only; `mise run validate` and the strict site build pass |
| 2 | Selector scaffolding, no behavior change: `resource-plane/` indirection with only `native` (AWS first), the `resource-plane` key, mirror and exclusivity tests | 1 | Rendered manifests identical to today apart from paths; the new test runs in `mise run validate` and CI |
| 3 | Crossplane core plus function on the management cluster, `crossplane` plane on local-host first (no cloud needed) | 2 | Local-host bootstrap: `crossplane` Kustomization Ready, `Function` Healthy; Renovate coverage test extended for the `Function` package pin |
| 4 | Local-host worked example: XRD, Composition, XR composing cloud-free objects, plus the aggregated RBAC | 3 | XR reaches Ready in bootstrap; teardown leaves no composed orphans |
| 5 | AWS `crossplane` plane: provider packages, ProviderConfig, credentials | 2 | Renders in `mise run validate` and konflate; exclusivity test green; live check pending #239 |
| 6 | AWS platform API: XRD and Compositions reproducing the bucket, DB instance and role, with tag and name parity and teardown sweeps still valid | 5 | Render parity with the ACK objects checked by a test; live create and delete recorded when a run is possible |
| 7 | Connection-secret delivery to workload clusters, Git-declared (spike, then implementation) | 6 | A Crossplane connection secret reaches a workload cluster with no imperative step |
| 8 | GCP plane, then Azure plane (one issue each, same shape as 5 and 6) | 2 | As slice 5 and 6 per cloud |
| 9 | Guarded switch of a live environment between planes | 6 | Documented procedure plus a test that the retain and adoption steps are present |

Every slice updates its own docs and `AGENTS.md` layout lines; the README sentence "krops introduces no krops-specific CRD or controller" is reworded in slice 1 to keep it accurate now that the repo carries an example XRD.

## Risks and open questions for review

1. Is `resource-plane` per environment (recommended) what you had in mind, or do you want it finer, per resource kind (for example S3 through Crossplane, RDS through ACK)? Per kind would break the "one resource, one controller" guarantee unless the guard is extended.
2. Local-host: always-on Crossplane (every bootstrap and CI run exercises it) or opt-in through the `resource-plane` key on a CI matrix leg? Recommendation: opt-in via the key, since the key now makes that cheap.
3. What does the local-host XR compose: the Podinfo `OCIRepository` plus `HelmRelease` pair (from the original proposal) or plain `Namespace` and `ConfigMap`?
4. Provider choice per cloud: Upbound provider families or community equivalents. Needs a check of current licensing, package sizes and release cadence before slice 5.
5. Switching (slice 9) can delete real cloud resources if done wrongly. Should slice 9 be a hard prerequisite for merging slice 5, so a Crossplane plane cannot be enabled without the safe-switch story?
6. Air-gap: Crossplane packages come from `xpkg.crossplane.io`. Still out of scope for the Zarf bundle, as in the description, and would need its own `area:airgap` issue.
7. Live verification of AWS, GCP and Azure planes depends on sandbox accounts (#239); until then those slices are CI-render only, as with #236.
