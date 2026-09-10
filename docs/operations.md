# Operations

## Prerequisites

### Toolbox container (primary interface)

The toolbox image (`ghcr.io/polarsquad/krops-toolbox`) carries
`krops-bootstrap` plus every tool used by bootstrap, pivot, and teardown. It
intentionally omits development-only Go and Python toolchains and the Zarf CLI.
The host needs the repository checkout and a running Docker engine or Podman
5.5+.

No semver release has been published yet. A future matching `v*` tag runs
`.github/workflows/toolbox-release.yml`, which is configured to publish Linux
amd64 and arm64 tags `X.Y.Z`, `X.Y`, and stable `latest`, sign the image with
GitHub OIDC, and attach a Syft SPDX JSON SBOM attestation. Build the current
checkout locally:

```sh
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
export TOOLBOX_IMAGE=krops-toolbox:dev
mkdir -p .kube
```

A complete raw Docker run for `local-host` is:

```sh
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  "$TOOLBOX_IMAGE" local-host
```

This form assumes the standard `/var/run/docker.sock` daemon socket. Use the
wrapper below for Docker contexts or Podman installations with a different
host socket.

The AWS environment also needs the Git source, PAT, age key, and AWS
credentials inside the container. Source the repository `.env` so shell quotes
are removed, then pass values by name rather than putting secrets in argv:

```sh
cp .env.example .env
$EDITOR .env
set -a
. ./.env
set +a

docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  -e GIT_REPO_URL -e GITHUB_TOKEN -e GITHUB_USER \
  -e AGE_KEY_FILE -e AGE_PUBLIC_KEY \
  -e AWS_REGION -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN -e AWS_PROFILE \
  "$TOOLBOX_IMAGE"
```

Generate the age key and re-encrypt secrets for a new fork before the AWS run;
see [Secret management](./secrets.md). If credentials come from an AWS shared
configuration instead of environment variables, also mount that configuration
under `/root/.aws` and pass `AWS_PROFILE`. The local-talos environment needs
the same Git source, PAT, and age key, but no AWS credentials.

Podman socket locations differ across rootful Linux, rootless Linux, and
`podman machine`. Use the checked-in wrapper to resolve the host mount and the
socket path seen by the daemon:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
CONTAINER_ENGINE=podman TOOLBOX_IMAGE="$TOOLBOX_IMAGE" \
  scripts/toolbox-run.sh bootstrap local-host
```

The same wrapper powers `mise run bootstrap`, `mise run pivot`, and
`mise run teardown`. It detects the engine, loads every `.env` assignment with
outer quote stripping, and passes only this allowlist into the container:

- Engine and lifecycle: `CONTAINER_ENGINE`, `ENGINE_SOCK`, `KROPS_PROFILE`,
  `REGISTRY_PORT`, `OCI_REPOSITORY`, `OCI_TAG`, `BOOTSTRAP_PIVOT`,
  `PIVOT_SKIP_DELETE`
- GitHub and age: `GIT_REPO_URL`, `GITHUB_TOKEN`, `GITHUB_USER`, `AGE_KEY_FILE`,
  `AGE_PUBLIC_KEY`
- AWS: `AWS_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`

It does not pass `BOOTSTRAP_CONFIG`, `REGISTRY_READY_RETRIES`,
`LOCAL_RECONCILE_TIMEOUT`, `MGMT_KUBECONFIG`, `MGMT_READY_TIMEOUT`,
`MGMT_POLL_INTERVAL`, `BOOTSTRAP_KUBECONTEXT`, or the teardown controls
`AWS_ONLY`, `FORCE_KIND_DELETE`, `CLUSTER_DELETE_TIMEOUT`, and
`PROVIDER_DELETE_TIMEOUT`. Use a raw container run with explicit `-e` entries,
or the native CLI, when overriding those values. Direct use of the wrapper does
not require host mise.

Inside the toolbox:

- The entrypoint sets `KROPS_TOOLBOX=1` and resolves the daemon-side
  `ENGINE_SOCK` used by kind's socket mount.
- Each new toolbox container best-effort joins an existing `kind` network at
  startup. Bootstrap joins explicitly after creating kind; recreate, pivot, and
  teardown detach before deleting the bootstrap cluster.
- Kind's internal API endpoint and `krops-registry:5000` then resolve by name.
- Host-only CAPD endpoint rewrites are skipped because the recorded endpoints
  already resolve on that network.
- `KUBECONFIG` must name one writable file. The documented invocation uses
  `/workspace/.kube/kind.yaml`, and the CLI replaces it with kind's internal
  kubeconfig after creation.
- `/root/.kube` maps to the checkout's `.kube/`, so the exported management
  kubeconfig persists on the host as `.kube/krops-mgmt.yaml`.

### Verifying a toolbox release

After the first release is published, replace `X.Y.Z` in these commands with
the matching Cargo and Git tag version:

```sh
IMAGE=ghcr.io/polarsquad/krops-toolbox:X.Y.Z
IDENTITY=https://github.com/polarsquad/krops/.github/workflows/toolbox-release.yml@refs/tags/vX.Y.Z

