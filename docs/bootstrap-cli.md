# The bootstrap CLI (`krops-bootstrap`)

The imperative part of krops lives in one Rust binary under
[`bootstrap-rs/`](../bootstrap-rs/). It implements the initial bootstrap, the
default CAPI pivot into the self-managed management cluster, and teardown.
After bootstrap and pivot finish, Flux owns the declared state until teardown.

The binary is a behavioral port of `bootstrap.sh`, `pivot.sh`, and
`teardown.sh`. It preserves their step order, progress messages, environment
interface, and safety guards, with two deliberate upgrades:

- **Reruns are safe by default.** An existing healthy `mgmt` kind cluster is
  reused and each bootstrap or pivot step is idempotent. Pass `--recreate` to
  delete and rebuild the kind cluster instead.
- **Typed process execution.** Tool arguments are passed as argv entries,
  secrets travel through stdin or the environment, and the GitHub and registry
  HTTP checks use reqwest with explicit timeouts.

## Distribution and build

The primary distribution is the toolbox image,
`ghcr.io/polarsquad/krops-toolbox`. It contains `krops-bootstrap` and the
pinned tools required by the lifecycle. See [Operations](./operations.md) for
the container invocation and host runtime contract.

The `toolbox-release` workflow runs on `v*` tags. It requires the tag to match
`bootstrap-rs/Cargo.toml`, builds Linux amd64 and arm64 images, publishes
`X.Y.Z`, `X.Y`, and stable `latest` tags, signs the image with GitHub OIDC, and
attaches a Syft SPDX JSON SBOM attestation. The `bootstrap-rs` CI workflow also
builds and smokes the arm64 image when its inputs change.

No semver release has been published yet. Build the current checkout as shown
in [Operations](./operations.md) until the first tag completes the workflow.

Build the CLI directly for native development:

```sh
cd bootstrap-rs
cargo build --locked
cd ..
./bootstrap-rs/target/debug/krops-bootstrap --help
./bootstrap-rs/target/debug/krops-bootstrap teardown --help
```

Run the binary from the repository root so its default `./bootstrap.toml` path
resolves. Set `BOOTSTRAP_CONFIG` when running from another directory.

CI runs `cargo fmt --check`, clippy with warnings denied, a locked build, and
the test suite. The toolchain is pinned in `bootstrap-rs/rust-toolchain.toml`;
crate dependencies are locked in `Cargo.lock`.

## Interface

```text
krops-bootstrap [OPTIONS] [PROFILE] [COMMAND]
krops-bootstrap teardown [PROFILE]
```

Common examples:

```sh
krops-bootstrap                         # aws bootstrap, then pivot
krops-bootstrap local-host              # local-host bootstrap, then pivot
krops-bootstrap --recreate local-host   # rebuild the bootstrap kind cluster
krops-bootstrap teardown                # aws teardown
krops-bootstrap teardown local-host     # local-host teardown
```

- `PROFILE` is the CLI's retained positional name. Its value names a section
  under `[environments.*]` in
  [`bootstrap.toml`](../bootstrap.toml). The checked-in environments are `aws`,
  `local-host`, and `local-talos`.
- A non-empty `KROPS_PROFILE` overrides the positional profile. If
  neither is set, `bootstrap.default-environment` from `bootstrap.toml` is
  used.
- `--recreate` applies to bootstrap only. Teardown is a subcommand and keeps
  its script-compatible controls in environment variables.
- There is no pivot subcommand. Pivot is the default exit from bootstrap, and
  rerunning the normal command resumes an interrupted bootstrap or pivot.

## Repository configuration

Repository-owned cluster names, paths, chart versions, provider manifests, and
teardown targets live in [`bootstrap.toml`](../bootstrap.toml). The binary
retains generic fallback defaults and sequence-level contracts. It reads
`./bootstrap.toml` by default; `BOOTSTRAP_CONFIG` selects another path.

Runtime environment variables take precedence where an override exists.
`mise run validate` parses the file and cross-checks its chart pins and
teardown names against the Git manifests. Renovate updates the annotated chart
pins together with their declarative counterparts. See
[Dependencies](./dependencies.md).

- `pivot-sops-secrets` (optional, list): SOPS-encrypted manifests the pivot
  decrypts with `SOPS_AGE_KEY_FILE` (defaults to `AGE_KEY_FILE`) and applies to
  the target before `clusterctl move`. Used by `azure` for the ASO/CAPZ
  credential Secret that moved objects reference by name.
