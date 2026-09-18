# AWS authentication & IAM

## ACK runs only on the management cluster (#346)

The ACK S3, RDS, and IAM controllers (`mgmt/aws/infrastructure/ack-controllers/`)
run once, on the management cluster, and manage resources for every workload
cluster directly through the AWS API. No workload cluster runs an ACK
controller or holds any AWS credential.

This replaced an earlier design where each workload cluster ran its own copy
of the S3/RDS/IAM controllers, authenticated via EKS Pod Identity against a
per-controller IAM role the management cluster declared ahead of time
(`krops-ack-s3-controller`, `krops-ack-rds-controller`,
`krops-ack-iam-controller`). Pod Identity only solves "how does a controller
running on cluster X authenticate to AWS" — since ACK's job is calling the
AWS API, not talking to the cluster it happens to run on, running one
instance on the management cluster removes the need for that authentication
path entirely.

### Static credentials, and what that costs

The ACK controllers authenticate with the same SOPS-encrypted static AWS
credentials CAPA already uses (`aws-credentials.sops.yaml`, see
[Secret management](./secrets.md)). Because these controllers now manage S3
buckets and RDS instances directly — work that used to be scoped to the
narrow, ACK-created Pod Identity roles — that shared static credential
needs a wider set of permissions than before:

The exact permission set is in
[Minimum IAM policy for the ACK credential](#minimum-iam-policy-for-the-ack-credential)
below.

This is a real least-privilege trade-off, not just a wiring change: before,
the bootstrap credential only needed to create narrowly-scoped IAM roles and
Pod Identity associations, and the actual S3/RDS permissions lived on those
short-lived, cluster-bound roles. Consolidating onto one controller means one
credential now carries all of it. The mitigation is the same one already used
throughout this repo: every permission above is scoped to the `krops-*`
name/ARN prefix, never a blanket grant.

### Minimum IAM policy for the ACK credential

The three policy documents below are the least-privilege record of what the
static credential needs. They are the inline policies of the former
`krops-ack-*-controller` Pod Identity roles, carried over verbatim; attach
them (or their union) to the principal behind `aws-credentials.sops.yaml`.
Keep this section current when a controller needs a new action.

#### `s3-bucket-management`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBuckets",
      "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
      "Resource": "*"
    },
    {
      "Sid": "ManageKrmOpsBuckets",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:GetBucket*",
        "s3:PutBucket*",
        "s3:DeleteBucketPolicy",
        "s3:GetEncryptionConfiguration",
        "s3:PutEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:GetReplicationConfiguration",
        "s3:PutReplicationConfiguration",
        "s3:GetAccelerateConfiguration",
        "s3:PutAccelerateConfiguration",
        "s3:GetAnalyticsConfiguration",
        "s3:PutAnalyticsConfiguration",
        "s3:GetInventoryConfiguration",
        "s3:PutInventoryConfiguration",
        "s3:GetMetricsConfiguration",
        "s3:PutMetricsConfiguration",
        "s3:GetIntelligentTieringConfiguration",
        "s3:PutIntelligentTieringConfiguration",
        "s3:ListBucket",
        "s3:TagResource",
        "s3:UntagResource",
        "s3:DeleteBucketTagging",
        "s3:ListTagsForResource"
      ],
      "Resource": "arn:aws:s3:::krops-*"
    }
  ]
}
```

#### `rds-instance-management`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DescribeRDS",
      "Effect": "Allow",
      "Action": ["rds:Describe*", "rds:ListTagsForResource"],
      "Resource": "*"
    },
    {
      "Sid": "ManageKropsInstances",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBInstance",
        "rds:ModifyDBInstance",
        "rds:DeleteDBInstance",
        "rds:RebootDBInstance",
        "rds:StartDBInstance",
        "rds:StopDBInstance",
        "rds:AddTagsToResource",
        "rds:RemoveTagsFromResource"
      ],
      "Resource": "arn:aws:rds:*:*:*:krops-*"
    },
    {
      "Sid": "ManagedMasterPasswordSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:TagResource",
        "secretsmanager:RotateSecret"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:rds!*"
    },
    {
      "Sid": "DescribeDefaultKMSKeys",
      "Effect": "Allow",
      "Action": "kms:DescribeKey",
      "Resource": "*"
    },
    {
      "Sid": "KMSGrantsForAWSResources",
      "Effect": "Allow",
      "Action": [
        "kms:CreateGrant",
        "kms:ListGrants",
        "kms:RevokeGrant"
      ],
      "Resource": "*",
      "Condition": {
        "Bool": { "kms:GrantIsForAWSResource": "true" }
      }
    },
    {
      "Sid": "RDSServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::*:role/aws-service-role/rds.amazonaws.com/AWSServiceRoleForRDS",
      "Condition": {
        "StringLike": { "iam:AWSServiceName": "rds.amazonaws.com" }
      }
    }
  ]
}
```

