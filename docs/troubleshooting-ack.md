# ACK troubleshooting

Symptoms, causes, and recovery steps for the AWS Controllers for Kubernetes
(ACK) controllers that run on the management cluster. The page covers only the
custom resources krops ships: the S3 `Bucket`, RDS `DBInstance`, IAM `Role`
and IAM `User` under `mgmt/aws/infrastructure/workload-resources/` and
`mgmt/aws/infrastructure/aws-global-iam/`. For how the controllers
authenticate, see [AWS authentication & IAM](./aws-iam.md); for what they
create, see [Workload resources](./workload-resources.md).

The first step for every entry below is the same: read the condition
**message** on the custom resource, not only the status column. The AWS error
text (`AccessDenied`, `EntityAlreadyExists`, and so on) only appears there.

```sh
# Management cluster. One CR's conditions (here the eu-north-1 data bucket):
kubectl -n ack-system get bucket.s3.services.k8s.aws data-eu-north-1 \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} :: {.message}{"\n"}{end}'

# Sync state of every CR of one kind at a glance (role.iam, user.iam,
# bucket.s3, dbinstance.rds):
kubectl -n ack-system get role.iam.services.k8s.aws \
  -o custom-columns='NAME:.metadata.name,SYNCED:.status.conditions[?(@.type=="ACK.ResourceSynced")].status'
```

## Terminal "Resource already exists" after the pivot

**What.** A CR sits at `ACK.Terminal=True` with a message that the resource
already exists, and `ACK.ResourceSynced` never becomes `True`. Flux still
reports the Kustomization `Ready`.

**Where it bites.** `aws` environment, after the pivot (the first reconcile on
the self-managed management cluster).

**Why.** A krops design choice, not an ACK fault. The kind bootstrap cluster
syncs `mgmt/aws` and its ACK controllers create the real AWS objects before the
pivot; after the pivot the management cluster's Flux applies the same CRs to
controllers that find the object already there. Without an adoption policy the
controller treats that as a create conflict. Every krops ACK CR therefore
carries `services.k8s.aws/adoption-policy: adopt-or-create`, which makes the
controller adopt an existing object and create it otherwise. Per the ACK
documentation, after adopting, the controller updates the AWS object so the
manifest stays the source of truth. The header comments in
`mgmt/aws/infrastructure/workload-resources/bucket.yaml`, `dbinstance.yaml`,
`role.yaml` and `mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` carry
the same explanation.

**Workaround / status.** Add the annotation to any new ACK CR declared under
`mgmt/aws/infrastructure/`; a CR without it hits this on the first pivot. The
CRs krops ships already set the field the controller looks the object up by
(`spec.name`, or `spec.dbInstanceIdentifier` for the `DBInstance`), so no
`adoption-fields` annotation is needed, and the controller needs read
permission on the object to find it. Recovery for a CR that is already
Terminal is not established here: whether adding the annotation alone clears
the condition has not been verified on a krops run, and deleting the CR is not
a safe default because a CR that has adopted an object deletes it from AWS
(see the next entry).

Sources:

- [ACK features: resource adoption](https://aws-controllers-k8s.github.io/community/docs/user-docs/features/)
  (`adopt` and `adopt-or-create` policies).
- In-repo evidence: the header comments named above.

## A Terminal condition does not retry

**What.** A CR reports `ACK.Terminal=True` with an AWS error such as
`InvalidParameterValue` or a name conflict, and the controller logs nothing
further for it.

**Where it bites.** Any environment that runs ACK controllers (`aws`), at any
lifecycle phase.

**Why.** Provider behavior. ACK sets `ACK.Terminal` when it concludes the
resource cannot reconcile without a change to the spec, and it does not retry
until the spec changes. Transient errors (throttling, missing permissions) are
reported as `ACK.Recoverable` instead and are retried with backoff.

**Workaround / status.** Read the message, fix the spec in Git, and let Flux
apply it. Do not delete the CR to clear the condition: the default deletion
policy deletes the AWS object as well, and for an adopted bucket, role or
database that is the real resource.

Sources:

- [ACK resource CRUD: condition types](https://aws-controllers-k8s.github.io/community/docs/user-docs/resource-crud/)
  (`ACK.Terminal`, `ACK.Recoverable`).
- [ACK concepts (Amazon EKS user guide)](https://docs.aws.amazon.com/eks/latest/userguide/ack-concepts.html)
  (deleting the Kubernetes resource deletes the AWS resource unless the
  deletion policy is `retain`).

## A Recoverable error outlives its cause

**What.** A CR shows `ACK.Recoverable=True` (for example an `AccessDenied` for a
missing action) and keeps showing it after the cause was fixed.

**Where it bites.** `aws`, run phase. The documented case is S3 tagging: the S3
controller's role lacked `s3:TagResource` and the `Bucket` stayed Recoverable
(#352); #411 verified the same actions on the static principal that runs the
controllers now.

**Why.** Provider behavior. Recoverable errors are retried with exponential
backoff, so the next attempt can be some time away. The fix for these is a
policy or permission change outside the CR, so the CR itself needs no edit.

**Workaround / status.** Wait for the next reconcile. To skip the backoff,
restart the controller Deployment, which re-reconciles every CR it owns:

```sh
kubectl -n ack-system get deploy
kubectl -n ack-system rollout restart deploy/<controller-deployment>
```

For the IAM controller at the pinned chart the Deployment is
`ack-iam-controller-iam-chart` (rendered from the `iam-chart` pin in
`mgmt/aws/infrastructure/ack-controllers/helm.yaml`). The restart was observed
to clear the backoff in the TrueHear fork of krops; it has not been verified on
a krops run.

Sources:

- Issues #352 and #411.
- [ACK concepts (Amazon EKS user guide)](https://docs.aws.amazon.com/eks/latest/userguide/ack-concepts.html)
  (retry strategy).
