# Adding clusters and apps

## Adding a workload cluster

1. Create `mgmt/aws/clusters/<region>/<env>/` with a `cluster.yaml`,
   `kustomization.yaml` (set `namePrefix`), and `capi-nameref.yaml` (so CAPI
   cross-references get the prefix applied; see the existing regions).
2. Label the `Cluster` with `fluxcd: enabled` **and** `region: <region>`.
3. Register it in `mgmt/aws/clusters/<region>/kustomization.yaml` and add a
   `Kustomization` entry in `mgmt/aws/clusters/flux-ks.yaml` with
   `dependsOn: [capa-system]`.
4. In `mgmt/aws/addons/flux-apps/flux-instance.yaml`, add a per-region
   FluxInstance ConfigMap (sync path `workload/<region>-01`) and a matching
   `ClusterResourceSet`.
5. Add the cluster's `Bucket`, `DBInstance`, and reader `Role` to
   `mgmt/aws/infrastructure/workload-resources/` (use the
   `services.k8s.aws/region` annotation for non-default regions — see
   [docs/aws-iam.md](./aws-iam.md)).
6. Create `workload/<region>-01/kustomization.yaml` pointing at `../base`
   (empty today — see "Adding apps to the workload clusters").
7. Run `mise run validate`, commit, and push.

## Adding apps to the workload clusters

`workload/base/` is empty (#346: ACK now runs only on the management
cluster, see "Adding AWS resources" below). Follow the local-host Podinfo
pattern (`workload/local-host/podinfo/`) for an app that should run
per-workload-cluster rather than on the management cluster:

1. Create `workload/base/<app>/` with a `kustomization.yaml` listing the app's
   manifests, and a `flux-ks.yaml` defining the Flux `Kustomization`
   (path `./workload/base/<app>`; add `dependsOn` and `wait: true` as needed).
2. Register the `flux-ks.yaml` in `workload/base/kustomization.yaml`.
3. Run `mise run validate`, commit, and push. Every workload cluster picks it
   up on its next sync.

## Adding AWS resources (ACK, management-cluster-only)

Since #346, ACK's S3, RDS, and IAM controllers run once, on the management
cluster, and manage resources for every workload cluster directly — they are
not workload-cluster apps. To add a new AWS resource:

1. Add the CR to `mgmt/aws/infrastructure/workload-resources/` (a new file,
   or append to `buckets.yaml`/`dbinstances.yaml`/`roles.yaml` if it's another
   instance of an existing kind), one per workload cluster it applies to.
2. There is no `cluster-vars` ConfigMap on the management cluster: use literal
   values for the account ID, region, and cluster name, and the
   `services.k8s.aws/region` annotation for anything outside `eu-north-1`
   (see [docs/aws-iam.md](./aws-iam.md)).
3. If the resource type is new (not S3/RDS/IAM), add its ACK controller
   HelmRelease to `mgmt/aws/infrastructure/ack-controllers/helm.yaml` and
   widen the static credential's IAM policy to cover it.
4. Run `mise run validate`, commit, and push.

## Using other providers

The management cluster is not AWS-only. Providers are declared as CAPI
operator CRs (`operator.cluster.x-k8s.io/v1alpha2`) under
`mgmt/<environment>/capi-providers/` (for example `mgmt/aws/capi-providers/` for
AWS EKS, `mgmt/local-host/capi-providers/` for local Docker, and
`mgmt/local-talos/capi-providers/` for Talos and Tinkerbell), one directory per
provider namespace, and registered in that environment's `capi-providers/flux-ks.yaml`.
The operator resolves the well-known provider names (`aws`, `azure`, `gcp`,
`talos`, `k0sproject-k0smotron`) from the same built-in registry `clusterctl` uses, so
a provider is just a typed CR with a pinned version:

```
mgmt/aws/capi-providers/<name>-system/
  namespace.yaml
  kustomization.yaml        # lists namespace.yaml + providers.yaml (+ secrets)
  providers.yaml            # the typed provider CR(s)
  <credentials>.sops.yaml   # optional cloud credentials, SOPS-encrypted
```

The Flux registration in `flux-ks.yaml` follows the existing entries:
`dependsOn: [capi-system]`, `decryption.provider: sops` when credentials ship
in Git, and a `healthChecks` entry naming one CRD the provider installs so
Flux waits for it.

Contract note: this repo pins CAPI core v1.14.0, which speaks the v1beta2
contract and still accepts v1beta1-contract providers until the v1beta1
removal (tentatively CAPI v1.16, April 2027). Prefer providers that already
speak v1beta2.

### AWS (CAPA): the reference wiring

CAPA is the provider this repo already runs; use it as the template:

- `mgmt/aws/capi-providers/capa-system/providers.yaml` declares
  `InfrastructureProvider aws` v2.13.0 with `configSecret: aws-credentials`
  and the EKS feature gates
  (`EKS=true,EKSEnableIAM=true,EKSAllowAddRoles=true,MachinePool=true`).
- `aws-credentials.sops.yaml` carries `AWS_B64ENCODED_CREDENTIALS`, produced
  by `mise run aws-credentials` (rotation: [docs/secrets.md](./secrets.md)).
- Cluster definitions in `mgmt/aws/clusters/<region>/<env>/` use
  `AWSManagedControlPlane` + `AWSManagedMachinePool` (EKS). ACK's S3, RDS, and
  IAM controllers run only on the management cluster
  (`mgmt/aws/infrastructure/ack-controllers/`) and create each workload
  cluster's resources from
  `mgmt/aws/infrastructure/workload-resources/` (see
  [docs/aws-iam.md](./aws-iam.md)).

### Azure (CAPZ)