cosign verify \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"

cosign verify-attestation \
  --type spdxjson \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"
```

### Native host path

Tool versions are pinned in `mise.toml`, which requires mise 2026.8.10 or
newer. With [mise](https://mise.jdx.dev/) installed:

```sh
mise install
```

This provides `kubectl`, `kind`, `helm`, `flux`, `clusterctl`, `go`, `sops`,
`age`, and the `zarf` CLI. The `aws` environment layers on `aws-cli` and
`clusterawsadm` (`mise -E aws install`); the `local-host` environment needs
no extra tools; the `local-talos` environment layers on `talosctl`
(`mise -E local-talos install`). Building the bootstrap CLI additionally
requires a Rust
toolchain ([rustup](https://rustup.rs/); the pin lives in
`bootstrap-rs/rust-toolchain.toml`). The lifecycle mise tasks still use the
toolbox. For a fully native run from the repository root, invoke
`./bootstrap-rs/target/debug/krops-bootstrap` or call `./bootstrap.sh`,
`./pivot.sh`, and `./teardown.sh` directly.

You also need:

- A running container engine for kind: Docker, or Podman 5.5+ (auto-detected
  at bootstrap; set `CONTAINER_ENGINE=docker|podman` to override).
  For local-host environment: the same engine is used to host the local
  container registry for OCI artifacts.

**AWS environment only:**
- A GitHub personal access token (PAT) with read access to this repository
  (fine-grained with read-only Contents permission, or classic with `repo`
  scope). The Flux Operator chart is pulled anonymously.
- AWS credentials with permission to create EKS clusters, VPCs, and IAM roles.
  For the ACK controllers the same principal additionally needs
  `iam:CreateRole`/`PutRolePolicy`/`GetRole`/`TagRole`,
  `iam:CreateUser`/`PutUserPolicy`/`GetUser`/`GetUserPolicy`/`TagUser`
  (for the `krops-reader` console user), and
  `eks:CreatePodIdentityAssociation`/`DescribePodIdentityAssociation`/
  `DeletePodIdentityAssociation`. The `rds:*` management and
  `secretsmanager:CreateSecret`/`TagResource`/`RotateSecret` permissions
  (managed master passwords) used by the workload clusters' ACK RDS
  controllers are granted through the Git-declared
  `krops-ack-rds-controller` pod-identity role; no extra static
  credentials are required for them.
- The `clusterawsadm` IAM CloudFormation stack provisioned before bootstrap and
  removed by a full AWS teardown:

  ```sh
  clusterawsadm bootstrap iam create-cloudformation-stack --region eu-north-1
  ```

**local-talos environment only:**
- The GitHub PAT and age key, as for the AWS environment: local-talos syncs
  its configuration from GitHub (a physical machine cannot reach the
  laptop-hosted OCI registry), so bootstrap seeds the same `flux-github-pat`
  and `sops-age` secrets.
- A reachable [Tinkerbell](https://tinkerbell.org/) stack on the machine's
  network, and a `Hardware` resource describing the target machine. The
  stack is operator-owned infrastructure this repository does not deploy.
  Minimal setup:
  1. Install the [tink stack](https://tinkerbell.org/docs/setup/install/)
     (Smee serving DHCP/PXE/iPXE, plus the Tinkerbell API) on a helper
     Kubernetes cluster that can reach the machine's L2 network.
  2. Create the `Hardware` CR naming the machine's MAC address and reserved
     IP; its name must match the `hardwareName` in
     `mgmt/local-talos/clusters/management/cluster.yaml` (`talos-mgmt-01`
     as checked in).
  3. Annotate the `Hardware` CR with
     `hardware.tinkerbell.org/installer-image: <image URL>` when the machine
     needs a non-default Image Factory schematic (system extensions, custom
     kernel args). The pinned CAPT fork mirrors the annotation into
     `status.installerImage` and CABPT injects it as the Talos
     `machine.install.image`; without the annotation the default schematic
     is used.
  4. Set the machine to PXE-boot from the network Smee serves.
- Two site-specific values in
  `mgmt/local-talos/clusters/management/cluster.yaml` before the first run:
  `spec.controlPlaneEndpoint.host` (the machine's stable IP) and the
  `TinkerbellMachineTemplate` `hardwareName` (the Hardware CR name).

### AWS service quotas (common first-run blockers)

| Quota | Code | Needed | Why |
|---|---|---|---|
| EC2-VPC Elastic IPs (per region) | `L-0263D0A3` | ≥ 3 free | One EIP per NAT gateway (3 AZs) |

Request increases with
`aws service-quotas request-service-quota-increase --service-code ec2 --quota-code <code> --desired-value <n> --region <region>`.

## Configuration

Copy the env template and fill it in. `mise` loads `.env` automatically and it
is gitignored:

```sh
cp .env.example .env
$EDITOR .env
```

The Flux Operator chart is pulled anonymously for all environments. The
GitHub PAT is needed for the AWS and local-talos environments (both sync
from GitHub); AWS credentials and `AWS_REGION` are only needed with the AWS
environment.

Repository-owned lifecycle configuration lives in `bootstrap.toml`. It defines
the environment names, sync paths, management clusters, imperative chart
versions, provider manifests, and teardown targets. `krops-bootstrap` reads it
from the working directory unless `BOOTSTRAP_CONFIG` selects another path.
Runtime environment variables take precedence over configurable defaults, and
`mise run validate` cross-checks the file against the manifests.

## Bootstrap

```sh
mise run bootstrap                 # AWS environment (toolbox container via scripts/toolbox-run.sh)
mise -E local-host run bootstrap   # local-host environment
mise -E local-talos run bootstrap  # local-talos environment
```

Azure: see [azure.md](./azure.md) for the subscription prep step that precedes `mise -E azure run bootstrap`.

> Before the first AWS bootstrap, generate an age key for SOPS. See
> [Secret management](./secrets.md) for native and toolbox-only setup.

This initial imperative phase performs these steps:

1. Creates the `mgmt` kind cluster.
2. Installs the Flux Operator (Helm).
3. Creates the `flux-github-pat` secret (for Git access) and the `sops-age`
   secret (the age private key Flux uses to decrypt SOPS-encrypted secrets).
4. Installs a `FluxInstance` that syncs `mgmt/aws/` and hands off to GitOps.
5. Pivots: moves the CAPI inventory into the self-managed management cluster
   and deletes the kind cluster (see [Pivot recovery](#pivot-recovery)).

Everything downstream (providers, EKS clusters, workload Flux instances, the
ACK operator, IAM role, pod identity bindings, and S3 buckets) reconciles
from Git with no further manual steps.

The local-host environment performs the cluster, Flux Operator, and FluxInstance
steps in the `mgmt` management cluster, but does not create GitHub or SOPS
secrets. Instead, it bootstraps a local Docker Registry container (`registry:2`)
running on the host machine (accessible at `localhost:5001` by default),
publishes the `mgmt/local-host/` and `workload/local-host/` folders as the
initial `krops:latest` OCI
artifact, and configures Flux to reconcile that path from the artifact. Flux
then installs the CAPI core, kubeadm, and Docker infrastructure providers and
creates `local-workload`, a one-control-plane/one-worker Kubernetes cluster in
containers. The management cluster then installs a Flux Operator and
FluxInstance on `local-workload`; that instance reconciles
`workload/local-host/` from the same OCI artifact. CAPD is intended for local
development and testing, not production.

Together, these stages make `local-host` an end-to-end environment: one command
bootstraps the management control plane, publishes and reconciles the OCI
configuration, provisions a workload cluster through CAPI, installs a distinct
Flux control plane on that cluster, and reconciles a reachable Podinfo workload.
It exercises the complete cluster-to-workload GitOps lifecycle locally; only
the AWS-specific infrastructure and ACK resources are outside its scope.

The local-talos environment performs the same kind bootstrap and Flux
handoff, but syncs from GitHub like AWS and pivots onto operator-provided
bare metal: CAPT PXE-boots the machine named by the Tinkerbell `Hardware`
resource, installs Talos with the image resolved from the Hardware's
`hardware.tinkerbell.org/installer-image` annotation, and the single-node
Talos cluster takes over as the management cluster. The management-ready
wait defaults to 30 minutes (`mgmt-ready-timeout` in `bootstrap.toml`) to
cover the PXE install and first Talos boot. Scope fence: management-only.
There is no workload cluster and no CNI addon; Talos ships flannel. Verify
after the pivot with `mise -E local-talos run kubeconfigs` and
`kubectl get nodes`: one Ready node on the committed endpoint, schedulable
for the full management plane (`allowSchedulingOnControlPlanes`).

**OCI Registry (local-host environment only):**
- Provides a local container registry for development workflows
- Enables developers to build and push OCI artifacts from git checkouts
- Flux syncs and deploys the OCI artifact without external dependencies
- Configurable via `REGISTRY_PORT` env var (defaults to 5001)
- Idempotent: restarts if stopped, no action needed if already running

**Workflow:**
```bash
# Republish the local management and workload folders after making changes
mise -E local-host run oci-push

