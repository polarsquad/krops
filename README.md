# krops
## kubernetes resource operations

![krops logo](docs/krops-logo.svg)

krops is a GitOps pattern for managing infrastructure through the Kubernetes API
with plain declarative YAML. Terraform and OpenTofu use HCL, a state file, and
discrete plan/apply runs; krops stores desired state as Kubernetes resources in
Git. [Flux](https://fluxcd.io/) delivers those resources, and controllers
continuously reconcile the infrastructure to match them. Kubernetes provides
one API, RBAC model, policy surface, and audit trail for infrastructure and
workloads. No HCL, no `.tfstate`, no second toolchain.

[Crossplane](https://www.crossplane.io/) is the closer comparison because it
also runs infrastructure reconciliation inside Kubernetes. Its providers expose
managed resources, while XRDs and compositions can turn them into higher-level
platform APIs. krops introduces no krops-specific CRD or controller: it combines
[Cluster API](https://cluster-api.sigs.k8s.io/) for clusters,
[ACK](https://aws-controllers-k8s.github.io/docs/) for AWS resources, and Flux
for GitOps. If those resource APIs already say what you mean, krops does not
wrap them to say it again.

This repository demonstrates the pattern end to end on AWS EKS, Azure AKS,
Google GKE, local Docker clusters, and a Tinkerbell-provisioned
[Talos Linux](https://www.talos.dev/) machine. A disposable
[kind](https://kind.sigs.k8s.io/) cluster bootstraps Flux,
CAPI pivots control to a self-managed management cluster, and the Rust
[`krops-bootstrap`](docs/bootstrap-cli.md) CLI handles bootstrap, pivot, and
teardown. After that, everything is declared in Git, with a
[Zarf](https://zarf.dev/) bundle for air-gapped local installs. It is a working
reference implementation, not a product, so fork it, strip it down, and adapt
it to your own cloud and clusters.

## Who this is for

Platform engineers who already run Kubernetes and want to manage their own
cloud infrastructure with the same API, RBAC, audit trail, and GitOps workflow
they use for workloads. If you're reaching for Terraform/OpenTofu, Pulumi, or
Crossplane to stand up cloud resources for Kubernetes, this pattern is the
alternative: the cluster you already operate becomes the control plane. It is
not a developer self-service portal; you are the consumer.

## Problems the pattern solves

- **State files**: drift, locking, corruption. Controllers reconcile actual
  state continuously instead of diffing a snapshot.
- **The plan/apply gap**: PRs are reviewed as **rendered** Flux diffs (blast
  radius, image changes, render failures) by
  [konflate](https://github.com/home-operations/konflate): a GitHub Actions
  workflow runs it on every PR push as a merge gate, and an in-cluster
  instance posts the summary to the PR. You review byte-for-byte what
  reconciles. See [docs/konflate.md](docs/konflate.md).
- **Two toolchains**: HCL for infra, YAML for workloads. One control plane
  means RBAC, policy, and audit cover both.
- **Lifecycle split**: Terraform builds the cluster but can't manage what's
  in it. CAPI + Flux is one dependency graph from cluster to workload.
- **A control plane on a laptop**: the management cluster is not a long-lived
  local kind cluster. The bootstrap kind cluster is disposable, and after the
  pivot the management cluster manages itself through the same GitOps flow it
  drives.

![krops aws architecture](docs/aws-infra.svg)

The other environments' diagrams are linked from their [Environments](#environments) sections below.

## Prerequisites

All krops interactions happen through the prereq
`krops-toolbox` container, driven by Docker or Podman. A container user
needs only the repository checkout and a running engine:

- Docker, or Podman 5.5+; kind creates clusters through the mounted engine
  socket
- The toolbox image, `ghcr.io/polarsquad/krops-toolbox:<version>` (Linux
  amd64 and arm64, published as `X.Y.Z`, `X.Y`, and stable `latest` on
  matching `v*` tags, with a keyless signature and SPDX SBOM attestation),
  or build the current checkout with
  `docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .`

The toolbox carries `krops-bootstrap` plus every tool the lifecycle and the
helper tasks use, so no host toolchain is required for bootstrap, pivot,
teardown, cloud preparation, SOPS key work, kubeconfig exports, or
`oci-push`. Those helpers are mise tasks that run in the same image with
`--entrypoint mise`; the run shapes are defined once in
[Operations](docs/operations.md#helper-tasks-in-the-toolbox) and shown per
environment below.

The `aws` environment additionally requires a GitHub PAT with read access,
AWS credentials and service quotas, and an age private key. The
`local-talos` environment needs the PAT and age key too (it syncs from
GitHub), plus a reachable Tinkerbell stack and the site values in
`mgmt/local-talos/clusters/management/cluster.yaml`; see
[Operations](docs/operations.md).

Two steps stay host-side on purpose and use [mise](https://mise.jdx.dev/):
`mise run validate` (repository development) and
`mise -E local-host run podinfo-port-forward` (the browser is on the host).
`mise.toml` and the `mise.<env>.toml` layers remain the toolbox image's
tool-pin source and the helper task definitions.

## Quickstart

Build the current checkout and run the complete local-host lifecycle with only
Docker installed:

```sh
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
mkdir -p .kube
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  krops-toolbox:dev local-host

# Teardown uses the same mounts and the teardown subcommand:
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  krops-toolbox:dev teardown local-host
```

Use `ghcr.io/polarsquad/krops-toolbox:<version>` instead of the local image
for a published release. The AWS form must also pass the Git source, PAT, age
key path, and any AWS credential variables. Podman socket paths vary by host.
`scripts/toolbox-run.sh` handles those mounts, loads `.env` without preserving
quote characters, and persists kubeconfigs under `.kube/`; see
[Operations](docs/operations.md) for both forms.

The lifecycle runs through `scripts/toolbox-run.sh`, the Docker/Podman
wrapper that handles the mounts, `.env` loading, and socket resolution. The
helper tasks run the same image with `--entrypoint mise`:

```sh
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
export TOOLBOX_IMAGE=krops-toolbox:dev
cp .env.example .env        # aws and local-talos: fill in the Git source and PAT
docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" run sops-keygen   # first time only: age key for SOPS
scripts/toolbox-run.sh bootstrap      # toolbox: bootstrap, Flux handoff, then pivot
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
flux get kustomizations --watch
mise run validate            # host: shell syntax, bootstrap.toml cross-check, overlays
scripts/toolbox-run.sh teardown      # toolbox: reverse-order lifecycle cleanup
```

On macOS, a `local-host` run leaves the exported management kubeconfig
pointing at the `kind` Docker network, which Docker Desktop does not
route; rewrite a host copy before the `export` line above. See
[Host-side access after a toolbox local-host run (macOS)](docs/operations.md#host-side-access-after-a-toolbox-local-host-run-macos).
Linux hosts route the `kind` network directly and need no rewrite.

Dependency versions are managed by Renovate
([renovate.json5](renovate.json5)) running as the hosted GitHub App; they live
in their native consumer files and update PRs open weekly. See
[Dependencies](docs/dependencies.md).

### Environments

Five management environments share one shape: a disposable kind bootstrap
cluster runs Flux, a CAPI infrastructure provider builds the self-managed
management cluster, the pivot moves the management objects into it, and the
management cluster then reconciles itself and its workload clusters from this
repository. The shared toolchain is defined in `mise.toml`; each environment
layers its own tools and tasks in a `mise.<env>.toml`.

| Environment | Management cluster | CAPI provider | Workload operator | Config sync |
|---|---|---|---|---|
| `aws` | EKS `eu-north-1-management` | CAPA | ACK (S3, RDS, IAM) | GitHub |
| `azure` | AKS `swedencentral-management` | CAPZ (bundles ASO) | Azure Service Operator | GitHub |
| `gcp` | GKE `europe-north1-management` | CAPG | Config Connector | GitHub |
| `local-host` | CAPD `local-management` (Docker) | CAPD | Flux + Podinfo | local OCI registry |
| `local-talos` | single-node Talos on bare metal | CAPT + CABPT + CACPPT | none (management-only) | GitHub |

Each environment has its own reference page; the ones below summarize it and
link to the full guide.

#### AWS

The reference environment. CAPA provisions an EKS management cluster in
`eu-north-1` plus workload EKS clusters in `eu-north-1` and `eu-west-1`; the
management cluster runs the ACK S3, RDS, and IAM operators reconciling the
per-cluster AWS resources, so workload clusters hold no credentials and run
no controllers. Credentials are a
SOPS-encrypted CAPA profile in Git (the same static pattern authenticates
the ACK controllers). It needs a GitHub PAT, an age key,
AWS credentials, and the `clusterawsadm` CloudFormation stack.

![krops aws architecture](docs/aws-infra.svg)

```sh
export TOOLBOX_IMAGE=ghcr.io/polarsquad/krops-toolbox:latest
docker run --rm -it -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E aws run aws-bootstrap   # once: clusterawsadm IAM CloudFormation stack
docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" run sops-keygen              # first time only: age key for SOPS
scripts/toolbox-run.sh bootstrap aws   # kind + Flux + CAPA; then pivot
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"                    # written by the pivot
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml -e KUBECONFIG_FILE=/root/.kube/krops-workloads.yaml \
  -e MISE_AUTO_INSTALL=0 --entrypoint mise "$TOOLBOX_IMAGE" -E aws run kubeconfigs   # workload kubeconfigs per region
scripts/toolbox-run.sh teardown aws    # full AWS + EKS + kind cleanup
```

Full guide: [AWS environment](docs/aws.md) (clusters, credentials, commit the
identifiers, reconciliation order, upgrades, known limitations). IAM and
per-cluster reader roles: [AWS authentication & IAM](docs/aws-iam.md). S3/RDS
posture: [Workload resources](docs/workload-resources.md).

#### Azure

CAPZ v1.27.0 provisions an AKS management cluster in `swedencentral` plus a
workload AKS cluster; each workload cluster runs its own Azure Service
Operator (ASO) reconciling Azure resources from `workload/azure-base/`. No
Azure secret exists at rest: CAPZ and the bundled ASO authenticate with
workload identity against the `krops-capz` user-assigned identity, and the
workload ASO authenticates through a federated credential. It needs a GitHub
PAT, an age key, and a subscription where you hold Owner.

![krops azure architecture](docs/azure-infra.svg)

```sh
mkdir -p "$HOME/.krops-azure"
docker run --rm -it -v "$HOME/.krops-azure:/root/.azure" \
  --entrypoint az "$TOOLBOX_IMAGE" login --use-device-code
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v "$HOME/.krops-azure:/root/.azure" \
  -e AZURE_SUBSCRIPTION_ID -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E azure run azure-bootstrap   # once: providers, resource group, identities
scripts/toolbox-run.sh bootstrap azure   # kind + Flux + CAPZ; then pivot
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml -e KUBECONFIG_FILE=/root/.kube/krops-workloads.yaml \
  -e MISE_AUTO_INSTALL=0 --entrypoint mise "$TOOLBOX_IMAGE" -E azure run kubeconfigs
```

Full guide: [Azure environment](docs/azure.md). Teardown is manual for now;
the CLI prints the steps.

#### GCP

CAPG v1.13.1 provisions a GKE management cluster in `europe-north1` plus a
workload GKE cluster; the workload cluster runs its own Config Connector (KCC)
reconciling GCP resources from `workload/gcp-base/`. No GCP secret exists at
rest: CAPG and the management-side Config Connector authenticate with
Workload Identity Federation against the `krops` pool (no service-account
keys), and the workload cluster uses GKE-native Workload Identity. It needs a
GitHub PAT, an age key, and a project with a billing account where you hold
Owner.

![krops gcp architecture](docs/gcp-infra.svg)

```sh
docker run --rm -it -v "$PWD:/workspace" -w /workspace -e CLOUDSDK_CONFIG=/workspace/.gcloud \
  --entrypoint gcloud "$TOOLBOX_IMAGE" auth login --no-launch-browser
docker run --rm -it -v "$PWD:/workspace" -w /workspace \
  -e CLOUDSDK_CONFIG=/workspace/.gcloud -e GCP_PROJECT -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E gcp run gcp-bootstrap   # once: APIs, service accounts, WIF pool
scripts/toolbox-run.sh bootstrap gcp     # kind + Flux + CAPG; then pivot
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml -e KUBECONFIG_FILE=/root/.kube/krops-workloads.yaml \
  -e CLOUDSDK_CONFIG=/workspace/.gcloud -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E gcp run kubeconfigs
```

Full guide: [GCP environment](docs/gcp.md). Teardown is manual for now; the
CLI prints the steps.

#### Local host

The end-to-end local environment: no cloud at all. It creates the management
kind cluster, a local OCI registry, and the Flux Operator and FluxInstance. It
publishes the `mgmt/local-host/` and `workload/local-host/` folders as the
`krops:latest` OCI artifact, and Flux syncs from that artifact (not GitHub).
Flux installs CAPI with its Docker provider (CAPD), provisions a
one-control-plane/one-worker workload cluster, and installs a separate Flux
instance there. That workload Flux instance reconciles Podinfo, giving a
complete local path from management bootstrap through workload delivery and
application access, with no GitHub or AWS credentials.

![krops local-host architecture](docs/local-host-infra.svg)

```sh
scripts/toolbox-run.sh bootstrap local-host
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
# republish local management and workload paths, then export the CAPD workload
# kubeconfig; both runs stay on the kind network (full commands in Operations)
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e REGISTRY_HOST=krops-registry -e REGISTRY_PORT=5000 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run oci-push
docker run --rm -it -v "$PWD:/workspace" -w /workspace -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e KROPS_TOOLBOX=1 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run kubeconfigs
docker run --rm -it --network kind -p 9898:9898 -v "$PWD:/workspace" -w /workspace \
  --entrypoint kubectl "$TOOLBOX_IMAGE" --kubeconfig local-workload.kubeconfig \
  port-forward -n podinfo --address 0.0.0.0 service/podinfo 9898:9898   # http://localhost:9898
scripts/toolbox-run.sh teardown local-host
```

On macOS, rewrite a host copy of the management kubeconfig and export
that copy in place of `.kube/krops-mgmt.yaml` above: Docker Desktop does
not route the `kind` network address the file carries. See
[Host-side access after a toolbox local-host run (macOS)](docs/operations.md#host-side-access-after-a-toolbox-local-host-run-macos).
Linux hosts need no rewrite.

With a host `mise` and `kubectl`, `mise -E local-host run kubeconfigs` followed
by `mise -E local-host run podinfo-port-forward` is the host-side equivalent
of the last two runs (the host task rewrites the endpoint to `127.0.0.1`).

`scripts/toolbox-run.sh bootstrap local-host` waits for both the management and workload
Flux reconciliation chains and surfaces workload errors; a successful
bootstrap plus the Podinfo port-forward verifies the end-to-end flow.
Teardown deletes the CAPD workload cluster first, then the pre-pivot kind
cluster or the post-pivot self-managed management containers, and removes the
local registry last.

#### Local Talos

Targets a physical machine through Tinkerbell: a PXE install of Talos Linux,
then the same bootstrap, pivot, and self-management flow, synced from GitHub.
Scope fence: management-only, no workload clusters. It needs the GitHub PAT
and age key, a reachable Tinkerbell stack with a `Hardware` resource for the
machine, and the site values in
`mgmt/local-talos/clusters/management/cluster.yaml`. The hardware acceptance
run (issue #105) has been executed end to end on operator-owned hardware; the
documented PXE/Tinkerbell-Workflow provisioning transport still needs a live
run (issue #225).

![krops local-talos architecture](docs/local-talos-infra.svg)

```sh
scripts/toolbox-run.sh bootstrap local-talos
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"   # written by the pivot; the machine is reached directly
kubectl get nodes
scripts/toolbox-run.sh teardown local-talos  # releases the Hardware; never wipes the machine
```

`talosctl` (`mise.local-talos.toml`) is an operator convenience for the
machine itself, not a lifecycle dependency; install it on the host with
`mise -E local-talos install` if you want it.

#### Air-gapped local host

The `local-host` profile packaged with [Zarf](https://zarf.dev) for
completely disconnected deployments: a connected build machine renders the
GitOps tree, pulls the images and charts, signs the package, and a
disconnected deploy host runs it with zero external traffic. Validated with
the radio off. See [Air-gapped krops](docs/airgap.md) for build, offline
deploy, and the update drill.

![krops air-gap architecture](docs/air-gap-infra.svg)

## The bootstrap CLI

The single `krops-bootstrap` binary implements bootstrap, the default pivot, and
`krops-bootstrap teardown`. Repository-owned cluster names, paths, chart
versions, provider manifests, and teardown targets come from
[`bootstrap.toml`](bootstrap.toml); `mise run validate` cross-checks that file
against the Git manifests. Sequence-level behavior and generic fallback
defaults remain in the binary.

`scripts/toolbox-run.sh bootstrap`, `pivot`, and `teardown` run the CLI in
the toolbox container. The shell scripts remain native reference and
fallback paths until all environments complete parity runs; local-host has
passed the full lifecycle, while AWS parity still gates retirement. See
[The bootstrap CLI](docs/bootstrap-cli.md) for the interface, configuration,
teardown controls, toolbox release, and current parity status.

## Documentation

| Page | Contents |
|---|---|
| [docs/bootstrap-cli.md](docs/bootstrap-cli.md) | The `krops-bootstrap` lifecycle CLI: toolbox distribution, interface, `bootstrap.toml`, pivot, teardown, parity status |
| [docs/dependencies.md](docs/dependencies.md) | Renovate-managed dependency updates: covered surfaces, update procedure, intentional differences |
| [docs/architecture.md](docs/architecture.md) | Architecture diagram, reconciliation order, how workload apps are delivered |
| [docs/aws.md](docs/aws.md) | AWS environment: clusters, credentials, identifiers, reconciliation order, upgrades, known limitations |
| [docs/wiremock-e2e-spike-findings-aws.md](docs/wiremock-e2e-spike-findings-aws.md) | WireMock e2e Phase 0 spike findings (AWS): CAPA/ACK honor `AWS_ENDPOINT_URL`, no network-layer interception needed |
| [docs/wiremock-e2e-spike-findings-azure.md](docs/wiremock-e2e-spike-findings-azure.md) | WireMock e2e Phase 0 spike findings (Azure): ASO honors endpoint settings, CAPZ needs the CoreDNS rewrite, `HTTPS_PROXY` is not viable |
| [docs/wiremock-e2e-spike-findings-gcp.md](docs/wiremock-e2e-spike-findings-gcp.md) | WireMock e2e Phase 0 spike findings (GCP): REST and gRPC both interceptable via CoreDNS rewrite + SAN certs; HTTPS_PROXY covers REST only; CAPG v1.13.1 `serviceEndpoints` covers REST compute only |
| [docs/aws-iam.md](docs/aws-iam.md) | Management-cluster ACK controllers (static SOPS credentials, union scope), per-cluster reader roles, the `krops-reader` console user |
| [docs/workload-resources.md](docs/workload-resources.md) | S3 bucket security posture, RDS instances, known limitations |
| [docs/konflate.md](docs/konflate.md) | Rendered Flux PR review: GitHub Actions gate, in-cluster instance, write-back to PRs, tokens |
| [docs/secrets.md](docs/secrets.md) | SOPS + age secret management, key setup, credential rotation |
| [docs/operations.md](docs/operations.md) | Toolbox runtime, prerequisites, quotas, bootstrap, pivot recovery, teardown, validation |
| [docs/extending.md](docs/extending.md) | Adding a workload cluster, adding apps to the workload clusters, adding other providers (Azure, Talos, k0smotron) |
| [docs/azure.md](docs/azure.md) | Azure environment: subscription prep, credentials, AKS clusters, ASO on workload clusters, upgrades |
| [docs/gcp.md](docs/gcp.md) | GCP environment: project prep, WIF credentials (no keys), GKE clusters, Config Connector on the workload cluster, upgrades |
| [docs/airgap.md](docs/airgap.md) | Zarf air-gap bundle: package build, offline deploy, verification checklist, update drill |
| [docs/proposals/](docs/proposals/README.md) | Design proposals under review (not yet decided or implemented) |

## Repository layout

```
├── .github/workflows/             Validation, Rust/toolbox CI, docs CI and
│                                  Pages deploy, signed releases
├── airgap/                        Zarf air-gap bundle, image inventory, scripts
├── virtualized-e2e/               WireMock-virtualized e2e harness (#355):
│                                  lib/ shared components + one arm per
│                                  cloud (aws/ is the reference); not
│                                  Flux-reconciled, no mise task yet
├── bootstrap-rs/                  Lifecycle CLI, toolbox Dockerfile, Rust tests
├── bootstrap.toml                 Repository-owned lifecycle configuration
├── bootstrap.sh / pivot.sh /      Native shell references and fallback paths;
│   teardown.sh                    retained until both parity gates pass
├── scripts/toolbox-run.sh         Docker/Podman wrapper: the lifecycle entry
│                                  point (bootstrap, pivot, teardown)
├── tests/                         Config and Renovate coverage cross-checks
├── mkdocs.yml                     MkDocs Material config for the docs site
├── pyproject.toml / uv.lock       Python project for the docs site build
├── tools/assemble_docs.py         Assembles build/docs/ from README.md + docs/
├── website/                       Docs site assets: colour scheme CSS, CNAME
├── docs/                          Detailed documentation (see table above)
├── mise.toml / mise.*.toml        Pinned toolchain (the toolbox image is
│                                  built from these pins) and the helper
│                                  task definitions (run in the toolbox with
│                                  --entrypoint mise; validate and the
│                                  podinfo port-forward stay host tasks)
├── renovate.json5                 Hosted Renovate discovery and grouping rules
├── mgmt/aws/                      Synced by the MANAGEMENT cluster's Flux
│   ├── infrastructure/           cert-manager, CAPI operator, CAPA identity,
│   │                              ACK controllers (S3, RDS, IAM) and the
│   │                              per-cluster Bucket/DBInstance/reader Role
│   │                              CRs, account-global IAM (reader console
│   │                              user), konflate (rendered Flux PR review)
│   ├── capi-providers/           capi-system, capa-system (SOPS creds),
│   │                              caaph-system
│   ├── addons/flux-apps/         Installs Flux on each workload cluster
│   │                              (HelmChartProxy + ClusterResourceSets)
│   └── clusters/                 EKS cluster defs: eu-north-1, eu-west-1
│                                  (ARM + GPU MachinePools); eu-north-1 also
│                                  carries the self-managed management cluster
├── mgmt/local-host/              OCI-synced CAPI/CAPD local workload cluster
│   │                              and its management cluster definition
│   ├── infrastructure/           capi-operator, cert-manager
│   ├── capi-providers/           caaph-system, capd-system, capi-system
│   ├── addons/                   kindnet CNI, flux-apps
│   └── clusters/                 docker (workload), management (self-managed)
├── mgmt/local-talos/             Single-node Talos management cluster on
│   │                              bare metal via Tinkerbell (CAPT);
│   │                              GitHub-synced like mgmt/aws
│   ├── infrastructure/           capi-operator, cert-manager
│   ├── capi-providers/           cabpt-system, cacppt-system, capi-system,
│   │                              capt-system (Tinkerbell)
│   └── clusters/                 management (self-managed)
├── mgmt/azure/                    AKS management cluster (CAPZ + ASO)
├── mgmt/gcp/                      GKE management cluster (CAPG + Config
│   │                              Connector operator + WIF identities)
└── workload/                     Synced by each WORKLOAD cluster's Flux
    ├── base/                     Intentionally empty since #346 (ACK moved
    │                              to the management cluster); ready for a
    │                              future application workload
    ├── azure-base/               cert-manager, ASO, and the Azure workload
    │                              resources (VNet, storage, PostgreSQL)
    ├── gcp-base/                 KCC operator + ConfigConnector, PSA range,
    │                              storage bucket, Cloud SQL, per-cluster
    │                              reader GSA
    ├── local-host/               OCI-synced Podinfo workload overlay
    ├── eu-north-01/              Per-cluster overlay (sync target)
    ├── eu-west-01/               Per-cluster overlay (sync target)
    ├── swedencentral-01/         Per-cluster overlay -> azure-base
    └── europe-north1-01/         Per-cluster overlay -> gcp-base
```

## License

This repository is licensed under the [Apache License 2.0](LICENSE).
