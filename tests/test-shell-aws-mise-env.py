#!/usr/bin/env python3
"""Shell lifecycle must scope MISE_ENV=aws to every aws child (#504).

Inside the toolbox image `aws` resolves through a mise shim and the
aws-cli pin lives in mise.aws.toml, which mise loads only when the `aws`
environment is active (MISE_ENV=aws). Nothing on the lifecycle path set
it, so a bare `aws` failed with "No version is set for shim: aws": the
EIP quota preflight pointed at credentials, and every teardown sweep
listing read as "nothing to delete".

The gate requires, in both bootstrap-common.sh and teardown.sh:

  1. an `aws()` wrapper that scopes MISE_ENV=aws to the child
     (`MISE_ENV=aws command aws "$@"`), defined exactly once. On hosts
     where `aws` is a plain binary rather than a shim, the variable is
     inert, so native runs are unchanged;
  2. an execution preflight that runs `aws --version` (not a PATH stat)
     and whose failure message names the shim, so a broken shim is never
     read as an empty result or a credentials problem.

The Rust side of the same contract (MISE_ENV-scoped spawn helpers plus
the `aws --version` probe in the EIP quota preflight and the teardown
availability check) is covered by the in-file unit tests in
bootstrap-rs (cargo test -p krops-bootstrap).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("bootstrap-common.sh", "teardown.sh")
WRAPPER_DEF = re.compile(r"^aws\(\)\s*\{\s*$")
WRAPPER_BODY = 'MISE_ENV=aws command aws "$@"'
# An actual `aws --version` invocation at command position, not the string
# inside an error message.
VERSION_CALL = re.compile(r"^\s*(if\s+!?\s*)?aws --version\s+>")
SHIM_ERROR = "No version is set for shim"


def find_aws_wrappers(text: str) -> list[tuple[int, list[str]]]:
    """All aws() function definitions: (1-indexed def line, body lines)."""
    lines = text.splitlines()
    found = []
    i = 0
    while i < len(lines):
        if WRAPPER_DEF.match(lines[i]):
            body = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != "}":
                body.append(lines[j])
                j += 1
            found.append((i + 1, body))
            i = j + 1
        else:
            i += 1
    return found


def main() -> int:
    failed = 0
    for name in SCRIPTS:
        path = REPO_ROOT / name
        text = path.read_text()

        wrappers = find_aws_wrappers(text)
        if len(wrappers) != 1:
            print(f"{name}: expected exactly one aws() MISE_ENV wrapper, found {len(wrappers)}", file=sys.stderr)
            failed += 1
            continue
        line_no, body = wrappers[0]
        body_flat = [b.strip() for b in body if b.strip() and not b.strip().startswith("#")]
        if body_flat != [WRAPPER_BODY]:
            print(f"{name}:{line_no}: aws() wrapper body must be exactly `{WRAPPER_BODY}`, got: {body_flat}", file=sys.stderr)
            failed += 1

        if not any(VERSION_CALL.match(line) for line in text.splitlines()):
            print(f"{name}: preflight must execute `aws --version` (an execution probe, not a PATH stat)", file=sys.stderr)
            failed += 1
        if SHIM_ERROR not in text:
            print(f"{name}: the preflight failure message must name the shim ({SHIM_ERROR!r})", file=sys.stderr)
            failed += 1

    if failed:
        print(f"{failed} #504 shell contract regression(s); see docs/bootstrap-cli.md 'Toolbox runtime contracts'", file=sys.stderr)
        return 1
    print("shell aws MISE_ENV wrapper OK: both lifecycle scripts scope MISE_ENV=aws to the aws child")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
