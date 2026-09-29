# Crossplane as a resource plane

This page records the decisions for running
[Crossplane](https://www.crossplane.io/) on the management cluster as an
alternative to each cloud's native operator. It is the accepted outcome of the
[Crossplane management-cluster proposal](proposals/crossplane-management-cluster.md)
(tracking issue [#313](https://github.com/polarsquad/krops/issues/313)).

Status: decided, not yet implemented. Nothing on this page is reconciled
today; every environment runs its native operator. The slices below land it
one at a time, and each one updates this page when it merges.

## The resource plane

A resource plane is the one controller that reconciles an environment's cloud
resources (buckets, databases, IAM roles and so on). Cluster lifecycle is not
part of it: that stays with Cluster API and its infrastructure provider in
every environment, and Crossplane never composes CAPI `Cluster` objects.

| Environment | `native` plane | `crossplane` plane |
|---|---|---|
| aws | ACK (S3, RDS, IAM) | Crossplane AWS provider packages |
| azure | ASO | Crossplane Azure provider packages |
| gcp | Config Connector | Crossplane GCP provider packages |
| local-host | none (no cloud) | Crossplane with a cloud-free example |

Rules:

- **One resource, one controller.** An environment runs exactly one plane.
  Crossplane is an alternative to the native operator, never an addition to
  it, so two controllers never reconcile the same cloud object.
- **Per environment.** The choice is made per environment (aws, azure, gcp,
  local-host), not per resource kind. Mixing planes, for example S3 through
  Crossplane and RDS through ACK, is out of scope, and so is mixing planes
  across clouds in one management cluster.
- **Management cluster only.** Both planes run on the management cluster,
  matching where ACK already runs since
  [#346](https://github.com/polarsquad/krops/issues/346).
- **`native` is the default.** Every environment keeps today's behavior until
  its selector is changed in Git.

## Selecting a plane

The choice lives in Git and is structural. It cannot be a variable: Flux does
not substitute `Kustomization.spec.path` (`postBuild` runs after the build),
and kustomize cannot include or exclude a directory conditionally, so a value
such as `RESOURCE_PLANE=crossplane` in `cluster-vars` could not switch
anything.

Each environment gets a selector directory next to both planes:

```
mgmt/<env>/infrastructure/resource-plane/
  kustomization.yaml   # resources: [native] or [crossplane], the only line that changes
  native/              # the cloud's native operator and its CRs
  crossplane/          # Crossplane providers, XRDs, Compositions, XRs
```

Choosing a plane is a one-line pull request, which konflate renders as a
diff. Rejected alternatives: one sync path per plane (`mgmt/aws` beside
`mgmt/aws-crossplane`, duplicating the tree), toggling Kustomizations with
Flux `suspend` or patches (does not remove what is installed, unclear prune
behavior), and having the CLI rewrite manifests from its configuration file
(breaks golden rule 1: edit YAML in Git, never mutate).

Each environment also gets a `resource-plane = "native" | "crossplane"` key
in `bootstrap.toml`, defaulting to `native`. It is the operator-facing place
to read the choice, not a second source of truth:
`tests/test-bootstrap-config.py` fails when the key and the selector
directory disagree.

An exclusivity test renders each environment and fails when both planes'
resources are present, or when the CRDs of a native operator and the
Crossplane provider for the same cloud would both be installed (for example
`*.services.k8s.aws` alongside `*.aws.upbound.io`). On Azure the test covers
only the resource-plane ASO resources, not the ASO that CAPZ bundles in
`capz-system` for cluster lifecycle.

## Switching a running environment

The plane is chosen when an environment is set up. Changing the selector on a
running environment is not supported yet: with `prune: true`, removing a
plane deletes its custom resources, and ACK, ASO and Config Connector delete
the cloud object when its custom resource is deleted. Changing the selector
therefore destroys the environment's buckets, databases and roles.

A safe switch needs retain or orphan deletion policies on the old plane and
adoption of the existing cloud objects by the new plane before anything is
pruned. That procedure is slice 9
([#455](https://github.com/polarsquad/krops/issues/455)). The cloud
`crossplane` planes may merge before it, because they are selected only at
setup and this page states the limitation.

## Ownership

The [#224](https://github.com/polarsquad/krops/issues/224) rule (never point
two reconcilers at the same objects) applies to composition:

- Flux applies and prunes only what is in Git: Crossplane itself, composition
  functions, provider packages and their configuration, XRDs, Compositions
  and XRs.
- Crossplane creates and deletes only composed resources.
- Flux never applies a composed resource, and Crossplane never manages a Flux
  object.

Flux `postBuild` substitution does not reach composed resources, because Flux
never sees them. Per-cluster values (`${CLUSTER_NAME}`, `${AWS_REGION}`,
account IDs) are substituted into the XR, and the Composition patches them
through to the composed resources.

Resources that Crossplane produces at runtime and that must reach a workload
cluster, such as connection secrets, still have to be declared in Git and
drift-corrected, with no imperative copy step (slice 7).

## Credentials

Each plane follows its environment's existing credential model. Crossplane
adds no new secret-at-rest model:

- **AWS:** static SOPS credentials on the management cluster, as
  `mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml` provides
  for ACK today. See [AWS authentication and IAM](aws-iam.md).
- **Azure:** workload identity, as in
  `mgmt/azure/infrastructure/aso-workload-identity/`. See
  [Azure environment](azure.md).
- **GCP:** Workload Identity Federation through the `krops` pool, as the
  `kcc-wif-credentials` Secret does. See [GCP environment](gcp.md).

## Platform API

On a cloud environment, the `crossplane` plane ships XRDs, Compositions and
XRs that reproduce what the native plane creates for each workload cluster
(on AWS, the contents of `mgmt/aws/infrastructure/workload-resources/`: the
bucket, the DB instance and the reader role). The two planes must be
functionally equivalent for the same inputs, including per-cluster naming and
the `managed-by` tag lineage, so the teardown sweeps stay valid whichever
plane created a resource. A test checks render parity.

## Local-host

Local-host has no cloud, so its `crossplane` plane is a worked example:
Crossplane, a composition function, and an XRD, Composition and XR that
compose a plain `Namespace` and `ConfigMap`, plus the aggregated RBAC that
lets Crossplane create them. The XR reaches Ready without any cloud.

It is opt-in through the local-host `resource-plane` key, not part of every
bootstrap. A dedicated CI matrix leg selects it, so the default local-host
bootstrap stays as fast as it is today.

## Out of scope

- Crossplane composing CAPI `Cluster` objects or anything else that belongs
  to cluster lifecycle.
- Choosing a plane per resource kind.
- Air-gap: Crossplane packages come from `xpkg.crossplane.io` and are not in
  the Zarf bundle. Adding them needs its own `area:airgap` issue.

## Decided open questions

The proposal left these open for review:

| Question | Decision |
|---|---|
| Plane per environment or per resource kind | Per environment |
| Local-host Crossplane always on or opt-in | Opt-in through the `resource-plane` key, on a CI matrix leg |
| What the local-host XR composes | A plain `Namespace` and `ConfigMap` |
| Must the guarded switch (slice 9) land before the AWS plane (slice 5) | No: the plane is chosen at setup only, and this page says so until slice 9 lands |
| Provider packages per cloud (Upbound families or community equivalents) | Deferred to slice 5, after a check of licensing, package sizes and release cadence |
| Air-gap coverage | Out of scope, as above |

Live verification of the cloud planes depends on sandbox accounts
([#239](https://github.com/polarsquad/krops/issues/239)). Until a live run is
possible, those slices are CI-render only.

## Slices

| # | Slice | Issue |
|---|---|---|
| 1 | This decision record | [#446](https://github.com/polarsquad/krops/issues/446) |
| 2 | Selector scaffolding with only the `native` plane (AWS first), `resource-plane` key, exclusivity test | [#447](https://github.com/polarsquad/krops/issues/447) |
| 3 | Crossplane core and a composition function, local-host `crossplane` plane | [#448](https://github.com/polarsquad/krops/issues/448) |
| 4 | Local-host worked example: XRD, Composition, XR, aggregated RBAC | [#449](https://github.com/polarsquad/krops/issues/449) |
| 5 | AWS `crossplane` plane: providers, ProviderConfig, credentials | [#450](https://github.com/polarsquad/krops/issues/450) |
| 6 | AWS platform API with render parity against the ACK objects | [#451](https://github.com/polarsquad/krops/issues/451) |
| 7 | Git-declared connection-secret delivery to workload clusters | [#452](https://github.com/polarsquad/krops/issues/452) |
| 8 | GCP plane and platform API | [#453](https://github.com/polarsquad/krops/issues/453) |
| 8 | Azure plane and platform API | [#454](https://github.com/polarsquad/krops/issues/454) |
| 9 | Guarded switch of a running environment between planes | [#455](https://github.com/polarsquad/krops/issues/455) |
