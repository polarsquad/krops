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