#### `iam-role-management`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ManageKropsRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:UpdateRole",
        "iam:UpdateRoleDescription",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:ListRoleTags"
      ],
      "Resource": "arn:aws:iam::*:role/krops-*"
    }
  ]
}
```

### Why the resources carry `adoption-policy: adopt-or-create`

Every Bucket, DBInstance and Role in `mgmt/aws/infrastructure/workload-resources/`
and the reader User carry `services.k8s.aws/adoption-policy: adopt-or-create`.
During bootstrap the kind cluster reconciles `mgmt/aws/infrastructure` first
and its ACK controllers create the AWS resources; `clusterctl move` does not
carry ACK CRs, so after the pivot the management cluster's controllers find
the resources already present. Without the annotation they would report
`ACK.Terminal` "Resource already exists"; with it they adopt the existing
resource. Verify with `ACK.ResourceSynced=True`, not only Flux `Ready`.

## Multi-region resources from a single controller

Each ACK controller HelmRelease defaults to one AWS region (the `aws.region`
Helm value, `eu-north-1`). A resource in a different region (eu-west-1)
overrides this per-resource with the `services.k8s.aws/region` annotation on
its CR — see `mgmt/aws/infrastructure/workload-resources/buckets.yaml` and
`dbinstances.yaml`. IAM is a global service, so its roles need no such
annotation.

## Per-cluster resources, declared on the management cluster

`mgmt/aws/infrastructure/workload-resources/` creates, for every workload
cluster, the same three resources the old per-cluster design created:

- a `Bucket` (`buckets.yaml`, was `workload/base/s3-buckets/bucket.yaml`)
- a `DBInstance` (`dbinstances.yaml`, was
  `workload/base/rds-instances/dbinstance.yaml`)
- a reader `Role` (`roles.yaml`, was `workload/base/iam-roles/role.yaml`)

There is no `cluster-vars` ConfigMap on the management cluster (no per-cluster
Flux `postBuild` substitution target lives there), so `AWS_ACCOUNT_ID`,
`AWS_REGION`, and `CLUSTER_NAME` are literal values in each manifest instead
of substituted — the same reason `aws-global-iam/reader-user.yaml` already
wildcards the account ID, and the account ID is already a literal in
`mgmt/aws/addons/flux-apps/flux-instance.yaml`.

## Per-cluster read-only IAM roles

`mgmt/aws/infrastructure/workload-resources/roles.yaml` has the management
cluster's ACK IAM controller create one read-only IAM role
(`krops-<cluster>-reader`) per workload cluster. IAM is global, so the
cluster name is part of the role name to keep the two clusters from fighting
over one role:

- trust policy: the AWS account root (`arn:aws:iam::<account>:root`,
  `sts:AssumeRole`): any principal in the account that is itself allowed to
  assume the role can use it
- read-only permissions covering the resources this repo creates on **both**
  clusters: `krops-*` S3 buckets (bucket + object reads) and `krops-*`
  RDS instances (`rds:DescribeDBInstances`, `rds:ListTagsForResource`:
  only Describe actions that support resource-level scoping)

## Console access: the `krops-reader` IAM user

`mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` has the
**management** cluster's ACK IAM controller create one IAM `User`
(`krops-reader`) whose only permission is `sts:AssumeRole` on
`arn:aws:iam::*:role/krops-*-reader`: it can see nothing directly and is
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
   `krops-eu-west-1-workload-reader`, or use the direct link:

   ```
   https://signin.aws.amazon.com/switchrole?roleName=krops-eu-north-1-workload-reader&account=<account-id>
   ```

3. Browse the `krops-*` S3 buckets and RDS instances (switch the console
   region to eu-north-1/eu-west-1 for the databases).
