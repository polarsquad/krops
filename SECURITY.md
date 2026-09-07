# Security policy

krops is a reference implementation of a GitOps pattern for managing cloud
infrastructure through the Kubernetes API. It is not a product with a
release cadence, but its manifests, scripts, CLI, container image, and CI
workflows provision real cloud resources and handle real credentials, so
security reports are taken seriously.

## Reporting a vulnerability

Report privately through GitHub:
[Report a vulnerability](https://github.com/polarsquad/krops/security/advisories/new).
This opens a draft security advisory visible only to the repository
maintainers.

Do not open a public issue, pull request, or discussion for anything that
could be exploited before a fix is available. That includes a committed
credential, a bypass of the SOPS or signing chain, or a CI workflow that
can be made to run untrusted code with repository permissions.

Include:

- The affected path or component (manifest, script, `bootstrap-rs/`,
  the toolbox image, the air-gap bundle, a workflow, `renovate.json5`).
- The commit on `main` you tested against.
- Steps to reproduce, or the reasoning if reproduction needs cloud
  resources you cannot provision.
- Impact as you understand it: what an attacker gains and which
  environment (`aws`, `local-host`, `local-talos`) is affected.

You will get an acknowledgement within 7 days. Maintainers volunteer their
time; there is no paid response team and no bug bounty. Please allow up to
90 days before public disclosure, or coordinate an earlier date once a fix
is merged.

## Supported versions

Only `main` is supported. There are no release branches. The toolbox image
`ghcr.io/polarsquad/krops-toolbox` is published from `v*` tags when they
exist; the newest tag is the only supported image version, and fixes land
on `main` first.

## Scope

In scope:

- Kubernetes manifests under `mgmt/` and `workload/`, including the IAM
  policies, S3 bucket posture, and RDS configuration they declare.
- The bootstrap, pivot, and teardown lifecycle: `bootstrap-rs/`, the
  shell scripts, `bootstrap.toml`, and `scripts/toolbox-run.sh`.
- The toolbox container image and its signing and SBOM attestation.
- The air-gap bundle build, signing, and offline verification under
  `airgap/`.
- GitHub Actions workflows under `.github/workflows/`, including token
  permissions and fork handling.
- The Renovate configuration, where a malicious or mistaken rule could
  pull an unintended dependency.
- Secret handling: the SOPS and age setup, `.env` handling, and anything
  that could leak a credential into Git, logs, or CI artifacts.

Out of scope:

- Vulnerabilities in upstream projects the repository consumes (Flux,
  Cluster API and its providers, ACK controllers, konflate, Zarf, Talos,
  Tinkerbell, kind). Report those upstream; a report here is welcome only
  if krops configures the component in a way that makes the issue worse
  or bypasses a mitigation.
- The AWS service side (EKS, IAM, S3, RDS) itself.
- Deployments made from forks or adapted copies of this repository. The
  README states the intended use: fork it and adapt it. Once adapted, the
  security posture is the operator's.
- The demo `krops-reader` console user password, which is set
  imperatively by the operator and never stored in Git.

## What the repository already does

Knowing the existing controls helps frame whether a finding is a bypass or
a gap.

- Secrets are encrypted with SOPS and age. Only `data` and `stringData`
  fields in `*.sops.yaml` are encrypted; the age public recipient is
  committed, the private key (`age.agekey`) and `.env` are gitignored.
  Rotation procedures are in `docs/secrets.md`.
- Workload clusters use EKS Pod Identity; no static AWS keys are present
  on them. The management cluster holds the CAPA and ACK credentials as
  SOPS-encrypted secrets. See `docs/aws-iam.md`.
- Read access to AWS is scoped: one IAM user whose only permission is
  `sts:AssumeRole` into per-cluster `krops-*-reader` roles.
- S3 buckets block all public access, enforce SSE, enable versioning,
  disable ACLs, and deny non-TLS requests. See `docs/workload-resources.md`.
- The `konflate` PR review workflow uses only the workflow `GITHUB_TOKEN`
  with `contents: read` and is skipped for pull requests from forks so
  untrusted sources are not rendered with repository permissions.
- The toolbox image is signed with cosign keyless (GitHub OIDC, workflow
  identity) and carries an SPDX SBOM attestation. Verify with:

  ```sh
  cosign verify ghcr.io/polarsquad/krops-toolbox:X.Y.Z \
    --certificate-identity-regexp \
      '^https://github.com/polarsquad/krops/.github/workflows/toolbox-release.yml@refs/tags/vX.Y.Z$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  ```

- The air-gap bundle is signed (keyless in CI, operator key locally) and
  ships per-component Syft SBOMs. `airgap/scripts/offline-run.sh` verifies
  the signature, checksums, and SBOMs before touching a cluster. External
  images in the bundle are pinned by digest and a CI gate rejects unpinned
  additions. See `docs/airgap.md`.
- Dependency updates are proposed by the hosted Renovate GitHub App and
  reviewed as ordinary pull requests, rendered as Flux diffs before merge.

Open hardening work is tracked under the `4-hardening` milestone,
principally #80 (air-gap supply chain) and #138 (package build and signing
model). A report that overlaps that work is still useful; say so and the
maintainers will link it.

## If you find a committed secret

Treat it as compromised even if it was removed in a later commit; Git
history and forks retain it.

1. Report it privately as described above.
2. Maintainers rotate the credential following `docs/secrets.md` (AWS
   credentials, the GitHub PAT, the konflate tokens) or the provider's
   own procedure, then re-encrypt and merge.
3. The exposure is disclosed in the advisory once rotation is confirmed.

## Attribution

Reporters are credited in the published advisory unless they ask not to
be.
