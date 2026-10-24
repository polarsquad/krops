#!/usr/bin/env python3
"""Verify the air-gap dependency graph record (#606).

Runs airgap/scripts/render-dependency-graph.py against this repository
offline and asserts the record schema and required fields:
schema_version, nonempty tools/charts/images/release_assets, every
zarf.yaml component image present with a sha256 digest, every images.txt
line present with its M/H/W category, charts carrying versions, and the
package name/sha256 echoing the build inputs. The record itself is a
build artifact (airgap/dependency-graph.json) and stays gitignored.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDERER = REPO_ROOT / "airgap" / "scripts" / "render-dependency-graph.py"
SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"@(?P<digest>sha256:[a-f0-9]{64})"
)
SECTION_HEADER = re.compile(r"^# ── .*\[(?P<category>[MHW])\]")
LOCAL_CONFIG_ARTIFACT = "localhost:5001/krops-airgap:latest"
FAKE_SHA256 = "ab" * 32


def run_renderer(tmp: Path, package_sha256: str = FAKE_SHA256) -> subprocess.CompletedProcess:
    out = tmp / "dependency-graph.json"
    return subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--out",
            str(out),
            "--package-name",
            "zarf-package-krops-airgap-arm64-0.1.0.tar.zst",
            "--package-sha256",
            package_sha256,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def zarf_component_images(failures: list[str]) -> list[tuple[str, str, str]]:
    """Independently parse zarf.yaml per-component images: (name, tag, digest)."""
    config = yaml.safe_load((REPO_ROOT / "airgap" / "zarf.yaml").read_text())
    refs = []
    for component in config.get("components", []):
        for ref in component.get("images", []) or []:
            if ref == LOCAL_CONFIG_ARTIFACT:
                continue
            match = IMAGE_REF.fullmatch(ref)
            if match is None:
                failures.append(f"airgap/zarf.yaml: unparseable image ref: {ref}")
                continue
            refs.append((match.group("name"), match.group("tag"), match.group("digest")))
    return refs


def zarf_release_assets() -> set[tuple[str, str]]:
    config = yaml.safe_load((REPO_ROOT / "airgap" / "zarf.yaml").read_text())
    assets = set()
    for component in config.get("components", []):
        for entry in component.get("files", []) or []:
            if entry.get("shasum"):
                assets.add((entry["source"], entry["shasum"]))
    return assets


def inventory_lines() -> dict[str, set[str]]:
    """images.txt non-comment lines by M/H/W section: {category: {ref}}."""
    sections: dict[str, set[str]] = {}
    category = None
    for line in (REPO_ROOT / "airgap" / "images.txt").read_text().splitlines():
        code = line.strip()
        if not code:
            continue
        if code.startswith("#"):
            match = SECTION_HEADER.match(code)
            if match:
                category = match.group("category")
            continue
        if category is not None:
            sections.setdefault(category, set()).add(code)
    return sections


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        result = run_renderer(tmp_path)
        if result.returncode != 0:
            print("dependency graph record check FAILED:", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return 1

        record = json.loads((tmp_path / "dependency-graph.json").read_text())

    if record.get("schema_version") != 1:
        failures.append(f"schema_version must be 1, got {record.get('schema_version')!r}")

    build = record.get("build") or {}
    package = build.get("package") or {}
    if package.get("name") != "zarf-package-krops-airgap-arm64-0.1.0.tar.zst":
        failures.append(f"build.package.name does not echo the package file name: {package.get('name')!r}")
    if package.get("sha256") != FAKE_SHA256:
        failures.append(f"build.package.sha256 does not echo the package digest: {package.get('sha256')!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", build.get("git_sha") or ""):
        failures.append(f"build.git_sha must be a 40-hex git SHA, got {build.get('git_sha')!r}")
    if not build.get("git_ref"):
        failures.append("build.git_ref is missing")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", build.get("timestamp_utc") or ""):
        failures.append(f"build.timestamp_utc must be UTC with a Z suffix, got {build.get('timestamp_utc')!r}")

    tools = record.get("tools") or []
    if not tools:
        failures.append("tools is empty")
    for tool in tools:
        for field in ("name", "version", "pin_source"):
            if not tool.get(field):
                failures.append(f"tools entry missing {field}: {tool!r}")
    for expected in ("zarf", "clusterctl", "flux", "helm"):
        if expected not in {t.get("name") for t in tools}:
            failures.append(f"tools is missing the required tool {expected}")

    charts = record.get("charts") or []
    if not charts:
        failures.append("charts is empty")
    for chart in charts:
        if not chart.get("name") or not chart.get("version") or not chart.get("source_file"):
            failures.append(f"chart entry missing name/version/source_file: {chart!r}")
    for chart_name in ("cert-manager", "flux-operator", "podinfo"):
        if chart_name not in {c.get("name") for c in charts}:
            failures.append(f"charts is missing {chart_name}")

    images = record.get("images") or []
    if not images:
        failures.append("images is empty")
    for image in images:
        if not SHA256.fullmatch(image.get("digest") or ""):
            failures.append(f"image digest is not sha256:<64 hex>: {image!r}")
        if image.get("category") not in ("M", "H", "W"):
            failures.append(f"image category must be M/H/W: {image!r}")
        if not image.get("ref") or not image.get("tag"):
            failures.append(f"image entry missing ref/tag: {image!r}")
    image_refs = {i.get("ref") for i in images}
    for name, tag, digest in zarf_component_images(failures):
        if f"{name}:{tag}@{digest}" not in image_refs:
            failures.append(f"zarf.yaml component image {name}:{tag}@{digest} missing from the record")

    record_by_category: dict[str, set[str]] = {}
    for image in images:
        record_by_category.setdefault(image.get("category") or "?", set()).add(image.get("ref") or "")
    for category, refs in inventory_lines().items():
        have = record_by_category.get(category, set())
        for ref in sorted(refs - have):
            failures.append(f"images.txt line missing from the record [{category}]: {ref}")

    release_assets = record.get("release_assets") or []
    if not release_assets:
        failures.append("release_assets is empty")
    for asset in release_assets:
        for field in ("url", "sha256", "target"):
            if not asset.get(field):
                failures.append(f"release_assets entry missing {field}: {asset!r}")
    for url, sha in sorted(zarf_release_assets()):
        if (url, sha) not in {(a.get("url"), a.get("sha256")) for a in release_assets}:
            failures.append(f"zarf.yaml release asset missing from the record: {url}")

    gitignore = (REPO_ROOT / ".gitignore").read_text()
    if "airgap/dependency-graph.json" not in gitignore.splitlines():
        failures.append(".gitignore must keep airgap/dependency-graph.json out of the repo")

    # A malformed package digest must be rejected (required-field validation).
    with tempfile.TemporaryDirectory() as tmp:
        bad = run_renderer(Path(tmp), package_sha256="ab" * 31 + "c")
    if bad.returncode == 0:
        failures.append("renderer accepted a 63-character --package-sha256; it must fail closed")

    if failures:
        print("dependency graph record check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"dependency graph record OK "
        f"({len(images)} images, {len(charts)} charts, {len(tools)} tools, "
        f"{len(release_assets)} release assets)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
