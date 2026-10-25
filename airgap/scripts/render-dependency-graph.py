#!/usr/bin/env python3
"""Render the air-gap bundle's build-time dependency graph record (#606).

Reads only the DECLARED sources in the repository — airgap/images.txt,
airgap/zarf.yaml, bootstrap.toml, mise.toml, and the podinfo workload
HelmRelease — never pulled or live state, so it runs fully offline and
cannot drift from what the build consumed. airgap/scripts/build-package.sh
calls it after signing the package; the record is stored alongside the
package archive (airgap/dependency-graph.json, gitignored) and retained
as its own CI artifact.

Schema (schema_version 1):
  build:         {git_sha, git_ref, workflow_ref, timestamp_utc,
                  package: {name, sha256}}
  tools:         [{name, version, pin_source}]
  charts:        [{name, version, source_file}]
  images:        [{ref, tag, digest, category}]   # category: M/H/W per images.txt
  release_assets: [{url, sha256, target}]

git_ref comes from KROPS_DEPENDENCY_GRAPH_GIT_REF (set by the air-gapped
workflow to ${{ github.ref }}), else the current branch or tag. workflow_ref
comes from KROPS_DEPENDENCY_GRAPH_WORKFLOW_REF (${{ github.workflow_ref }})
and is null outside CI. See docs/airgap.md ("Dependency graph record").
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"@(?P<digest>sha256:[a-f0-9]{64})"
)
SECTION_HEADER = re.compile(r"^# ── .*\[(?P<category>[MHW])\]")
SHA256 = re.compile(r"^[a-f0-9]{64}$")

# A zarf.yaml image ref without a digest cannot be a remote pin (the digest
# gate rejects those in zarf.yaml); by construction it is a locally built
# artifact (the localhost config artifact), so it is excluded from the
# inventory cross-check. Same documented exception as test-airgap-ownership.py.
LOCAL_HOSTS = ("localhost", "127.0.0.1")

# Tools the record must carry, mapped from their mise.toml pins. flat_key is
# a plain [tools] entry; table_suffix matches a table-valued [tools] pin by
# its table name's tail (the Zarf pin is a GitHub release asset, whose table
# name contains a colon and so is looked up by suffix, not as a literal path).
MISE_TOOLS = {
    "clusterctl": ("clusterctl", None),
    "flux": ("flux2", None),
    "helm": ("helm", None),
    "zarf": (None, "zarf"),
}


def fail(failures: list[str]) -> None:
    print("dependency graph render FAILED:", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    sys.exit(1)


def parse_images_txt(path: Path, failures: list[str]) -> list[dict]:
    """Return image entries with their M/H/W category from the section headers."""
    entries: dict[tuple[str, str], dict] = {}
    category = None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        code = line.strip()
        if not code:
            continue
        if code.startswith("#"):
            match = SECTION_HEADER.match(code)
            if match:
                category = match.group("category")
            continue
        if category is None:
            failures.append(f"{path}:{number}: image line before any [M]/[H]/[W] section: {code}")
            continue
        match = IMAGE_REF.fullmatch(code)
        if match is None:
            failures.append(f"{path}:{number}: unparseable image line (a digest-pinned reference is required): {code}")
            continue
        key = (category, code)
        if key not in entries:
            entries[key] = {
                "ref": code,
                "tag": match.group("tag"),
                "digest": match.group("digest"),
                "category": category,
            }
    return sorted(entries.values(), key=lambda e: (e["category"], e["ref"]))


def parse_zarf(path: Path, failures: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    """Return (charts, release_assets, image refs) declared in zarf.yaml."""
    config = yaml.safe_load(path.read_text())
    charts: list[dict] = []
    assets: list[dict] = []
    image_refs: list[str] = []
    for component in config.get("components", []):
        for chart in component.get("charts", []) or []:
            if not chart.get("name") or not chart.get("version"):
                failures.append(f"{path}: component {component.get('name')}: chart needs name and version")
                continue
            charts.append(
                {
                    "name": chart["name"],
                    "version": str(chart["version"]),
                    "source_file": "airgap/zarf.yaml",
                }
            )
        for file_entry in component.get("files", []) or []:
            if file_entry.get("shasum"):
                assets.append(
                    {
                        "url": file_entry["source"],
                        "sha256": file_entry["shasum"],
                        "target": file_entry["target"],
                    }
                )
        for ref in component.get("images", []) or []:
            image_refs.append(ref)
    assets.sort(key=lambda a: (a["url"], a["target"]))
    return charts, assets, image_refs


def parse_bootstrap_toml(path: Path, failures: list[str]) -> tuple[list[dict], str | None]:
    """Return ([charts], kind-node-image) from bootstrap.toml."""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    charts = [
        {"name": name, "version": str(version), "source_file": "bootstrap.toml"}
        for name, version in sorted((data.get("charts") or {}).items())
    ]
    kind_node_image = (data.get("bootstrap") or {}).get("kind-node-image")
    return charts, kind_node_image


def parse_mise_toml(path: Path, failures: list[str]) -> list[dict]:
    """Return the tool version pins declared in mise.toml [tools]."""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    tools_table = data.get("tools") or {}
    tools: list[dict] = []
    for name in sorted(MISE_TOOLS):
        flat_key, table_suffix = MISE_TOOLS[name]
        if flat_key is not None:
            value = tools_table.get(flat_key)
            version = value if isinstance(value, str) else None
        else:
            version = None
            for key, value in tools_table.items():
                if key.endswith(table_suffix) and isinstance(value, dict):
                    version = value.get("version")
                    break
        if not version:
            failures.append(f"{path}: missing mise pin for tool {name}")
            continue
        tools.append({"name": name, "version": str(version), "pin_source": "mise.toml"})
    return tools


def parse_podinfo_chart(path: Path, failures: list[str]) -> list[dict]:
    """Return the podinfo chart version pinned in the workload OCIRepository."""
    for doc in yaml.safe_load_all(path.read_text()):
        if not isinstance(doc, dict) or doc.get("kind") != "OCIRepository":
            continue
        tag = (((doc.get("spec") or {}).get("ref")) or {}).get("tag")
        if not tag:
            failures.append(f"{path}: OCIRepository has no ref.tag")
            continue
        return [
            {
                "name": "podinfo",
                "version": str(tag),
                "source_file": "workload/local-host/podinfo/helm.yaml",
            }
        ]
    failures.append(f"{path}: no OCIRepository document found")
    return []


def git_head_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_ref(root: Path) -> str:
    ref = os.environ.get("KROPS_DEPENDENCY_GRAPH_GIT_REF")
    if ref:
        return ref
    result = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = subprocess.run(
        ["git", "-C", str(root), "describe", "--tags", "--exact-match"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "detached"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    parser.add_argument("--out", default=None, help="output path (default: <root>/airgap/dependency-graph.json)")
    parser.add_argument("--package-name", required=True, help="package archive file name")
    parser.add_argument("--package-sha256", required=True, help="package archive SHA-256 hex digest")
    args = parser.parse_args()

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "airgap" / "dependency-graph.json"
    failures: list[str] = []

    if not SHA256.fullmatch(args.package_sha256):
        failures.append(f"--package-sha256 must be a 64-hex-character digest, got {args.package_sha256!r}")
    if not args.package_name:
        failures.append("--package-name must be nonempty")

    images = parse_images_txt(root / "airgap" / "images.txt", failures)
    zarf_charts, release_assets, zarf_image_refs = parse_zarf(root / "airgap" / "zarf.yaml", failures)
    bootstrap_charts, kind_node_image = parse_bootstrap_toml(root / "bootstrap.toml", failures)
    tools = parse_mise_toml(root / "mise.toml", failures)
    podinfo_charts = parse_podinfo_chart(root / "workload" / "local-host" / "podinfo" / "helm.yaml", failures)

    if failures:
        fail(failures)

    # Every zarf component image must be carried by the inventory with the
    # identical tag and digest; digest-less refs are locally built artifacts
    # (localhost) and are excluded, while a digest-less remote ref fails.
    by_name_tag = {(e["ref"].split("@")[0], e["digest"]) for e in images}
    for ref in zarf_image_refs:
        match = IMAGE_REF.fullmatch(ref)
        if match is not None:
            if (f"{match.group('name')}:{match.group('tag')}", match.group("digest")) not in by_name_tag:
                failures.append(f"airgap/zarf.yaml: {ref} missing from airgap/images.txt (tag or digest differs)")
            continue
        host = ref.split("/", 1)[0]
        if host.split(":", 1)[0] not in LOCAL_HOSTS:
            failures.append(f"airgap/zarf.yaml: digest required for remote image: {ref}")

    # The bootstrap kind node image must be in the inventory's host section.
    if kind_node_image:
        if kind_node_image not in {e["ref"] for e in images if e["category"] == "H"}:
            failures.append(
                f"bootstrap.toml: kind-node-image {kind_node_image} missing from the [H] section of airgap/images.txt"
            )

    if failures:
        fail(failures)

    charts = sorted(
        zarf_charts + bootstrap_charts + podinfo_charts,
        key=lambda c: (c["name"], c["source_file"]),
    )

    record = {
        "schema_version": 1,
        "build": {
            "git_sha": git_head_sha(root),
            "git_ref": git_ref(root),
            "workflow_ref": os.environ.get("KROPS_DEPENDENCY_GRAPH_WORKFLOW_REF") or None,
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "package": {"name": args.package_name, "sha256": args.package_sha256},
        },
        "tools": tools,
        "charts": charts,
        "images": images,
        "release_assets": release_assets,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(
        f"dependency graph record OK: {out} "
        f"({len(images)} images, {len(charts)} charts, {len(tools)} tools, "
        f"{len(release_assets)} release assets)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
