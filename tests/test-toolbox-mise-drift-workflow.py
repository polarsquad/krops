#!/usr/bin/env python3
"""Regression test for the toolbox-mise-drift workflow (#639).

Checks the structural wiring of the compare and report-status jobs: the
scheduled, dispatch, and push-to-main triggers; per-job permissions; the
polarsquad owner guard; the absence of cloud credentials or OIDC; and the
open-on-drift / close-on-match issue wiring.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIFT_YML = REPO_ROOT / ".github/workflows/toolbox-mise-drift.yml"

IMAGE = "ghcr.io/polarsquad/krops-toolbox:latest"
EXPECTED_TITLE = "toolbox: :latest mise version drift (release needed)"


def triggers(workflow: dict) -> dict:
    # PyYAML parses an unquoted `on:` as the boolean True; the workflow
    # quotes the key, but accept both spellings.
    block = workflow.get("on", workflow.get(True))
    return block if isinstance(block, dict) else {}


def check_workflow_root(workflow: dict) -> list[str]:
    failures = []

    on = triggers(workflow)
    if not on.get("schedule"):
        failures.append("workflow must have a schedule trigger")
    if "workflow_dispatch" not in on:
        failures.append("workflow must have a workflow_dispatch trigger")

    push = on.get("push", {})
    if push.get("branches") != ["main"]:
        failures.append(
            f"push trigger must watch main only, got {push.get('branches')!r}"
        )
    paths = push.get("paths", [])
    for watched in ("mise.toml", "bootstrap-rs/Dockerfile"):
        if watched not in paths:
            failures.append(f"push trigger must watch {watched}, got {paths!r}")

    return failures


def check_compare_job(jobs: dict) -> list[str]:
    job = jobs.get("compare")
    if job is None:
        return ["compare job is missing"]

    failures = []

    perms = job.get("permissions", {})
    if perms != {"contents": "read"}:
        failures.append(
            f"compare permissions must be exactly contents: read, got {perms!r}"
        )

    if_cond = job.get("if", "")
    if "github.repository_owner == 'polarsquad'" not in if_cond:
        failures.append(
            f"compare must check github.repository_owner == 'polarsquad', "
            f"got if: {if_cond!r}"
        )

    outputs = job.get("outputs", {})
    for name in ("drift", "pinned_mise", "image_mise"):
        if name not in outputs:
            failures.append(f"compare must emit output {name!r}, got {outputs!r}")

    steps = job.get("steps", [])
    run_blob = "\n".join(step.get("run", "") for step in steps)
    if "docker run" not in run_blob or "--entrypoint mise" not in run_blob:
        failures.append(
            "compare must read the image mise via "
            "'docker run --entrypoint mise ... version'"
        )
    if IMAGE not in run_blob:
        failures.append(f"compare must inspect the published image {IMAGE}")
    if "min_version" not in run_blob:
        failures.append("compare must read min_version from mise.toml")

    for step in steps:
        uses = step.get("uses", "")
        if "aws-actions" in uses or "configure-aws-credentials" in uses:
            failures.append(
                f"compare must not use cloud credentials, found {uses!r}"
            )
        if "id-token" in step.get("permissions", {}):
            failures.append("compare steps must not request id-token")

    return failures


def check_report_status_job(jobs: dict) -> list[str]:
    job = jobs.get("report-status")
    if job is None:
        return ["report-status job is missing"]

    failures = []

    needs = job.get("needs", [])
    needs_set = {needs} if isinstance(needs, str) else set(needs)
    if "compare" not in needs_set:
        failures.append(f"report-status must need compare, got {needs_set!r}")

    if_cond = job.get("if", "")
    if "always()" not in if_cond:
        failures.append(
            f"report-status must run on always(), got if: {if_cond!r}"
        )
    if "github.repository_owner == 'polarsquad'" not in if_cond:
        failures.append(
            f"report-status must check github.repository_owner == 'polarsquad', "
            f"got if: {if_cond!r}"
        )

    perms = job.get("permissions", {})
    if perms != {"issues": "write"}:
        failures.append(
            f"report-status permissions must be exactly issues: write, "
            f"got {perms!r}"
        )

    script = ""
    for step in job.get("steps", []):
        if "actions/github-script@" in step.get("uses", ""):
            script = (step.get("with", {}) or {}).get("script", "")

    if not script:
        failures.append("report-status must use actions/github-script")
    else:
        if f'const title = "{EXPECTED_TITLE}";' not in script:
            failures.append(
                f"report-status must track the issue titled {EXPECTED_TITLE!r}"
            )
        if "process.env.DRIFT" not in script:
            failures.append("report-status must branch on the drift output")
        if "issues.create" not in script:
            failures.append("report-status must open the issue on drift")
        if 'state: "closed"' not in script:
            failures.append("report-status must close the issue on match")

    return failures


def check_no_cloud_credentials() -> list[str]:
    failures = []
    raw = DRIFT_YML.read_text()
    for forbidden in (
        "aws-actions",
        "configure-aws-credentials",
        "id-token",
        "AWS_ACCESS_KEY",
    ):
        if forbidden in raw:
            failures.append(
                f"workflow must not reference cloud credentials or OIDC "
                f"({forbidden!r} found)"
            )
    return failures


def main() -> int:
    workflow = yaml.safe_load(DRIFT_YML.read_text())
    jobs = workflow.get("jobs", {})

    failures = []
    failures.extend(check_workflow_root(workflow))
    failures.extend(check_compare_job(jobs))
    failures.extend(check_report_status_job(jobs))
    failures.extend(check_no_cloud_credentials())

    if failures:
        print("toolbox-mise-drift workflow check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("toolbox-mise-drift workflow check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
