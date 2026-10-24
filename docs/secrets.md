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

<a id="setting--rotating-aws-credentials"></a>

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

## Age-key compromise recovery

Treat every value decryptable by the compromised identity as exposed, including
historical Git revisions, clones, and backups. Re-encrypting the current tree
cannot make historical ciphertext secret again. Use a trusted workstation and
coordinate recovery with the account owner and repository maintainers.

1. Inventory current and historical encrypted credentials without printing
   plaintext into logs or tracked files. Immediately disable all still-valid
   exposed AWS access keys (CAPA, ACK, and any shared bootstrap `.env` key),
   and revoke exposed GitHub PATs: every regional workload pull secret across
   AWS/Azure/GCP, `KONFLATE_TOKEN`, and `KONFLATE_WRITE_TOKEN`. Include older
   values that remain valid. If bootstrap's `GITHUB_TOKEN` shares an exposed
   PAT, revoke it too. Check other credentials accessible from the compromised
   host or identity; do not assume the current Git tree bounds the exposure.
   Follow [AWS containment](./aws-iam.md#e2e-account-incident-and-credential-revocation)
   for affected AWS sessions and assess GitHub access and write activity.
2. Generate a fresh age identity on the trusted workstation; securely replace
   the gitignored `age.agekey` (keygen refuses to overwrite it). Update
   `AGE_KEY_FILE` and any `AGE_PUBLIC_KEY` override to match. Replace the
   compromised public recipient in `.sops.yaml` with the new recipient and
   only other explicitly trusted recipients. Inventory **every** encrypted
   manifest and its SOPS envelope; remove the compromised recipient everywhere,
   including envelopes carrying recipients not present in `.sops.yaml`.
3. Use the old identity only in the isolated recovery process when necessary
   to decrypt existing files, never as a recipient of new ciphertext. Rewrap
   every envelope with `sops updatekeys --yes <file>` against the new
   configuration, then use `sops rotate --in-place <file>` with the trusted
   replacement identity for each manifest to generate a fresh data key.
   `sops-updatekeys` alone changes recipients, not data keys. Complete both
   operations **before** inserting replacement credentials, so new values are
   never encrypted for the compromised recipient. See
   [SOPS key management](https://getsops.io/docs/usage/key-management/).
   Then replace exposed credential values with newly issued, validated values:
   update AWS `.env`, CAPA, and ACK together using the rotation procedure above;
   update every regional pull secret and both konflate tokens using their
   respective procedures. Preserve encryption of all `data`/`stringData`
   values; leave no plaintext in Git.
4. Coordinate live replacement **before activating the new ciphertext**.
   Management `flux-system/flux-github-pat` is bootstrap-owned, not Git-owned;
   editing regional manifests or `.env` does not update it. Management
   `flux-system/sops-age` and `default/sops-age` are also bootstrap-owned;
   the latter decrypts the remote-apply workload pull secrets. Inventory active
   bootstrap and post-pivot management clusters and any additional copies of
   these identities. Have the operator replace these bootstrap-owned inputs
   through a scoped, coordinated recovery operation, removing every entry
   containing the compromised private identity. Confirm the new Git credential
   can fetch the recovery commit and the new age identity is available in both
   namespaces before merge/reconciliation. Do not blindly rerun bootstrap: it
   performs lifecycle operations beyond secret replacement. Git-owned Secrets
   must still change in Git and converge through Flux; the operator-owned
   bootstrap prerequisites require this separate recovery step.
5. In isolated local processes, verify every resulting manifest decrypts with
   only the replacement identity and **fails** with only the compromised
   identity (exclude other identity files/environment inputs from that check).
   Check each SOPS envelope contains only trusted recipients and compare
   replacement credential values without printing them. Run `mise run validate`
   and review the encrypted diff before merging to `main`.
6. After merge, verify management GitRepository fetches and Flux decryption in
   both namespaces, CAPA and each ACK controller's successful AWS operations,
   each workload Flux GitRepository fetch with the replacement regional PAT,
   and konflate's read and write-back access with their separate tokens.
   Verify running bootstrap processes and local `.env` inputs use replacements.
   Secret readiness alone does not prove credential reload; keep old credentials
   revoked even if reconciliation stalls. Remove compromised private-key copies
   from live bootstrap-owned Secrets, workstations, and recovery material;
   retain only protected incident evidence as required. Confirm provider-side
   revocation for current and historical exposed credentials before declaring
   recovery complete.
