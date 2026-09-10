# Contributing to krops

krops is a working reference implementation, not a product: it shows how to
run cloud infrastructure through the Kubernetes API with CAPI, Flux, and
provider operators, and nothing else. Contributions that sharpen that
demonstration, make it reproducible on more platforms, or make it safer to
operate are welcome. Contributions that turn it into a framework are not.

This guide covers where the project stands, where help is wanted, and how a
change gets from an idea to `main`. `AGENTS.md` holds the same rules in the
form AI coding agents consume; the two files must agree, so update both when
you change a workflow.

## Where the project stands

Work is organized into numbered milestones that build on each other.

| Milestone | State | What it delivered or still owes |
|---|---|---|
| 1-renovate-foundations | closed | Renovate as the hosted GitHub App, the central version catalog retired, a shared integration-test harness (`tests/renovate_harness.py`) |
| 2-rust-bootstrap | closed | `krops-bootstrap`, the Rust CLI covering bootstrap, pivot, and teardown; `bootstrap.toml` as the repository-owned configuration; the toolbox container image |
| 3-environments | open | `local-talos` wiring and docs are on `main`; the hardware acceptance run (#105) is pending. Azure via ASO (#71) and GCP via Config Connector (#72) are unstarted |
| 4-hardening | open | Air-gap supply chain (#80): digest pins everywhere, signed SBOMs, offline verification, transactional updates. The build, signing, and publication model needs a design decision first (#138) |

Two facts follow from that table and shape what a contributor can rely on:

- The shell scripts (`bootstrap.sh`, `pivot.sh`, `teardown.sh`) are still the
  native reference path. They are retired only after the CLI passes a live
  parity run on every environment. `local-host` has passed; the `aws` run
  (#143) has not happened yet because it provisions a billed EKS cluster.
- No semver toolbox release exists yet. Until a `v*` tag is pushed, build the
  image from your checkout as the README shows.

Outside the milestones, the open backlog groups into four themes. These are
the best places to start if you are new to the repository.

| Theme | Issues | Why it matters |
|---|---|---|
| Documentation structure | #151, #149, #150, #152 | README, AGENTS.md, and `docs/` overlap and drift; external links are unchecked; `bootstrap.toml` should be the single configuration reference; "environment" is used for two different things |
| Developer experience | #139, #134, #135, #153, #159 | A stale host toolchain broke a PR once; validation tools should come from mise in CI too; merged branches are not auto-deleted; the commit policy for PRs is under evaluation |
| Python quality | #160, #161, #162, #164 | The test scripts under `tests/` and `airgap/tests/` have no linter or test runner; Renovate parser resilience and the Dependency Dashboard are unverified |
| Air-gap coverage gaps | #165, #170, #173, #142, #136, #137 | The digest gate misses new source types; Renovate cannot pin digests in `airgap/scripts/*.sh`; Cluster topology versions are not covered |

Labels mark the entry points:

- [`good first issue`](https://github.com/polarsquad/krops/labels/good%20first%20issue):
  scoped, self-contained, no cloud account or hardware needed. Mostly
  documentation and script fixes.
- [`help wanted`](https://github.com/polarsquad/krops/labels/help%20wanted):
  larger items the maintainers are not actively working on, including the
  Azure and GCP providers and the Python test tooling.
- `bug`, `documentation`, `enhancement` classify the change type.

Issues without those labels are either in progress, blocked on a design
decision, or need an operator with AWS or bare-metal access.

## Ways to contribute

- Run the `local-host` lifecycle on your machine and report what breaks. This
  is the most useful contribution with the lowest cost. It needs only Docker
  or Podman.
- Pick an issue from the themes above. Comment on it before starting so work
  is not duplicated; milestone descriptions state ordering constraints where
  they exist.
- Add a provider or environment. `docs/extending.md` documents the pattern
  (CAPA reference wiring, then Azure, Talos, and k0smotron sections).
  `mgmt/local-talos/` is the worked example of a complete new environment
  landing as a series of small PRs (#155, #169, #171).
- Improve the documentation. Every `docs/` page is fair game; the README
  should get shorter, not longer (#151).
- Review open pull requests. Rendered Flux diffs make review approachable
  without cluster access.

Ideas that need discussion before code: a new cloud provider, a change to the
bootstrap sequence, anything that touches `renovate.json5`, and anything
that changes the shape of `bootstrap.toml`. Open an issue first.

## Setting up

The toolbox container is the primary lifecycle interface; native tools are
for development and validation.

```sh
git clone https://github.com/polarsquad/krops.git
cd krops
mise trust
mise install                 # kubectl, kind, flux, sops, age, kustomize, ...
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
export TOOLBOX_IMAGE=krops-toolbox:dev
mise run validate            # must pass on a clean checkout before you change anything
```

Requirements:

- Mise 2026.8.10 or newer. Older versions fail on the pinned tool
  definitions.
- Docker, or Podman 5.5+. kind creates clusters through the mounted engine
  socket.
- Rust via rustup only when building `bootstrap-rs/` outside the image; the
  pin is `bootstrap-rs/rust-toolchain.toml`.
- Node 24 or newer only for the Renovate tests and dry-run
  (`mise x node@24 -- ...`).
- Python 3 for the test scripts under `tests/` and `airgap/tests/`.

Environment layers: `mise -E local-host`, `mise -E aws`, and
`mise -E local-talos` add per-environment tools and tasks. Contributors
without an AWS account or bare metal should work in `local-host`; it walks
the full bootstrap, pivot, workload, and teardown chain with no cloud cost.

## Rules that apply to every change

These come from `AGENTS.md` and are not negotiable.

1. Edit YAML in Git; never mutate a cluster by hand. Use `kubectl` to
   inspect, then make the persistent change in the repository and let Flux
   converge.
2. Flux tracks `main`. Nothing reconciles until merged. Do not describe a fix
   as live before then.
3. Secrets go through SOPS and age. Encrypted files are named `*.sops.yaml`
   and only `data`/`stringData` fields are encrypted. `age.agekey` and `.env`
   are gitignored and must stay that way. Never commit plaintext credentials.
4. Run `mise run validate` before pushing.

Additional conventions:

- Each component pairs a plain kustomize root (`kustomization.yaml`) with a
  Flux `Kustomization` (`flux-ks.yaml`). Register new components in the
  parent `kustomization.yaml` and order them with `dependsOn` and
  `wait: true`.
- Per-cluster values come from `postBuild.substituteFrom: cluster-vars`, not
  from hardcoded strings.
- Dependency versions live in the files that consume them and are updated by
  Renovate. Do not reintroduce a central version list. If you add a new
  pinned dependency, add Renovate coverage for it in the same PR and prove it
  with the dry-run described in `AGENTS.md` ("Editing renovate.json5").
- External images in air-gap sources must be pinned as
  `repository:tag@sha256:<digest>`. The digest gate runs in `mise run
  validate` and in CI.
- When a change affects repository structure, workflows, or how to navigate
  the repository, update `AGENTS.md` and the relevant `docs/` page in the
  same PR.

## Making a change

### Branch and commits

- Branch from `main`. The usual branch shape is `<type>/<issue>-<slug>`,
  for example `feat/104-toolbox-image` or `fix/100-teardown-local-host-preflight`.
  Drop the issue number when there is none. `renovate/*` is reserved for
  Renovate.
- Commit messages follow Conventional Commits: `feat`, `fix`, `docs`,
  `chore`, `refactor`, `test`, `ci`, with an optional scope such as
  `feat(local-talos):` or `docs(agents):`. Reference the issue in the body
  (`Refs #74`, `Closes #172`).
- Write the body for a reader who was not in the room: what was wrong, what
  changed, how it was verified. Merged PRs #171 and #176 are good examples.
- The single-commit-per-PR question is open (#159). Until it is decided,
  iterative commits during review are accepted and maintainers choose the
  merge method. Do not force-push a branch that someone else has commented
  on unless a maintainer asks for it.

### Scope

- One logical change per PR. Large features land as a numbered series
  (`(#105, 1/3)`, `2/3`, `3/3`) where each PR is reviewable and mergeable
  on its own.
- File each distinct problem you find as its own issue, cross-referenced to
  the issue or PR where you found it. Do not fold unrelated fixes into a
  feature PR.

### Validate locally

Run the checks that match what you touched. CI runs all of them.

| You changed | Run |
|---|---|
| Anything | `mise run validate` (shell syntax, air-gap digest gate, `bootstrap.toml` cross-check, every kustomize overlay) |
| `README.md` or `docs/` | `mise run docs-build` (assembles `build/docs/` and runs the strict MkDocs build that CI gates on) |
| `tools/assemble_docs.py` | `mise run docs-test`, then `mise run docs-build` |
| `mgmt/` or `workload/` YAML | `yamllint` with the CI settings (line length and document start disabled, `*.sops.yaml` ignored) |
| `bootstrap-rs/` | `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, `cargo build --locked`, `cargo test --locked` |
| `renovate.json5` or a pinned version | The pinned dry-run from `AGENTS.md`, then `mise x node@24 -- python3 tests/test-renovate-coverage.py` and `python3 airgap/tests/test-renovate-digest-pinning.py` |
| `bootstrap.toml` or anything it references | `python3 tests/test-bootstrap-config.py` |
| `airgap/` | `python3 airgap/tests/test-airgap-image-digests.py --all` for a full audit |
| Lifecycle scripts or the CLI | A full `local-host` bootstrap and teardown through the toolbox |

### Open the pull request

- Fill in the PR description with the problem, the change, and the
  verification you ran, including command output where it proves a claim.
- Link the issue. Use `Closes #N` only when the PR completes the whole issue.
- Expect three automated checks:
  - `validate`: air-gap digest pins, kustomize builds, Renovate coverage,
    `bootstrap.toml` cross-check, YAML lint.
  - `bootstrap-rs`: fmt, clippy, build, test for the Rust CLI.
  - `konflate`: the PR rendered as a Flux diff (blast radius, image changes,
    render failures), posted as a PR comment and required for merge. This
    job is skipped for pull requests from forks because it would run
    untrusted sources through konflate. A maintainer may recreate your
    branch inside the repository to obtain the render before merging.
- Review is done against the rendered diff and the evidence in the
  description, not against intent. If a reviewer asks for evidence, add it
  to the PR rather than replying with a description.
- `main` accepts no direct pushes, deletions, or force-pushes. Maintainers
  merge once checks are green and the review is resolved.

## Testing against real infrastructure

- `local-host` is free and covers the full lifecycle. Use it by default.
- `aws` provisions billed resources: EKS control planes, node groups, RDS
  instances, VPCs, and load balancers. Teardown deletes real AWS resources
  in two regions and removes the `clusterawsadm` CloudFormation stack. Run it
  only in an account you control, with the quotas in `docs/operations.md`
  established, and confirm the sweep completed.
- `local-talos` needs a reachable Tinkerbell stack and a machine you are
  willing to PXE-boot. Teardown releases the Hardware resource and never
  wipes the disk.
- The nightly `air-gapped` workflow runs on `main` only. Air-gap changes are
  verified locally with `airgap/scripts/offline-run.sh`; see
  `docs/airgap.md` for the checklist.

## Reporting problems

- Bugs and proposals: open a GitHub issue. State the environment
  (`aws`, `local-host`, `local-talos`), the commit on `main`, the command,
  and the observed output.
- Security-sensitive findings (a leaked credential, a bypass of the SOPS or
  signing chain): do not open a public issue. Contact a maintainer listed on
  the repository directly.

## License

krops is licensed under the Apache License 2.0 (`LICENSE`). By submitting a
contribution you agree that it is licensed under the same terms. The project
does not currently require a Developer Certificate of Origin sign-off.
