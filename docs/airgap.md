# Air-gapped krops with Zarf (local-host / CAPD environment)

This document describes how krops is packaged with [Zarf](https://zarf.dev)
on a connected machine and deployed end-to-end with **no internet access**:
the management cluster, Flux, CAPI, and a CAPD workload cluster, all from one
package plus a small set of image archives.

Status: **validated with the radio off** (2026-08-18). Rehearsed connected on
an isolated `airgap-mgmt` cluster + a renamed `airgap-wl` workload cluster
(coexisting with the live baseline), then deployed and verified end-to-end by
the autonomous script `scripts/offline-run.sh` with Wi-Fi disabled for the
full deploy + reconcile window. Re-run that script the same way to
re-verify.

By default, `offline-run.sh` waits until it confirms that the internet is
unreachable before deploying. Set `SKIP_OFFLINE_CHECK=1` only when the caller
enforces network isolation and monitors external traffic for the full run; in
that mode, the result log explicitly records that the connectivity check was
skipped and caller-monitored.

```sh
# Caller must enforce isolation and monitor external traffic until completion.
SKIP_OFFLINE_CHECK=1 airgap/scripts/offline-run.sh
```

## Prerequisites

The bundle is single-architecture (arm64), including the pinned Zarf CLI. The
mise pin in `mise.toml` selects the matching Linux or macOS arm64 release
asset. Docker and kind are also required on the deploy host (the prototype
keeps kind as the management-cluster substrate; see Known limitations).

CI requires every external image in an air-gap inventory or script changed by
a pull request to use `repository:readable-tag@sha256:<digest>`. The tag
documents the selected version; the digest makes the fetched content
immutable. Untouched legacy files are migrated when first changed. Run
`python3 airgap/tests/test-airgap-image-digests.py` (or `mise run validate`)
to check relevant working-tree changes without registry or network access.
Run `python3 airgap/tests/test-airgap-image-digests.py --all` in a clone to
audit every air-gap inventory and shell script, including untouched legacy
files.
The locally built `localhost:5001/krops-airgap:latest` config artifact is the
only documented exception because it is created immediately before packaging.

Every package build must be signed. For an operator build, generate or obtain
a Cosign-compatible key pair, keep the private key outside the repository, and
set `ZARF_SIGNING_KEY` to it. Set `ZARF_SIGNING_KEY_PASS` when the key is
password-protected. Transfer the corresponding public key with the bundle and
set `ZARF_VERIFY_KEY` to its gap-side path. The upstream `air-gapped` workflow
instead uses GitHub OIDC keyless signing; its Sigstore bundle and Rekor
inclusion proof are embedded in the Zarf archive and require no online lookup
at verification time.

## Architecture

Two registries, two image layers, one package.

```
connected side (build)                        gap (deploy)
----------------------                        --------------
airgap/scripts/build-package.sh               airgap/scripts/offline-run.sh
  validate + render                            1. verify signature + checksums
  build-config-artifact.sh -> OCI artifact     2. extract + validate embedded SBOMs
  zarf package create (Syft SBOMs)             3. docker load + kind create + seed registry
  zarf package sign                            4. zarf init + package deploy
        |                                      zarf init --registry-mode=nodeport
        v                                      zarf package deploy
zarf-package-krops-airgap-*.tar.zst               |
archives/ (node images, workload images,           v
  charts, zarf-init tarball)                 Zarf internal registry (127.0.0.1:31999)
                                                    + agent (mutating webhook)
                                             substrate: cert-manager, flux-operator,
                                               CAPI core+providers, CAPD, CAAPH
                                             FluxInstance -> sync from Zarf registry
                                             Flux -> clusters/docker (CAPD) -> workload
                                             workload Flux <- krops-registry (config+charts)
```

**Ownership split.** Zarf owns the *substrate* (internal registry, agent,
cert-manager, the flux-operator chart, CAPI core + kubeadm bootstrap /
control-plane + CAPD + CAAPH component manifests, and the krops config
artifact). Flux owns the *workload definitions* (the CAPD cluster, the kindnet
CNI addon, the per-cluster Flux addon), reconciled from the config artifact.
The `mgmt/` and `workload/` trees in git are **not** modified; the airgap
variant is generated at build time by `build-config-artifact.sh`.

**Why OCI, not Gitea.** The local-host environment moved from a GitHub
`GitRepository` to an OCI-artifact sync (`oci://krops-registry:5000/krops`)
in PRs #30/#31/#33. There is no `GitRepository` left to rewrite, so the Zarf
git-server is unused; the config crosses the gap as an OCI artifact inside the
Zarf package and is published into Zarf's internal registry at deploy time.
The verification linchpin is the agent's **image rewrite** plus a Ready
`OCIRepository` pointing at the internal registry, with the radio off.

## What crosses the gap (the bundle)

| Item | Purpose |
|---|---|
| `zarf` CLI binary | runs the deploy |
| `archives/zarf-init-arm64.tar.zst` | `zarf init` (registry + agent); built by `zarf tools download-init` from the zarf CLI pinned in `mise.toml` and renamed at build time, so `mise.toml` stays the sole version declaration |
| `zarf-package-krops-airgap-arm64-0.1.0.tar.zst` | signed package, including per-component Syft JSON/HTML SBOMs and the Sigstore signature bundle |
| `archives/kindest_node_v1.37.0_mgmt.tar` | mgmt kind node (host daemon) |
| `archives/kindest_node_v1.37.0.tar` | CAPD workload and management nodes (host daemon) |
| `archives/kindest_haproxy_*.tar` | CAPD load balancer |
| `archives/docker.io_library_registry_2.tar` | krops-registry container |
| `archives/workload-pod-images.tar` | flux controllers + podinfo for `preLoadImages` |
| `archives/charts/{flux-operator,podinfo}-*.tgz` | OCI charts seeded into krops-registry |
| `config-artifact/` | trimmed GitOps tree, re-pushed as `krops:latest` |

The CI artifact excludes the `zarf` CLI; fetch it via mise or the Zarf release
for the deploy host's target OS before crossing the gap.

## Sequence

Connected (build):

```sh
ZARF_SIGNING_KEY=/secure/path/cosign.key airgap/scripts/build-package.sh
```

The build refuses to create an unsigned deliverable. Zarf generates
per-component Syft SBOMs by default during `package create`; the script then
signs the completed archive so its checksum manifest covers those SBOMs and
all other package contents. CI sets `ZARF_KEYLESS_SIGNING=1` and grants OIDC
only to the build job.

The `air-gapped` GitHub Actions workflow runs only on upstream `main`, nightly
or by manual dispatch. It builds the ARM64 bundle, then starts two deployment
jobs in parallel: one observes public traffic without blocking it, while the
other blocks new external connections from the kind network. Both capture
public traffic and fail if any public packet is observed or attempted. The
workflow retains the bundle and verification evidence as one-day artifacts.

Running both deployment jobs is a temporary evaluation, not the intended
long-term workflow shape. Their results and timings provide comparable samples
from the same bundle so maintainers can determine which method detects offline
violations more accurately and whether either has a meaningful performance
cost. After enough nightly and manual runs have been assessed, the less
effective job will be removed.

The nightly schedule starts at 02:17 UTC rather than at the top of the hour to
reduce GitHub Actions queue contention. Build and deployment remain separate
jobs so CI verifies that the uploaded transfer bundle can be downloaded and
used on clean runners. Both deployment jobs depend only on the build job, so
they run concurrently to make their timing comparison fair and avoid doubling
elapsed validation time. The bundle upload uses compression level 0 because its
largest contents are already-compressed container layers and Zstandard
archives. CI also
deliberately avoids caching container images: pulling them during every run
verifies that the declared air-gap inventory remains available and complete.
Fork and default-branch guards prevent fork or non-default-branch manual
dispatches from consuming the ARM64 runners.

Gap (deploy): from `airgap/`:

```sh
# CI keyless-signed package (default trusted workflow identity)
scripts/offline-run.sh zarf-package-krops-airgap-arm64-0.1.0.tar.zst

# Operator key-signed package
ZARF_VERIFY_KEY=/transfer/cosign.pub \
  scripts/offline-run.sh zarf-package-krops-airgap-arm64-0.1.0.tar.zst
```

Before it creates or changes a cluster, `offline-run.sh` verifies the package
signature and every archive checksum, extracts the embedded SBOMs with
signature verification forced, and decodes each document with syft's own
decoder (`zarf tools sbom convert`, the syft CLI vendored inside the zarf
binary) so invalid Syft JSON fails the run. CI performs these steps while
public egress is blocked or monitored; the extracted SBOM directory is
retained with the deployment evidence. Any verification failure aborts
before staging.

Rehearsal isolation knobs (never touch a live baseline on the same Docker
daemon): `CLUSTER_NAME`, `AIRGAP_CLUSTER_NAME`, `WORKLOAD_REGISTRY_HOST`,
`REGISTRY_NAME`, `REGISTRY_PORT`.

## Verification checklist

- The package checksum and signature verify against either the pinned upstream
  workflow identity or the explicitly supplied public key.
- Extracted SBOM JSON files are decoded by syft itself (`zarf tools sbom
  convert`); every document must decode as a Syft SBOM, and at least one
  document must be present.
- `kubectl get pods -A -o jsonpath=...`: every non-kind-baked image is
  prefixed `127.0.0.1:31999/` (the Zarf internal registry).
- `kubectl -n flux-system get ocirepository`: `url` is
  `oci://zarf-docker-registry.zarf.svc.cluster.local:5000/krops-airgap`,
  Ready with a stored digest equal to the connected-side push.
- `flux get kustomizations`: all Ready, from the artifact.
- `kubectl get clusters.cluster.x-k8s.io -A`: workload cluster `Provisioned` /
  `Available=True`; `clusterctl describe cluster` machines Running.
- Workload cluster: `kubectl get nodes` Ready; flux + podinfo pods Running.

## Empirical findings (why the package looks the way it does)

1. **Captured CAPI provider components are clusterctl templates.** The
   capi-operator substitutes `${VAR:=default}` placeholders and applies the
   provider-spec `--feature-gates=ClusterTopology=true` arg override at
   install. In the gap we are the operator, so
   `scripts/substitute-components.sh` performs both steps up front.
2. **`spec.distribution.artifact` must be omitted** from the FluxInstance.
   The operator fetches it at every reconcile and its fetcher has no
   insecure-registry option (verified: "http: server gave HTTP response to
   HTTPS client" against the plain-HTTP internal registry). Omitting it makes
   the operator use its embedded distribution manifests, the documented
   airgap default. The `flux-instance` Helm chart is unusable offline because
   it renders `artifact` unconditionally.
3. **The embedded distribution digest-pins controller images to multi-arch
   manifest-list digests** that Zarf's single-arch image pipeline cannot
   resolve. The FluxInstance kustomize patches pin the four controllers back
   to tags; the agent rewrites tags to the `<tag>-zarf-<hash>` tags that do
   exist in the registry.
4. **Zarf v0.83 wraps `manifests:` in a generated Helm chart that fails to
   adopt CRs** ("exists and cannot be imported into the current release").
   The FluxInstance is deployed via `files:` + a `kubectl apply` action.
5. **In-cluster fetchers cannot use the nodeport address.** `127.0.0.1:31999`
   resolves only from a node's loopback. The FluxInstance `sync.url` therefore
   uses the internal service DNS name (`###ZARF_CONST_REGISTRY_INTERNAL###` =
   `zarf-docker-registry.zarf.svc.cluster.local:5000`), which the Zarf
   `private-registry` pull secret already covers. Tree-defined OCIRepositories
   additionally need `spec.insecure: true`.
6. **Workload-node k8s images are pre-baked** into `kindest/node` (verified:
   136 content blobs), so CAPD nodes come up offline with no pulls. Only the
   workload Flux controllers and podinfo need `preLoadImages`.

## Known limitations / follow-ups

- The `krops-toolbox` image is not included in `airgap/images.txt` or the
  current Zarf package. Offline deployment still uses the dedicated
  `airgap/scripts/` flow and its pinned tool and image inventory; the connected
  toolbox lifecycle does not replace that flow yet.
- **capi-operator is omitted** in the gap. Provider upgrades ride package
  rebuilds. Acceptable for the prototype.
- **kind stays** (macOS host). On Linux targets `zarf init --components=k3s`
  replaces kind; not prototyped here.
- **Single architecture** (arm64). Multi-arch is a `--architecture` follow-up.
- The **flux-operator chart for the HelmChartProxy** and the podinfo chart are
  seeded into krops-registry as OCI charts; CAAPH fetches them over plain HTTP
  (verified). The workload-cluster FluxInstance omits `distribution.artifact`
  the same way as the mgmt one.
- **AWS flavor is out of scope** here but shapes the design: in a disconnected
  AWS region the same package pattern covers CAPA/ACK/EKS via in-region
  endpoints; on-prem it maps to the provider-swap path in `docs/extending.md`.

## Update drill

A config-only change (edit the tree, re-run `build-config-artifact.sh`,
re-push `krops:latest` to krops-registry) moves the workload Flux to a new
digest without rebuilding the package. A substrate change (images/components)
requires `build-package.sh` and a fresh `zarf package deploy`.
