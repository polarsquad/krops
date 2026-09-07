# PR review: konflate

[Konflate](https://github.com/home-operations/konflate) reviews this repo's
open PRs as **rendered** Flux diffs instead of raw file diffs. For each PR it
renders the full Flux output at the merge-base and at the head, then diffs the
two, so a review shows:

- **Blast radius** — which clusters/Kustomizations a change actually touches
  (a one-line kustomize edit can fan out to many rendered resources).
- **Image changes** — container image bumps extracted from the rendered
  output.
- **Render failures** — a PR that breaks the Flux render is caught before
  merge, not at reconcile time.
- **Danger lint** — cautions on risky changes.

Reporting happens two ways:

- **GitHub Actions** (the reliable merge gate): the `konflate` workflow runs a
  one-shot konflate as a service container on each PR push and fails the job
  unless the render is clean. It needs no inbound reachability and no stored
  secrets. See [GitHub Actions](#github-actions).
- **In-cluster write-back**: the konflate instance on the management cluster
  posts results to GitHub itself, outbound-only, so the local kind cluster
  needs no inbound reachability and nothing is exposed publicly. It also hosts
  the review UI. See [Write-back](#write-back).

## Deployment

A single instance runs on the **management cluster**, deployed from
`mgmt/aws/infrastructure/konflate/` by the `konflate` Flux Kustomization
(`mgmt/aws/infrastructure/flux-ks.yaml` — SOPS decryption enabled, no
`dependsOn`, so it comes up independently of the CAPI/ACK chains).

| Piece | Detail |
|---|---|
| Chart | OCI artifact `oci://ghcr.io/home-operations/charts/konflate`, pinned tag (see `helm.yaml`) |
| Namespace | `konflate` |
| `config.repo` | `github://polarsquad/krops` |
| `config.clusterPath` | `""` — render from the repo root, matching this repo's root-relative Flux Kustomization paths (`./mgmt/aws/...`, `./workload/...`) |
| `config.prComments` | `true` — post the rendered summary as a PR comment |
| `config.statusChecks` | `true` — post the `Konflate` commit status with the render verdict |
| Secret | `konflate-token` (SOPS-encrypted, `konflate-token.sops.yaml`) |
| Persistence | Enabled (kind's default local-path StorageClass) so source caches and rendered diffs survive pod restarts |

## GitHub Actions

`.github/workflows/konflate.yml` runs konflate on every PR push, independent
of whether the kind management cluster is online:

- konflate runs as a **service container** inside the job
  (`ghcr.io/home-operations/konflate`, pinned to the same version as the
  in-cluster chart). `KONFLATE_PR_FILTER_EXPR` scopes it to render only the
  triggering PR, and `KONFLATE_REFRESH_INTERVAL=0` makes it a one-shot
  render: the job ends when the render does.
- The job pulls the summary Markdown from
  `GET /api/prs/{n}/summary?forge=github`. The endpoint answers
  `503 + Retry-After` until the render reaches a terminal state, so
  `curl --retry` waits it out with no polling loop.
- The summary is upserted as a single PR comment keyed by konflate's hidden
  `<!-- konflate:pr-N -->` marker. The in-cluster write-back keys on the same
  marker, so the two share one comment instead of piling up duplicates.
- The job gates on the `X-Konflate-Render-Status` header: only `ok` passes.
  `failures` (some resources did not render) and `error` (no diff produced)
  fail the job. To gate merges on the render, require the
  `konflate / Rendered Flux diff` check in branch protection.
- Auth is the workflow's own `GITHUB_TOKEN` (konflate clones the repo and
  lists PRs with it; the job posts the comment with it). There are no PATs or
  stored secrets to rotate. Fork PRs are skipped: rendering a fork runs
  untrusted sources through flate.

The in-cluster instance remains useful when online: it hosts the review UI
and posts the `Konflate` commit status. The workflow is what makes the render
verdict a dependable gate.

## Authentication

The `konflate-token` secret carries two values (rotation:
[Secret management](./secrets.md#setting--rotating-the-konflate-tokens)):

- **`KONFLATE_TOKEN`** — a read-only GitHub PAT. The repo is private, so
  konflate needs it to list PRs and clone.
- **`KONFLATE_WRITE_TOKEN`** — the write-back credential, kept separate from
  the read token so that one carries no write scope. A fine-grained PAT with
  **Pull requests** and **Commit statuses** (R/W) on this repo, or a classic
  PAT with `repo` scope.

## Write-back

On every render konflate:

1. **Posts / edits the PR comment** — the rendered summary (blast radius,
   image changes, cautions, render failures) as a single comment, found by a
   hidden marker and edited in place on each subsequent render — it never
   piles up duplicates.
2. **Posts the `Konflate` commit status** on the PR head — `success` when the
   diff rendered, `failure` when it didn't. To gate merges on the render, mark
   `Konflate` as a required status check in branch protection.

Notes:

- PRs re-render automatically on konflate's refresh interval (default 30m)
  and whenever it observes the head advance; there is no push/webhook trigger
  configured, so a fresh push can take up to one interval to be reviewed.
- The posted comment and status carry no "view review" link because
  `KONFLATE_PUBLIC_URL` is unset (the UI isn't reachable from outside).
- Fork PRs are never rendered (`KONFLATE_RENDER_FORK_PRS` is off by default).

## UI

The UI is not exposed outside the cluster:

```sh
kubectl port-forward -n konflate svc/konflate 8080:8080
# then open http://localhost:8080
```
