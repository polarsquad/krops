# Dependencies

Dependency versions live in the files that consume them: mise configuration,
`bootstrap.toml`, Cargo manifests and locks, the toolbox Dockerfile,
Kubernetes and Flux manifests, GitHub Actions workflows, and the air-gap image
inventory. There is no central version catalog.

Renovate discovers the pins through [`renovate.json5`](../renovate.json5) and
opens weekly update PRs through the hosted Renovate GitHub App. Pending and
proposed updates appear in the Renovate dependency dashboard issue.

## Managed surfaces

Renovate discovers and updates versions in:

- `mise.toml`, `mise.aws.toml`, `mise.local-host.toml`, and
  `mise.local-talos.toml`: tool pins and the Zarf CLI pin. Explicit per-tool
  custom managers replace the native mise manager so each pin resolves against
  the intended upstream project.
- `pyproject.toml` and `uv.lock`: documentation site dependencies (mkdocs-material,
  pytest) through the native Python managers.
- `bootstrap-rs/Cargo.toml` and `bootstrap-rs/Cargo.lock`: Rust crate
  dependencies through Renovate's Cargo manager.
- `bootstrap.toml`: the Flux Operator, cert-manager, and CAPI Operator chart
  pins consumed by `krops-bootstrap`. One annotation-driven custom manager reads
  the adjacent `# renovate:` metadata. `mise run validate` cross-checks these
  pins against their declarative Helm releases and proxies. cert-manager's
  pin, its `pivot.sh`/HelmRelease counterparts, its `airgap/zarf.yaml` chart
  pin, and its four `airgap/images.txt`/`airgap/zarf.yaml` image tags all
  share the `platform-charts` group so they can't drift apart (issue #322).
- `bootstrap-rs/Dockerfile`: digest-pinned build and runtime base images, and
  the mise CLI and Podman remote-client build arguments used by the toolbox.
  The `mise install` layer names tools without versions (`python`, `uv`,
  etc.), so every pin resolves from the copied `mise.toml` at build time;
  there is no inline version to keep in lockstep.
- `mgmt/**` and `workload/**` YAML: Flux, Helm, Kubernetes manifests, chart
  values, and clusterctl provider CRs under `capi-providers/`.
- `kindest/node` image tags wherever they are referenced in management
  manifests and air-gap scripts.
