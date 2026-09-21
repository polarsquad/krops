#!/usr/bin/env python3
"""Cross-check ACK CR kinds against the documented principal scope (issue #411).

docs/aws-iam.md is the only in-repo record of the static ACK principal's
permissions (the grant itself lives outside this repo). For every ACK CR
kind declared under mgmt/aws/infrastructure/, assert the actions its
controller needs to reconcile that kind appear in that document, so a docs
edit that drops an action (the gap named in #352) goes red in validate
before the next bootstrap hits AccessDenied.

Pure stdlib: apiVersion/kind are column-0 keys in these manifests.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "mgmt/aws/infrastructure"
DOC = REPO_ROOT / "docs/aws-iam.md"

ACK_API = re.compile(r"^apiVersion:\s*[a-z0-9.-]+\.services\.k8s\.aws/\S+\s*$")
KIND = re.compile(r"^kind:\s*(\S+)\s*$")

# Actions each ACK controller needs to reconcile the CR kinds this repo
# declares. Grounding:
# - Bucket: the ACK S3 controller embeds the tagSet in CreateBucket (needs
#   s3:TagResource) and removes drifted tags via DeleteBucketTagging.
#   s3:UntagResource and s3:ListTagsForResource are directory-bucket-only
#   and deliberately NOT required (issue #411, docs change in #410).
# - DBInstance: tagging goes through rds:AddTagsToResource /
#   RemoveTagsFromResource.
# - Role/User: the ACK IAM controller's role/user management set.
REQUIRED_ACTIONS = {
    "Bucket": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:TagResource",
        "s3:DeleteBucketTagging",
    ],
    "DBInstance": [
        "rds:CreateDBInstance",
        "rds:ModifyDBInstance",
        "rds:DeleteDBInstance",
        "rds:AddTagsToResource",
        "rds:RemoveTagsFromResource",
    ],
    "Role": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:TagRole",
    ],
    "User": [
        "iam:CreateUser",
        "iam:GetUser",
        "iam:PutUserPolicy",
        "iam:TagUser",
    ],
}


def ack_cr_kinds():
    """Map each ACK CR kind under mgmt/aws/infrastructure/ to its files."""
    kinds = {}
    for path in sorted(INFRA.rglob("*.yaml")):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not ACK_API.match(line) or i + 1 >= len(lines):
                continue
            kind_match = KIND.match(lines[i + 1])
            if kind_match:
                kinds.setdefault(kind_match.group(1), set()).add(
                    str(path.relative_to(REPO_ROOT))
                )
    return kinds


def action_present(doc_text, action):
    # Boundary-aware: iam:GetUser must not be satisfied by iam:GetUserPolicy.
    pattern = r"(?<![A-Za-z])" + re.escape(action) + r"(?![A-Za-z])"
    return re.search(pattern, doc_text)


def main():
    # Strip backticks so `s3:TagResource`/`s3:UntagResource` reads plainly.
    doc_text = DOC.read_text().replace("`", "")
    # The docs write a policy group as one service-prefixed action with the
    # rest of the group slash-joined bare
    # (`rds:CreateDBInstance`/`ModifyDBInstance`/`DeleteDBInstance`), and
    # wrap long groups across lines; rejoin the wrap so each group is one
    # contiguous slash run, then expand each bare token with the group's
    # prefix so the boundary-aware search below sees fully qualified
    # action names.
    doc_text = re.sub(r"/\n\s*", "/", doc_text)
    doc_text = re.sub(
        r"([a-z]+):([A-Za-z0-9]+)((?:/[A-Za-z0-9]+(?![A-Za-z0-9:]))+)",
        lambda m: " ".join(
            m.group(1) + ":" + token
            for token in (m.group(2) + m.group(3)).replace("/", " ").split()
        ),
        doc_text,
    )
    kinds = ack_cr_kinds()
    failures = []
    if not kinds:
        failures.append(
            "no ACK CRs found under mgmt/aws/infrastructure/ (discovery broken?)"
        )
    for kind, paths in sorted(kinds.items()):
        actions = REQUIRED_ACTIONS.get(kind)
        if actions is None:
            failures.append(
                f"unknown ACK CR kind {kind} in {sorted(paths)[0]}: add its "
                "required actions to REQUIRED_ACTIONS and to docs/aws-iam.md"
            )
            continue
        for action in actions:
            if not action_present(doc_text, action):
                failures.append(f"{kind}: {action} not documented in docs/aws-iam.md")
    if failures:
        print("ACK CR scope cross-check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {len(kinds)} ACK CR kinds cross-checked against docs/aws-iam.md")


if __name__ == "__main__":
    main()
