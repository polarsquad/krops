#!/usr/bin/env python3
"""Docs must not show bare host `mise` helper commands (#423): every helper
task that interacts with an environment runs through the krops-toolbox
container, and the README carve-out that called moving them a follow-up is
gone. Host-side by design and therefore allowed: validate, docs-*, and
podinfo-port-forward (the browser is on the host)."""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Helper tasks this issue moved into the toolbox. A bare `mise run <task>` or
# `mise -E <env> run <task>` line inside a fenced block is a regression.
MOVED_TASKS = (
    "sops-keygen", "sops-encrypt", "sops-decrypt", "sops-updatekeys",
    "aws-bootstrap", "aws-credentials", "azure-bootstrap", "arc-federate",
    "gcp-bootstrap", "wif-federate", "mgmt-kubeconfig", "kubeconfigs", "oci-push",
)
# Tools whose bare `mise x -- <tool>` form is likewise host-only.
MOVED_TOOLS = ("sops", "age-keygen", "clusterawsadm", "az", "gcloud")

HOST_MISE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*mise\s+(?:"
    r"(?:-E\s+\S+\s+)?(?:run\s+(?:%s)\b|x\s+--\s+(?:%s)\b)"
    r"|-E\s+\S+\s+install\b)"
    % ("|".join(map(re.escape, MOVED_TASKS)), "|".join(map(re.escape, MOVED_TOOLS)))
)
CARVE_OUT = "moving them into the toolbox as well is a follow-up"


def tracked_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "README.md", "docs/*.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [REPO_ROOT / p for p in out]


def offending_lines(path: Path) -> list[tuple[int, str]]:
    hits = []
    in_fence = False
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and HOST_MISE.match(line):
            hits.append((n, line.strip()))
    return hits


def main() -> int:
    failed = 0
    for path in tracked_docs():
        for n, line in offending_lines(path):
            print(f"{path.relative_to(REPO_ROOT)}:{n}: host mise helper in docs: {line}", file=sys.stderr)
            failed += 1
    readme = (REPO_ROOT / "README.md").read_text()
    if CARVE_OUT in readme:
        print("README.md still carries the helper-task follow-up carve-out", file=sys.stderr)
        failed += 1
    if failed:
        print(f"{failed} docs regression(s); see docs/operations.md 'Helper tasks in the toolbox'", file=sys.stderr)
        return 1
    print("docs toolbox helpers OK: no bare host mise helper commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