# Optional overrides
OCI_REPOSITORY=my-config OCI_TAG=v1 \
  mise -E local-host run oci-push

# FluxInstance pulls and reconciles the artifact's mgmt/local-host kustomization.
# The bootstrap configures kind's containerd to mirror localhost:5001 to the
# registry's in-cluster endpoint, krops-registry:5000.
```

The artifact contains only `mgmt/local-host/` and `workload/local-host/`,
preserving those directory paths when the artifact is pulled. Keeping the
source scope narrow also prevents local credentials and age private keys
elsewhere in the repository from being packaged.

The AWS and local-talos environments add the GitHub/SOPS secrets and
configure the FluxInstance to sync `mgmt/aws/` or `mgmt/local-talos/`
respectively.

Watch reconciliation after a toolbox run with the persisted management
kubeconfig:

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
flux get kustomizations --watch
```

A native CLI or shell run writes the same context to
`~/.kube/krops-mgmt.yaml` unless `MGMT_KUBECONFIG` overrides it.

For the local-host environment, export and verify the CAPD workload kubeconfig
after `docker-workload-cluster` reports Ready:

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"  # toolbox management cluster
mise -E local-host run kubeconfigs
KUBECONFIG=local-workload.kubeconfig kubectl get nodes
```

The local workload Flux instance installs Podinfo from its OCI Helm chart. Open
it in a host browser by running the port-forward task in a separate terminal:

```sh
mise -E local-host run podinfo-port-forward
```

Then browse to <http://localhost:9898>. Press Ctrl-C to stop forwarding.

The workload uses Kubernetes v1.37.0. A CAPI ClusterResourceSet installs a
pinned Kindnet daemon as its CNI before the Flux addons are delivered.
The management cluster needs access to the container-engine socket, which
the bootstrap mounts automatically.

Local-host bootstrap first waits for the management `flux-apps` Kustomization
without printing transient `Unknown` status rows. After `flux-apps` becomes
Ready, it connects to `local-workload`, streams the workload Flux controller
error logs, and returns after the workload root Kustomization becomes Ready.
Filtering the workload stream to errors avoids showing normal startup retries
and advisory messages as apparent failures. Each readiness wait defaults to 15
minutes and can be changed with `LOCAL_RECONCILE_TIMEOUT` in a raw container or
native run. The current wrapper does not forward that override.

EKS clusters typically take 15–25 minutes to come up; node groups and the
downstream app chain follow a few minutes after.

### Verifying the full chain

For the local-host end-to-end chain:

```sh
mise -E local-host run bootstrap
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
mise -E local-host run kubeconfigs
KUBECONFIG=local-workload.kubeconfig flux get all --all-namespaces
mise -E local-host run podinfo-port-forward  # browse to http://localhost:9898
```

Bootstrap does not return until the management and workload root
Kustomizations are Ready. The final port-forward verifies that the workload
Flux instance successfully delivered the application.

For the AWS chain:

```sh
# Management cluster after a toolbox run
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
kubectl get kustomizations -n flux-system            # all Ready
kubectl get clusters.cluster.x-k8s.io -A             # Provisioned
kubectl get roles.iam.services.k8s.aws -n ack-system
kubectl get podidentityassociations.eks.services.k8s.aws -n ack-system

