# AWS environment

The `aws` environment is the reference: a disposable kind bootstrap cluster
runs Flux, CAPA v2.13.0 provisions self-managed EKS clusters, the pivot moves
the management objects into the `eu-north-1-management` cluster, and the
management cluster runs the ACK operators (S3, RDS, IAM) reconciling the
per-workload-cluster AWS resources from
`mgmt/aws/infrastructure/workload-resources/` (issue #346).

![krops aws architecture](aws-infra.svg)

## Clusters

| Region | Clusters |
|---|---|
| `eu-north-1` | `eu-north-1-management` (the self-managed management cluster, provisioned by the pivot) and `eu-north-1-workload` |
| `eu-west-1` | `eu-west-1-workload` |

Every cluster is an EKS control plane with an x86 plus an ARM (Graviton2)
`AWSManagedMachinePool` at the cheapest
offered 2 vCPU / 4 GiB shape for the region. The management cluster lives in
`eu-north-1` and, after the pivot, reconciles its own Cluster objects.

## Prerequisites

- An AWS account where you hold permission to create EKS clusters, VPCs, and
  IAM roles. The static credentials principal runs every ACK controller on
  the management cluster, so it needs the union of the former per-controller
  pod-identity role policies: S3 and RDS management scoped to `krops-*`
  (plus `secretsmanager` on `rds!*` secrets and KMS grant management for the
  managed master password), IAM role management scoped to `krops-*`
  (`iam:CreateRole`/`PutRolePolicy`/`GetRole`/`TagRole`), and
  `iam:CreateUser`/`PutUserPolicy`/`GetUser`/`GetUserPolicy`/`TagUser`
  (for the `krops-reader` console user). See
  [AWS authentication & IAM](./aws-iam.md) for the exact actions and the
  least-privilege trade-off.
- `mise -E aws install` (adds `aws-cli` and `clusterawsadm`), a GitHub PAT and
  an age key as for any GitHub-synced environment (`.env`, see
  [operations.md](./operations.md)).
- The `clusterawsadm` IAM CloudFormation stack, provisioned once before
  bootstrap and removed by a full AWS teardown:

  ```sh
  mise -E aws run aws-bootstrap
  # == clusterawsadm bootstrap iam create-cloudformation-stack --region eu-north-1
  ```

- AWS service quotas for a clean account. The default run creates two EKS
  clusters in `eu-north-1` and one in `eu-west-1`, each with a NAT gateway per
  AZ (one EIP each), so it needs at least 6 free EIPs in `eu-north-1` and 3 in
  `eu-west-1`. The default regional limit is 5, so request the increase before
  the first run (see [Operations](./operations.md#aws-service-quotas-common-first-run-blockers)).

## Credentials

There are two credential surfaces, and neither is a credential on a workload
cluster.

- **CAPA (management cluster).** The EKS control planes and node pools are
  provisioned by CAPA from a single SOPS-encrypted profile in Git at
  `mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml` (the
  `configSecret` on the `aws` infrastructure provider). To set or rotate it:

  ```sh
  mise -E aws run aws-bootstrap                                  # 1. CloudFormation stack
  mise run aws-credentials                                       # 2. clusterawsadm bootstrap credentials encode-as-profile
  # 3. paste the printed profile into
  #    mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
  #    as AWS_B64ENCODED_CREDENTIALS
  mise run sops-encrypt mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
  ```

- **ACK controllers (management cluster).** Same static SOPS credential
  pattern (`mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml`).
  Since issue #346 the S3, RDS, and IAM controllers all run on the management
  cluster and reconcile the per-workload-cluster `Bucket`, `DBInstance`, and
  reader `Role` CRs declared in `mgmt/aws/infrastructure/workload-resources/`.
  Workload clusters run no controllers and hold no credentials. The static
  principal's policy must cover the union of the former per-controller
  pod-identity roles; see [AWS authentication & IAM](./aws-iam.md) for the
  full action list and the least-privilege trade-off.

## Commit the identifiers

1. `mgmt/aws/addons/flux-apps/flux-instance.yaml` (the `cluster-vars`
   ConfigMap per region): set `AWS_ACCOUNT_ID` to your account ID, kept as
   the `postBuild` substitution channel for a future workload app.
2. `mgmt/aws/infrastructure/workload-resources/`: the account ID is a
   literal in the bucket names, the bucket policy ARNs, the reader-role
   trust principal, and the RDS resource-level policy ARNs (there is no
   `cluster-vars` ConfigMap on the management cluster). The reader role in
   `mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` wildcards the
   account ID instead.
3. The EKS version is pinned in each `cluster.yaml`
   (`mgmt/aws/clusters/<region>/<env>/`); bump it deliberately, not with a
   generic dependency update.

## Bootstrap, pivot, teardown

```sh
scripts/toolbox-run.sh bootstrap aws   # kind + Flux + CAPA; then pivot into eu-north-1-management
mise run mgmt-kubeconfig               # ~/.kube/krops-mgmt.yaml
mise -E aws run kubeconfigs            # workload kubeconfigs (aws eks update-kubeconfig per region)
```

Bootstrap ends with the pivot: the CAPI inventory moves from the disposable
`mgmt` kind cluster into the self-managed `eu-north-1-management` EKS cluster
and the kind cluster is deleted (see [Pivot recovery](./operations.md#pivot-recovery)).

Teardown is automated for `aws`. `scripts/toolbox-run.sh teardown aws`
suspends Flux, deletes every workload CAPI Cluster, runs a best-effort AWS
sweep for both workload regions and the self-managed management cluster
(nodegroups, EKS control planes, orphaned RDS, CAPA-tagged VPC resources,
versioned S3 buckets, CAPA and ACK IAM roles, the `krops-reader` user, and the
`clusterawsadm` CloudFormation stack), and removes the kind bootstrap cluster.
See [Teardown](./operations.md#teardown) for the controls.

## Reconciliation order

Management cluster (`mgmt/aws/`):

```
cert-manager > capi-operator > capi-system > capa-system > clusters (eu-north-1, eu-west-1)
                                     + caaph-system > flux-apps
ack-controllers > workload-resources
ack-controllers > aws-global-iam
konflate (no dependencies)
```

Workload clusters (`workload/<region>-01/`):

```
(empty: workload/base reconciles nothing since issue #346; the per-cluster
Flux instance stays installed, ready for a future application workload)
```

## Upgrading CAPA

CAPA minors: merge one at a time and let the management cluster settle before
the next. EKS cluster versions are not Renovate-managed and upgrade
independently of the provider (see
[Dependencies](./dependencies.md)).

## Known limitations

- The ACK RDS `DBInstance` sets no `dbSubnetGroupName`, so the instance lands
  in the region's **default VPC**, not the EKS VPC (CAPA creates the EKS VPC
  dynamically). See [Workload resources](./workload-resources.md).
- No GPU node pools: they would need a GPU instance type and quota, and the
  default run is the cheapest offered ARM and x86 shapes only.