- `pivot-manifests` (optional, list): plain (unencrypted) manifests applied to
  the target before `clusterctl move`, after the provider CRs. The
  workload-identity replacement for `pivot-sops-secrets`; used by `azure` for
  the secret-free `aso-credentials` Secret (issue #236). `${VAR}` placeholders
  are substituted from the ConfigMaps in the Flux namespace of the bootstrap
  cluster (the values its Flux reconciled, e.g. `azure-vars`); the pivot fails
  naming any placeholder no ConfigMap provides.
- `post-kind-create-task` (optional, string): name of a mise task in the
  active profile (`mise.<env>.toml`) run once the kind bootstrap cluster
  exists, on both the create and healthy-reuse paths; a non-zero exit aborts
  the bootstrap. `azure` uses it for Arc OIDC federation (issue #236).
- `teardown.manual` (optional, string): when set, `krops-bootstrap teardown`
  refuses to run for that environment and prints the text. `azure` uses it
  until the live acceptance run defines the Azure orphan sweep.

## Bootstrap and pivot controls

| Variable | Default | Used by |
|---|---|---|
| `BOOTSTRAP_CONFIG` | `./bootstrap.toml` | Repository configuration path |
| `KROPS_PROFILE` | positional profile, then `bootstrap.default-environment` (`aws` checked in) | Environment selection |
| `REGISTRY_PORT` | `5001` | Local-host registry host port |
| `REGISTRY_READY_RETRIES` | `120` | Local-host registry readiness attempts |
| `LOCAL_RECONCILE_TIMEOUT` | `15m` | Local-host management and workload reconciliation waits |
| `CONTAINER_ENGINE` | auto-detect Docker, then Podman | kind and registry engine |
| `GIT_REPO_URL` | required for `aws` and `local-talos` | Management Flux Git source |
| `GITHUB_TOKEN` | required for `aws` and `local-talos` | PAT with read access to the repository |
| `GITHUB_USER` | `git` | Basic-auth username paired with the PAT |
| `AGE_KEY_FILE` | `age.agekey` | SOPS age private key loaded into `sops-age` |
| `AGE_PUBLIC_KEY` | derived from `AGE_KEY_FILE` | Public key override during secret creation |
| `OCI_REPOSITORY` / `OCI_TAG` | `krops` / `latest` | Local-host OCI artifact name |
| `BOOTSTRAP_PIVOT` | `1` | Any value other than literal `1` skips pivot |
| `MGMT_KUBECONFIG` | `~/.kube/krops-mgmt.yaml` | Exported management kubeconfig for native runs |
| `MGMT_READY_TIMEOUT` | `40m` for aws, `15m` for local-host, `30m` for local-talos (PXE install + first Talos boot) | Management cluster provisioning wait |
| `MGMT_POLL_INTERVAL` | `10` seconds | Management cluster provisioning poll |
| `BOOTSTRAP_KUBECONTEXT` | config value `kind-mgmt` | Source context required by pivot |
| `PIVOT_SKIP_DELETE` | `0` | Literal `1` keeps kind after a successful pivot |

The toolbox runtime adds three contracts:

- `KROPS_TOOLBOX=1` enables internal kind networking and disables host-only CAPD
  endpoint rewrites.
- `ENGINE_SOCK` names the engine socket path as seen by the daemon. The wrapper
  resolves it for Docker Desktop, Docker contexts, rootful or rootless Podman,
  and `podman machine`.
- `KUBECONFIG` must name one writable file, not a colon-separated list. The
  wrapper uses `/workspace/.kube/kind.yaml`.

The container reaches the local registry at `krops-registry:5000`. Its
`/root/.kube` mount makes the management kubeconfig persist on the host as
`./.kube/krops-mgmt.yaml`. The wrapper's environment allowlist and override
limitations are documented in [Operations](./operations.md#toolbox-container-primary-interface).

## What bootstrap and pivot do

1. **Preflight:** validate the environment and required tools, select a running
   container engine, and perform the GitHub token/age-key checks for
   GitHub-synced environments (`aws`, `local-talos`). Native runs require
   `kind`, `helm`, `kubectl`, `clusterctl`, and `mise`; OCI-synced
   environments (`local-host`) also require `flux` and `curl`.
2. **Bootstrap kind:** create or reuse `mgmt`, start the local registry for
   local-host, install the Flux Operator, create the Git and SOPS secrets
   (GitHub-synced environments) or publish the local OCI artifact, install
   the `FluxInstance`, and watch reconciliation.
3. **Pivot by default:** wait for the CAPI-managed management cluster, export
   its kubeconfig, install cert-manager, the CAPI operator, and provider CRs at
   the versions declared in `bootstrap.toml`, suspend Flux in kind, run
   `clusterctl move`, unpause the moved clusters, seed Flux on the target, and
   delete kind after the safety checks pass.

If a phase fails, fix the cause and rerun the same command. `clusterctl move`
is re-runnable, and kind remains authoritative until the final deletion.
Recovery details and the warning against deleting moved CAPI objects are in
[Pivot recovery](./operations.md#pivot-recovery).

## Teardown controls and behavior

```sh
krops-bootstrap teardown [PROFILE]
```

| Variable | Default | Effect |
|---|---|---|
| `AWS_ONLY` | `0` | Literal `1` skips Kubernetes steps and runs only the AWS orphan sweep; invalid with `local-host` and `local-talos` |
| `FORCE_KIND_DELETE` | `0` | Literal `1` removes the controller host even when CAPI cluster deletion was not confirmed |
| `CLUSTER_DELETE_TIMEOUT` | `1200` seconds | CAPI cluster deletion wait (aws workloads, local-talos management) |
| `PROVIDER_DELETE_TIMEOUT` | `300` seconds | CAPI provider deletion wait |
| `MGMT_KUBECONFIG` | `~/.kube/krops-mgmt.yaml` | Post-pivot controller-host kubeconfig |

Teardown checks required tools before mutation:

| Mode | Required tools |
|---|---|
| `local-host` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| `local-talos` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| normal `aws` | `kind`, `helm`, `kubectl`, `xargs`; AWS CLI is optional and its absence skips the orphan sweep |
| `AWS_ONLY=1` | AWS CLI only |

Teardown discovers where the CAPI controllers run: the pre-pivot kind cluster,
the post-pivot self-managed management cluster, or no reachable cluster. It
then preserves the shell implementation's reverse-order and best-effort
cleanup semantics.

- `local-host`: suspend the workload Kustomization, delete the CAPD workload
  cluster and wait for its containers to disappear, remove kind or the
  self-managed management containers, then remove the local registry.
- `local-talos`: suspend Flux and delete every CAPI Cluster, the management
  cluster included; the deletion IS the release, and CAPT returns the
  machine's Hardware to the Tinkerbell pool. The machine is never wiped: it
  keeps running Talos for the operator. `AWS_ONLY=1` is rejected because
  there is no AWS orphan sweep for operator-owned hardware.
- `aws`: suspend Flux, delete and wait for workload CAPI clusters, run the AWS
  orphan sweep for both workloads and the self-managed management cluster,
  remove CAPI providers and bootstrap Helm releases when the controller host is
  still reachable, and enforce the controller-host deletion guard. The sweep
  covers pod identity associations, nodegroups, EKS clusters, RDS, CAPA-tagged
  VPC resources, versioned S3 buckets, IAM roles and users, and the
  `clusterawsadm` CloudFormation stack.

`AWS_ONLY=1` is the recovery path when only AWS cleanup remains. A missing tool
fails preflight before mutation; in the normal AWS path, a missing AWS CLI is
reported and the orphan sweep is skipped rather than misclassified as an
empty account.

## Entry-point and parity status

`mise run bootstrap`, `mise run pivot`, and `mise run teardown` now invoke
`scripts/toolbox-run.sh`, which runs this CLI in the toolbox container. The
`pivot` task is a named resume path for the rerun-safe default lifecycle; it
does not select a separate CLI subcommand.

The three shell scripts remain as native reference and fallback paths until
full parity runs pass for all environments. Local-host bootstrap, pivot, and
post-pivot teardown have completed parity runs. AWS full-parity runs still gate
script retirement. The local-talos environment has not run end to end yet;
its acceptance run waits on operator Tinkerbell hardware (issue #105). At
this revision no semver tag has run the release workflow,
and no Podman-host acceptance run is recorded.
