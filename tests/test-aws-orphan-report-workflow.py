#!/usr/bin/env python3
"""Regression test for the aws-orphan-report workflow (#380).

Checks the structural wiring of the orphan discovery and reporting jobs.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_ORPHAN_REPORT_YML = REPO_ROOT / ".github/workflows/aws-orphan-report.yml"


def check_discover_job(jobs: dict) -> list[str]:
    job = jobs.get("discover")
    if job is None:
        return ["discover job is missing"]

    failures = []

    # Check permissions
    perms = job.get("permissions", {})
    if not isinstance(perms, dict):
        failures.append(f"discover permissions must be a dict, got {type(perms).__name__}")
    elif perms.get("contents") != "read" or perms.get("id-token") != "write":
        failures.append(
            f"discover permissions must have contents: read and id-token: write, "
            f"got {perms!r}"
        )

    # Check if condition
    if_cond = job.get("if", "")
    if "github.repository == 'polarsquad/krops'" not in if_cond:
        failures.append(
            f"discover must check github.repository == 'polarsquad/krops', "
            f"got if: {if_cond!r}"
        )

    # Check for schedule trigger in workflow root
    on_triggers = job.get("on", {})
    # The schedule is at the workflow level, not job level

    # Check for configure-aws-credentials step
    steps = job.get("steps", [])
    has_aws_creds = False
    has_orphans_step = False
    has_mutating_verb = False

    for step in steps:
        uses = step.get("uses", "")
        run = step.get("run", "")

        if "configure-aws-credentials" in uses:
            has_aws_creds = True
            # Check for the right ARN
            with_block = step.get("with", {})
            role_arn = with_block.get("role-to-assume", "")
            if "krops-ci-e2e" not in role_arn:
                failures.append(
                    f"configure-aws-credentials must use krops-ci-e2e role, "
                    f"got {role_arn!r}"
                )

        if "krops-bootstrap orphans" in run:
            has_orphans_step = True

        # Check for mutating AWS verbs
        for verb in ["delete-", "release-", "terminate-", "detach-", "revoke-", "create-", "run-instances", "modify-", "put-", "disassociate-"]:
            if verb in run:
                has_mutating_verb = True
                failures.append(
                    f"discover step contains mutating AWS verb '{verb}': {run!r}"
                )

    if not has_aws_creds:
        failures.append("discover missing configure-aws-credentials step")

    if not has_orphans_step:
        failures.append("discover missing 'krops-bootstrap orphans' step")

    if has_mutating_verb:
        # Already added to failures above
        pass

    return failures


def check_report_status_job(jobs: dict) -> list[str]:
    job = jobs.get("report-status")
    if job is None:
        return ["report-status job is missing"]

    failures = []

    # Check needs
    needs = job.get("needs", [])
    needs_set = {needs} if isinstance(needs, str) else set(needs)
    if "discover" not in needs_set:
        failures.append(f"report-status must need discover, got {needs_set!r}")

    # Check if condition
    if_cond = job.get("if", "")
    if "always()" not in if_cond or "schedule" not in if_cond:
        failures.append(
            f"report-status must check always() and schedule, got if: {if_cond!r}"
        )

    # Check permissions
    perms = job.get("permissions", {})
    if perms != {"issues": "write"}:
        failures.append(
            f"report-status permissions must be exactly issues: write, "
            f"got {perms!r}"
        )

    # Check for github-script
    steps = job.get("steps", [])
    has_github_script = any(
        "actions/github-script@" in step.get("uses", "") for step in steps
    )
    if not has_github_script:
        failures.append("report-status must use actions/github-script")

    return failures


def check_workflow_root(workflow: dict) -> list[str]:
    failures = []

    # Check schedule trigger
    on_block = workflow.get("on", {})
    schedule = on_block.get("schedule", [])
    if not schedule:
        failures.append("workflow must have a schedule trigger")

    # Check concurrency
    concurrency = workflow.get("concurrency", {})
    group = concurrency.get("group", "")
    if "aws-e2e-account" not in group:
        failures.append(
            f"concurrency group must contain 'aws-e2e-account', got {group!r}"
        )

    return failures


def main() -> int:
    workflow = yaml.safe_load(AWS_ORPHAN_REPORT_YML.read_text())
    jobs = workflow.get("jobs", {})

    failures = []
    failures.extend(check_workflow_root(workflow))
    failures.extend(check_discover_job(jobs))
    failures.extend(check_report_status_job(jobs))

    if failures:
        print("AWS orphan-report workflow check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("AWS orphan-report workflow check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