- `airgap/images.txt` and `airgap/zarf.yaml`: container image references,
  pinned by digest. `airgap/zarf.yaml` also embeds its own Helm chart-version
  pins (e.g. cert-manager's); the cert-manager one is annotation-driven like
  `bootstrap.toml`'s, so it isn't just carried along by the image-ref manager.
- `airgap/zarf.yaml` and `airgap/files/clusterctl-providers.yaml`: the CAPI
  core, kubeadm bootstrap, kubeadm control-plane, and CAPD provider release
  files, rendered versions, and staged config paths. One custom manager per
  pattern covers all four via a `depName` alternation (e.g.
  `kubernetes-sigs/cluster-api-(?:core|bootstrap-kubeadm|...)`) rather than
  one manager per provider, since the four differed only in that name;
  `depNameTemplate` resolves the match back to the real
  `kubernetes-sigs/cluster-api` repo for version lookup.
- `.github/workflows/`: GitHub Actions references and the Renovate CLI pin used
  by the digest and managed-pin coverage tests.
- `pivot.sh`: imperative cert-manager and CAPI Operator chart pins, retained
  and grouped with `bootstrap.toml` and the Git manifests until the native
  shell path retires.

Grouping rules keep GitHub Actions updates together (excluding workflow container
images and runners), Flux updates together, CAPI updates together, imperative
chart pins with their declarative counterparts, and every pin whose value is a
literal Kubernetes release version together. Renovate proposes one PR at the
newest available version for each dependency, rather than parallel major and
non-major update PRs. Base images in `bootstrap-rs/Dockerfile` and air-gap
images are digest-pinned while retaining readable tags. Nothing automerges.

The `kubernetes-version` group (#142) covers `kindest/node` (docker datasource:
the local-host node image and both Cluster `topology.version` pins),
`kubernetes/kubernetes` (github-releases datasource: the `kubectl` pin in
`mise.toml` and the local-talos `TalosControlPlane.spec.version` annotation),
and the `airgap/images.txt` images kubeadm itself deploys for that release
(`kube-apiserver`, `kube-controller-manager`, `kube-proxy`, `kube-scheduler`,
`coredns/coredns`, `etcd`, `pause`, all under the `registry.k8s.io` docker
datasource). All of these carry a literal Kubernetes release version rather
than an independent versioning scheme, so a Kubernetes bump opens a single PR
across every file that tracks it. This gives same-PR visibility only:
Renovate still resolves each images.txt component to its latest upstream tag,
not the tag kubeadm deploys for the tracked release (etcd and coredns can
drift, and `pause` differs between the two platforms), so a reviewer must
reconcile them (see the update procedure). Other `registry.k8s.io`
images (the CAPI/kubeadm provider controllers) are unaffected: they're
matched by exact depName, not by registry host, and stay in the separate
`cluster-api` group. The kind CLI and Talos's own `talosVersion`
machine-config contract version each follow their own release cadence and
are intentionally excluded from this group.

cert-manager's Helm chart version and its container image tags used to be
tracked as separate, ungrouped dependencies. Four separate Renovate PRs
(#281-#284) bumped only the image tags in `airgap/images.txt` and
`airgap/zarf.yaml` to v1.21.2, while nothing bumped any chart-version pin,
still at 1.21.1. Since `values/cert-manager.yaml` has no image-tag override,
the chart deployed pods expecting v1.21.1 images by default, but Zarf had
only mirrored v1.21.2, so every cert-manager pod sat in `ImagePullBackOff`
and Helm's install wait ran out its 15-minute timeout (issue #322) --
initially misdiagnosed as a Kubernetes-version incompatibility and reported
as such upstream before being retracted
([cert-manager/cert-manager#9123](https://github.com/cert-manager/cert-manager/issues/9123)).
The `platform-charts` group now covers cert-manager's chart pin and its
image tags together, same as the `kubernetes-version` group (#142) does for
Kubernetes; `airgap/zarf.yaml`'s own chart-version line was not tracked by
any manager at all before this (confirmed with a real `renovate
--dry-run=full` run, which returned zero dependencies for that line), so an
annotation-driven custom manager was added for it, mirroring
`bootstrap.toml`'s pattern.

The CAPI group spans both the `github-releases`/`github-release-attachments`
release lookups and the `docker`-datasource digest-pinned images those same
providers deploy (`registry.k8s.io/cluster-api*`,
`registry.k8s.io/cluster-api-helm/*`, `gcr.io/k8s-staging-cluster-api/*`), so
a CAPI version bump lands its release assets and images in one PR instead of
two. CAPZ's one-minor-at-a-time override (issue #71) only matches the
`github-releases` datasource, so it is unaffected by the image grouping.

## Toolbox release version

The `krops-bootstrap` package version lives in `bootstrap-rs/Cargo.toml`. It is a
release version, not a dependency pin, so Renovate does not increment it. When
a `v*` tag is pushed, `.github/workflows/toolbox-release.yml` fails unless the
tag matches that package version, then publishes the multi-architecture
toolbox image, signs it, and attaches an SPDX SBOM attestation.

Lifecycle tool versions installed in the image come from `mise.toml` and
`mise.aws.toml`. The Dockerfile separately pins its Rust builder and Debian
runtime base images, plus the mise installer and Podman remote-client build
arguments. Renovate manages those base references and build arguments.

## Update procedure

1. Wait for a Renovate PR. For configuration troubleshooting, use the pinned
   local dry-run procedure in [AGENTS.md](../AGENTS.md) under "Editing
   renovate.json5".
2. Review the raw and rendered diffs. The `validate` workflow checks kustomize
   builds, air-gap digest pinning, managed-pin extraction coverage,
   `bootstrap.toml` consistency, YAML, and (#142) that `airgap/images.txt`'s
   `kube-apiserver`/`kube-controller-manager`/`kube-proxy`/`kube-scheduler`,
   `coredns`, `etcd`, and `pause` pins match what the pinned `kindest/node`
   version's real `kubeadm` binary actually deploys
   (`airgap/tests/test-airgap-kubeadm-images.py`), rather than drifting to
   whatever the latest upstream tag happens to be. The test verifies the
   kubeadm binary's SHA-256 and each pinned digest against the registry.
   Renovate bumps these pins like any other; when a bump disagrees with
   kubeadm the check fails, and `python3
   airgap/tests/test-airgap-kubeadm-images.py --fix` regenerates every tracked
   pin from kubeadm and the registry so the fix can be committed by hand. The
   workload cluster, the
   CAPD management cluster, and the kind bootstrap mgmt node all run the
   same `kindest/node` version. The check compares against `kubeadm config
   images list` for that version, not a live `crictl` harvest against a
   running node -- worth re-confirming there once an operator has one.
   Also (#322) that cert-manager's chart
   version (`bootstrap.toml`) matches `pivot.sh`, `airgap/zarf.yaml`'s
   embedded chart version, and the cert-manager image tags in
   `airgap/images.txt`/`airgap/zarf.yaml`
   (`tests/test-cert-manager-version-consistency.py`) -- catches the same
   drift the `platform-charts` Renovate group prevents, regardless of how it
   happens.
3. For toolbox inputs, also require the `bootstrap-rs` workflow's Rust checks
   and container build/smoke job.
4. For a `kubernetes-version` PR, re-harvest the images kubeadm deploys for
   the new release (`crictl images` on a node) and reconcile the
   `airgap/images.txt` component tags (`etcd`, `coredns/coredns`, `pause`)
   with them before merging.
5. Merge manually.

The best fix for #142 items 3-4 would be Renovate itself running a script
that regenerates `airgap/images.txt`'s k8s component pins from
`kubeadm config images list` right after a `kindest/node` bump, via
[`postUpgradeTasks`](https://docs.renovatebot.com/configuration-options/#postupgradetasks)
with `executionMode: "branch"` -- the PR would open already correct, with
nothing left to verify. That isn't available here: `postUpgradeTasks` is not
enabled by default on the hosted Mend/Renovate GitHub App this repo runs,
only on a self-hosted Renovate, and this repo deliberately moved off
self-hosting (issue #90, `6ad1db6`) to avoid maintaining a runner. Mend has
allowlisted `postUpgradeTasks` for individual hosted-App repos on request
before (after reviewing the script), so this is a request to make, not a
hard platform limit -- tracked in
[renovatebot/renovate#46270](https://github.com/renovatebot/renovate/discussions/46270),
with the tech debt tracked in #326. The `validate.yml`
`test-airgap-kubeadm-images.py` check is the fallback in the meantime: it
fails the PR before merge instead of letting the drift ship silently, and
`--fix` is the manual stand-in for the script. If Mend allowlists
`postUpgradeTasks` for this repo, or self-hosting is revisited, replacing
this check with that script would close the gap properly and make the CI
test redundant.

If an image appears in both a manifest and the air-gap inventory
(`airgap/images.txt` or `airgap/zarf.yaml`), update both in the same PR. There
is no automated completeness check between manifests and the inventory, so
verify the pairing during review.

## Intentional differences

- EKS cluster versions are not Renovate-managed pins and upgrade independently
  of `kindest/node`.
- EKS addon versions (`*-eksbuild.*`) have no public registry datasource and
  are updated manually.
- Unversioned local tags such as
  `localhost:5001/krops-airgap:latest` have no comparable release version
  and remain untracked.
- `scripts/toolbox-run.sh` defaults `TOOLBOX_IMAGE` to the mutable
  `ghcr.io/polarsquad/krops-toolbox:latest`; Renovate does not manage this
  runtime default.
- The toolbox Dockerfile installs `docker-ce-cli` from Docker's apt repository
  without a package-version pin. Its client version intentionally follows that
  repository, while the base image, mise installer, and Podman client remain
  Renovate-managed.
- `*.sops.yaml` `version:` fields, Kubernetes `apiVersion` strings, Helm chart
  `appVersion` values, `bootstrap-rs/Cargo.toml`'s package version, and the
  Zarf package `metadata.version` are not dependency pins.
- The Zarf CLI pin in `mise.toml` keeps `version` unprefixed and adds `v`
  literally in `asset_pattern` (issue #324): mise's `{{ version }}` template
  variable has stripped a leading `v` inconsistently across mise releases, so
  an unprefixed pin sidesteps that. `asset_pattern` also remaps `arch()` to
  `amd64`/`arm64`, since Zarf's release assets don't use mise's default
  `x64`/`arm64` naming. `tests/test-mise-zarf-pin.py` installs the pinned
  release via mise and checks the reported version, since a template mismatch
  otherwise fails silently until the pin is exercised.
