#!/usr/bin/env python3
"""Validate that bootstrap-common.sh REGION_PLAN matches bootstrap.toml."""

import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        print("ERROR: Python 3.11+ or 'tomli' package required", file=sys.stderr)
        sys.exit(1)


def parse_bootstrap_toml():
    """Extract AWS environment regions and clusters from bootstrap.toml."""
    toml_file = Path(__file__).parent.parent / "bootstrap.toml"

    with open(toml_file, "rb") as f:
        data = tomllib.load(f)

    # Extract mgmt-cluster from [environments.aws]
    aws_env = data.get("environments", {}).get("aws", {})
    mgmt_cluster = aws_env.get("mgmt-cluster")

    if not mgmt_cluster or not mgmt_cluster.endswith("-management"):
        print(f"ERROR: Invalid or missing mgmt-cluster in bootstrap.toml: {mgmt_cluster}")
        sys.exit(1)

    mgmt_region = mgmt_cluster[:-len("-management")]
    regions = {mgmt_region: {mgmt_cluster}}

    # Extract [[environments.aws.teardown.aws-workloads]] entries
    teardown = aws_env.get("teardown", {})
    workloads = teardown.get("aws-workloads", [])
    for workload in workloads:
        region = workload.get("region")
        cluster_name = workload.get("cluster-name")
        if region and cluster_name:
            if region not in regions:
                regions[region] = set()
            regions[region].add(cluster_name)

    return regions


def parse_region_plan():
    """Extract REGION_PLAN from bootstrap-common.sh."""
    sh_file = Path(__file__).parent.parent / "bootstrap-common.sh"
    content = sh_file.read_text()

    match = re.search(r'local REGION_PLAN="([^"]+)"', content)
    if not match:
        print("ERROR: Could not find REGION_PLAN in bootstrap-common.sh")
        sys.exit(1)

    region_plan_str = match.group(1)
    plan = {}

    for entry in region_plan_str.split():
        parts = entry.split(":")
        if len(parts) != 3:
            print(f"ERROR: Invalid REGION_PLAN entry: {entry}")
            sys.exit(1)

        region, required_str, clusters_str = parts
        try:
            required = int(required_str)
        except ValueError:
            print(f"ERROR: Invalid required count in REGION_PLAN entry: {entry}")
            sys.exit(1)

        clusters = set(clusters_str.split(","))
        plan[region] = {"required": required, "clusters": clusters}

    return plan


def main():
    """Validate REGION_PLAN against bootstrap.toml."""
    toml_regions = parse_bootstrap_toml()
    plan = parse_region_plan()

    errors = []

    # Check every region from bootstrap.toml exists in REGION_PLAN
    for region in sorted(toml_regions.keys()):
        if region not in plan:
            errors.append(f"Region '{region}' from bootstrap.toml not in REGION_PLAN")
        else:
            # Validate clusters match
            expected_clusters = toml_regions[region]
            plan_clusters = plan[region]["clusters"]

            if expected_clusters != plan_clusters:
                missing = expected_clusters - plan_clusters
                extra = plan_clusters - expected_clusters
                if missing:
                    errors.append(f"Region '{region}': missing clusters in REGION_PLAN: {missing}")
                if extra:
                    errors.append(f"Region '{region}': extra clusters in REGION_PLAN: {extra}")

            # Validate required count = 3 * cluster count
            expected_required = 3 * len(expected_clusters)
            actual_required = plan[region]["required"]
            if expected_required != actual_required:
                errors.append(
                    f"Region '{region}': expected required={expected_required} "
                    f"(3 * {len(expected_clusters)} clusters), got {actual_required}"
                )

    # Check no extra regions in REGION_PLAN
    for region in plan.keys():
        if region not in toml_regions:
            errors.append(f"Region '{region}' in REGION_PLAN not found in bootstrap.toml")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        sys.exit(1)

    print("✓ REGION_PLAN matches bootstrap.toml")


if __name__ == "__main__":
    main()
