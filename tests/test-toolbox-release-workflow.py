#!/usr/bin/env python3
"""Regression test for the toolbox-release workflow.

Covers two structural contracts of .github/workflows/toolbox-release.yml:

#545 - the version comes from the tag on push runs and from the required
`version` input on dispatch runs (the `resolve` job), and no job outside
`resolve` may read GITHUB_REF_NAME or github.ref_name for the version.

#250 - a `release` job runs only after sign-attest, creates the GitHub
release with the SBOM and a machine-readable artifact manifest attached,
scopes contents:write to itself, marks prerelease versions as such (and
stable ones as latest), and refuses to silently re-point an existing
full-version release when the published index digest no longer matches.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_RELEASE_YML = REPO_ROOT / ".github/workflows/toolbox-release.yml"

SEMVER_RE = r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$"
RESOLVED_VERSION = "${{ needs.resolve.outputs.version }}"


def _step_texts(step: dict) -> str:
    parts = [step.get("run", "") or ""]
    env = step.get("env", {}) or {}
    parts.extend(str(v) for v in env.values())
    with_block = step.get("with", {}) or {}
    parts.extend(str(v) for v in with_block.values())
    return "\n".join(parts)


def _needs_set(job: dict) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def check_triggers(workflow: dict) -> list[str]:
    failures = []
    on = workflow.get("on") or {}
    tags = (on.get("push") or {}).get("tags") or []
    if "v*" not in tags:
        failures.append(f"tag trigger must remain push.tags [\"v*\"], got {tags!r}")
    inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    version_input = inputs.get("version") or {}
    if not version_input.get("required"):
        failures.append(
            "workflow_dispatch must define a required `version` input, "
            f"got {inputs!r}"
        )
    if not version_input.get("description"):
        failures.append(
            "workflow_dispatch `version` input must carry a description"
        )
    return failures


def check_resolve_job(jobs: dict) -> list[str]:
    job = jobs.get("resolve")
    if job is None:
        return ["resolve job is missing"]

    failures = []
    outputs = job.get("outputs", {}) or {}
    if outputs.get("version") != "${{ steps.version.outputs.value }}":
        failures.append(
            "resolve outputs.version must be steps.version.outputs.value, "
            f"got {outputs.get('version')!r}"
        )

    version_steps = [s for s in job.get("steps", []) if s.get("id") == "version"]
    if not version_steps:
        return failures + ["resolve must have a step with id: version"]

    text = _step_texts(version_steps[0])
    if "github.ref_name" not in text:
        failures.append("resolve step must consume github.ref_name for tag runs")
    if "${{ inputs.version }}" not in text:
        failures.append(
            "resolve step must consume the workflow_dispatch `version` input"
        )
    if SEMVER_RE not in text:
        failures.append(
            "resolve step must reject versions that do not match the semver "
            f"regex {SEMVER_RE}"
        )
    return failures


def check_gate_job(jobs: dict) -> list[str]:
    job = jobs.get("gate")
    if job is None:
        return ["gate job is missing"]

    failures = []
    needs = _needs_set(job)
    if "resolve" not in needs:
        failures.append(f"gate must need resolve, got {needs!r}")

    text = "\n".join(_step_texts(s) for s in job.get("steps", []))
    if RESOLVED_VERSION not in text:
        failures.append(
            "gate must compare needs.resolve.outputs.version against "
            "Cargo.toml"
        )
    if "bootstrap-rs/Cargo.toml" not in text:
        failures.append("gate must read bootstrap-rs/Cargo.toml")
    return failures


def check_build_job(jobs: dict) -> list[str]:
    job = jobs.get("build")
    if job is None:
        return ["build job is missing"]

    failures = []
    needs = _needs_set(job)
    if not {"resolve", "gate"}.issubset(needs):
        failures.append(f"build must need resolve and gate, got {needs!r}")

    version_steps = [s for s in job.get("steps", []) if s.get("id") == "version"]
    if not version_steps:
        failures.append("build must keep a step with id: version")
    elif RESOLVED_VERSION not in _step_texts(version_steps[0]):
        failures.append(
            "build version step must echo needs.resolve.outputs.version"
        )
    return failures


def check_merge_job(jobs: dict) -> list[str]:
    job = jobs.get("merge")
    if job is None:
        return ["merge job is missing"]

    failures = []
    needs = _needs_set(job)
    if not {"resolve", "build"}.issubset(needs):
        failures.append(f"merge must need resolve and build, got {needs!r}")

    publish = [s for s in job.get("steps", []) if s.get("id") == "publish"]
    if not publish:
        return failures + ["merge must keep the publish step (id: publish)"]

    env = publish[0].get("env", {}) or {}
    if env.get("VERSION") != RESOLVED_VERSION:
        failures.append(
            "merge publish step VERSION must be "
            f"needs.resolve.outputs.version, got {env.get('VERSION')!r}"
        )

    run = publish[0].get("run", "") or ""
    if 'tags=("$IMAGE:$version" "$IMAGE:$major_minor")' not in run:
        failures.append("merge must keep the X.Y.Z + X.Y tag set")
    if 'tags+=("$IMAGE:latest")' not in run:
        failures.append("merge must keep the latest tag for stable versions")
    if "latest moves only for stable" not in run:
        failures.append("merge must keep the latest prerelease-suffix guard")
    return failures


def check_no_version_reads_outside_resolve(jobs: dict) -> list[str]:
    failures = []
    for name, job in jobs.items():
        if name == "resolve":
            continue
        if name == "release":
            # release reads the fully-qualified GITHUB_REF only to record
            # the cosign signing identity in the manifest; its version
            # still comes from resolve (asserted in check_release_job).
            continue
        text = "\n".join(_step_texts(s) for s in job.get("steps", []))
        for needle in ("GITHUB_REF_NAME", "github.ref_name"):
            if needle in text:
                failures.append(
                    f"job {name} still reads {needle} for the version; only "
                    "resolve may derive the version"
                )
    return failures


def check_workflow_permissions(workflow: dict) -> list[str]:
    perms = workflow.get("permissions") or {}
    if perms.get("contents") != "read":
        return [
            "workflow-level permissions.contents must stay read "
            f"(contents:write is scoped to the release job), got {perms!r}"
        ]
    return []


def check_release_job(jobs: dict) -> list[str]:
    job = jobs.get("release")
    if job is None:
        return ["release job is missing"]

    failures = []
    needs = _needs_set(job)
    if not {"resolve", "merge", "sign-attest"}.issubset(needs):
        failures.append(
            "release must need resolve, merge, and sign-attest: its env "
            "block reads needs.merge.outputs, which only resolves when "
            f"merge is a direct dependency, got {needs!r}"
        )

    perms = job.get("permissions") or {}
    if perms.get("contents") != "write":
        failures.append(
            f"release must scope contents: write to the job, got {perms!r}"
        )
    if perms.get("packages") != "read":
        failures.append(
            f"release must keep packages: read to inspect the published "
            f"index, got {perms!r}"
        )

    env = job.get("env") or {}
    if env.get("VERSION") != RESOLVED_VERSION:
        failures.append(
            "release VERSION must come from needs.resolve.outputs.version "
            f"(tag and dispatch runs share one source of truth), got "
            f"{env.get('VERSION')!r}"
        )

    steps = job.get("steps", [])
    text = "\n".join(_step_texts(s) for s in steps)

    # The version must never be derived from the tag name in this job.
    if "GITHUB_REF_NAME" in text:
        failures.append(
            "release must not derive the version from GITHUB_REF_NAME"
        )

    # Durable SBOM: downloaded from the sign-attest workflow artifact.
    downloads = [
        s
        for s in steps
        if isinstance(s.get("uses"), str)
        and s["uses"].startswith("actions/download-artifact@")
    ]
    if not downloads:
        failures.append("release must download the toolbox-sbom-spdx artifact")
    elif "toolbox-sbom-spdx" not in _step_texts(downloads[0]):
        failures.append(
            f"release must download the toolbox-sbom-spdx artifact, got "
            f"{_step_texts(downloads[0])!r}"
        )

    # Retry safety: the published index digest must be compared against the
    # digest sign-attest signed, and an existing release recording a
    # different digest must abort the job.
    if "INDEX_DIGEST" not in text:
        failures.append(
            "release must compare the published index digest against the "
            "digest sign-attest signed"
        )
    if "refusing to silently replace" not in text:
        failures.append(
            "release must abort (not silently replace) when an existing "
            "release records a different index digest"
        )

    # Release creation semantics: stable X.Y.Z -> --latest, suffixed
    # version -> --prerelease, durable assets uploaded to the release.
    if "gh release create" not in text:
        failures.append("release must create the GitHub release")
    if "--prerelease" not in text:
        failures.append(
            "release must mark suffixed (prerelease) versions with "
            "--prerelease"
        )
    if "--latest" not in text:
        failures.append(
            "release must mark stable X.Y.Z versions with --latest"
        )
    if "gh release upload" not in text:
        failures.append("release must upload the durable release assets")
    uploads = [s for s in steps if "gh release upload" in _step_texts(s)]
    if uploads:
        upload_text = "\n".join(_step_texts(s) for s in uploads)
        for asset in ("sbom.json", "release-manifest.json"):
            if not any(
                "gh release upload" in line and asset in line
                for line in upload_text.splitlines()
            ):
                failures.append(
                    f"release must attach {asset} to the release "
                    "(asset named on a gh release upload line)"
                )

    # Artifact manifest content: the extensible inventory must carry the
    # source revision, the immutable identities, and both platforms.
    for needle in (
        "schema_version",
        "source_run",
        "index_digest",
        "linux/amd64",
        "linux/arm64",
        "sha256",
        "release-manifest.json",
    ):
        if needle not in text:
            failures.append(f"release manifest must carry {needle!r}")
    return failures


def main() -> int:
    workflow = yaml.safe_load(TOOLBOX_RELEASE_YML.read_text())
    jobs = workflow.get("jobs", {})

    failures = []
    failures.extend(check_workflow_permissions(workflow))
    failures.extend(check_triggers(workflow))
    failures.extend(check_resolve_job(jobs))
    failures.extend(check_gate_job(jobs))
    failures.extend(check_build_job(jobs))
    failures.extend(check_merge_job(jobs))
    failures.extend(check_release_job(jobs))
    failures.extend(check_no_version_reads_outside_resolve(jobs))

    if failures:
        print("toolbox-release workflow check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("toolbox-release workflow check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
