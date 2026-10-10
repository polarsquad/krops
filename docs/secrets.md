# Secret management

In-cluster secrets are managed with [SOPS](https://github.com/getsops/sops) +
[age](https://github.com/FiloSottile/age), so encrypted manifests can live
safely in Git and Flux decrypts them at reconcile time.

- **`.sops.yaml`** declares the age *public* key (safe to commit) and a rule
  that encrypts only `data`/`stringData` fields of any `*.sops.yaml` file under
  `mgmt/aws/`, `mgmt/azure/`, or `mgmt/gcp/`.
- The age *private* key lives in `age.agekey` (gitignored). The bootstrap
  loads it into the cluster as the `sops-age` secret in both `flux-system` and `default`.

SOPS-encrypted secrets in this repo (each referenced by a Flux `Kustomization`
with `spec.decryption.provider: sops`):

| File | Consumed by | Purpose |
|---|---|---|
| `mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml` | `capa-system` | CAPA controller AWS credentials |
| `mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml` | `ack-controllers` | ACK S3/RDS/IAM controller AWS credentials (shared-credentials-file format) |
| `mgmt/aws/addons/flux-apps/regions/<region>/flux-github-pat.sops.yaml` | `flux-apps` | GitHub PAT pull secret (basic auth), delivered to each workload cluster by a templated ResourceSet (management Kustomization in `default` + `kubeConfig` remote-apply + SOPS decryption via `default/sops-age`) so its Flux can clone this (private) repo. The `mgmt/azure/addons/flux-apps/` and `mgmt/gcp/addons/flux-apps/` copies serve the same role for their environments. |
| `mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml` | `konflate` | `KONFLATE_TOKEN` (read-only GitHub PAT so konflate can list PRs and clone this private repo) and `KONFLATE_WRITE_TOKEN` (write-back credential konflate uses to post the PR summary comment and the `Konflate` commit status) |

## The age key in two namespaces

Both bootstrap paths (`bootstrap-common.sh` and `create_github_secrets` in
`bootstrap-rs/src/main.rs`) create `sops-age` in `flux-system` and `default` on
the kind cluster and again on the pivot target. The ResourceSet-generated
Kustomizations live in `default` and `spec.decryption.secretRef` resolves only
in the Kustomization's own namespace.

Exposure is the same full age key in both namespaces, capable of decrypting all
`*.sops.yaml`. No workload pods run in `default` on the management cluster.
Controllers with cluster-wide Secret read already have access to
`flux-system/sops-age`. The second copy in `default` adds exposure only to
principals with namespace-scoped Secret read in `default`, who can also read
`<cluster>-kubeconfig` admin credentials there.

Do not grant Secret read in `default` to anyone who should not hold the age
key; do not run workloads there. Rotate both copies when rotating the age key.

## First-time setup

Every SOPS step is a mise task run in the toolbox as your own user, so the
key file it writes is owned by you (the run shape is defined in
[Helper tasks in the toolbox](./operations.md#helper-tasks-in-the-toolbox)).
The commands below use this shell function for brevity:

```sh
export TOOLBOX_IMAGE=ghcr.io/polarsquad/krops-toolbox:latest   # or krops-toolbox:dev
krops_mise() {
  docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/workspace" -w /workspace \
    -e MISE_AUTO_INSTALL=0 -e SOPS_AGE_KEY_FILE=/workspace/age.agekey \
    --entrypoint mise "$TOOLBOX_IMAGE" "$@"
}
```

Generate the key (refuses to overwrite an existing `age.agekey`) and read
the public key it prints:

```sh
krops_mise run sops-keygen        # creates ./age.agekey and prints the public key
```

`AGE_KEY_FILE` (in `.env`, default `age.agekey`) is the bootstrap input;
`SOPS_AGE_KEY_FILE` (set by `krops_mise` above) tells the SOPS CLI which
private key to use while editing encrypted files.

Put the printed public key into the `age:` field of `.sops.yaml`, then
re-encrypt every `*.sops.yaml` under `mgmt/` so they target your key:

```sh
krops_mise run sops-updatekeys
```

## Setting / rotating AWS credentials

Rotate all consumers of the affected key together: the gitignored `.env`,
CAPA's encrypted profile, and the management ACK shared credentials file.
Updating CAPA alone leaves ACK using its previous credential. For a suspected
compromise, disable the old key immediately as described in
[Credential revocation](./aws-iam.md#e2e-account-incident-and-credential-revocation),
even if controllers temporarily lose AWS access.

1. Validate the replacement with `aws sts get-caller-identity` in the intended
   credential context; check the account and principal against the environment
   being managed. Update `.env` before generating CAPA's profile: mise loads
   that file ahead of process environment values.
2. Update both encrypted manifests using the commands below. Preserve CAPA's
   generated profile and ACK's `[default]` profile, including a session token
   when using temporary credentials. Keep the existing age recipient.

```sh
# CAPA: generate the base64 profile. clusterawsadm reads AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN / AWS_REGION from .env first
# (mise env_file), then from the process environment:
krops_mise -E aws run aws-credentials
# Put the value into stringData.AWS_B64ENCODED_CREDENTIALS, then encrypt:
$EDITOR mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
krops_mise run sops-encrypt mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml

# ACK: standard AWS shared-credentials-file format under stringData.credentials:
$EDITOR mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml
krops_mise run sops-encrypt mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml
```

3. Decrypt both manifests in a local process, decode CAPA's base64 profile,
   and compare its access key, secret key, and optional session token with
   ACK's profile and the intended replacement. Report only match/mismatch;
   do not print credentials or put decrypted material in tracked files or
   logs. If the age identity cannot decrypt either manifest, stop rather than
   assuming its encrypted value matches.
4. Run `mise run validate` and review that both manifests still encrypt all
   credential fields before committing. A Git update is not live adoption:
   after merge to `main`, inspect Flux reconciliation and verify successful
   AWS operations from CAPA and each management ACK controller (S3, RDS,
   IAM). Also verify any running bootstrap process has adopted the replacement.
   Secret reconciliation alone does not prove a process reloaded its credential.
5. For a routine rotation, retire the old key only after those checks. For an
   incident, keep it disabled throughout recovery and delete it after verifying
   replacement adoption; do not reactivate it to restore reconciliation.

View a decrypted secret without changing it:

```sh
krops_mise run sops-decrypt <file>.sops.yaml
```

## Azure credentials

Azure holds no secret at rest (issue #236): CAPZ and the bundled ASO
authenticate with workload identity, so there is nothing to rotate here. The
only Azure credentials involved are the operator's own `az login` session and
the age key used for the remaining SOPS-encrypted files above
([azure.md](./azure.md) covers the identity flow).

## Setting / rotating the GitHub PAT

The management cluster's own `flux-github-pat` secret is created imperatively
at bootstrap (from `GITHUB_TOKEN` in `.env`) and is **not** in Git; Flux
needs it to clone the repo before it could ever decrypt anything (a
chicken-and-egg constraint). The workload clusters' copy *is* in Git
(`regions/<region>/flux-github-pat.sops.yaml`, one per region) because the
management cluster's Flux SOPS-decrypts it in namespace `default` and
remote-applies it to the workload via the ResourceSet-generated
`Kustomization` (through `kubeConfig` + the CAPI `<cluster>-kubeconfig`
Secret).

To set or rotate the PAT in the workload clusters' pull secret, decrypt,
edit `stringData.password`, and re-encrypt the copy in **every** region
directory that carries it (aws: eu-north-1 + eu-west-1, azure: swedencentral,
gcp: europe-north1) so the per-region copies stay identical:

```sh
for f in \
  mgmt/aws/addons/flux-apps/regions/eu-north-1/flux-github-pat.sops.yaml \
  mgmt/aws/addons/flux-apps/regions/eu-west-1/flux-github-pat.sops.yaml \
  mgmt/azure/addons/flux-apps/regions/swedencentral/flux-github-pat.sops.yaml \
  mgmt/gcp/addons/flux-apps/regions/europe-north1/flux-github-pat.sops.yaml; do
  krops_mise x -- sops --decrypt --in-place --input-type yaml --output-type yaml "$f"
  $EDITOR "$f"
  krops_mise run sops-encrypt "$f"
done
```

Remember to also update `GITHUB_TOKEN` in `.env` so the next bootstrap uses
the new token.

## Setting and rotating the konflate tokens

What each token does is covered in [PR review: konflate](./konflate.md).

```sh
# Decrypt in place, set stringData.KONFLATE_TOKEN (a read-only GitHub PAT) and
# stringData.KONFLATE_WRITE_TOKEN (a fine-grained PAT with Pull requests +
# Commit statuses R/W on this repo, or a classic PAT with `repo` scope),
# then re-encrypt:
krops_mise x -- sops --decrypt --in-place --input-type yaml --output-type yaml \
  mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
$EDITOR mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
krops_mise run sops-encrypt mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
```

Keep the two tokens separate: the read token (`KONFLATE_TOKEN`) should carry
no write scope, and the write token is used only for konflate's write-back
(the PR summary comment and the `Konflate` commit status).
