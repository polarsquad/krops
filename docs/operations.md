# Operations

## Prerequisites

### Toolbox container (primary interface)

The toolbox image (`ghcr.io/polarsquad/krops-toolbox`) carries
`krops-bootstrap` plus every tool used by bootstrap, pivot, and teardown. It
intentionally omits development-only Go and Python toolchains and the Zarf CLI.
The host needs the repository checkout and a running Docker engine or Podman
5.5+.

The toolbox image is published to `ghcr.io/polarsquad/krops-toolbox` for
Linux amd64 and arm64 as `X.Y.Z`, `X.Y`, and stable `latest`, signed with
GitHub OIDC and carrying a Syft SPDX JSON SBOM attestation. The
`toolbox-release` workflow publishes those tags from a matching `v*` tag.
Use a published tag, or build the current checkout only for unreleased
changes:

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

The wrapper is the lifecycle entry point for every environment:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap aws
```

`KROPS_PROFILE` (or the positional profile argument after the lifecycle
verb, which reaches `krops-bootstrap`) selects the environment. It loads
every `.env` assignment with outer quote
stripping before detecting the engine or resolving a socket for it, so a
`.env`-selected `CONTAINER_ENGINE` takes effect from the start (issue #257).
An already-exported variable is left alone, so process environment wins over
`.env` (the same precedence as mise's `env_file`, for the helper tasks). It
then passes only this
allowlist into the container:

- Engine and lifecycle: `CONTAINER_ENGINE`, `ENGINE_SOCK`, `KROPS_PROFILE`,
  `REGISTRY_PORT`, `OCI_REPOSITORY`, `OCI_TAG`, `BOOTSTRAP_PIVOT`,
  `PIVOT_SKIP_DELETE`
- GitHub and age: `GIT_REPO_URL`, `GITHUB_TOKEN`, `GITHUB_USER`, `AGE_KEY_FILE`,
  `AGE_PUBLIC_KEY`
- AWS: `AWS_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`

`CLOUDSDK_CONFIG` is forwarded twice: once through the GCP allowlist entry
(an operator-set value, if any), then again as an explicit `-e
CLOUDSDK_CONFIG=/workspace/.gcloud` appended after it. The container engine
takes the last value for a repeated `-e` key, so the explicit one wins by
design: the toolbox always uses the repo-local `.gcloud/` directory (inside
the `/workspace` mount), shared with the host gcp session
([gcp.md](./gcp.md)), never an operator override. Keep the two in sync if
the mount path ever changes.

It does not pass `BOOTSTRAP_CONFIG`, `REGISTRY_READY_RETRIES`,
`LOCAL_RECONCILE_TIMEOUT`, `MGMT_KUBECONFIG`, `MGMT_READY_TIMEOUT`,
`MGMT_POLL_INTERVAL`, `BOOTSTRAP_KUBECONTEXT`, or the teardown controls
`AWS_ONLY`, `FORCE_KIND_DELETE`, `CLUSTER_DELETE_TIMEOUT`, and
`PROVIDER_DELETE_TIMEOUT`. Use a raw container run with explicit `-e` entries
when overriding those values.

Inside the toolbox:

- The entrypoint sets `KROPS_TOOLBOX=1` and execs `krops-bootstrap`, which
  owns engine detection, the daemon-side `ENGINE_SOCK` used by kind's socket
  mount, and kind-network attach/detach (issue #256).
- Bootstrap joins the `kind` network explicitly after creating (or reusing)
  the cluster; recreate, pivot, and teardown detach before deleting the
  bootstrap cluster; teardown re-joins first if it needs the internal API
  endpoint.
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

### AWS service quotas (common first-run blockers)

| Quota | Code | Needed | Why |
|---|---|---|---|
| EC2-VPC Elastic IPs (per region) | `L-0263D0A3` | ≥ 6 free in `eu-north-1`, ≥ 3 free in `eu-west-1` | One EIP per NAT gateway (3 AZs): two clusters in `eu-north-1` (management + workload), one in `eu-west-1` |
| VPCs per region | `L-F678F1CE` | 8 in `eu-north-1` (raised from the default 5) | One VPC per cluster plus pre-existing non-krops VPCs. e2e account 120392301094: `eu-north-1` quota raised to 8 (5 in use, headroom 3), `eu-west-1` at 3/5 (headroom 2) |

The check is per region, and the default regional limit is 5, so a clean
account stalls mid-run on the second `eu-north-1` cluster. Request the
increase before the first run with
`aws service-quotas request-service-quota-increase --service-code ec2 --quota-code <code> --desired-value <n> --region <region>`
(for VPCs use `--service-code vpc`).

### E2E account budget

The e2e AWS account 120392301094 carries a monthly cost budget
`krops-e2e-monthly` with a $200 ceiling. Notifications publish to the SNS
topic `krops-e2e-budget-alerts` (us-east-1) at 80% forecasted and 100%
actual spend, and the topic's email subscription delivers them to
joseph.shriner@polarsquad.com, the escalation path for budget alerts. The
email subscription only activates after the SNS confirmation email is
accepted.

### local-talos prerequisites

In addition to the PAT and age key shared with the AWS environment
(local-talos syncs its configuration from GitHub, so bootstrap seeds the same
`flux-github-pat` and `sops-age` secrets), the `local-talos` environment
needs:

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
  3. Customize the installer image through `spec.imageFactory` on the
     TalosConfig (declared under `controlPlaneConfig` in
     `mgmt/local-talos/clusters/management/cluster.yaml`): the block mirrors
     the Image Factory schematic (official system extensions, extra kernel
     args, an SBC overlay, and the bootloader). CABPT v0.8.x resolves the
     block against the Image Factory API and renders
     `machine.install.image`; a bare minor `talosVersion` (the committed
     `v1.14`) resolves to the newest non-prerelease patch the Factory serves.
     The committed definition declares no `imageFactory` block, so CABPT
     renders no installer-image override. The earlier mechanism, the
     `hardware.tinkerbell.org/installer-image` Hardware annotation mirrored
     into `status.installerImage` by the CAPT fork, is no longer read by
     CABPT v0.8.x.
  4. Set the machine to PXE-boot from the network Smee serves.

     **Installer image handoff status:** the CABPT pin moved to v0.8.2
     (Renovate #295), which deleted the `status.installerImage` lookup and
     moved the installer image to `spec.imageFactory`. The CAPT fork v0.7.1
     still mirrors the Hardware annotation, but nothing consumes it on the
     current pins, so the annotation path is a no-op. The `spec.imageFactory`
     path has not been exercised live: the #105 hardware acceptance run
     predates the CABPT bump, and the documented PXE/Tinkerbell-Workflow
     path still needs a run (issue #225). Until that run, verify the image
     the machine actually receives and do not treat the installer image as
     Git-pinned. Fork retirement is tracked in issue #266, blocked on
     upstream PR tinkerbell/cluster-api-provider-tinkerbell#604; with the
     installer-image mirror no longer consumed, that PR is the fork's only
     remaining differentiator.
- Two site-specific values in
  `mgmt/local-talos/clusters/management/cluster.yaml` before the first run:
  `spec.controlPlaneEndpoint.host` (the machine's stable IP) and the
  `TinkerbellMachineTemplate` `hardwareName` (the Hardware CR name).

## Configuration

Copy the env template and fill it in. Both the lifecycle wrapper
(`scripts/toolbox-run.sh`) and the helper mise tasks load `.env`
automatically, and it is gitignored:

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

Every environment boots through the same lifecycle wrapper; the profile
selects the environment (the default is `aws` from `bootstrap.toml`):

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap            # aws
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-talos
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap azure      # after azure-bootstrap
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap gcp        # after gcp-bootstrap
```