CAPZ v1.27.0 speaks the v1beta1 contract (accepted by CAPI v1.14 until the v1beta1 removal) and bundles Azure Service Operator (ASO) v2.19.0, which backs the managed-AKS path.

The `azure` environment is the worked example: `mgmt/azure/` (see
[azure.md](./azure.md)). Reuse it rather than adding CAPZ to `mgmt/aws/`:
the provider is `capi-providers/capz-system/providers.yaml`
(`InfrastructureProvider azure` v1.27.0 with `configSecret: capz-variables`
carrying `ADDITIONAL_ASO_CRDS`), credentials are one SOPS Secret read by
both `AzureClusterIdentity` and ASO's `credential-from` annotation, and
clusters use `AzureASOManagedCluster` / `AzureASOManagedControlPlane` /
`AzureASOManagedMachinePool` with the ASO resources inline (their names are
literal final names: kustomize's `namePrefix` does not descend into
`spec.resources`).

### GCP (CAPG + Config Connector)

CAPG v1.13.1 speaks the v1beta1 contract (accepted by CAPI v1.14 until the
v1beta1 removal). The worked example is the `gcp` environment: `mgmt/gcp/`
(see [gcp.md](./gcp.md)), `europe-north1` with a GKE management cluster
(`europe-north1-management`) and a GKE workload cluster. Reuse it rather
than adding CAPG to another environment:

- The provider is `capi-providers/capg-system/providers.yaml`
  (`InfrastructureProvider gcp` v1.13.1 with `configSecret: capg-variables`;
  GKE and MachinePool are feature gates in the variables, not in the CR).
- Credentials are one plain `external_account` Secret per consumer
  (`capg-wif-credentials` for CAPG, `kcc-wif-credentials` for Config
  Connector): Workload Identity Federation against the `krops` pool, the
  provider name templated as `${GCP_WIF_PROVIDER:=mgmt}` (the kind bootstrap
  cluster overrides it to `kind` through the `gcp-wif` ConfigMap the
  `wif-federate` task creates, and the pivot pins `mgmt` through
  `pivot-manifest-vars`). No service-account key exists anywhere.
- Cloud resources on the workload cluster are reconciled by a second Config
  Connector (pinned release bundle, `workload/gcp-base/kcc/`) running under
  GKE-native Workload Identity, not by a CAPI add-on.
- Clusters use `GCPManagedCluster` / `GCPManagedControlPlane` /
  `GCPManagedMachinePool` with the literal GKE `clusterName` kept as-is
  (kustomize's `namePrefix` does not rename it; the CAPI cross-references
  get the prefix through `capi-nameref.yaml`).

### Talos (CABPT + CACPPT)

Talos supplies the bootstrap and control plane providers only; pair it with
any infrastructure provider (CAPT, CAPA, CAPZ, ...) that supplies the
machines. The worked in-repo example is the `local-talos` environment:
`mgmt/local-talos/` pairs the Talos providers with Tinkerbell (CAPT) to
PXE-boot a bare-metal management machine. See the architecture diagram in
[docs/local-talos-infra.svg](local-talos-infra.svg).

1. Two directories, matching the upstream namespace conventions:

   ```yaml
   # mgmt/local-talos/capi-providers/cabpt-system/provider.yaml
   apiVersion: operator.cluster.x-k8s.io/v1alpha2
   kind: BootstrapProvider
   metadata:
     name: talos
     namespace: cabpt-system
   spec:
     version: "v0.7.6"
     fetchConfig:
       url: "https://github.com/sidero-community/cluster-api-bootstrap-provider-talos/releases"
   ```

   The control plane provider is the same shape
   (`cacppt-system/provider.yaml`: ControlPlaneProvider `talos` v0.6.4).
2. Set `fetchConfig.url` explicitly to the
   [sidero-community](https://github.com/sidero-community) releases: the CAPI
   operator's embedded clusterctl defaults resolve `talos` to the archived
   siderolabs org. The sidero-community line serves the v1beta2 contract
   (`cluster.x-k8s.io/v1beta2: v1beta1` in the released metadata), so the
   contract note above does not impose a migration deadline on it.
3. No cloud credentials: Talos machine secrets are generated per cluster by
   `TalosControlPlane` / `TalosConfig`. Always set `talosVersion` explicitly
   (e.g. `v1.14`) so a provider upgrade does not silently change the
   generated machine config.
4. In cluster definitions, swap `KubeadmControlPlane` for `TalosControlPlane`
   and `KubeadmConfigTemplate` for `TalosConfigTemplate`; the infrastructure
   templates stay whatever the paired infra provider supplies.

### k0smotron

k0smotron v2.1.0 is v1beta2-native and ships bootstrap, control plane, and
infrastructure providers under one name:

```yaml
# mgmt/aws/capi-providers/k0smotron-system/providers.yaml
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: BootstrapProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
---
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: ControlPlaneProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
---
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: InfrastructureProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
```

- No cloud credentials are needed, and the cert-manager the repo already
  installs covers its webhooks.
- Hosted control planes: `K0smotronControlPlane` runs the child cluster's
  control plane as pods on the management cluster; workers come from any
  infra provider.
- Remote machines: `RemoteMachine` provisions k0s onto existing machines over
  SSH (bare metal, arbitrary VMs, anywhere with no CAPI cloud provider).
- Drop the `InfrastructureProvider` CR if you only want hosted control
  planes.

### After adding a provider

1. `mise run validate` builds every overlay, including the new directory.
2. Open the PR; konflate renders the blast radius (new CRDs, provider
   deployments) into the PR comment and the `konflate / Rendered Flux diff`
   check.
3. After merge, confirm the provider came up:
   `kubectl get pods -n <name>-system` and
   `kubectl get <kind>providers.operator.cluster.x-k8s.io -A` (e.g.
   `infrastructureproviders`).
