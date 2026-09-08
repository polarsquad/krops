# Secret management

In-cluster secrets are managed with [SOPS](https://github.com/getsops/sops) +
[age](https://github.com/FiloSottile/age), so encrypted manifests can live
safely in Git and Flux decrypts them at reconcile time.

- **`.sops.yaml`** declares the age *public* key (safe to commit) and a rule
  that encrypts only `data`/`stringData` fields of any `*.sops.yaml` file under
  `mgmt/aws/`.
- The age *private* key lives in `age.agekey` (gitignored). The bootstrap
  loads it into the cluster as the `sops-age` secret in `flux-system`.

SOPS-encrypted secrets in this repo (each referenced by a Flux `Kustomization`
with `spec.decryption.provider: sops`):

| File | Consumed by | Purpose |
|---|---|---|
| `mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml` | `capa-system` | CAPA controller AWS credentials |
| `mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml` | `ack-controllers` | ACK IAM/EKS controller AWS credentials (shared-credentials-file format) |
| `mgmt/aws/addons/flux-apps/flux-pull-secret.sops.yaml` | `flux-apps` | GitHub PAT pull secret (basic auth), delivered to each workload cluster via ClusterResourceSet so its Flux can clone this (private) repo |
| `mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml` | `konflate` | `KONFLATE_TOKEN` (read-only GitHub PAT so konflate can list PRs and clone this private repo) and `KONFLATE_WRITE_TOKEN` (write-back credential konflate uses to post the PR summary comment and the `Konflate` commit status) |

## First-time setup

```sh
mise run sops-keygen        # creates ./age.agekey and prints the public key
export SOPS_AGE_KEY_FILE="$PWD/age.agekey"
```

`AGE_KEY_FILE` is the bootstrap input; `SOPS_AGE_KEY_FILE` tells the SOPS CLI
which private key to use while editing encrypted files.

With the toolbox and no host mise installation:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$PWD:/workspace" -w /workspace \
  --entrypoint age-keygen "$TOOLBOX_IMAGE" -o age.agekey
```

Set `TOOLBOX_IMAGE` to a published toolbox tag or a local build. Put the
printed public key into the `age:` field of `.sops.yaml`, then re-encrypt
existing secrets so they target your key:

```sh
mise run sops-updatekeys
```

The container-only equivalent uses the new private key explicitly:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$PWD:/workspace" -w /workspace \
  -e SOPS_AGE_KEY_FILE=/workspace/age.agekey \
  --entrypoint sh "$TOOLBOX_IMAGE" -c '
    for f in $(find mgmt/aws -name "*.sops.yaml"); do
      sops updatekeys --yes "$f"
    done
  '
```

## Setting / rotating AWS credentials

```sh
# CAPA: generate the base64 profile (requires AWS creds in your shell env):
clusterawsadm bootstrap credentials encode-as-profile
# Put the value into stringData.AWS_B64ENCODED_CREDENTIALS, then encrypt:
$EDITOR mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
mise run sops-encrypt mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml

# ACK: standard AWS shared-credentials-file format under stringData.credentials:
$EDITOR mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml
mise run sops-encrypt mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml
```

View a decrypted secret without changing it:

```sh
mise run sops-decrypt <file>.sops.yaml
```

## Setting / rotating the GitHub PAT

The management cluster's own `flux-github-pat` secret is created imperatively
at bootstrap (from `GITHUB_TOKEN` in `.env`) and is **not** in Git; Flux
needs it to clone the repo before it could ever decrypt anything (a
chicken-and-egg constraint). The workload clusters' copy *is* in Git
(`flux-pull-secret.sops.yaml`) because the management cluster's Flux decrypts
it before shipping it out via ClusterResourceSet.

To set or rotate the PAT in the workload clusters' pull secret:

```sh
# Decrypt in place, put the PAT into the nested stringData.password field,
# then re-encrypt:
mise x -- sops --decrypt --in-place --input-type yaml --output-type yaml \
  mgmt/aws/addons/flux-apps/flux-pull-secret.sops.yaml
$EDITOR mgmt/aws/addons/flux-apps/flux-pull-secret.sops.yaml
mise run sops-encrypt mgmt/aws/addons/flux-apps/flux-pull-secret.sops.yaml
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
mise x -- sops --decrypt --in-place --input-type yaml --output-type yaml \
  mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
$EDITOR mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
mise run sops-encrypt mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml
```

Keep the two tokens separate: the read token (`KONFLATE_TOKEN`) should carry
no write scope, and the write token is used only for konflate's write-back
(the PR summary comment and the `Konflate` commit status).
