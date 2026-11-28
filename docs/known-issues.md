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
- **Why**: platform constraint: multi-zone machine availability in the
  project is unverified.
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

### Autopilot and GPU pools are follow-ups

- **What**: GKE Autopilot and GPU machine pools are not part of the current
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
  1.33-1.36, while the repo pins v1.37.0 (local-host) and v1.37.1
  (local-talos).
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

### The nightly air-gapped workflow fails in isolated-deploy

- **What**: the scheduled air-gapped workflow failed in the "Deploy without
  external egress" (`isolated-deploy`) job on every scheduled run from
  2026-11-24 to 2026-11-27. The 2026-11-28 scheduled run
  ([run 46097761178](https://github.com/polarsquad/krops/actions/runs/46097761178),
  on `main` without these fixes) exceeded the job's 30 min limit and was
  reported as cancelled. Its job log is not available, so its cause is not
  confirmed; `report-status` files nothing for a cancelled run
  ([#809](https://github.com/polarsquad/krops/issues/809)). Three causes are
  known, tracked in
  [#807](https://github.com/polarsquad/krops/issues/807). The clusterctl and
  syft causes surfaced after
  [PR #692](https://github.com/polarsquad/krops/pull/692) (host DNS block for
  unavailable external DNS, merged 2026-11-23; 2026-11-24 was the first
  scheduled run with it). On 2026-11-24 and 2026-11-26 the job did not report
  a job timeout. Both ended with "The runner has received a shutdown signal"
  during `zarf package deploy`, about 27 to 29 min into a 30 min limit; the
  logs do not show the reason. The earlier update-rollback ENOSPC failure
  ([#612](https://github.com/polarsquad/krops/issues/612)) is closed: fixed by
  [#674](https://github.com/polarsquad/krops/pull/674) and
  [#687](https://github.com/polarsquad/krops/pull/687); closed 2026-10-24
  after manual run [40142073894](https://github.com/polarsquad/krops/actions/runs/40142073894)
  passed (#674 alone did not fix it).
- **Where it bites**: air-gapped local-host, CI (nightly runs; the job fails).
- **Why**: provider behavior (causes A and C: the vendored syft and clusterctl
  made network requests by default) and platform constraint (cause B: the
  runner's Docker DNS path).
  - Cause A, provider behavior, confirmed in the job logs: `zarf tools sbom
    convert` runs once per SBOM document, and the vendored syft fetched
    `toolbox-data.anchore.io` on every call. The update check was on by
    default ([syft `update_check.go` at v1.51.1](https://github.com/anchore/syft/blob/v1.51.1/cmd/syft/internal/options/update_check.go)).
    With blocked DNS each call failed after about 20 s, so the verification
    pass over all 63 documents took about 21 min
    ([run 45312061908](https://github.com/polarsquad/krops/actions/runs/45312061908),
    2026-11-24; [run 45742055306](https://github.com/polarsquad/krops/actions/runs/45742055306),
    2026-11-26).
  - Cause C, provider behavior, from the clusterctl v1.14.4 source: after each
    command, clusterctl asked the Go module proxy, falling back to GitHub, for
    the latest release ([`version_checker.go`](https://github.com/kubernetes-sigs/cluster-api/blob/v1.14.4/cmd/clusterctl/cmd/version_checker.go),
    [`cmd/clusterctl/cmd/root.go`](https://github.com/kubernetes-sigs/cluster-api/blob/v1.14.4/cmd/clusterctl/cmd/root.go),
    [`internal/goproxy`](https://github.com/kubernetes-sigs/cluster-api/tree/v1.14.4/internal/goproxy)).
    With the proxy refused, it retried the proxy for 60 s before falling back
    to GitHub, and failed checks were not cached. With
    `CLUSTERCTL_DISABLE_VERSIONCHECK=true` it makes no version-check request.
    Observed in CI logs: the core render (`namespace/capi-system created` to
    `configured`) took about 81 s on 2026-11-24 (02:54:49 to 02:56:10) and on
    2026-11-26 (02:54:48 to 02:56:09). Attributing that time to the version
    check is inferred from the source and the timing; the logs do not show the
    cause. The `capi-providers` action runs three renders under a 180 s
    `maxTotalSeconds`; at 81 s each they project to about 243 s. Neither run
    reached the end of that action, so 243 s is a projection, not a
    measurement.
  - Cause B, platform constraint, partly observed: the DNS baseline probe got
    no response on 2026-11-25 and 2026-11-27, and the job failed there
    ([run 45532655320](https://github.com/polarsquad/krops/actions/runs/45532655320),
    [run 45928038954](https://github.com/polarsquad/krops/actions/runs/45928038954)).
    The probe passed on 2026-11-24 and failed on 2026-11-25 on runner image
    [20261116.190](https://github.com/actions/runner-images/blob/ubuntu26-arm64/20261116.190/images/ubuntu/Ubuntu2604-Arm64-Readme.md),
    and passed on 2026-11-26 and failed on 2026-11-27 on runner image
    [20261122.195](https://github.com/actions/runner-images/blob/ubuntu26-arm64/20261122.195/images/ubuntu/Ubuntu2604-Arm64-Readme.md).
    Both readmes list Docker Server 29.4.2, which does not show whether an
    override is set. Both failing runs inserted the bridge egress block before
    the baseline probe, so the probe ran with that block already active (the
    host DNS block came later). tcpdump captured 6 packets on the bridge in
    both failing runs; the stall runs captured 0.
    The path is inferred. Upstream servers from the host's resolver
    configuration are dialed from the host namespace; servers set by `--dns` or
    the daemon's `dns` setting are dialed from the container namespace, over the
    kind bridge, where the egress block drops the query (moby at tag
    docker-v29.4.2:
    [`TransformForIntNS`](https://github.com/moby/moby/blob/docker-v29.4.2/daemon/libnetwork/internal/resolvconf/resolvconf.go#L251-L264),
    [`dialExtDNS`](https://github.com/moby/moby/blob/docker-v29.4.2/daemon/libnetwork/resolver.go#L499),
    [`OptionDNS` callers](https://github.com/moby/moby/blob/docker-v29.4.2/daemon/container_operations.go#L60-L64)).
    The logs of the failing runs do not record the runner's resolver
    configuration, so whether those runners set such an override is not known;
    the 2026-11-28 fork runs below show that the override is present on some
    runners of image 20261122.195.1. The isolated-deploy job
    now records the forwarding path in its probe transcript. The pcap gate
    failure on 2026-11-18
    ([run 44222887171](https://github.com/polarsquad/krops/actions/runs/44222887171))
    captured 894 packets to 168.63.129.16:53 on the bridge, from 172.18.0.2
    (718), 172.18.0.5 (134) and 172.18.0.6 (42), before the baseline probe
    existed; it appears to be the same class (inferred). See
    [DNS in the gap](./airgap.md#dns-in-the-gap).
- **Workaround / status**: two environment variables address causes A and C:
  `SYFT_CHECK_FOR_APP_UPDATE=false` (`airgap/scripts/verify-package.sh`) and
  `CLUSTERCTL_DISABLE_VERSIONCHECK=true` (`airgap/scripts/resolve-clusterctl.sh`).
  For cause B, `isolated-deploy` now runs the DNS baseline probe before any
  block, records the forwarding path, and excludes only UDP and TCP port 53 to
  container-namespace IPv4 upstreams from the egress gate (see
  [DNS in the gap](./airgap.md#dns-in-the-gap)). A manual run on a fork at
  commit `b8f0641`, before the capture liveness check was added
  ([run 46070854386](https://github.com/lmilbaum/krops/actions/runs/46070854386),
  2026-11-27, runner image 20261122.195.1), passed. Observed in its logs:
  signature, checksum and SBOM verification of all 63 documents took 17 s
  (21:37:40 to 21:37:57); the core render took about 1 s
  (`namespace/capi-system created` at 21:40:55, `configured` at 21:40:56); and
  the run took the host-namespace path (`# ExtServers: [host(127.0.0.53)]`),
  captured 0 packets, and had an unfiltered gate. The DNS assertions are
  inferred from the step result: the step passed, so every DNS assertion
  passed. Three manual runs on a fork on 2026-11-28 (runner image
  20261122.195.1) covered the container-namespace path. [Run
  46118384418](https://github.com/lmilbaum/krops/actions/runs/46118384418), on
  commit `d230847` with nothing added to the runner's Docker setup, took it and
  passed: `/etc/docker/daemon.json` already set `dns` to `168.63.129.16`, Docker
  reported `# ExtServers: [168.63.129.16]`, and the gate excluded 938 DNS
  packets to that address, leaving 0. [Run
  46118380143](https://github.com/lmilbaum/krops/actions/runs/46118380143), on
  `throwaway/807-forced-dns-pr`, also took it; its DNS assertions passed (932
  packets excluded, 0 remaining), and its cluster-clean scan then failed (see
  the cluster-clean scan entry below). A control from `main` at `bbd5909` with a
  throwaway step setting the same daemon `dns` ([run
  46118382520](https://github.com/lmilbaum/krops/actions/runs/46118382520))
  failed at the DNS baseline probe with 6 packets captured, matching the failure
  message and packet count of the 2026-11-25 and 2026-11-27 runs. Those runs'
  logs do not show `daemon.json`, so the override on their runners remains
  inferred. [Run 46070854386](https://github.com/lmilbaum/krops/actions/runs/46070854386)
  on the same image took the host-namespace path, so the Docker DNS setting
  varies between runners of one image, for an unknown reason. The entry stays
  open until a scheduled run passes.
- **Status**: open

### The cluster-clean scan matches a `dns` branch name

- **What**: the isolated-deploy cluster-clean scan fails the job when a cluster
  event matches `\bdns\b`. Flux writes the git ref into the `NewArtifact`
  event of an `OCIRepository`, so a manual run from a branch that has `dns` as
  a separate word in its name fails isolated-deploy after its DNS assertions
  have passed. Observed in
  [run 46118380143](https://github.com/lmilbaum/krops/actions/runs/46118380143)
  (2026-11-28, fork, branch `throwaway/807-forced-dns-pr`): the mgmt cluster
  scan reported the `NewArtifact` event for `ocirepository/flux-system`, and
  the workload-cluster scan and the public-egress check did not run.
- **Where it bites**: air-gapped local-host, CI (manual runs from a branch with
  `dns` as a separate word in its name; scheduled runs on `main` are
  unaffected).
- **Why**: krops scan design. `assert_cluster_clean` in
  `.github/workflows/air-gapped.yml` matches `\bdns\b` anywhere in the
  `kubectl get events -A` output, which includes the git ref that Flux reports
  in `NewArtifact` events. The pattern exists so that coredns pod names do not
  match, but it does not exclude the ref.
- **Workaround / status**: do not use `dns` as a separate word in the branch
  name of a manual run.
- **Status**: open
