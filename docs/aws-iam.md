# AWS authentication & IAM

## ACK controllers on the management cluster (ambient credentials)

All ACK controllers (S3, RDS, IAM) run on the **management** cluster only
(issue #346). ACK controllers talk to the AWS API directly, so they do not
need to run inside the cluster whose resources they manage; the workload
clusters run no controllers and hold no credentials at all.

The controllers authenticate with credentials seeded imperatively from the
ambient `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (optionally `AWS_SESSION_TOKEN`),
which the `aws-credentials` mise task encodes into `AWS_B64ENCODED_CREDENTIALS`
for `capa-system`; the bootstrap/pivot code decodes it into a shared-credentials
file for `ack-system`, at bootstrap/pivot time (issue #379). This mirrors the Azure and GCP posture
("none at rest"): no static credentials are stored in Git. The kind management
cluster runs on kind (not EKS), so IRSA/Pod Identity is not available;
credentials are created imperatively by the bootstrap and pivot phases.

### One-time migration for a running management cluster

This step is needed only if a management cluster is already running with the
`aws-credentials` Secrets created by Flux from the now-deleted SOPS files.
Fresh bootstraps from the new `main` need no migration.

The two Flux Kustomizations use `prune: true`. Flux strips `kubectl`-written
annotations on its next apply, so adding the prune-disable annotation while
the old revision is still reconciled does not help. Suspend both
Kustomizations, annotate the live Secrets, merge to `main`, then resume:

```sh
flux suspend kustomization capa-system -n flux-system
flux suspend kustomization ack-controllers -n flux-system
kubectl annotate secret aws-credentials -n capa-system \
  kustomize.toolkit.fluxcd.io/prune=disabled --overwrite
kubectl annotate secret aws-credentials -n ack-system \
  kustomize.toolkit.fluxcd.io/prune=disabled --overwrite
# after merging to main:
flux reconcile source git flux-system -n flux-system
flux resume kustomization capa-system -n flux-system
flux resume kustomization ack-controllers -n flux-system
kubectl get secret aws-credentials -n capa-system
kubectl get secret aws-credentials -n ack-system
```

If the Secrets were already pruned before this runbook ran, re-create them by
re-running `bootstrap.sh` (or the equivalent `pivot.sh` path) with the
correct ambient credentials.

> Note: this is a deliberate, one-time exception to golden rule 1 (never
> mutate the clusters); the annotation is harmless and persists after
> bootstrap/pivot; it only prevents Flux from pruning the Secret during the
> transition.

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
  `s3:DeleteBucketPolicy`, `s3:GetEncryptionConfiguration`/
  `PutEncryptionConfiguration`, `s3:GetAccelerateConfiguration`,
  `s3:GetAnalyticsConfiguration`, `s3:GetIntelligentTieringConfiguration`,
  `s3:GetInventoryConfiguration`, `s3:GetLifecycleConfiguration`,
  `s3:GetMetricsConfiguration`, `s3:GetReplicationConfiguration`,
  lifecycle/replication/accelerate/
  analytics/inventory/metrics/intelligent-tiering configuration Get+Put,
  `s3:ListBucket`, `s3:TagResource` (the `CreateBucket` tagSet is authorized
  against it, see #352/#353). The [DeleteBucketTagging API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketTagging.html)
  used for tag-removal drift requires `s3:PutBucketTagging`, covered by
  `s3:PutBucket*`. `s3:UntagResource`/`s3:ListTagsForResource` are not needed: the ACK
  S3 controller only calls them for directory buckets, which krops does not
  create.
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
  (`iam:CreateUser`/`UpdateUser`/`DeleteUser`/`PutUserPolicy`/`DeleteUserPolicy`/
  `GetUser`/`GetUserPolicy`/`ListUserPolicies`/`ListAttachedUserPolicies`/
  `TagUser`/`UntagUser`/`ListUserTags`). The pinned IAM controller reads
  attached and inline policies and tags on every refresh, and removes inline
  policies and tags during drift reconciliation (even though krops declares
  no attached managed policies)

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

   ```text
   https://signin.aws.amazon.com/switchrole?roleName=krops-eu-north-1-workload-reader&account=<account-id>
   ```

3. Browse the `krops-*` S3 buckets and RDS instances (switch the console
   region to eu-north-1/eu-west-1 for the databases).

## CI access: GitHub Actions OIDC and the `krops-ci-e2e` role

CI authenticates to the e2e account (`120392301094`) through GitHub's OIDC
provider and assumes a least-privilege role for the e2e lifecycle
(issue #379). No AWS access keys are stored; every session is short-lived.
Created imperatively, additive only.

### Resources

- OIDC provider:
  `arn:aws:iam::120392301094:oidc-provider/token.actions.githubusercontent.com`
  (audience `sts.amazonaws.com`).
- Role: `arn:aws:iam::120392301094:role/krops-ci-e2e` (max session duration
  4 hours; e2e runs must complete within this window).
- Five customer-managed policies, grouped by concern:
  `krops-ci-e2e-capa-ec2`, `krops-ci-e2e-capa-eks`, `krops-ci-e2e-ack-mgmt`,
  `krops-ci-e2e-sweep`, `krops-ci-e2e-self-deny`.

### Trust policy

The role trusts only the GitHub OIDC provider with audience
`sts.amazonaws.com` and `sub` matching `repo:polarsquad/krops:*` (any ref in
that repo). To narrow later, replace the `StringLike` `sub` value with
explicit refs, for example
`repo:polarsquad/krops:ref:refs/heads/main`, via
`iam:UpdateAssumeRolePolicy`.

### Permission groupings

Derived from the repo, not guesswork: the CAPA surface comes from the pinned
`clusterawsadm` 2.13.1 `print-policy` output evaluated with krops' feature
gates (`EKS=true,EKSEnableIAM=true,EKSAllowAddRoles=true,MachinePool=true`),
the sweep surface from `teardown.sh` / `bootstrap-rs/src/teardown.rs`, and
the ACK surface from the prerequisites in [AWS environment](./aws.md).

- EC2/VPC (`krops-ci-e2e-capa-ec2`): the full describe set; creates only in
  `eu-north-1` and `eu-west-1` (`aws:RequestedRegion`); mutations (delete,
  modify, attach/detach, associate, authorize/revoke, release) only on
  resources carrying a `sigs.k8s.io/cluster-api-provider-aws/role` tag
  (CAPA-created), plus security-group rule and delete operations on the
  EKS-created cluster security groups (`aws:eks:cluster-name` = `default_*`);
  `CreateTags`/`DeleteTags` only with CAPA tag keys.
- EKS (`krops-ci-e2e-capa-eks`): cluster, nodegroup, addon, and access-entry
  CRUD scoped to `default_*` clusters in the two e2e regions (the non-krops
  `training` cluster is out of scope); the two EKS service-linked roles;
  CAPA per-cluster IAM roles on `capa_*` and the cluster name prefixes;
  `iam:PassRole` to `eks.amazonaws.com` for the clusterawsadm stack roles
  and `capa_*`; SSM optimized-AMI parameters, KMS grants on
  `alias/cluster-api-provider-aws-*`, Secrets Manager on
  `aws.cluster.x-k8s.io/*` secrets.
- Management ACK controllers (`krops-ci-e2e-ack-mgmt`): `krops-*` IAM role
  and user management (the `krops-ack-*` roles, the `krops-*-reader` roles,
  the `krops-reader` user) and pod identity associations on `default_*`
  clusters. No OIDC provider management anywhere: IRSA is unused (pod
  identity instead), which also protects the GitHub OIDC provider itself.
- Orphan sweep (`krops-ci-e2e-sweep`): `sts:GetCallerIdentity`; RDS delete
  on `krops-*` instances; S3 version purge and bucket delete on `krops-*`
  buckets; IAM cleanup on `krops-*`/`capa_*` roles, their instance profiles,
  and `krops-*` users; CloudFormation delete on the
  `cluster-api-provider-aws-sigs-k8s-io` stack in both regions.
- Self-escalation guard (`krops-ci-e2e-self-deny`): explicit Deny on the
  role's own ARN for `PutRolePolicy`, `DeleteRolePolicy`,
  `AttachRolePolicy`, `DetachRolePolicy`, `UpdateAssumeRolePolicy`,
  `DeleteRole`, and permissions-boundary changes. The `role/krops-*` globs
  in `ack-mgmt` and `sweep` match `krops-ci-e2e` itself; without this Deny a
  workflow session could grant itself arbitrary inline permissions or widen
  its own trust (verified with `simulate-principal-policy`: the four
  escalation actions are now `explicitDeny`, lifecycle actions on other
  `krops-*` roles remain `allowed`).

Narrowed relative to the upstream CAPA policy, with the repo as ground
truth: no `ec2:RunInstances`/`TerminateInstances` (no EC2 machine pools), no
ELB/Auto Scaling writes (no `AWSMachinePool` or `AWSCluster`), no spot or
fargate service-linked roles (all pools `onDemand`, no fargate profiles), no
IPAM/IPv6 actions (IPv4 clusters), no launch-template writes (the
`AWSManagedMachinePool` specs declare none).

### Not covered (operator steps)

- First-time creation of the clusterawsadm CloudFormation stack
  (the `aws-bootstrap` task, run in the toolbox per [docs/aws.md](./aws.md)) needs `cloudformation:CreateStack` plus
  IAM writes on the `*.cluster-api-provider-aws.sigs.k8s.io` roles. The
  stack is already `CREATE_COMPLETE` (2026-09-01, per #143), so recreating
  it stays an operator step.
- Service-quota increases (see [Operations](./operations.md)) and the
  one-time `iam:CreateLoginProfile` for `krops-reader` (above) stay operator
  steps.
- CAPA and ACK data-plane actions: the `krops-ci-e2e` role must hold the same
  AWS action grants that were previously held by the `capi-demo` user principal,
  covering the full S3, RDS, and IAM action sets documented
  [above](#least-privilege-trade-off-the-static-principals-union-scope)
  for a successful full e2e reconciliation. These actions are account-side
  prerequisites the operator must add to the role policies.

### Assuming the role from a workflow

```yaml
permissions:
  id-token: write
  contents: read
steps:
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::120392301094:role/krops-ci-e2e
      aws-region: eu-north-1
```

The action exchanges the workflow's OIDC token for session credentials;
nothing is stored. The `aws-orphan-report` workflow assumes this role for
read-only orphan discovery (issue #380). Full e2e CI wiring is tracked in #185.

### Revocation

- Cut all CI access: delete the OIDC provider
  (`aws iam delete-open-id-connect-provider --open-id-connect-provider-arn
  arn:aws:iam::120392301094:oidc-provider/token.actions.githubusercontent.com`).
  The role remains but can no longer be assumed from GitHub. Note this stops
  NEW assumptions only: sessions already issued run until their granted
  expiry (4 hour maximum), so access is not cut instantly during an
  incident.
- Narrow instead: tighten the trust policy's `sub` condition to specific
  refs (above).
- Rotate: nothing to rotate; STS sessions are short-lived (1 hour typical,
  4 hour maximum) and independent of the short-lived OIDC token.

## E2E account incident and credential revocation

If a credential is suspected compromised, contain it immediately, then assess impact.

### Containment

**(a) `capi-demo` long-lived IAM key** (stored in `.env` only; used by ambient credential flow):

1. Disable the key immediately:

   ```sh
   aws iam update-access-key --user-name capi-demo --access-key-id <ACCESS_KEY_ID> --status Inactive
   ```

2. Issue a new key and update `.env`, following
   [Setting / rotating AWS credentials](./secrets.md#setting--rotating-aws-credentials).
   No re-encryption of SOPS secrets is needed: credentials are seeded
   imperatively from `.env` at bootstrap/pivot time.
3. Verify Flux reconciliation and successful AWS operations from CAPA and all
   management ACK controllers (S3, RDS, IAM), plus replacement adoption by any
   running bootstrap process. Keep the compromised key disabled throughout
   recovery, then delete it once those checks pass.

**(b) `krops-ci-e2e` OIDC role** (normally covers itself; see [CI access](#ci-access-github-actions-oidc-and-the-krops-ci-e2e-role)):

1. Delete or update the OIDC provider to block new assumptions; verify that
   attempts to obtain new sessions fail after IAM propagation.
2. Attach the following inline Deny to the role to revoke existing sessions.
   Use an explicit UTC cutoff (`YYYY-MM-DDTHH:MM:SSZ`) covering sessions issued
   while the assumption block was propagating. Refresh the cutoff if later
   sessions were issued before the block became effective. See
   [AWS role-session revocation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html)
   for the cutoff and propagation behavior:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Deny",
         "Action": "*",
         "Resource": "*",
         "Condition": {
           "DateLessThan": {
             "aws:TokenIssueTime": "<ISO-8601 timestamp of the revocation>"
           }
         }
       }
     ]
   }
   ```

3. Verify the deployed policy and denied access with affected session
   credentials. Keep the Deny until **every affected session has expired**,
   even if the investigation finishes sooner: removing it can restore access
   to unexpired credentials. Read `MaxSessionDuration` with
   `aws iam get-role --role-name krops-ci-e2e`; the documented maximum is four
   hours, but use the maximum applicable when the affected sessions were
   issued, not a newly lowered limit. If individual expiry times are unknown,
   retain the Deny until at least the final cutoff plus that maximum duration.
   Restore new assumptions only after fixing the compromise; new trusted
   sessions issued after the cutoff can work while the Deny remains.

**(c) `krops-reader` console login:**

1. Delete the login profile to revoke console access:

   ```sh
   aws iam delete-login-profile --user-name krops-reader
   ```

2. Deleting a login profile prevents new password logins; it does not revoke
   existing console or assumed-role credentials. In
   `mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml`, add an explicit
   inline Deny of `sts:AssumeRole` on `arn:aws:iam::*:role/krops-*-reader`
   to block new assumptions by the reader, including from existing user
   sessions. Also add the timestamp-conditioned Deny from (b) as a separate
   user inline policy in that same Git-owned file, with a UTC cutoff covering
   all compromised IAM-user console sessions. This revokes their permissions
   as described in [AWS user-session revocation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_passwords_admin-change-user.html).
   Preserve the unconditional assumption block through investigation and
   password recovery. Refresh the user cutoff if sessions were issued during
   containment propagation; verify the deployed user policy and that old user
   credentials cannot call `sts:AssumeRole`. The user-session policy does not
   revoke credentials already issued by a role, so step 3 is also required.
3. In `mgmt/aws/infrastructure/workload-resources/role.yaml`, add the
   timestamp-conditioned inline Deny from (b) to **both**
   `krops-eu-north-1-workload-reader` and
   `krops-eu-west-1-workload-reader`. A user-policy change alone does not
   remove permissions from credentials already issued by those roles. This
   role Deny affects all sessions before the cutoff, including other readers.
   Merge these Git-owned policy changes to `main`; verify Flux readiness,
   ACK `ResourceSynced` status, the actual AWS user/role policies, blocked new
   assumptions, and denied access with pre-cutoff role credentials. Do not
   patch the cluster or rely on an out-of-band IAM edit that ACK could undo.
   Containment is not established while reconciliation or IAM propagation
   remains incomplete. Refresh the UTC cutoff to cover sessions issued during
   that interval.
4. Check `MaxSessionDuration` on each deployed role with `aws iam get-role`.
   Retain each role Deny until all affected sessions expire, using the final
   cutoff plus the maximum duration applicable at issuance if exact expiries
   are unknown. Investigation completion or password replacement is not a
   reason to remove it early.
5. Recreate the login profile with a new, unique password and required reset
   only after addressing the compromise. Restore the user's assumption
   permission through Git only after the user-session Deny has converged and
   been verified to block pre-cutoff user credentials; retain that Deny so old
   user sessions cannot obtain fresh role credentials after password recovery.
   Verify a trusted post-cutoff login can assume the roles while an old user
   session cannot. Keep the user-session Deny until **all affected IAM-user
   sessions expire**: establish their expiry times or a verified upper bound
   for the console/temporary credential sessions actually issued. A role's
   `MaxSessionDuration` is not the IAM-user session lifetime. If user-session
   expiry cannot be established, leave that Deny in place. Likewise retain
   both role-session Denies until their separate expiry condition in step 4
   passes. Remove each Deny through Git only after its respective user or role
   credentials have expired, then verify Flux/ACK/AWS convergence.

**(d) age key compromise:**

The age identity exposes both AWS profiles, regional GitHub pull PATs, and
both konflate tokens, including still-valid values in historical ciphertext.
Shared bootstrap credentials may also be affected. Follow
[Age-key compromise recovery](./secrets.md#age-key-compromise-recovery) to
revoke exposed credentials and replace the encryption identity and data keys;
routine credential rotation alone is insufficient.

### Impact assessment

Review CloudTrail event history for the affected principal (90 days of management events):

```sh
# IAM/global-service events (these are always recorded in us-east-1)
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=Username,AttributeValue=capi-demo \
  --region us-east-1

# Resource events in the krops e2e regions
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=Username,AttributeValue=capi-demo \
  --region eu-north-1

aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=Username,AttributeValue=capi-demo \
  --region eu-west-1
```

The member-account CloudTrail covers API calls made within the account.
The long-term organization trail is in the log-archive account
(Control Tower-managed); contact the account owner
(<joseph.shriner@polarsquad.com>) to retrieve it.

### Cleanup and notification

1. Clean up any resources provisioned by the compromised credential using the [teardown](./operations.md#teardown) path.
2. Notify the account owner (<joseph.shriner@polarsquad.com>) and, for OIDC or age-key compromise, the repository maintainers.

## EKS worker lifecycle tag reconciliation

`mgmt/aws/infrastructure/worker-run-tags/` runs a minute CronJob in
`ack-system`, using the existing SOPS `aws-credentials` Secret's `credentials`
key and `default` profile. It uses no Kubernetes API token or node IMDS
credentials. Its effective IAM permissions are unknown from Git: an operator
must grant and verify the following additional permissions on the static
principal before deployment. This repository does not modify that principal.
The read-side ASG/EC2 actions require `Resource: "*"`; requested regions are
limited. EKS reads use the three named clusters and their nodegroups, following
[AWS EKS action/resource authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelastickubernetesservice.html).
EC2 mutation uses the resource and tag conditions documented in
[AWS EC2 tagging permissions](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["eks:DescribeCluster", "eks:ListNodegroups"],
      "Resource": [
        "arn:aws:eks:eu-north-1:120392301094:cluster/default_eu-north-1-management-control-plane",
        "arn:aws:eks:eu-north-1:120392301094:cluster/default_eu-north-1-workload-control-plane",
        "arn:aws:eks:eu-west-1:120392301094:cluster/default_eu-west-1-workload-control-plane"
      ],
      "Condition": {"StringEquals": {"aws:RequestedRegion": ["eu-north-1", "eu-west-1"]}}
    },
    {
      "Effect": "Allow",
      "Action": "eks:DescribeNodegroup",
      "Resource": [
        "arn:aws:eks:eu-north-1:120392301094:nodegroup/default_eu-north-1-management-control-plane/*/*",
        "arn:aws:eks:eu-north-1:120392301094:nodegroup/default_eu-north-1-workload-control-plane/*/*",
        "arn:aws:eks:eu-west-1:120392301094:nodegroup/default_eu-west-1-workload-control-plane/*/*"
      ],
      "Condition": {"StringEquals": {"aws:RequestedRegion": ["eu-north-1", "eu-west-1"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["autoscaling:DescribeAutoScalingGroups", "ec2:DescribeInstances"],
      "Resource": "*",
      "Condition": {"StringEquals": {"aws:RequestedRegion": ["eu-north-1", "eu-west-1"]}}
    },
    {
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": [
        "arn:aws:ec2:eu-north-1:120392301094:instance/*",
        "arn:aws:ec2:eu-west-1:120392301094:instance/*"
      ],
      "Condition": {
        "StringEquals": {
          "aws:RequestedRegion": ["eu-north-1", "eu-west-1"],
          "ec2:ResourceTag/aws:eks:cluster-name": [
            "default_eu-north-1-management-control-plane", "default_eu-north-1-workload-control-plane", "default_eu-west-1-workload-control-plane"
          ]
        },
        "ForAllValues:StringEquals": {
          "aws:TagKeys": [
            "krops.io/run-id", "krops.io/revision", "krops.io/expires-at", "krops.io/run-kind"
          ]
        },
        "Null": {"aws:TagKeys": "false"}
      }
    }
  ]
}
```

These restrictions apply to this policy's grant; existing broader grants on the
shared principal can still authorize other actions. Verify the effective policy
and exercise a real missing-tag backfill before claiming adoption. The script
also checks the caller account, exact region/cluster allowlist, fresh ASG
membership, and the instance's `aws:eks:cluster-name` ownership immediately
before writing only the four lifecycle keys. The IAM ownership condition checks
ownership at write time. API denials produce a failed Job; they never trigger
an unscoped fallback or a credential replacement.
