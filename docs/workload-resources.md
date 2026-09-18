# Workload resources

What the ACK controllers on each workload cluster create in AWS. For how the
controllers authenticate (and the per-cluster reader roles / console user),
see [AWS authentication & IAM](./aws-iam.md).

## Bucket security posture

`workload/base/s3-buckets/bucket.yaml` creates one bucket per cluster
(`krops-<account>-<cluster>-data`) with:

- all public access blocked
- server-side encryption enforced (SSE-S3/AES256, bucket keys)
- versioning enabled
- ACLs disabled (`BucketOwnerEnforced`)
- a bucket policy denying non-TLS requests

## RDS instances

`workload/base/rds-instances/dbinstance.yaml` creates one PostgreSQL 17 instance
per cluster (`krops-<cluster>-db`) in that cluster's own region; the ACK
RDS controller runs with `aws.region: ${AWS_REGION}`:

- `db.t4g.micro`, 20 GiB gp3, single-AZ (smallest footprint)
- not publicly accessible, storage encrypted
- master password managed by RDS (`manageMasterUserPassword: true`) and stored
  in Secrets Manager; workload clusters have no SOPS key, so an in-Git
  password secret is not an option

> **Known limitation**: the `DBInstance` sets no `dbSubnetGroupName`, so the
> instance lands in the region's **default VPC**, not the EKS VPC. CAPA
> creates the EKS VPC dynamically, so its subnet IDs cannot be declared in
> Git ahead of time.

## GCP resources (GKE workload cluster)

On the GKE workload cluster, Config Connector (KCC, PR 2) creates the
counterparts from `workload/gcp-base/`; the management cluster's KCC is
the authoring identity (see [GCP environment](./gcp.md) for how it
authenticates).

### Storage bucket

`workload/gcp-base/storage/bucket.yaml` creates one bucket per cluster
(`krops-<project-number>-<cluster>-data`) with the same posture as the S3
bucket: uniform bucket-level access on (no object ACLs), versioning on,
no public access. GCS caps bucket names at 63 characters; the identity chain
test checks the name against the real cluster name with a 19-digit project
number.

### Cloud SQL (private IP, IAM auth only)

`workload/gcp-base/postgres/postgres.yaml` creates one PostgreSQL 17
instance per cluster (`krops-<cluster>-db`), zonal, with:

- private IP only (`ipv4Enabled: false`, no public address), the private IP
  reaching the workload VPC through the Private Service Access range and
  VPC peering in `workload/gcp-base/networking/`
- `requireSsl: true` + `sslMode: ENCRYPTED_ONLY`
- IAM authentication only (`cloudsql.iam_authentication: on`): the reader
  user is the per-cluster reader service account, so no password exists
  anywhere. AWS and Azure take the same posture by different mechanisms:
  RDS uses `manageMasterUserPassword` (the password is generated and held
  in Secrets Manager, not in Git or the cluster) and the Azure flexible
  server sets `passwordAuth: Disabled` (Entra ID only)

Connection:

```sh
# Impersonate the per-cluster reader GSA (the SQLUser), then connect.
gcloud auth application-default login \
  --impersonate-service-account krops-<cluster>-r@<project-id>.iam.gserviceaccount.com
gcloud sql connect krops-<cluster>-db --db=app --zone=europe-north1-a
```

`gcloud sql connect` additionally needs the Cloud SQL Client
(`roles/cloudsql.client`) or the instance's `cloudsql.instances.get` +
`cloudsql.instances.update` permissions on top of the reader's
`cloudsql.instanceUser` login role, so the human's project grants must cover
both the connect path and the impersonation.

### Per-cluster reader identity

`workload/gcp-base/iam/reader.yaml` creates the per-cluster reader service
account (`krops-<cluster>-r`; the account ID, taken from the resource's `metadata.name`, is capped at GCP's 30-char
service-account ID limit) with `storage.objectViewer` on the bucket and
`cloudsql.instanceUser` on the project (the IAM database-auth login role,
which carries `cloudsql.instances.login`), plus a
`roles/iam.serviceAccountTokenCreator` grant ON the per-cluster account
whose member is the project-level `krops-reader` GSA. The per-cluster GSA
is also the Cloud SQL `SQLUser`, so the effective database identity is the
per-cluster reader; the human path is human IAM account to `krops-reader`
(imperative token-creator grant from gcp-bootstrap) to the per-cluster
reader (the grant above). `tests/test-gcp-identity-chain.py` (in `mise run
validate` and CI) cross-checks the identity couplings between these and
`mgmt/gcp/`.