# Workload clusters: export kubeconfigs first
#   mise -E aws run kubeconfigs && export KUBECONFIG=~/.kube/krops-workloads.yaml
#   kubectl config use-context eu-north-1-workload   (or eu-west-1-workload)
kubectl get kustomizations -n flux-system            # aws-operators, s3-buckets, rds-instances, iam-roles

# AWS
aws s3api get-bucket-encryption    --bucket krops-<account>-eu-north-1-workload-data
aws s3api get-public-access-block  --bucket krops-<account>-eu-north-1-workload-data
```

## Pivot recovery

Bootstrap ends with a pivot: the CAPI inventory moves from the local `mgmt`
kind cluster into the self-managed management cluster, and the kind cluster
is deleted. `mise run bootstrap` runs the pivot by default
(`BOOTSTRAP_PIVOT=0` opts out). `mise run pivot` starts the same rerun-safe CLI
and resumes through the pivot; there is no separate pivot subcommand. See
[the bootstrap CLI](./bootstrap-cli.md) for its interface and controls.

`clusterctl move` is re-runnable: an object is deleted from the source kind
cluster only after it was created on the target, so kind stays authoritative
until the final kind deletion. If a pivot phase fails:

1. Fix the reported cause.
2. Re-run the pivot (`mise run pivot`, or rerun bootstrap) from a checkout of
   the revision you want self-managed, normally `main`. A native run must use
   the bootstrap context (`kind-mgmt`; `BOOTSTRAP_KUBECONTEXT` overrides).
   Toolbox mode selects kind's internal kubeconfig automatically. The CLI
   reuses an existing healthy `mgmt` kind cluster on rerun.
3. Set `PIVOT_SKIP_DELETE=1` to keep the kind bootstrap cluster around for
   inspection once the pivot completes.

> Note: never delete moved `Cluster`, `AWSManaged*`, `MachinePool`, or
> `Dev*` objects on the target cluster to work around a failure. The CAPI
> providers treat deletion as deprovisioning and destroy the real
> infrastructure (EKS clusters, VPCs, IAM roles; CAPD the local containers).
> Re-run the move instead. Only pure-config duplicates without a move hook
> (for example an identity created by hand during a failed install) are safe
> to delete.

The management kubeconfig is written to `MGMT_KUBECONFIG`, with context
`krops-mgmt`. Native runs default to `~/.kube/krops-mgmt.yaml`; the toolbox
mount makes its `/root/.kube/krops-mgmt.yaml` appear on the host as
`./.kube/krops-mgmt.yaml`.

## Teardown

The mise tasks run the Rust subcommand in the toolbox:

```sh
mise run teardown                 # aws
mise -E local-host run teardown   # local-host
mise -E local-talos run teardown  # local-talos
```

Azure teardown is manual; the CLI prints the steps.

Native equivalents are `krops-bootstrap teardown [PROFILE]` and the retained
`./teardown.sh` reference path. The positional profile selects an environment
from `bootstrap.toml`. Teardown reads resource names and targets from
`bootstrap.toml` and discovers where the CAPI controllers are running:

- the `mgmt` kind cluster before pivot
- the exported self-managed management kubeconfig after pivot
- no reachable Kubernetes controller host, which falls back to AWS orphan
  cleanup for the AWS environment

The main controls keep the shell interface:

| Variable | Default | Effect |
|---|---|---|
| `AWS_ONLY` | `0` | Literal `1` runs only the AWS orphan sweep; invalid with `local-host` and `local-talos` |
| `FORCE_KIND_DELETE` | `0` | Literal `1` overrides the final controller-host deletion guard |
| `CLUSTER_DELETE_TIMEOUT` | `1200` seconds | CAPI cluster deletion wait (aws workloads, local-talos management) |
| `PROVIDER_DELETE_TIMEOUT` | `300` seconds | CAPI provider deletion wait |
| `MGMT_KUBECONFIG` | native: `~/.kube/krops-mgmt.yaml` | Post-pivot controller-host kubeconfig |

The hard preflight depends on the mode:

| Mode | Required tools |
|---|---|
| `local-host` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| `local-talos` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| normal `aws` | `kind`, `helm`, `kubectl`, `xargs`; a missing AWS CLI skips the orphan sweep |
| `AWS_ONLY=1` | AWS CLI only |

A required-tool failure happens before mutation. The wrapper does not forward
the four teardown controls in the table above; use a raw container invocation
with explicit `-e` entries or a native run for recovery overrides.

For `local-host`, teardown suspends the workload Kustomization, deletes the
CAPD workload cluster, waits for its containers to disappear, removes either
the pre-pivot kind cluster or the post-pivot self-managed management
containers, and removes `krops-registry` last.

For `local-talos`, teardown suspends Flux and deletes every CAPI Cluster,
the management cluster included: the deletion IS the release, and CAPT
returns the machine's `Hardware` entry to the Tinkerbell pool. It waits for
the deprovision to complete, then deletes the kind bootstrap cluster if the
pivot has not run yet. The machine itself is never wiped: it keeps running
Talos for the operator to re-use or PXE-boot fresh. There is no orphan
sweep; the environment owns no cloud resources.

For `aws`, teardown suspends Flux, deletes every workload CAPI Cluster while
leaving the management Cluster object alone, and waits before touching the
controller host. It then runs a best-effort AWS sweep for both workload
regions and the self-managed management cluster. The sweep removes pod
identity associations, nodegroups, EKS control planes, orphaned RDS instances,
CAPA-tagged VPC resources in dependency order, versioned S3 buckets, CAPA and
ACK IAM roles, the `krops-reader` user, and the `clusterawsadm`
CloudFormation stack. It removes CAPI providers and bootstrap Helm releases
when the controller host remains reachable.

The controller-host guard prevents removal while CAPI workload deletion is
unconfirmed. Do not bypass it unless you accept orphaned infrastructure. If a
management cluster is already unreachable, rerun with `AWS_ONLY=1` to make the
recovery mode explicit. A missing AWS CLI in normal AWS mode is reported and
the orphan sweep is skipped; `AWS_ONLY=1` requires the CLI and fails preflight
without it.

ACK resources can survive if their workload cluster disappears before their
custom resources finish deleting. The explicit sweep is what removes those
orphans, including both workload regions and the self-managed management
cluster.

## Validation

Run the repository validation before pushing:

```sh
mise run validate
```

The task checks shell syntax for the retained lifecycle scripts, runs the
`bootstrap.toml` manifest cross-check, and builds every kustomize overlay under
`mgmt/` and `workload/`.

`.github/workflows/validate.yml` separately builds every overlay, runs the
Renovate air-gap digest and managed-pin coverage tests, cross-checks
`bootstrap.toml`, and lints YAML on pushes to `main` and on pull requests.
`.github/workflows/bootstrap-rs.yml` runs Rust format, clippy, build, and tests,
then builds and smokes the toolbox image when its inputs change.
