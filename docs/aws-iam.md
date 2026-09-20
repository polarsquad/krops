# AWS authentication & IAM

## ACK controllers on the management cluster (static SOPS credentials)

All ACK controllers (S3, RDS, IAM) run on the **management** cluster only
(issue #346). ACK controllers talk to the AWS API directly, so they do not
need to run inside the cluster whose resources they manage; the workload
clusters run no controllers and hold no credentials at all.

The controllers authenticate with the same SOPS-encrypted static credential
pattern as CAPA
(`mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml`). The
kind management cluster runs on kind (not EKS), so IRSA/Pod Identity is not
available there; static credentials via SOPS is the established pattern.

### Least-privilege trade-off: the static principal's union scope

Before #346 each workload-cluster controller assumed its own scoped IAM role
via EKS Pod Identity (`krops-ack-s3-controller`, `krops-ack-rds-controller`,
`krops-ack-iam-controller`, declared in the deleted
`mgmt/aws/infrastructure/ack-pod-identity/`). Moving the controllers to the
management cluster deleted those roles, so the single static principal behind
`aws-credentials` now needs the **union** of their former policies (granted
outside this repo, same as the CAPA permissions):

- **S3**: `s3:ListAllMyBuckets` + `s3:GetBucketLocation` on `*`, and bucket
  management on `arn:aws:s3:::krops-*` only: `s3:CreateBucket`,
  `s3:DeleteBucket`, `s3:GetBucket*`/`s3:PutBucket*`,
  `s3:DeleteBucketPolicy`, encryption/lifecycle/replication/accelerate/
  analytics/inventory/metrics/intelligent-tiering configuration Get+Put,
  `s3:ListBucket`, `s3:TagResource`/`s3:UntagResource`/
  `s3:DeleteBucketTagging`/`s3:ListTagsForResource`
- **RDS**: `rds:Describe*` + `rds:ListTagsForResource` on `*`; instance
  management (`rds:CreateDBInstance`/`ModifyDBInstance`/`DeleteDBInstance`/
  `RebootDBInstance`/`StartDBInstance`/`StopDBInstance`,
  `rds:AddTagsToResource`/`RemoveTagsFromResource`) on
  `arn:aws:rds:*:*:*:krops-*`; `secretsmanager:CreateSecret`/`TagResource`/
  `RotateSecret` on `arn:aws:secretsmanager:*:*:secret:rds!*` (required by
  `manageMasterUserPassword`); `kms:DescribeKey` on `*` plus
  `kms:CreateGrant`/`ListGrants`/`RevokeGrant` restricted with
  `kms:GrantIsForAWSResource` (so RDS can use the default `aws/rds` and
  `aws/secretsmanager` KMS keys; without these `CreateDBInstance` fails with
  `KMSKeyNotAccessibleFault`); `iam:CreateServiceLinkedRole` scoped to
  `AWSServiceRoleForRDS` (needed the first time an RDS instance is created
  in the account)
- **IAM**: role management (`iam:CreateRole`/`DeleteRole`/`GetRole`/
  `UpdateRole`/`UpdateRoleDescription`/`UpdateAssumeRolePolicy`/
  `PutRolePolicy`/`DeleteRolePolicy`/`GetRolePolicy`/`ListRolePolicies`/
  `ListAttachedRolePolicies`/`ListInstanceProfilesForRole`/`TagRole`/
  `UntagRole`/`ListRoleTags`) scoped to `arn:aws:iam::*:role/krops-*`, plus
  the user actions for the `krops-reader` console user
  (`iam:CreateUser`/`PutUserPolicy`/`GetUser`/`GetUserPolicy`/`TagUser`)

The trade-off is real: name-scoped `iam:CreateRole` + `iam:PutRolePolicy` is
still a privilege-escalation surface (any permission can be granted to a
role, as long as it is named `krops-*`), and the union now sits on one
long-lived static principal instead of three short-lived pod-identity
sessions. Accepted because the management cluster is already the only
cluster with static AWS credentials in Git and already owns every `Cluster`
object; the workload clusters shed their last credential and controller in
exchange.

## Per-cluster read-only IAM roles

`mgmt/aws/infrastructure/workload-resources/role.yaml` has the management
cluster's ACK IAM controller create one read-only IAM role per workload
cluster (`krops-<cluster>-reader`). IAM is global, so the cluster name is
part of the role name to keep the two clusters from fighting over one role:

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
