#!/usr/bin/env python3
"""Cross-check ACK CR kinds against the documented principal scope (issue #411).

docs/aws-iam.md is the only in-repo record of the static ACK principal's
permissions (the grant itself lives outside this repo). For every ACK CR
kind declared under mgmt/aws/infrastructure/, assert the actions its
controller needs to reconcile that kind appear in that document, so a docs
edit that drops an action (the gap named in #352) goes red in validate
before the next bootstrap hits AccessDenied.

YAML discovery uses the repository's locked PyYAML dependency.
"""

import fnmatch
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "mgmt/aws/infrastructure"
DOC = REPO_ROOT / "docs/aws-iam.md"

ACK_API = re.compile(r"[a-z0-9.-]+\.services\.k8s\.aws/\S+")

# Reconciliation surface for the committed specs, not every feature supported
# by these kinds. Pinned controller sources: aws-controllers-k8s/{s3,rds,iam}
# -controller, pkg/resource/{bucket,db_instance,role,user}/{sdk,hooks}.go
# (S3 calls its hooks hook.go), versions in ack-controllers/helm.yaml.
# IAM reads inline/attached policies and tags even when no managed policies
# are declared; inline-policy/tag removal supports drift and deletion.
# RDS managed passwords/encrypted storage also require AWS API prerequisites
# documented in docs/aws-iam.md (including first-create service-linked role).
# IAM action names can differ from SDK names: GetBucketEncryption requires
# GetEncryptionConfiguration. Directory-bucket-only S3 tag APIs are excluded.
REQUIRED_ACTIONS = {
    "Bucket": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:ListBucket",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:TagResource",
        "s3:GetBucketTagging", "s3:PutBucketTagging",
        "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
        "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
        "s3:GetBucketVersioning", "s3:PutBucketVersioning",
        "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls",
        "s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
        # Observe reads these even when their desired configuration is absent.
        "s3:GetBucketAbac", "s3:GetAccelerateConfiguration", "s3:GetBucketAcl",
        "s3:GetBucketCORS", "s3:GetAnalyticsConfiguration",
        "s3:GetIntelligentTieringConfiguration", "s3:GetInventoryConfiguration",
        "s3:GetLifecycleConfiguration", "s3:GetBucketLogging",
        "s3:GetMetricsConfiguration", "s3:GetBucketNotification",
        "s3:GetReplicationConfiguration", "s3:GetBucketRequestPayment",
        "s3:GetBucketWebsite", "s3:GetBucketObjectLockConfiguration",
    ],
    "DBInstance": [
        "rds:CreateDBInstance",
        "rds:ModifyDBInstance",
        "rds:DeleteDBInstance",
        "rds:AddTagsToResource",
        "rds:RemoveTagsFromResource",
        "rds:DescribeDBInstances", "rds:ListTagsForResource",
        "secretsmanager:CreateSecret", "secretsmanager:TagResource",
        "kms:DescribeKey", "kms:CreateGrant", "iam:CreateServiceLinkedRole",
    ],
    "Role": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:TagRole",
        "iam:GetRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies", "iam:ListRoleTags", "iam:UntagRole",
        "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
    ],
    "User": [
        "iam:CreateUser",
        "iam:GetUser",
        "iam:PutUserPolicy",
        "iam:TagUser",
        "iam:GetUserPolicy", "iam:ListUserPolicies",
        "iam:ListAttachedUserPolicies", "iam:ListUserTags", "iam:UntagUser",
        "iam:DeleteUserPolicy", "iam:DeleteUser", "iam:UpdateUser",
    ],
}


def ack_cr_kinds(infra=INFRA, repo_root=REPO_ROOT):
    """Map each ACK CR kind to its files; fail closed on invalid YAML."""
    kinds = {}
    for path in sorted(infra.rglob("*.yaml")):
        label = str(path.relative_to(repo_root))
        try:
            documents = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError as exc:
            raise ValueError(f"{label}: malformed YAML: {exc}") from exc
        for number, document in enumerate(documents, 1):
            if not isinstance(document, dict):
                continue
            api = document.get("apiVersion")
            if not isinstance(api, str) or not ACK_API.fullmatch(api):
                continue
            kind = document.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError(f"{label} document {number}: missing or invalid ACK kind")
            kinds.setdefault(kind, set()).add(label)
    return kinds


def principal_actions(doc_text):
    """Read only the three affirmative static-principal permission bullets."""
    heading = "### Least-privilege trade-off: the static principal's union scope"
    section = doc_text.split(heading, 1)[-1] if heading in doc_text else ""
    section = re.split(r"^#{2,3} ", section, maxsplit=1, flags=re.MULTILINE)[0]
    actions = set()
    for bullet in re.finditer(r"^- \*\*(S3|RDS|IAM)\*\*: (.*?)(?=^\S|\Z)",
                              section, re.MULTILINE | re.DOTALL):
        # Negative explanatory prose cannot count as a grant.
        text = " ".join(
            sentence for sentence in re.split(r"(?<=\.)\s+", bullet.group(2))
            if not re.search(r"\b(?:not needed|not required|not granted)\b", sentence)
        )
        text = text.replace("`", "")
        text = re.sub(r"/\s+", "/", text)
        for match in re.finditer(
            r"(?<![A-Za-z0-9:*])([a-z0-9]+):([A-Za-z0-9*]+)"
            r"((?:/[A-Za-z0-9*]+(?![A-Za-z0-9*:]))*)(?![A-Za-z0-9*:])", text
        ):
            service, first, rest = match.groups()
            actions.update(service + ":" + token for token in (first + rest).split("/"))
    return actions


def action_present(actions, action):
    return any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in actions)


def scope_failures(kinds, doc_text):
    actions = principal_actions(doc_text)
    failures = []
    if not kinds:
        failures.append("no ACK CRs found under mgmt/aws/infrastructure/ (discovery broken?)")
    for kind, paths in sorted(kinds.items()):
        required = REQUIRED_ACTIONS.get(kind)
        if required is None:
            failures.append(f"unknown ACK CR kind {kind} in {sorted(paths)[0]}: add its "
                            "required actions to REQUIRED_ACTIONS and to docs/aws-iam.md")
            continue
        for action in required:
            if not action_present(actions, action):
                failures.append(f"{kind}: {action} not documented in docs/aws-iam.md")
    return failures


def main():
    try:
        kinds = ack_cr_kinds()
        failures = scope_failures(kinds, DOC.read_text())
    except ValueError as exc:
        failures = [str(exc)]
    if failures:
        print("ACK CR scope cross-check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"OK: {len(kinds)} ACK CR kinds cross-checked against docs/aws-iam.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
