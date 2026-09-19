#!/usr/bin/env python3
"""Exercise the digest-less tag handling for host-daemon images (docs/airgap.md finding 9, 10).

Covers save_host_images (build-package.sh), the digest stripping in
build-config-artifact.sh, the pause image in every list that must carry it, and
the podinfo chart version derived from the workload tree.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PACKAGE = REPO_ROOT / "airgap/scripts/build-package.sh"
BUILD_ARTIFACT = REPO_ROOT / "airgap/scripts/build-config-artifact.sh"
IMAGES_TXT = REPO_ROOT / "airgap/images.txt"
PODINFO = REPO_ROOT / "workload/local-host/podinfo/helm.yaml"
DIGEST = "sha256:" + "a" * 64

FAKE_DOCKER = """#!/bin/sh
case "$1" in
  tag) echo "$3" >> "$0.tags"; exit 0 ;;
  save)
    out=$3
    shift 3
    dir=$(mktemp -d)
    if [ -n "$FAKE_DOCKER_DROP" ]; then shift; fi
    printf '[{"RepoTags":[' > "$dir/manifest.json"
    sep=
    for ref in "$@"; do grep -qx "$ref" "$0.tags" || { echo "no such image: $ref" >&2; exit 1; }; done
    for ref in "$@"; do printf '%s"%s"' "$sep" "$ref" >> "$dir/manifest.json"; sep=,; done
    printf ']}]' >> "$dir/manifest.json"
    tar -cf "$out" -C "$dir" manifest.json
    ;;
esac
"""


def function_body(path: Path, name: str) -> str:
    text = path.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        sys.exit(f"{path}: {name} not found")
    return match.group(0)


def check_save_host_images() -> None:
    body = function_body(BUILD_PACKAGE, "save_host_images")
    with tempfile.TemporaryDirectory() as tmp:
        docker = Path(tmp) / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        out = Path(tmp) / "out.tar"
        refs = [f"docker.io/library/registry:2@{DIGEST}", f"kindest/node:v1@{DIGEST}"]
        script = f"{body}\nsave_host_images {out} {' '.join(refs)}\n"

        def run(**extra):
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", **extra}
            return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)

        ok = run()
        if ok.returncode != 0:
            sys.exit(f"save_host_images failed on a complete archive: {ok.stderr!r}")
        listing = subprocess.run(["tar", "-xOf", str(out), "manifest.json"], capture_output=True, text=True, check=False).stdout
        if "@sha256" in listing or '"docker.io/library/registry:2"' not in listing:
            sys.exit(f"save_host_images must save digest-less tags, got {listing!r}")
        if run(FAKE_DOCKER_DROP="1").returncode == 0:
            sys.exit("save_host_images must fail when the archive lacks a tag")


def check_artifact_stripping() -> None:
    text = BUILD_ARTIFACT.read_text()
    seds = re.findall(r"^sed -E -i\.bak (.+) \"\$LH/(.+)\"$", text, re.M)
    by_target = {target: expr for expr, target in seds if "@sha256" in expr}
    class_expr = by_target.get("clusters/docker/cluster-class.yaml")
    flux_expr = by_target.get("addons/flux-apps/flux-instance.yaml")
    if not class_expr or not flux_expr:
        sys.exit("build-config-artifact.sh no longer strips digests from cluster-class.yaml and flux-instance.yaml")

    def strip(expr: str, content: str) -> str:
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml") as f:
            f.write(content)
            f.flush()
            subprocess.run(f"sed -E -i.bak {expr} {f.name}", shell=True, check=True)
            os.unlink(f.name + ".bak")
            return Path(f.name).read_text()

    node = f"  customImage: kindest/node:v1.37.0@{DIGEST}\n"
    preload = f"    - docker.io/kindest/kindnetd:v1@{DIGEST}\n"
    other = f"  note: keep @{DIGEST}\n"
    result = strip(class_expr, node + preload + other)
    if "customImage: kindest/node:v1.37.0\n" not in result or "kindnetd:v1\n" not in result:
        sys.exit(f"cluster-class digests were not stripped: {result!r}")
    if other not in result:
        sys.exit("cluster-class stripping touched an unrelated line")
    flux = f"            value: ghcr.io/fluxcd/source-controller:v1.9.4@{DIGEST}\n"
    if strip(flux_expr, flux) != "            value: ghcr.io/fluxcd/source-controller:v1.9.4\n":
        sys.exit("flux-instance digests were not stripped")
    for guard in ("digest references left in the workload flux-instance.yaml",
                  "digest references left in the artifact's cluster-class.yaml"):
        if guard not in text:
            sys.exit(f"missing post-strip guard: {guard}")


def check_pause_image() -> None:
    pins = re.findall(r"registry\.k8s\.io/pause:3\.10\.2@sha256:[a-f0-9]{64}", IMAGES_TXT.read_text())
    if not pins or len(set(pins)) != 1:
        sys.exit("images.txt must pin registry.k8s.io/pause:3.10.2 with one digest")
    for path in (BUILD_PACKAGE, BUILD_ARTIFACT):
        if pins[0] not in path.read_text():
            sys.exit(f"{path.name} must list {pins[0]} (workload archive and preLoadImages)")


def check_podinfo_version() -> None:
    text = BUILD_PACKAGE.read_text()
    match = re.search(r"^podinfo_chart_version=\$\(sed -nE '(.+)' (\S+) \|", text, re.M)
    if match is None:
        sys.exit("build-package.sh no longer derives the podinfo chart version")
    if match.group(2) != "workload/local-host/podinfo/helm.yaml":
        sys.exit("podinfo chart version must come from the workload tree")
    got = subprocess.run(["sed", "-nE", match.group(1), str(PODINFO)], capture_output=True, text=True, check=False).stdout.split()
    tag = re.search(r'^\s*tag: "?([0-9][^"\s]*)"?', PODINFO.read_text(), re.M).group(1)
    if got[:1] != [tag]:
        sys.exit(f"derived podinfo version {got!r} != workload tag {tag!r}")
    if "podinfo-6." in text or "podinfo-*.tgz" not in (REPO_ROOT / "airgap/scripts/stage-and-create-cluster.sh").read_text():
        sys.exit("the podinfo chart archive name must not hardcode a version")


def main() -> int:
    check_save_host_images()
    check_artifact_stripping()
    check_pause_image()
    check_podinfo_version()
    print("host image tag handling OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
