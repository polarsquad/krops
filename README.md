# krops

![krops logo](docs/krops-logo.svg)

krops is a reference architecture for managing cloud infrastructure and
Kubernetes clusters using GitOps. Everything is defined as standard Kubernetes
manifests in Git, and automated controllers continuously update the real-world
infrastructure to match the repository.

Most infrastructure setups require two separate toolchains: one for cloud
infrastructure (such as Terraform or OpenTofu, using HCL and state files) and
another for application workloads inside Kubernetes. krops eliminates that
split. You define clouds, clusters, and applications using the same declarative
Kubernetes resources, stored in the same Git repository, managed by the same
team workflow.

## How it works

krops divides responsibilities across three core layers:

- **Cluster lifecycle:** [Cluster API](docs/architecture.md) creates and manages
  the Kubernetes clusters themselves across supported cloud providers, bare
  metal, or local Docker environments.
- **Cloud resources:** Official cloud operators, including AWS Controllers for
  Kubernetes (ACK), Azure Service Operator (ASO), and Google Cloud Config
  Connector (KCC), provision cloud services like networks, storage, and IAM
  roles directly from Kubernetes manifests.
- **GitOps delivery:** [Flux](docs/architecture.md) monitors the Git repository.
  When changes merge into the main branch, Flux synchronizes the manifests to
  the cluster, prompting the controllers to reconcile the infrastructure.

Infrastructure is organized into two tiers: a **management cluster** that runs
the controllers and creates resources, and one or more **workload clusters**
where user applications run. Because every component speaks the Kubernetes API,
your infrastructure and applications share one security model (Kubernetes RBAC),
one configuration language, and one unified audit log.

