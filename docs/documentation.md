# Documentation guidelines

The structure rules every documentation change follows. The structure stays
stable while content keeps moving: when a change makes a fact true (or
stops it being true), the affected page is updated in the same pull
request, and a limitation that is discovered or resolved gets a
[known issues registry](./known-issues.md) entry with linked sources in the
same pull request.

## Page ownership

One page owns one subject; a fact lives where its subject lives.

- The repository `README.md`: the site home (the assembler renders it as
  `index.md`), the environment table, and the repository layout.
- `architecture.md`: the reconciliation pillars, the reconciliation order,
  and the per-environment architecture. Not a runbook.
- `operations.md`: toolbox usage, prerequisites, budgets and quotas, pivot
  recovery, and teardown.
- `bootstrap-cli.md`: the `krops-bootstrap` engine and its configuration.
- `extending.md`: how to add workload clusters, applications, and custom
  providers.
- `secrets.md`: SOPS + age setup and credential rotation.
- `aws.md`, `azure.md`, `gcp.md`: the environment guides: prerequisites,
  credentials, and the lifecycle. Known limitations are link-only mentions
  that point at the registry; the detail lives there.
- `aws-iam.md`: the AWS IAM surface: ACK controllers, reader role, and CI
  OIDC.
- `workload-resources.md`: the cloud resources the management cluster
  provisions per environment.
- `troubleshooting-ack.md`: ACK controller failure modes for the CRs krops
  ships.
- `konflate.md`: rendered pull request reviews and the CI gate.
- `dependencies.md`: Renovate-managed updates and the pinning policy.
- `airgap.md`: the Zarf offline bundle, the offline run, and the update
  drills.
- `crossplane.md`: the frozen design record for the Crossplane resource
  plane. It records a decided-but-not-implemented design: no live claims,
  updated only when the decision changes.
- `wiremock-e2e-spike-findings-aws.md`, `-azure.md`, `-gcp.md`: the Phase 0
  virtualized-e2e spike findings, one per cloud.
- `proposals/`: undecided designs. A design moves out of `proposals/` to a
  page (or to the registry) when it is implemented.
- `known-issues.md`: the known issues registry, the only place for known
  issues, provider constraints, and platform constraints.
- `documentation.md` (this page): these documentation structure guidelines.

## Page template

New pages follow this section order. A page may omit a section it has
nothing to say about, but the order of the sections it does carry must
hold:

1. Overview: what the page covers, in a paragraph.
2. How it works: the mechanism, with diagrams where they help.
3. Credentials: how the environment authenticates (or why it needs no
   credentials at rest).
4. Upgrading: how the pins and the components move forward.
5. Known limitations: link-only mentions. Each bullet names the
   limitation in one line and links the registry entry; the detail is not
   restated.
6. See also: links to the related pages.

## Link rules

- Internal cross-links use relative paths (`./other-page.md`); the docs
  assembler and the strict build resolve them.
- Background material is a full URL to provider or upstream documentation,
  a full GitHub issue/PR URL, or a repo-relative path in a code span for
  in-repo evidence. Never a bare issue number.
- Every registry entry carries at least one source link and, where a fix
  is tracked, the repo issue that tracks it.

## Status rule

Every registry entry ends with a status line: `open`, `mitigated`,
`upstream-tracked`, or `fixed in <PR>`. A human confirms the status; an
agent must not mark an entry fixed without a merged PR or a live
verification note. Remove an entry only when the limitation is fixed in a
merged PR or confirmed gone in a live run, and note what closed it.

## Placement

- Undecided designs go to `proposals/`; the registry holds what is true
  today.
- A limitation that is discovered or resolved gets a registry entry in the
  same pull request as the change that makes it true.

## Mechanics

A new page needs three touches in the same pull request or the site 404s:

1. The file under `docs/`.
2. The `mkdocs.yml` nav entry.
3. The repository README "Going deeper" line (the repository README is the
   site home).

`mise run docs-build` is strict: it fails on warnings, and a file under
`docs/` that is missing from the nav aborts it. The rendered
`build/site/` output is the pre-merge check, because the site deploys only
on push to main.