Azure: see [azure.md](./azure.md) for the subscription prep step that
precedes the `azure` wrapper run (`azure-bootstrap` registers the
providers — including the Arc ones — and creates the shared resource group
and the `krops-capz` / `krops-aso` user-assigned identities with their role
grants; nothing it prints is secret). After the kind cluster is created,
bootstrap-rs runs the `arc-federate` mise task, which Arc-connects kind with
an OIDC issuer for CAPZ/ASO workload identity (issue #236).

GCP: see [gcp.md](./gcp.md) for the project prep step that precedes
the `gcp` wrapper run (`gcp-bootstrap` enables the APIs and creates the
`krops-capg` / `krops-kcc` / `krops-reader` service accounts and the `krops`
workload identity pool; nothing it prints is secret). After the kind cluster
is created, bootstrap-rs runs the `wif-federate` mise task, which registers
the kind cluster's OIDC provider and the impersonation bindings for CAPG and
Config Connector.

> Before the first AWS bootstrap, generate an age key for SOPS. See
> [Secret management](./secrets.md) for the host and toolbox-only setups.

This initial imperative phase performs these steps:

1. Creates the `mgmt` kind cluster.
2. Installs the Flux Operator (Helm).
3. Creates the `flux-github-pat` secret (for Git access) and the `sops-age`
   secret (the age private key Flux uses to decrypt SOPS-encrypted secrets).
4. Installs a `FluxInstance` that syncs `mgmt/aws/` and hands off to GitOps.
5. Pivots: moves the CAPI inventory into the self-managed management cluster
   and deletes the kind cluster (see [Pivot recovery](#pivot-recovery)).

Everything downstream (providers, EKS clusters, workload Flux instances, the
ACK controllers, IAM roles, and S3 buckets) reconciles
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
minutes and can be changed with `LOCAL_RECONCILE_TIMEOUT` in a raw container
or fallback native run. The current wrapper does not forward that override.

EKS clusters typically take 15–25 minutes to come up; node groups and the
downstream app chain follow a few minutes after.

### Verifying the full chain

For the local-host end-to-end chain:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
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
kubectl get kustomizations -n flux-system            # all Ready (incl. ack-controllers, workload-resources)
kubectl get clusters.cluster.x-k8s.io -A             # Provisioned
kubectl get buckets.s3.services.k8s.aws -n ack-system
kubectl get dbinstances.rds.services.k8s.aws -n ack-system
kubectl get roles.iam.services.k8s.aws -n ack-system

# Workload clusters: export kubeconfigs first
#   mise -E aws run kubeconfigs && export KUBECONFIG=~/.kube/krops-workloads.yaml
#   kubectl config use-context eu-north-1-workload   (or eu-west-1-workload)
kubectl get kustomizations -n flux-system            # root only; workload/base is empty since #346

# AWS
aws s3api get-bucket-encryption    --bucket krops-<account>-eu-north-1-workload-data
aws s3api get-public-access-block  --bucket krops-<account>-eu-north-1-workload-data
```

## Pivot recovery

Bootstrap ends with a pivot: the CAPI inventory moves from the local `mgmt`
kind cluster into the self-managed management cluster, and the kind cluster
is deleted. `scripts/toolbox-run.sh bootstrap` runs the pivot by default
(`BOOTSTRAP_PIVOT=0` opts out). `scripts/toolbox-run.sh pivot` starts the same rerun-safe CLI
and resumes through the pivot; there is no separate pivot subcommand. See
[the bootstrap CLI](./bootstrap-cli.md) for its interface and controls.

`clusterctl move` is re-runnable: an object is deleted from the source kind
cluster only after it was created on the target, so kind stays authoritative
until the final kind deletion. On a clean first run the pivot also WAITS for
two Flux-driven prerequisites instead of failing fast: the management
`Cluster` definition (polled up to `MGMT_READY_TIMEOUT`, surfacing failed
Kustomizations on timeout) and the first target nodes (up to 15m, tolerating
a nodeless EKS start). A timeout in either prints the Kustomization or node
state and is safe to re-run. If a pivot phase fails:

1. Fix the reported cause.
2. Re-run the pivot (`scripts/toolbox-run.sh pivot`, or rerun bootstrap)
   from a checkout of the revision you want self-managed, normally `main`. A
   fallback native run must use the bootstrap context (`kind-mgmt`;
   `BOOTSTRAP_KUBECONTEXT` overrides). Toolbox mode selects kind's internal
   kubeconfig automatically. The CLI reuses an existing healthy `mgmt` kind
   cluster on rerun.
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
`krops-mgmt`. The toolbox mount makes its `/root/.kube/krops-mgmt.yaml`
appear on the host as `./.kube/krops-mgmt.yaml`; a fallback native run
defaults to `~/.kube/krops-mgmt.yaml`.

## Teardown

The wrapper runs the Rust subcommand in the toolbox:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh teardown            # aws
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh teardown local-host
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh teardown local-talos
```

Azure teardown is manual; the CLI prints the steps.

The retained `./teardown.sh` reference path and a native
`krops-bootstrap teardown [PROFILE]` run are the fallbacks. The positional
profile selects an environment from `bootstrap.toml`. Teardown reads
resource names and targets from `bootstrap.toml` and discovers where the
CAPI controllers are running:

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
| `MGMT_KUBECONFIG` | `~/.kube/krops-mgmt.yaml` (in the toolbox that is the checkout's `.kube/` mount) | Post-pivot controller-host kubeconfig |

The hard preflight depends on the mode:

| Mode | Required tools |
|---|---|
| `local-host` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| `local-talos` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| normal `aws` | `kind`, `helm`, `kubectl`, `xargs`; a missing AWS CLI skips the orphan sweep |
| `AWS_ONLY=1` | AWS CLI only |

A required-tool failure happens before mutation. The wrapper does not forward
the four teardown controls in the table above; use a raw container invocation
with explicit `-e` entries or a fallback native run for recovery overrides.

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
