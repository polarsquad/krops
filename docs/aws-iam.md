# AWS authentication & IAM

## EKS Pod Identity (no static keys on workload clusters)

The ACK S3, RDS, and IAM controllers on the workload clusters carry
**no credentials**. Instead:

- Each EKS control plane enables the `eks-pod-identity-agent` addon
  (declared in the CAPI cluster spec).
- The **management** cluster runs ACK IAM + EKS controllers
  (`mgmt/aws/infrastructure/ack-controllers/`, authenticated with the same
  SOPS-encrypted credential pattern as CAPA) which declaratively create:
  - an IAM `Role` per controller, trusted by `pods.eks.amazonaws.com`:
    - `krops-ack-s3-controller` — scoped to `krops-*` buckets only
    - `krops-ack-rds-controller` — RDS management scoped to `krops-*`
      RDS resources (plus read-only `rds:Describe*`), the
      `secretsmanager:CreateSecret`/`TagResource`/`RotateSecret` actions on
      `rds!*` secrets required by `manageMasterUserPassword`,
      `kms:DescribeKey` and grant management (`CreateGrant`/`ListGrants`/
      `RevokeGrant`, restricted with `kms:GrantIsForAWSResource`) so RDS can
      use the default `aws/rds` and `aws/secretsmanager` KMS keys — without
      these `CreateDBInstance` fails with `KMSKeyNotAccessibleFault` — and
      `iam:CreateServiceLinkedRole` for `AWSServiceRoleForRDS` (needed the
      first time an RDS instance is created in the account)
    - `krops-ack-iam-controller` — IAM role management scoped to
      `krops-*` roles only. Known trade-off: name-scoped `iam:CreateRole`
      + `iam:PutRolePolicy` is still a privilege-escalation surface (any
      permission can be granted to a role, as long as it is named
      `krops-*`), consistent with the pragmatic name-based scoping used
      for the other controllers
  - a `PodIdentityAssociation` per cluster and controller binding the
    `ack-s3-controller` / `ack-rds-controller` / `ack-iam-controller`
    ServiceAccounts to their roles.

Pod Identity is used instead of IRSA because its trust policy is static — it
does not embed a per-cluster OIDC provider ID, so the whole chain can live in
Git before the clusters exist. ACK retries the associations until CAPA has
finished provisioning the EKS clusters.

## Per-cluster read-only IAM roles

`workload/base/iam-roles/role.yaml` has each cluster's ACK IAM controller create
one read-only IAM role (`krops-<cluster>-reader`) — IAM is global, so the
cluster name is part of the role name to keep the two clusters from fighting
over one role:

- trust policy: the AWS account root (`arn:aws:iam::<account>:root`,
  `sts:AssumeRole`) — any principal in the account that is itself allowed to
  assume the role can use it
- read-only permissions covering the resources this repo creates on **both**
  clusters: `krops-*` S3 buckets (bucket + object reads) and `krops-*`
  RDS instances (`rds:DescribeDBInstances`, `rds:ListTagsForResource` —
  only Describe actions that support resource-level scoping)

## Console access: the `krops-reader` IAM user

`mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` has the
**management** cluster's ACK IAM controller create one IAM `User`
(`krops-reader`) whose only permission is `sts:AssumeRole` on
`arn:aws:iam::*:role/krops-*-reader` — it can see nothing directly and is
just a doorway into the per-cluster reader roles above.

The ACK IAM controller has no `LoginProfile` resource, so the console
password cannot be declared in Git. Set it **once** imperatively after the
user has been reconciled:

```sh
aws iam create-login-profile --user-name krops-reader \
  --password '<initial-password>' --password-reset-required
```

Then, to browse the repo-created resources in the AWS console:

1. Sign in at `https://<account-id>.signin.aws.amazon.com/console` as
   `krops-reader` (you will be prompted to set a new password on first
   login).
2. Use **Switch Role** (account menu, top right) with the account ID and role
   name `krops-eu-north-1-workload-reader` or
   `krops-eu-west-1-workload-reader` — or use the direct link:

   ```
   https://signin.aws.amazon.com/switchrole?roleName=krops-eu-north-1-workload-reader&account=<account-id>
   ```

3. Browse the `krops-*` S3 buckets and RDS instances (switch the console
   region to eu-north-1/eu-west-1 for the databases).
