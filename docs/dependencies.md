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
  pins against their declarative Helm releases and proxies.
- `bootstrap-rs/Dockerfile`: digest-pinned build and runtime base images, plus
  the mise CLI and Podman remote-client build arguments used by the toolbox.
- `mgmt/**` and `workload/**` YAML: Flux, Helm, Kubernetes manifests, chart
  values, and clusterctl provider CRs under `capi-providers/`.
- `kindest/node` image tags wherever they are referenced in management
  manifests and air-gap scripts.
- `airgap/images.txt` and `airgap/zarf.yaml`: container image references,
  pinned by digest.
- `.github/workflows/`: GitHub Actions references and the Renovate CLI pin used
  by the digest and managed-pin coverage tests.
- `pivot.sh`: imperative cert-manager and CAPI Operator chart pins, retained
  and grouped with `bootstrap.toml` and the Git manifests until the native
  shell path retires.

Grouping rules keep Flux updates together, CAPI updates together, imperative
chart pins with their declarative counterparts, and node-version updates
separate. Base images in `bootstrap-rs/Dockerfile` and air-gap images are
digest-pinned while retaining readable tags. Nothing automerges.

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
   `bootstrap.toml` consistency, and YAML.
3. For toolbox inputs, also require the `bootstrap-rs` workflow's Rust checks
   and container build/smoke job.
4. Merge manually.

If an image appears in both a manifest and the air-gap inventory
(`airgap/images.txt` or `airgap/zarf.yaml`), update both in the same PR. There
is no automated completeness check between manifests and the inventory, so
verify the pairing during review.

## Intentional differences

- The kind management node image, CAPD workload node images, and EKS cluster
  versions are separate pins and can upgrade independently.
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
