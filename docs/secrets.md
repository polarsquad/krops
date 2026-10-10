# Secret management

In-cluster secrets are managed with [SOPS](https://github.com/getsops/sops) +
[age](https://github.com/FiloSottile/age), so encrypted manifests can live
safely in Git and Flux decrypts them at reconcile time.

- **`.sops.yaml`** declares the age *public* key (safe to commit) and a rule
  that encrypts only `data`/`stringData` fields of any `*.sops.yaml` file under
  `mgmt/aws/`, `mgmt/azure/`, or `mgmt/gcp/`.
- The age *private* key lives in `age.agekey` (gitignored). The bootstrap
  loads it into the cluster as the `sops-age` secret in `flux-system`.

SOPS-encrypted secrets in this repo (each referenced by a Flux `Kustomization`
with `spec.decryption.provider: sops`):

| File | Consumed by | Purpose |
|---|---|---|
| `mgmt/aws/addons/flux-apps/regions/<region>/flux-github-pat.sops.yaml` | `flux-apps` | GitHub PAT pull secret (basic auth), delivered to each workload cluster by a templated ResourceSet (management Kustomization in `default` + `kubeConfig` remote-apply + SOPS decryption via `default/sops-age`) so its Flux can clone this (private) repo. The `mgmt/azure/addons/flux-apps/` and `mgmt/gcp/addons/flux-apps/` copies serve the same role for their environments. |
| `mgmt/aws/infrastructure/konflate/konflate-token.sops.yaml` | `konflate` | `KONFLATE_TOKEN` (read-only GitHub PAT so konflate can list PRs and clone this private repo) and `KONFLATE_WRITE_TOKEN` (write-back credential konflate uses to post the PR summary comment and the `Konflate` commit status) |

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

AWS credentials are **not** stored in Git (issue #379): the CAPA and ACK
controller secrets are created imperatively from ambient credentials
(`AWS_B64ENCODED_CREDENTIALS`) at bootstrap/pivot time. This mirrors the Azure
and GCP posture ("none at rest").

To set or rotate credentials, update the AWS access key ID and secret in `.env`:

```sh
# Update .env with the new access key:
# AWS_ACCESS_KEY_ID=AKIA...
# AWS_SECRET_ACCESS_KEY=...
# (and optionally AWS_SESSION_TOKEN for temporary credentials)

# Re-run bootstrap/pivot to seed the new credentials:
./bootstrap.sh aws  # or ./pivot.sh if already bootstrapped
```

No SOPS encryption or re-encryption is needed: credentials are derived from
`.env` on demand by the bootstrap/pivot phases.

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
