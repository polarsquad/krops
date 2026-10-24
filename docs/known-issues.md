# Known issues

The single canonical page for every known issue, limitation, provider
constraint, and platform constraint that affects setup, run, or teardown.
Environment pages carry a one-line mention and link here instead of
restating the detail.

Add an entry when a limitation is discovered and update it in the same
pull request as the change that makes it true; remove an entry only when
the limitation is fixed in a merged PR or confirmed gone in a live run,
and note what closed it. The full documentation structure rules, including
the entry format and the link rules, live in the
[documentation guidelines](./documentation.md).

Every entry states what is observable, where it bites, the classified root
cause with a link to the source material, and a confirmed status. Status
values: `open`, `mitigated`, `upstream-tracked`, `fixed in <PR>`.

## AWS

### RDS DBInstance lands in the region's default VPC

- **What**: the ACK RDS `DBInstance` CRs set no `dbSubnetGroupName`, so the
  PostgreSQL instances land in the region's default VPC, not the VPC CAPA
  created for the EKS cluster.
- **Where it bites**: AWS, run (the workload resources reconcile after the
  cluster exists, so nothing fails at bootstrap; EKS-to-RDS connectivity
  crosses VPCs).
- **Why**: provider behavior. CAPA manages the EKS VPC and all of its
  subnets dynamically, so its subnet IDs cannot be declared in Git ahead of
  time ([CAPA pod networking docs, Managed VPC](https://cluster-api-aws.sigs.k8s.io/topics/eks/pod-networking)).
  The in-repo manifest documents the same decision:
  `mgmt/aws/infrastructure/workload-resources/dbinstance.yaml`. The field
  exists on the CRD ([ACK DBInstance reference, `dbSubnetGroupName`
  field](https://aws-controllers-k8s.github.io/community/reference/rds/v1alpha1/dbinstance/));
  it is simply left unset.
- **Workaround / status**: the instances are intentionally reachable from
  the default VPC; nothing to fix today. If VPC-internal placement is ever
  required, create a `DBSubnetGroup` in the CAPA VPC (its subnet IDs are
  only known at run time) and set `dbSubnetGroupName`.
- **Status**: open

### EIP quota: the default run needs 6 in eu-north-1, the account default is 5

- **What**: the default AWS run allocates 6 Elastic IPs in `eu-north-1`
  (one NAT gateway and one EIP per AZ, 3 AZs, 2 clusters); the account
  default is 5, so a clean account stalls mid-run until the quota is
  raised.
- **Where it bites**: AWS, bootstrap (the second `eu-north-1` cluster).
- **Why**: platform constraint. EC2 grants 5 Elastic IP addresses per
  region by default
  ([AWS EC2 resource limits](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-resource-limits.html));
  the quota code is `L-0263D0A3`. The quota table and the raise procedure
  live in the [AWS service quotas section of the operations
  page](./operations.md#aws-service-quotas-common-first-run-blockers)
  (merged from
  [#351](https://github.com/polarsquad/krops/issues/351), closed).
- **Workaround / status**: raise the quota before the run, per the
  operations page. `krops-bootstrap` also runs an EIP quota preflight on
  every AWS bootstrap: `runs_aws_quota_preflight` in
  `bootstrap-rs/src/main.rs` compares the quota and the krops-owned
  addresses against the requirement and prints the exact increase command
  on shortfall, so the failure surfaces before any cluster is created.
- **Status**: mitigated

### No GPU node pools

- **What**: GPU node pools are out of scope; the default run uses the
  cheapest offered ARM and x86 shapes only.
- **Where it bites**: AWS, bootstrap and run (workloads that need GPUs
  cannot be scheduled).
- **Why**: krops design choice. The node pools pin `t4g.medium` (ARM) and
  `t3.medium` / `t3a.medium` (x86) in
  `mgmt/aws/clusters/<region>/*/cluster.yaml`.
- **Workaround / status**: add a GPU `MachinePool` with a GPU instance type
  and request the corresponding quota first.
- **Status**: open

## Azure

### No GPU node pool

- **What**: the AKS cluster defines no GPU node pool; GPU quota is 0 on new
  subscriptions, so one cannot be added without a quota increase.
- **Where it bites**: Azure, bootstrap and run (workloads that need GPUs
  cannot be scheduled).
- **Why**: platform constraint. GPU vCPU quotas start at 0 for new
  subscriptions and require an increase request
  ([Microsoft Q&A: no GPU quota
  available](https://learn.microsoft.com/en-us/answers/questions/5557854/no-gpu-quota-available));
  the AKS quota table and the increase process are documented in
  [AKS quotas, SKUs, and
  regions](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions).
- **Workaround / status**: request the GPU vCPU quota for `swedencentral`
  through the portal Quotas page, then add a GPU node pool.
- **Status**: open

### Arc resource uuid change rotates the OIDC issuer URL

- **What**: deleting and recreating the Arc resource assigns a new uuid,
  which changes the OIDC issuer URL and breaks the kind-issuer federated
  credentials until re-federation.
- **Where it bites**: Azure, bootstrap.
- **Why**: provider behavior. The Arc OIDC key is an immutable, randomly
  generated GUID per cluster
  ([Arc workload identity
  docs](https://learn.microsoft.com/en-us/azure/azure-arc/kubernetes/workload-identity)),
  so the issuer URL is tied to the Arc resource's uuid.
- **Workaround / status**: self-healing. `arc-federate` (the
  `[tasks.arc-federate]` task in `mise.azure.toml`) upserts the kind-issuer
  FICs on the next bootstrap, so a stale issuer URL is repaired by the next
  `bootstrap azure`.
- **Status**: mitigated

### connectedk8s az extension pin is not Renovate-covered

- **What**: the `connectedk8s` Azure CLI extension is pinned to 1.11.3 in
  `mise.azure.toml`, but Renovate does not update it.
- **Where it bites**: Azure, bootstrap (the Arc connect step).
- **Why**: krops design choice (dependency gap): the pin is a CLI extension
  version inside a shell task, not a file a Renovate manager discovers.
- **Workaround / status**: bump it by hand when the Arc connect step breaks.
- **Status**: open

### No reader identity

- **What**: the Azure environment has no counterpart of the AWS
  `krops-reader` IAM role (a least-privilege read identity for inspection
  and reporting).
- **Where it bites**: Azure, run (and any read-only tooling).
- **Why**: krops design gap; the comparison table on the
  [Azure environment](./azure.md) page lists "IAM reader role: none yet
  (follow-up)".
- **Workaround / status**: use the `krops-capz` identity (broader than
  needed) for read access until a dedicated reader identity lands.
- **Status**: open

### Flexible Server's only administrator is krops-capz

- **What**: the PostgreSQL Flexible Server's sole database administrator is
  the `krops-capz` user-assigned identity; no human holds the admin role by
  default.
- **Where it bites**: Azure, run and teardown (interactive administration
  or manual cleanup needs an extra grant).
- **Why**: krops design choice (Entra-only authentication with a
  secret-free WIF posture). The grant is declared in
  `mgmt/azure/infrastructure/workload-resources/swedencentral-postgres.yaml`
  (`FlexibleServersAdministrator`).
- **Workaround / status**: grant a human through an additional
  `FlexibleServersAdministrator` resource, as described on the
  [Azure environment](./azure.md) page.
- **Status**: open

### No live acceptance run; teardown is manual

- **What**: no live acceptance run has been performed for the Azure
  environment (no subscription at implementation time), and
  `teardown azure` refuses to run (manual procedure).
- **Where it bites**: Azure, teardown (and confidence in the whole
  lifecycle).
- **Why**: krops design choice: `teardown.manual` in `bootstrap.toml` makes
  the CLI refuse with operator steps until the acceptance run exists. The
  manual steps are on the [Azure environment](./azure.md) page.
- **Workaround / status**: follow the manual teardown steps on the
  [Azure environment](./azure.md) page; the acceptance run is the open
  precondition.
- **Status**: open

## GCP

### Cloud SQL postgres user is passwordless

- **What**: Cloud SQL creates the built-in `postgres` user with no
  password; it cannot log in, and `cloudsql.iam_authentication` does not
  remove it.
- **Where it bites**: GCP, run (operators cannot open a superuser session
  without an out-of-band step).
- **Why**: krops design choice with a provider behavior behind it: no
  `rootPassword` is committed because the management cluster's KCC
  credentials are secret-free WIF files, and wiring a SOPS-backed password
  would undermine the no-credentials-at-rest posture. The CR documents the
  decision:
  `mgmt/gcp/infrastructure/workload-resources/postgres/postgres.yaml`
  (the `cloudsql.iam_authentication` label).
- **Workaround / status**: if an operator needs superuser access, set a
  password out-of-band and never commit it:

  ```sh
  gcloud sql users set-password postgres \
    --instance=krops-<cluster>-db --prompt-for-password
  ```

  Limit who holds `cloudsql.users.update` (roles/cloudsql.admin,
  roles/editor), as that permission can enable the user.
- **Status**: open

### Acceptance re-run pending after the CAPG fixes

- **What**: the first live acceptance run (#319 stage B, 2026-10-19)
  surfaced a zonal node-pool convergence bug and a WIF `generateToken`
  failure; both are fixed, but acceptance is pending the re-run.
- **Where it bites**: GCP, bootstrap, pivot, run (the environment is not
  yet acceptance-verified end to end).
- **Why**: provider behavior in CAPG v1.13.1, fixed in-repo:
  [#641](https://github.com/polarsquad/krops/issues/641) (zonal pools
  reconciled via the regional path; fixed by the CAPG fork pin) and
  [#642](https://github.com/polarsquad/krops/issues/642) (`generateToken`
  failure with the keyless WIF credential; fixed).
- **Workaround / status**: re-run the acceptance per
  [#319](https://github.com/polarsquad/krops/issues/319).
- **Status**: open

### Zonal clusters until multi-zone availability is confirmed

- **What**: the GKE cluster is zonal (`europe-north1-a`) until multi-zone
  availability in the project is confirmed.
- **Where it bites**: GCP, bootstrap.
- **Why**: platform constraint: machine availability in the project is not
  yet confirmed for multi-zone
  ([#319](https://github.com/polarsquad/krops/issues/319) spike S4).
- **Workaround / status**: switch the cluster to a regional topology once
  multi-zone is confirmed in the project.
- **Status**: open

### Cloud SQL instance name reuse cooldown

- **What**: Cloud SQL instance names have a reuse cooldown after deletion;
  a teardown-then-bootstrap on the same project can race it and fail to
  recreate the instance under its committed name.
- **Where it bites**: GCP, teardown into bootstrap.
- **Why**: platform constraint. The deletion docs describe the name as
  immediately reusable ([Cloud SQL: delete
  instance](https://cloud.google.com/sql/docs/postgres/delete-instance));
  the observed cooldown is the operational experience recorded on the
  [GCP environment](./gcp.md) page.
- **Workaround / status**: check the name's availability with
  `gcloud sql instances list` before re-bootstrap and retry once it is
  reusable.
- **Status**: open

### No Arm machine pool

- **What**: no Arm machine pool yet; machine-type availability in
  `europe-north1-a` is unverified; the x86 pool is the default.
- **Where it bites**: GCP, bootstrap and run (no arm64 scheduling).
- **Why**: platform constraint (availability unverified;
  [#319](https://github.com/polarsquad/krops/issues/319) spike S4).
- **Workaround / status**: check availability with
  `gcloud compute machine-types list --zones europe-north1-a
  --filter="name~'^(c4a|t2a)'"` and add an Arm `MachinePool` if present.
- **Status**: open

### kind WIF provider stays in the pool after the pivot

- **What**: the `kind` workload-identity provider remains in the `krops`
  pool after the pivot to the management cluster.
- **Where it bites**: GCP, pivot (and run).
- **Why**: krops design choice: it is harmless (its subject condition
  matches only the kind cluster's service accounts) and it keeps
  re-bootstrap of a kind cluster cheap. Pool definition:
  `mgmt/gcp/infrastructure/kcc-identity/pool.yaml`.
- **Workaround / status**: no action; safe to remove once kind-based
  bootstrapping of the GCP environment is retired.
- **Status**: open

### Autopilot, GPU pools, and the reader token grant are follow-ups

- **What**: GKE Autopilot, GPU machine pools, and the per-cluster reader's
  `iam.serviceAccountTokenCreator` grant are not part of the current
  environment.
- **Where it bites**: GCP, run (capability gap for those workloads).
- **Why**: krops design choice (scope of the initial GCP support,
  [#72](https://github.com/polarsquad/krops/issues/72)).
- **Workaround / status**: follow-up; no dedicated issue tracks them yet.
- **Status**: open

## Air-gapped (local-host)

Source for all entries in this section: the
[known limitations / follow-ups section of the airgap
page](./airgap.md#known-limitations-follow-ups) (that page is not edited
by the registry; this section is the canonical copy of its entries).

### The toolbox image is not part of the offline package

- **What**: the `krops-toolbox` image is not included in `airgap/images.txt`
  or the current Zarf package.
- **Where it bites**: air-gapped local-host, setup (the toolbox itself must
  already be present on the target host).
- **Why**: krops design choice (prototype scope). Source: the [known
  limitations / follow-ups section of the airgap
  page](./airgap.md#known-limitations-follow-ups).
- **Workaround / status**: offline deployment still uses the dedicated
  `airgap/scripts/` flow and its pinned tool and image inventory; the
  connected toolbox lifecycle does not replace that flow yet.
- **Status**: open

### capi-operator is omitted from the gap

- **What**: `capi-operator` is omitted from the offline package; provider
  upgrades ride package rebuilds.
- **Where it bites**: air-gapped local-host, run (provider upgrades need a
  rebuild).
- **Why**: krops design choice (acceptable for the prototype). Source: the
  [known limitations / follow-ups section of the airgap
  page](./airgap.md#known-limitations-follow-ups).
- **Workaround / status**: rebuild the Zarf package when a provider upgrade
  is needed.
- **Status**: open

### kind stays as the management substrate

- **What**: the air-gapped profile keeps kind as the management-cluster
  substrate (macOS host); on Linux targets `zarf init
  --components=k3s` would replace kind, but that path is not prototyped.
- **Where it bites**: air-gapped local-host, setup (Linux hosts are not
  covered).
- **Why**: krops design choice (scope). Source: the airgap page's [known
  limitations / follow-ups](./airgap.md#known-limitations-follow-ups).
- **Workaround / status**: use a macOS host with Docker for now.
- **Status**: open

### Single architecture (arm64)

- **What**: the Zarf bundle is single-architecture (arm64).
- **Where it bites**: air-gapped local-host, setup (non-arm64 hosts cannot
  use the current bundle).
- **Why**: krops design choice (follow-up). Source: the airgap page's
  [known limitations /
  follow-ups](./airgap.md#known-limitations-follow-ups).
- **Workaround / status**: build with `--architecture` when another
  architecture is needed.
- **Status**: open

### OCI charts are seeded into krops-registry

- **What**: the flux-operator chart for the HelmChartProxy and the podinfo
  chart are seeded into krops-registry as OCI charts; CAAPH fetches them
  over plain HTTP (verified). The workload-cluster FluxInstance omits
  `distribution.artifact` the same way as the management one.
- **Where it bites**: air-gapped local-host, bootstrap (the charts must
  exist in the registry before an offline deploy).
- **Why**: krops design choice (the air-gapped registry is the chart
  source). Source: the airgap page's [known limitations /
  follow-ups](./airgap.md#known-limitations-follow-ups).
- **Workaround / status**: none; documented behavior.
- **Status**: open

### The AWS flavor is out of scope

- **What**: an air-gapped AWS flavor is out of scope, although in a
  disconnected AWS region the same package pattern would cover
  CAPA/ACK/EKS via in-region endpoints.
- **Where it bites**: none today (future scope).
- **Why**: krops design choice (scope). Source: the airgap page's [known
  limitations / follow-ups](./airgap.md#known-limitations-follow-ups);
  on-prem it maps to the provider-swap path on the
  [extending](./extending.md) page.
- **Workaround / status**: none; tracked as a design follow-up.
- **Status**: open

## Cross-environment

### cert-manager 1.21.2 runs outside its published Kubernetes support window

- **What**: the pinned cert-manager 1.21.2 officially supports Kubernetes
  1.33-1.36, while the repo pins v1.37.0 for the local-host and local-talos
  environments.
- **Where it bites**: local-host and local-talos, bootstrap (and any
  bootstrap.toml PR that bumps one side without the other).
- **Why**: platform risk from the krops pin choice. The support window is
  published on the [cert-manager supported releases
  page](https://cert-manager.io/docs/installation/supported-releases); the
  pairing was validated by
  [air-gapped run 35554802424](https://github.com/polarsquad/krops/actions/runs/35554802424)
  and is listed in `tests/test-cert-manager-kubernetes-support.py` (now
  notice-only; the hard gate that blocked bootstrap.toml PRs was removed in
  [#458](https://github.com/polarsquad/krops/pull/458); the original issue
  is [#384](https://github.com/polarsquad/krops/issues/384)).
- **Workaround / status**: the pairing works in practice (air-gapped
  deploys pass); keep the two pins inside the published window when either
  moves.
- **Status**: mitigated

### The nightly air-gapped workflow is failing

- **What**: the scheduled air-gapped workflow fails in the "Staged update
  rejection and offline rollback" job (ENOSPC while staging the built Zarf
  package).
- **Where it bites**: air-gapped local-host, CI (the update-rollback drill
  does not run to completion).
- **Why**: platform constraint (runner disk space). Sources:
  [#612](https://github.com/polarsquad/krops/issues/612) (open), failing
  run [40054621664](https://github.com/polarsquad/krops/actions/runs/40054621664).
- **Workaround / status**: tracked in
  [#612](https://github.com/polarsquad/krops/issues/612).
- **Status**: open
