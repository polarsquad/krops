# Architecture

GitOps-driven [Cluster API](https://cluster-api.sigs.k8s.io/) (CAPI) management
platform. A disposable local [kind](https://kind.sigs.k8s.io/) cluster
bootstraps [Flux](https://fluxcd.io/), provisions the self-managed management
cluster through CAPI, and is deleted after a `clusterctl move` pivot. The
management cluster then reconciles itself and all downstream infrastructure from
this repository across multiple supported environments: AWS (`aws`), Azure
(`azure`), local Docker (`local-host`), bare-metal Talos (`local-talos`), and
an air-gapped bundle (`airgap`).

The operator normally runs the imperative lifecycle through the
`krops-toolbox` container. It mounts the host engine socket, joins the kind
network while the bootstrap cluster exists, and leaves no toolbox workload in
the managed clusters. This packaging changes the host tool boundary, not the
Flux or CAPI reconciliation architecture.

## AWS environment (aws)

The reference environment manages AWS infrastructure through the Kubernetes
API: CAPA provisions EKS workload clusters, CAPI addons deliver per-cluster Flux
instances, and ACK operators (S3, RDS, IAM) manage cloud resources with EKS Pod
Identity.

![krops aws architecture](aws-infra.svg)

```mermaid
flowchart TD
    subgraph bootstrap["Bootstrap (one-time, krops-bootstrap CLI)"]
        KIND[kind cluster: mgmt, disposable]
        HELM[Helm: flux-operator + FluxInstance]
        SEC[Secrets: flux-github-pat + sops-age]
        KIND --> HELM
        KIND --> SEC
    end

    subgraph git["Git: github.com/polarsquad/krops"]
        REPO[(main branch)]
    end

    HELM -->|"sync: mgmt/aws/"| REPO

    subgraph mgmt["Management cluster (self-managed after the pivot) - Flux Kustomizations (dependsOn order)"]
        FS[flux-system root]
        CM[cert-manager]
        CO[capi-operator]
        CI[capa-identity]
        AMC[aws-managed-clusters]
        MGMT[management cluster def<br/>self-hosted via the pivot]
        CAPIS[capi-system]
        CAPAS["capa-system (SOPS creds)"]
        CAAPH[caaph-system]
        ACKC["ack-controllers (SOPS creds)<br/>ACK IAM + EKS controllers"]
        ACKPI["ack-pod-identity<br/>IAM Role + PodIdentityAssociations"]
        AWSIAM["aws-global-iam<br/>krops-reader console user"]
        KONF["konflate (SOPS token)<br/>rendered Flux PR review"]
        EUN[eu-north-1 cluster def]
        EUW[eu-west-1 cluster def]
        FA["flux-apps (SOPS pull secret)<br/>HelmChartProxy + ClusterResourceSets"]

        FS --> CM --> CO
        CO --> CI --> AMC
        CO --> CAPIS --> CAPAS --> CAAPH --> FA
        CAPAS --> EUN
        CAPAS --> EUW
        CAPAS --> MGMT
        FS --> ACKC --> ACKPI
        ACKC --> AWSIAM
        FS --> KONF
    end

    REPO --> FS

    subgraph aws["AWS"]
        EKS1[EKS: eu-north-1-workload<br/>x86 + ARM node pools<br/>pod-identity agent addon]
        EKS2[EKS: eu-west-1-workload<br/>x86 + ARM node pools<br/>pod-identity agent addon]
        ROLE[IAM Role: krops-ack-s3-controller<br/>trust: pods.eks.amazonaws.com]
        RDSROLE[IAM Role: krops-ack-rds-controller<br/>trust: pods.eks.amazonaws.com]
        IAMROLE[IAM Role: krops-ack-iam-controller<br/>trust: pods.eks.amazonaws.com]
        B1[(S3: krops-...-eu-north-1-workload-data)]
        B2[(S3: krops-...-eu-west-1-workload-data)]
        DB1[(RDS: krops-eu-north-1-workload-db)]
        DB2[(RDS: krops-eu-west-1-workload-db)]
        RD1[IAM Role: krops-eu-north-1-workload-reader<br/>trust: account root]
        RD2[IAM Role: krops-eu-west-1-workload-reader<br/>trust: account root]
        RUSER[IAM User: krops-reader<br/>console login, assumes reader roles]
    end

    EUN -->|CAPA provisions| EKS1
    EUW -->|CAPA provisions| EKS2
    ACKPI -->|creates| ROLE
    ACKPI -->|creates| RDSROLE
    ACKPI -->|creates| IAMROLE
    AWSIAM -->|creates| RUSER
    RUSER -.->|sts:AssumeRole| RD1
    RUSER -.->|sts:AssumeRole| RD2
    ACKPI -->|binds SAs to roles| EKS1
    ACKPI -->|binds SAs to roles| EKS2

    FA -->|"HelmChartProxy: flux-operator<br/>CRS: FluxInstance + cluster-vars + pull secret"| WF1
    FA -->|same, per region label| WF2

    subgraph wl1["Workload cluster eu-north-1"]
        WF1["Flux (sync: workload/eu-north-01)"]
        AO1["aws-operators Ks<br/>ACK S3 + RDS + IAM controllers (Pod Identity)"]
        SB1["s3-buckets Ks<br/>dependsOn: aws-operators"]
        RI1["rds-instances Ks<br/>dependsOn: aws-operators"]
        IR1["iam-roles Ks<br/>dependsOn: aws-operators"]
        WF1 --> AO1 --> SB1
        AO1 --> RI1
        AO1 --> IR1
    end

    subgraph wl2["Workload cluster eu-west-1"]
        WF2["Flux (sync: workload/eu-west-01)"]
        AO2["aws-operators Ks<br/>ACK S3 + RDS + IAM controllers (Pod Identity)"]
        SB2["s3-buckets Ks<br/>dependsOn: aws-operators"]
        RI2["rds-instances Ks<br/>dependsOn: aws-operators"]
        IR2["iam-roles Ks<br/>dependsOn: aws-operators"]
        WF2 --> AO2 --> SB2
        AO2 --> RI2
        AO2 --> IR2
    end

    WF1 --> REPO
    WF2 --> REPO
    ROLE -.->|credentials via pod identity| AO1
    ROLE -.->|credentials via pod identity| AO2
    RDSROLE -.->|credentials via pod identity| AO1
    RDSROLE -.->|credentials via pod identity| AO2
    IAMROLE -.->|credentials via pod identity| AO1
    IAMROLE -.->|credentials via pod identity| AO2
    SB1 -->|Bucket CR reconciled| B1
    SB2 -->|Bucket CR reconciled| B2
    RI1 -->|DBInstance CR reconciled| DB1
    RI2 -->|DBInstance CR reconciled| DB2
    IR1 -->|Role CR reconciled| RD1
    IR2 -->|Role CR reconciled| RD2
```

### Reconciliation order (AWS management cluster)

Enforced with Flux `dependsOn`:

```
cert-manager ▶ capi-operator ▶ capi-system ▶ capa-system ▶ clusters (eu-north-1, eu-west-1)
                            │                            └▶ caaph-system ▶ flux-apps
                            └▶ capa-identity ▶ aws-managed-clusters
ack-controllers ▶ ack-pod-identity
ack-controllers ▶ aws-global-iam
konflate (no dependencies)
```

The `eu-north-1` cluster definitions include the management cluster itself
(`clusters/management/`): after the pivot, the management cluster's own Flux
instance reconciles the Cluster objects that define it. The bootstrap kind
cluster no longer exists at this point; nothing runs from an operator's
laptop.

### PR review: konflate

The management cluster also runs a single
[konflate](https://github.com/home-operations/konflate) instance
(`mgmt/aws/infrastructure/konflate/`), pointed at this repo
(`github://polarsquad/krops`, rendering from the repo root). It renders each
open PR at its merge-base and head and shows the diff of the *rendered* Flux
output (blast radius, image changes, render failures, and danger lint)
instead of the raw file diff. Results reach the PR two ways: a GitHub Actions
workflow (`.github/workflows/konflate.yml`) runs a one-shot konflate service
container on each PR push and gates the `konflate / Rendered Flux diff` check
on a clean render, and the in-cluster instance posts the rendered summary
comment and a `Konflate` commit status itself (write-back, outbound-only, so
the local kind cluster needs no inbound reachability from GitHub). Both upsert
the same marker-keyed comment. Full details (deployment, CI gate, tokens,
write-back, UI access): [PR review: konflate](./konflate.md).

### Reconciliation order (AWS workload clusters)

```
aws-operators (ACK S3 + RDS + IAM controllers) ▶ s3-buckets (Bucket CRs)
                                               ├▶ rds-instances (DBInstance CRs)
                                               └▶ iam-roles (Role CRs)
```

### How workload apps are delivered (AWS)

1. Each `Cluster` in `mgmt/aws/clusters/` carries labels `fluxcd: enabled`
   and `region: <region>`.
2. `flux-apps` matches those labels: a **HelmChartProxy** installs the Flux
   Operator on every workload cluster, and per-region **ClusterResourceSets**
   apply a `FluxInstance` (syncing `workload/<region>-01/`), a `cluster-vars`
   ConfigMap (`AWS_REGION`, `CLUSTER_NAME`, `AWS_ACCOUNT_ID`, used by Flux
   `postBuild` substitution), and the Git pull secret.
3. The workload cluster's Flux reconciles `workload/`: first `aws-operators`
   (ACK S3 + RDS + IAM controllers, `wait: true`), then `s3-buckets`,
   `rds-instances`, and `iam-roles` (all `dependsOn: aws-operators`).

See [AWS authentication & IAM](./aws-iam.md) for how the ACK controllers
authenticate, and [Workload resources](./workload-resources.md) for what they
create.

## Azure environment (azure)

The `azure` environment mirrors `aws`: a disposable kind cluster bootstraps
Flux, CAPZ v1.27.0 provisions an AKS management cluster (`swedencentral-management`),
the pivot moves the management objects into it, and each AKS workload cluster
runs its own Azure Service Operator (ASO 2.19.0) reconciling Azure resources from
`workload/azure-base/`.

No Azure secret exists at rest: the management cluster authenticates CAPZ and
the bundled ASO with workload identity against the `krops-capz`
User-Assigned Identity (federated to the kind cluster's Arc OIDC issuer at
bootstrap, and to the management cluster's own OIDC issuer post-pivot), while
workload clusters hold no credentials at all. The bundled ASO on the
management cluster reconciles identity resources (`krops-aso` and `krops-capz`
identities, resource group role assignments, and Federated Identity
Credentials), enabling the workload ASO to authenticate with Entra ID
Workload Identity.

See the architecture diagram in [docs/azure-infra.svg](azure-infra.svg) and
the [Azure environment guide](./azure.md).

![krops azure architecture](azure-infra.svg)

```mermaid
flowchart TD
    subgraph bootstrap["Bootstrap (one-time, krops-bootstrap CLI)"]
        KIND[kind cluster: mgmt, disposable]
        HELM[Helm: flux-operator + FluxInstance]
        SEC[Secrets: flux-github-pat + sops-age]
        KIND --> HELM
        KIND --> SEC
    end

    subgraph git["Git: github.com/polarsquad/krops"]
        REPO[(main branch)]
    end

    HELM -->|"sync: mgmt/azure/"| REPO

    subgraph mgmt["Management cluster (self-managed after the pivot) - Flux Kustomizations"]
        FS[flux-system root]
        CM[cert-manager]
        CO[capi-operator]
        CAPIS[capi-system]
        CAPZS["capz-system (CAPZ v1.27.0 + bundled ASO)"]
        CAAPH[caaph-system]
        AZID["azure-identity (secret-free)<br/>AzureClusterIdentity: WorkloadIdentity"]
        ASOWI["aso-workload-identity<br/>krops-aso + krops-capz identities + FICs + roles"]
        SWEDENC["swedencentral cluster def<br/>swedencentral-management (self-hosted)<br/>swedencentral-workload"]
        FA["flux-apps (SOPS pull secret)<br/>HelmChartProxy + ClusterResourceSets"]

        FS --> CM --> CO --> CAPIS --> CAPZS
        CAPIS --> CAAPH --> FA
        CAPZS --> AZID --> ASOWI
        CAPZS --> SWEDENC
        AZID --> SWEDENC
    end

    REPO --> FS

    subgraph azure["Azure: swedencentral"]
        AKS[AKS: swedencentral-workload<br/>AzureASOManagedControlPlane + MachinePool]
        UAI[User-Assigned Identity: krops-aso]
        FIC[Federated Identity Credential: workload-identity]
        ROLE[Role Assignment: Contributor on data RG]
        RG[(Resource Group: krops-swedencentral-workload-data)]
        VNET[(VNet: vnet-swedencentral-workload<br/>subnet: snet-postgres<br/>private DNS zone)]
        SA[(Storage Account: blob container 'data')]
        PSQL[(PostgreSQL Flexible Server<br/>private access, Entra-only auth)]
    end

    SWEDENC -->|CAPZ provisions| AKS
    ASOWI -->|bundled ASO creates| UAI
    ASOWI -->|bundled ASO creates| FIC
    ASOWI -->|bundled ASO creates| ROLE
    ROLE -.->|scopes to| RG

    FA -->|"HelmChartProxy: flux-operator<br/>CRS: FluxInstance + cluster-vars + pull secret"| WF

    subgraph wl["Workload cluster swedencentral-workload"]
        WF["Flux (sync: workload/swedencentral-01)"]
        WCM["cert-manager Ks"]
        WASO["aso Ks<br/>ASO 2.19.0 (Workload Identity)"]
        WNET["networking Ks<br/>dependsOn: aso"]
        WSTOR["storage Ks<br/>dependsOn: aso"]
        WPSQL["postgres Ks<br/>dependsOn: aso, networking"]

        WF --> WCM --> WASO --> WNET --> WPSQL
        WASO --> WSTOR
    end

    WF --> REPO
    UAI -.->|Entra Workload Identity token| WASO
    FIC -.->|federates workload SA| WASO
    WNET -->|reconciles| VNET
    WSTOR -->|reconciles in data RG| SA
    WPSQL -->|reconciles with private DNS| PSQL
```

### Reconciliation order (Azure management cluster)

```
cert-manager ▶ capi-operator ▶ capi-system ▶ capz-system (bundled ASO)
                                           ├▶ azure-identity ▶ aso-workload-identity
                                           ├▶ clusters (swedencentral)
                                           └▶ caaph-system ▶ flux-apps
```

### Reconciliation order (Azure workload cluster)

```
cert-manager ▶ aso (ASO 2.19.0 via Workload Identity) ▶ networking (VNet + delegated subnet + DNS)
                                                      ├▶ storage (Storage Account + blob container)
                                                      └▶ postgres (dependsOn: aso, networking)
```

## Local host environment (local-host)

The `local-host` environment replaces AWS with local Docker containers (CAPD)
and GitHub with a local in-memory OCI registry (`krops-registry:5000`). It
provisions a one-control-plane/one-worker workload cluster and reconciles the
Podinfo demo application end to end on a single host.

See the architecture diagram in [docs/local-host-infra.svg](local-host-infra.svg).

![krops local-host architecture](local-host-infra.svg)

```mermaid
flowchart TD
    subgraph bootstrap["Bootstrap (one-time, krops-bootstrap CLI)"]
        KIND[kind cluster: mgmt, disposable]
        REG["Local OCI Registry: krops-registry:5000"]
        HELM[Helm: flux-operator + FluxInstance]
        KIND --> HELM
        REG -->|oci://krops-registry:5000/krops| HELM
    end

    subgraph oci["Local OCI Registry: krops-registry:5000"]
        ARTIFACT["OCI Artifact: krops:latest<br/>published via mise run oci-push"]
    end

    subgraph mgmt["Management cluster (self-managed CAPD after pivot)"]
        FS[flux-system root]
        CM[cert-manager]
        CO[capi-operator]
        CAPIS[capi-system]
        CAPDS["capd-system (Docker provider)"]
        CAAPH[caaph-system]
        MGMT["management cluster def<br/>local-management (Docker)"]
        DOCKER["clusters/docker def<br/>local-workload"]
        FA["flux-apps (HelmChartProxy + CRS)"]

        FS --> CM --> CO --> CAPIS --> CAPDS
        CAPIS --> CAAPH --> FA
        CAPDS --> MGMT
        CAPDS --> DOCKER
    end

    ARTIFACT --> FS

    subgraph wl["Workload cluster local-workload (Docker containers)"]
        CP[1 Control Plane container]
        WORKER[1 Worker container]
        WF["Flux (syncs workload/local-host from OCI)"]
        APP["podinfo demo application<br/>port-forward to localhost:9898"]
        WF --> APP
    end

    DOCKER -->|CAPD provisions| CP
    DOCKER -->|CAPD provisions| WORKER
    FA -->|installs Flux via HelmChartProxy| WF
    ARTIFACT --> WF
```

### Reconciliation order (local-host)

Management cluster:
```
cert-manager ▶ capi-operator ▶ capi-system ▶ capd-system ▶ clusters (local-management, local-workload)
                            │                            └▶ caaph-system ▶ cni ▶ flux-apps
```

Workload cluster:
```
podinfo (HelmRelease reconciled by local workload Flux from OCI artifact)
```

## Bare-metal Talos environment (local-talos)

The `local-talos` environment targets physical bare metal: a disposable kind
cluster installs CAPI with the Tinkerbell infrastructure provider (CAPT pinned
to fork v0.7.1) and Talos bootstrap/control-plane providers (CABPT v0.7.8,
CACPPT v0.6.5). The controllers match a committed Tinkerbell Hardware object
(`talos-mgmt-01`), PXE-boot the target machine, write the Talos installer
image to disk, and bring up an immutable single-node control plane.

Scope fence: `local-talos` is management-only. It owns no workload clusters and
deploys no CAPI addons (Talos ships its own internal CNI). Teardown deletes the
CAPI objects so CAPT releases the Hardware back to the Tinkerbell pool; the
physical machine is never wiped or reclaimed.

See the architecture diagram in [docs/local-talos-infra.svg](local-talos-infra.svg).

![krops local-talos architecture](local-talos-infra.svg)

```mermaid
flowchart TD
    subgraph bootstrap["Bootstrap (one-time, krops-bootstrap CLI)"]
        KIND[kind cluster: mgmt, disposable]
        HELM[Helm: flux-operator + FluxInstance]
        SEC[Secrets: flux-github-pat + sops-age]
        KIND --> HELM
        KIND --> SEC
    end

    subgraph git["Git: github.com/polarsquad/krops"]
        REPO[(main branch)]
    end

    HELM -->|"sync: mgmt/local-talos/"| REPO

    subgraph mgmt["Management cluster (bare metal, self-managed after pivot)"]
        FS[flux-system root]
        CM[cert-manager]
        CO[capi-operator]
        CAPIS[capi-system]
        CAPT["capt-system (Tinkerbell CAPT v0.7.1 fork)"]
        CABPT["cabpt-system (Talos bootstrap v0.7.8)"]
        CACPPT["cacppt-system (Talos control plane v0.6.5)"]
        TALOSMGMT["clusters/management<br/>talos-mgmt-01 (explicit controlPlaneRef)"]

        FS --> CM --> CO --> CAPIS
        CAPIS --> CAPT
        CAPIS --> CABPT
        CAPIS --> CACPPT
        CAPT --> TALOSMGMT
        CABPT --> TALOSMGMT
        CACPPT --> TALOSMGMT
    end

    REPO --> FS

    subgraph metal["Physical Metal (Tinkerbell Substrate)"]
        HW["Tinkerbell Hardware: talos-mgmt-01<br/>committed MAC / BMC credentials"]
        PXE["PXE network boot + Hook environment"]
        IMG["Talos installer image streamed to disk"]
        NODE["Single-node bare metal control plane<br/>static VIP / Talos API"]
    end

    TALOSMGMT -->|CAPT claims| HW
    HW --> PXE --> IMG --> NODE
```

### Reconciliation order (local-talos)

```
cert-manager ▶ capi-operator ▶ capi-system ▶ capt-system (Tinkerbell CAPT) ──┐
                                           ├▶ cabpt-system (Talos bootstrap) ─┼▶ clusters/management (talos-mgmt-01)
                                           └▶ cacppt-system (Talos CP) ───────┘
```

## Air-gapped bundle (airgap)

The air-gap bundle packages the `local-host` profile with [Zarf](https://zarf.dev)
for completely disconnected deployments. A connected build machine renders the
krops GitOps tree, pulls required container images and charts, generates Syft
SBOMs, and signs the resulting package. On the disconnected deploy host, Zarf
initializes an internal container registry and proxy agent that rewrites image
pulls, while Flux reconciles the offline management and workload clusters with zero
external network traffic.

See the architecture diagram in [docs/air-gap-infra.svg](air-gap-infra.svg) and
the [Air-gapped krops guide](./airgap.md).

![krops air-gap architecture](air-gap-infra.svg)
