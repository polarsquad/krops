# Architecture

GitOps-driven [Cluster API](https://cluster-api.sigs.k8s.io/) (CAPI) management
platform. A disposable local [kind](https://kind.sigs.k8s.io/) cluster
bootstraps [Flux](https://fluxcd.io/), provisions the self-managed management
cluster through CAPI, and is deleted after a `clusterctl move` pivot: the
management cluster then reconciles itself and everything else from this
repository: AWS EKS workload clusters provisioned via
[CAPA](https://cluster-api-aws.sigs.k8s.io/), per-cluster Flux instances
delivered through CAPI addons, and application workloads (the
[ACK](https://aws-controllers-k8s.github.io/docs/) S3, RDS, and IAM operators
managing secure S3 buckets, PostgreSQL instances, and read-only IAM roles)
running on each workload cluster.

The operator normally runs the imperative lifecycle through the
`krops-toolbox` container. It mounts the host engine socket, joins the kind
network while the bootstrap cluster exists, and leaves no toolbox workload in
the managed clusters. This packaging changes the host tool boundary, not the
Flux or CAPI reconciliation architecture.

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
        AWSIAM["aws-iam<br/>krops-reader console user"]
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

## Reconciliation order (management cluster)

Enforced with Flux `dependsOn`:

```
cert-manager ▶ capi-operator ▶ capi-system ▶ capa-system ▶ clusters (eu-north-1, eu-west-1)
                            │                            └▶ caaph-system ▶ flux-apps
                            └▶ capa-identity ▶ aws-managed-clusters
ack-controllers ▶ ack-pod-identity
ack-controllers ▶ aws-iam
konflate (no dependencies)
```

The `eu-north-1` cluster definitions include the management cluster itself
(`clusters/management/`): after the pivot, the management cluster's own Flux
instance reconciles the Cluster objects that define it. The bootstrap kind
cluster no longer exists at this point; nothing runs from an operator's
laptop.

## PR review: konflate

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

## Reconciliation order (each workload cluster)

```
aws-operators (ACK S3 + RDS + IAM controllers) ▶ s3-buckets (Bucket CRs)
                                               ├▶ rds-instances (DBInstance CRs)
                                               └▶ iam-roles (Role CRs)
```

## How workload apps are delivered

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

## Other deployment environments

While the diagram above details the `aws` reference environment, the repository includes two other deployment targets walking the same GitOps and CAPI lifecycle:

- **`local-host`**: replaces AWS with local Docker containers (CAPD) and GitHub with a local OCI registry (`krops-registry:5000`). It provisions a one-control-plane/one-worker workload cluster and reconciles Podinfo end to end on a single host. See the architecture diagram in [docs/local-host-infra.svg](local-host-infra.svg).
- **`local-talos`**: pivots onto bare metal via Tinkerbell (CAPT) and Talos Linux (CABPT and CACPPT), syncing directly from GitHub.
- **Air-gap bundle**: packages `local-host` with Zarf for completely disconnected deployment with verified SBOMs and signed artifacts. See the air-gap architecture diagram in [docs/air-gap-infra.svg](air-gap-infra.svg) and [Air-gapped krops](./airgap.md).
