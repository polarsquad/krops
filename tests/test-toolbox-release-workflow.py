#!/usr/bin/env python3
"""Regression test for the toolbox-release workflow dispatch retry (#545).

Checks the structural wiring of the release jobs so the tag-triggered
happy path and the workflow_dispatch retry path stay intact: the version
comes from the tag on push runs and from the required `version` input on
dispatch runs, and no job outside `resolve` may read GITHUB_REF_NAME or
github.ref_name for the version.
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
        text = "\n".join(_step_texts(s) for s in job.get("steps", []))
        for needle in ("GITHUB_REF_NAME", "github.ref_name"):
            if needle in text:
                failures.append(
                    f"job {name} still reads {needle} for the version; only "
                    "resolve may derive the version"
                )
    return failures


def main() -> int:
    workflow = yaml.safe_load(TOOLBOX_RELEASE_YML.read_text())
    jobs = workflow.get("jobs", {})

    failures = []
    failures.extend(check_triggers(workflow))
    failures.extend(check_resolve_job(jobs))
    failures.extend(check_gate_job(jobs))
    failures.extend(check_build_job(jobs))
    failures.extend(check_merge_job(jobs))
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
