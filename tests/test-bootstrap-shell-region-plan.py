#!/usr/bin/env python3
"""Validate the shell region plan and offline EIP quota preflight behavior."""

import json
import os
import subprocess
import tempfile
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


AWS_STUB = r'''
import json
import sys
from pathlib import Path
args = sys.argv[1:]
root = Path(__file__).parent
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
region = args[args.index('--region') + 1]
fixture = json.loads((root / 'fixture.json').read_text())
if args[:2] == ['service-quotas', 'get-service-quota']:
    print(fixture['limit'] if region == 'eu-north-1' else 100)
elif args[:2] == ['ec2', 'describe-addresses']:
    filters = args[args.index('--filters') + 1:args.index('--query')]
    query = args[args.index('--query') + 1]
    # Mirror the live EC2 API: no pool filter exists, so the request
    # fails instead of silently narrowing (issue #636).
    if any(f.startswith('Name=public-ipv4-pool') for f in filters):
        print("aws: [ERROR]: An error occurred (InvalidParameterValue) when calling the DescribeAddresses operation: The filter 'public-ipv4-pool' is invalid", file=sys.stderr)
        raise SystemExit(255)
    addresses = [a for a in fixture['addresses'] if a['region'] == region]
    for item in filters:
        name, value = item.removeprefix('Name=').split(',Values=')
        if name.startswith('tag:'):
            addresses = [a for a in addresses if a['owner'] == name.split('/cluster/')[1] and value == 'owned']
        else:
            key = {'domain': 'domain'}[name]
            addresses = [a for a in addresses if a[key] == value]
    # The preflight narrows client-side to the Amazon pool via the
    # documented PublicIpv4Pool response field.
    if "PublicIpv4Pool=='amazon'" in query:
        addresses = [a for a in addresses if a['pool'] == 'amazon']
    print(len(addresses))
else:
    raise SystemExit('unexpected AWS command')
'''


def check_quota_population():
    """Execute the real preflight against address fixtures, without AWS access."""
    script = Path(__file__).parent.parent / "bootstrap-common.sh"
    scenarios = [
        ("foreign BYOIP", 6, [("byoip-pool", "", 8)], True, "6 available to krops (need 6, 0 already owned)"),
        ("owned BYOIP", 6, [("amazon", "", 2), ("byoip-pool", "eu-north-1-workload", 4)], False,
         "4 available to krops, 6 needed (2 short; limit 6, 2 allocated, 0 owned by krops)"),
        ("Amazon ownership", 8, [("amazon", "", 2), ("amazon", "eu-north-1-workload", 2)], True,
         "6 available to krops (need 6, 2 already owned)"),
    ]
    for name, limit, groups, succeeds, diagnostic in scenarios:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addresses = [dict(region="eu-north-1", domain="vpc", pool=pool, owner=owner)
                         for pool, owner, count in groups for _ in range(count)]
            # Matching ownership outside the VPC domain must not produce credit.
            addresses.append(dict(region="eu-north-1", domain="standard", pool="amazon",
                                  owner="eu-north-1-workload"))
            (root / "fixture.json").write_text(json.dumps(dict(limit=limit, addresses=addresses)))
            executable = root / "aws"
            executable.write_text(f"#!{sys.executable}\n" + AWS_STUB)
            executable.chmod(0o755)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", "-c", 'source "$1"; preflight_aws_quotas', "bash", str(script)],
                                    env=env, capture_output=True, text=True, check=False)
            output = result.stdout + result.stderr
            assert (result.returncode == 0) == succeeds, (name, output)
            assert diagnostic in output, (name, output)
            if not succeeds:
                assert "--desired-value 8 --region eu-north-1" in output, output
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            for args in calls:
                if args[:2] == ["ec2", "describe-addresses"]:
                    assert "Name=domain,Values=vpc" in args, args
                    # The live API rejects the pool filter (issue #636); the
                    # BYOIP exclusion must come from the PublicIpv4Pool field.
                    assert "Name=public-ipv4-pool,Values=amazon" not in args, args
                    assert "length(Addresses[?PublicIpv4Pool=='amazon'])" in args, args
    print("✓ Shell EIP preflight excludes BYOIP and preserves Amazon ownership credit")


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
    check_quota_population()


if __name__ == "__main__":
    main()
