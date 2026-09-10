#!/usr/bin/env python3
"""Cross-check bootstrap.toml against the Git manifests (issue #98).

The chart versions krops-bootstrap installs imperatively must equal the
versions Flux reconciles from Git, or Flux cannot adopt the imperative
installs without drift. Fails when they disagree, when a declared
provider-manifest or sync path is missing, when an environment's kind
does not match its section name (the section name is authoritative for
profile resolution), or when the file does not parse.
Requires Python 3.11+ (tomllib); mise and CI both provide it.
"""

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# chart key in [charts] -> manifest files that must carry the same version
CHART_MANIFESTS = {
    "flux-operator": [
        "mgmt/aws/addons/flux-apps/flux-operator.yaml",
        "mgmt/local-host/addons/flux-apps/flux-operator.yaml",
        "mgmt/azure/addons/flux-apps/flux-operator.yaml",
    ],
    "cert-manager": [
        "mgmt/aws/infrastructure/cert-manager/helmrelease.yaml",
        "mgmt/local-host/infrastructure/cert-manager/helmrelease.yaml",
        "mgmt/local-talos/infrastructure/cert-manager/helmrelease.yaml",
        "mgmt/azure/infrastructure/cert-manager/helmrelease.yaml",
        "workload/azure-base/cert-manager/helmrelease.yaml",
    ],
    "capi-operator": [
        "mgmt/aws/infrastructure/capi-operator/helmrelease.yaml",
        "mgmt/local-host/infrastructure/capi-operator/helmrelease.yaml",
        "mgmt/local-talos/infrastructure/capi-operator/helmrelease.yaml",
        "mgmt/azure/infrastructure/capi-operator/helmrelease.yaml",
    ],
}

VERSION_RE = re.compile(r'^\s*version:\s*"([^"]+)"\s*$', re.MULTILINE)


def manifest_version(path: Path) -> str:
    matches = VERSION_RE.findall(path.read_text())
    if len(matches) != 1:
        raise SystemExit(
            f"FAILED: expected exactly one quoted version: line in {path}, found {len(matches)}"
        )
    return matches[0]