For a detailed breakdown of the components and reconciliation sequence, see
[Architecture](docs/architecture.md). For how this model compares to alternative
approaches, see [Pattern comparisons](docs/architecture.md#pattern-comparisons)
and [Crossplane as a resource plane](docs/crossplane.md).

## Who this is for

krops is designed for platform teams and engineers who already run Kubernetes
and want to manage cloud infrastructure without maintaining a separate
infrastructure toolchain.

This repository is a working reference, not a packaged product. It is intended
to be forked, adapted, and pared down to the specific cloud providers and
services your organization requires. You do not need deep Kubernetes internals
experience to run the local quickstart, though operating the cloud environments
assumes familiarity with cluster administration.

### Key capabilities

- **Multi-cloud and local environments:** Complete configurations for AWS (EKS),
  Azure (AKS), Google Cloud (GKE), bare-metal [Talos Linux](https://www.talos.dev/),
  and local Docker clusters.
- **Git as the single source of truth:** Infrastructure and applications are
  defined in one repository. When you merge a pull request, the live environment
  converges to match.
- **Pre-merge diff reviews:** Pull requests are validated with
  [konflate](docs/konflate.md), rendering exact resource diffs so you can review
  what will change before merging.
- **Encrypted secrets:** Sensitive data is encrypted in Git using
  [SOPS and age](docs/secrets.md), keeping cloud provider credentials off workload
  clusters.
- **Self-contained toolbox:** All lifecycle commands run inside a pre-built
  container image (`krops-toolbox`), requiring only Docker or Podman on your host
  machine.
- **Air-gapped operation:** A self-contained [air-gapped bundle](docs/airgap.md)
  allows fully offline installations for restricted environments.

## Quickstart

You can test the entire workflow on your local machine in a few minutes. You
need only a container engine: Docker, or Podman 5.5+. The `krops-toolbox`
container carries every required lifecycle tool, so nothing else needs to be
installed on your host.

```sh
git clone https://github.com/polarsquad/krops.git
cd krops
scripts/toolbox-run.sh bootstrap local-host
```

This single command starts a local container registry and a temporary
[kind](https://kind.sigs.k8s.io/) cluster, publishes the repository to it,
installs Flux and Cluster API, transfers control to a self-managed management
cluster, and creates a workload cluster running the Podinfo demo application.

### Verify the deployment

Once bootstrap completes, you can inspect the deployed resources. If you have
the `flux` CLI installed on your host:

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
flux get kustomizations
```

Alternatively, you can inspect the cluster directly through the toolbox
container without installing anything on your host:

```sh
docker run --rm --network kind \
  -v "$PWD/.kube:/root/.kube" \
  ghcr.io/polarsquad/krops-toolbox:latest \
  kubectl --kubeconfig /root/.kube/krops-mgmt.yaml get clusters -A
```

### Access the workload application

To access the Podinfo demo app running on the workload cluster, export the
workload kubeconfig and forward the application port using the toolbox:

```sh
export TOOLBOX_IMAGE=ghcr.io/polarsquad/krops-toolbox:latest

docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e KROPS_TOOLBOX=1 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run kubeconfigs

docker run --rm --network kind -p 9898:9898 -v "$PWD:/workspace" -w /workspace \
  --entrypoint kubectl "$TOOLBOX_IMAGE" --kubeconfig local-workload.kubeconfig \
  port-forward --namespace podinfo --address 0.0.0.0 service/podinfo 9898:9898
```

Open <http://localhost:9898> in your browser to view the application.

### Teardown

To remove all local clusters and clean up container resources, run:

```sh
scripts/toolbox-run.sh teardown local-host
```

On macOS, the exported management kubeconfig points to the internal `kind`
Docker network, which Docker Desktop does not route directly to the host. If you
plan to use host-side tools on macOS, see
[Host-side access after a toolbox local-host run (macOS)](docs/operations.md#host-side-access-after-a-toolbox-local-host-run-macos).
Linux hosts route this network directly and require no changes.

## Supported environments

To try a cloud environment, configure your cloud credentials once (such as an
AWS CloudFormation stack, an Azure resource group, or a GCP workload identity
pool), configure `.env` from `.env.example`, and run
`scripts/toolbox-run.sh bootstrap <environment>` using `aws`, `azure`, or `gcp`.

Each environment guide walks through account preparation, prerequisites, and
deployment:

| Environment | Management cluster | Guide |
|---|---|---|
| `aws` (reference) | EKS in `eu-north-1` | [docs/aws.md](docs/aws.md) |
| `azure` | AKS in `swedencentral` | [docs/azure.md](docs/azure.md) |
| `gcp` | GKE in `europe-north1` | [docs/gcp.md](docs/gcp.md) |
| `local-host` | kind (Docker) | [docs/operations.md](docs/operations.md) |
| `local-talos` | Talos on bare metal | [docs/operations.md](docs/operations.md) |
| air-gapped `local-host` | kind, zero external traffic | [docs/airgap.md](docs/airgap.md) |

## Going deeper

- [Architecture](docs/architecture.md): pillars, reconciliation order, and per-environment details
- [Operations](docs/operations.md): toolbox usage, prerequisites, pivot recovery, and teardown
- [Bootstrap CLI](docs/bootstrap-cli.md): the `krops-bootstrap` engine and configuration options
- [Secrets](docs/secrets.md): SOPS + age encryption setup and key rotation workflows
- [Dependencies](docs/dependencies.md): Renovate-managed automated updates across the repository
- [Extending](docs/extending.md): adding new clusters, applications, and custom providers
- [AWS IAM](docs/aws-iam.md), [Workload resources](docs/workload-resources.md), and [ACK troubleshooting](docs/troubleshooting-ack.md)
- [konflate](docs/konflate.md): rendered pull request reviews and GitHub Actions gating
- [Air-gapped krops](docs/airgap.md): Zarf bundle generation, offline installation, and update drills

## Repository layout

```text
.
├── mgmt/<env>/                Reconciled by the MANAGEMENT cluster's Flux
│   ├── infrastructure/        Platform components and cloud-resource controllers
│   ├── capi-providers/        Cluster API and its infrastructure providers
│   ├── addons/                What gets installed on workload clusters
│   └── clusters/              Cluster definitions (workload + self-managed management)
├── workload/<env>/            Reconciled by each WORKLOAD cluster's Flux
├── airgap/                    Zarf offline bundle for the local-host profile
├── bootstrap-rs/              The krops-bootstrap CLI and the toolbox image
├── bootstrap.toml             Repository-owned lifecycle configuration
├── scripts/toolbox-run.sh     Docker/Podman wrapper: the lifecycle entry point
├── docs/                      Detailed documentation (links above)
├── tests/                     Validation gates (run by `mise run validate`)
└── .github/workflows/         CI: validate, toolbox release, docs, konflate
```

The `mgmt/` and `workload/` directories contain one subfolder per environment
(`aws`, `azure`, `gcp`, `local-host`, `local-talos`). Each component pairs a
standard Kustomize base with a Flux `Kustomization` manifest. See
[Architecture](docs/architecture.md) and [Operations](docs/operations.md) for full
walkthroughs of the tree structure.

## Validation

Every change is verified by local checks (`mise run validate`) and continuous integration:

- Shell scripts and configuration files are syntax-checked.
- Manifests in `bootstrap.toml` are cross-referenced against Git files.
- Every Kustomize target under `mgmt/` and `workload/` is built.
- Air-gapped image pins are verified.
- The documentation site is assembled with strict link validation.
- The Rust CLI passes format, clippy, and unit tests.

See [Operations: Validation](docs/operations.md#validation) for details.

## Contributing

Review [CONTRIBUTING.md](CONTRIBUTING.md) for branch and commit conventions, and
[AGENTS.md](AGENTS.md) for guidelines governing automated coding agents. Flux
reconciles from `main`: changes only take effect after pull requests are merged.

## License

Apache-2.0. See [LICENSE](LICENSE).