def main() -> int:
    failures = []
    config_path = REPO_ROOT / "bootstrap.toml"
    try:
        config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"FAILED: cannot parse {config_path}: {error}")
        return 1

    charts = config.get("charts", {})
    for required_env in ("local-host", "aws", "local-talos", "azure"):
        if required_env not in config.get("environments", {}):
            failures.append(f"environments.{required_env} section missing from bootstrap.toml")
    for chart, manifests in CHART_MANIFESTS.items():
        declared = charts.get(chart)
        if declared is None:
            failures.append(f"charts.{chart} missing from bootstrap.toml")
            continue
        for relative in manifests:
            actual = manifest_version(REPO_ROOT / relative)
            if actual != declared:
                failures.append(
                    f"charts.{chart} = {declared} but {relative} pins {actual}"
                )

    for name, env in config.get("environments", {}).items():
        kind = env.get("kind")
        if kind != name:
            failures.append(
                f"environments.{name} kind {kind!r} must match its section name"
            )
        sync = REPO_ROOT / env.get("sync-path", "")
        if not sync.is_dir():
            failures.append(f"environments.{name}.sync-path '{env.get('sync-path')}' is not a directory")
        # The sync source drives the FluxInstance seeding (issue #105):
        # 'github' (GitRepository + PAT secret, aws and local-talos) or
        # 'oci' (local registry artifact, local-host). The Rust enum must
        # agree with every declared value (unknown value = startup error).
        if "sync" not in env:
            failures.append(f"environments.{name} is missing the 'sync' key")
        elif env["sync"] not in ("github", "oci"):
            failures.append(
                f"environments.{name}.sync {env['sync']!r} must be 'github' or 'oci'"
            )
        for manifest in env.get("provider-manifests", []):
            if not (REPO_ROOT / manifest).is_file():
                failures.append(f"environments.{name} provider manifest missing: {manifest}")
        for fallback in env.get("move-fallbacks", []):
            if not (REPO_ROOT / fallback.get("manifest", "")).is_file():
                failures.append(
                    f"environments.{name} move-fallback manifest missing: "
                    f"{fallback.get('manifest')}"
                )
        # SOPS manifests the pivot decrypts and applies in the target before
        # the move (issue #71): must exist and must follow the *.sops.yaml
        # naming so .sops.yaml's creation rule covers them.
        for manifest in env.get("pivot-sops-secrets", []):
            if not (REPO_ROOT / manifest).is_file():
                failures.append(f"environments.{name} pivot-sops-secret missing: {manifest}")
            if not manifest.endswith(".sops.yaml"):
                failures.append(f"environments.{name} pivot-sops-secret is not a *.sops.yaml: {manifest}")
        # Plain manifests the pivot applies in the target before the move
        # (issue #236): the workload-identity replacement for
        # pivot-sops-secrets, so the naming must be plain, not *.sops.yaml.
        for manifest in env.get("pivot-manifests", []):
            if not (REPO_ROOT / manifest).is_file():
                failures.append(f"environments.{name} pivot-manifest missing: {manifest}")
            elif manifest.endswith(".sops.yaml"):
                failures.append(f"environments.{name} pivot-manifest must be plain, not *.sops.yaml: {manifest}")
        hook = env.get("post-kind-create-task")
        if hook:
            mise_toml = REPO_ROOT / f"mise.{name}.toml"
            if not mise_toml.is_file():
                failures.append(f"environments.{name} post-kind-create-task but no {mise_toml.name}")
            elif f"[tasks.{hook}]" not in mise_toml.read_text():
                failures.append(f"environments.{name} post-kind-create-task '{hook}' has no [tasks.{hook}] in {mise_toml.name}")

        # ── teardown constants (issue #100) ─────────────────────────────
        td = env.get("teardown", {})
        for workload in td.get("aws-workloads", []):
            region = workload.get("region", "")
            prefix_dir = REPO_ROOT / "mgmt/aws/clusters" / region
            if not prefix_dir.is_dir():
                failures.append(f"environments.{name} teardown region {region!r} has no cluster directory")
            # The K8s cluster name must equal the Kustomization namePrefix
            # + 'workload' (the staged cluster.yaml names Cluster 'workload').
            kustomization = prefix_dir / "staging/kustomization.yaml"
            if kustomization.is_file():
                text = kustomization.read_text()
                expected_prefix = f"namePrefix: {region}-"
                if expected_prefix not in text:
                    failures.append(
                        f"environments.{name} teardown: {kustomization.relative_to(REPO_ROOT)} "
                        f"lacks '{expected_prefix}'"
                    )
            cluster_name = workload.get("cluster-name", "")
            if cluster_name and not cluster_name.startswith(region):
                failures.append(
                    f"environments.{name} teardown cluster-name {cluster_name!r} "
                    f"does not start with its region {region!r}"
                )
            # The RDS instance id is krops-<cluster>-db (workload/base/
            # rds-instances/dbinstance.yaml substitutes CLUSTER_NAME).
            rds = workload.get("rds-instance", "")
            if cluster_name and rds != f"krops-{cluster_name}-db":
                failures.append(
                    f"environments.{name} teardown rds-instance {rds!r} != "
                    f"krops-{cluster_name}-db"
                )
            # The EKS name is '<namespace>_<kcp-name>' — only the namespace
            # separator becomes an underscore (teardown.sh documents this
            # against the live account; the KCP name keeps its dashes).
            eks = workload.get("eks-cluster-name", "")
            expected_eks = f"default_{cluster_name}-control-plane"
            if cluster_name and eks != expected_eks:
                failures.append(
                    f"environments.{name} teardown eks-cluster-name {eks!r} != "
                    f"{expected_eks}"
                )

    # Global teardown constants pin to the manifests that define them.
    teardown = config.get("teardown", {})
    if teardown:
        expected_roles = [
            "krops-ack-s3-controller",
            "krops-ack-rds-controller",
            "krops-ack-iam-controller",
        ]
        roles = teardown.get("global-iam-roles", [])
        for role in expected_roles:
            if role not in roles:
                failures.append(f"teardown.global-iam-roles missing {role}")
        reader_user = REPO_ROOT / "mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml"
        users = teardown.get("global-iam-users", [])
        if reader_user.is_file() and "krops-reader" not in users:
            failures.append("teardown.global-iam-users missing krops-reader")
        pattern = teardown.get("s3-bucket-pattern", "")
        if pattern and "{account_id}" not in pattern or "{cluster_name}" not in pattern:
            failures.append(
                f"teardown.s3-bucket-pattern {pattern!r} must contain "
                "{account_id} and {cluster_name}"
            )

    if failures:
        print("bootstrap.toml cross-check FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"bootstrap.toml cross-check OK ({len(CHART_MANIFESTS)} charts, "
          f"{len(config.get('environments', {}))} environments)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
