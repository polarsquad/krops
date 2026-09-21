#!/usr/bin/env python3
"""Require airgap/images.txt's k8s component pins to match what kubeadm deploys.

airgap/images.txt's kube-apiserver/controller-manager/proxy/scheduler,
coredns, etcd, and pause pins are meant to track what kubeadm actually
deploys for the Kubernetes version this environment runs (the kindest/node
tag), not just "latest upstream tag" (issue #142, items 3-4). This gate runs
the real kubeadm binary for that exact version (`kubeadm config images list
--kubernetes-version vX.Y.Z`) and fails if any pinned tag disagrees, printing
kubeadm's expected tag next to the offending pin.

The kubeadm binary is verified against its published SHA-256 before it runs,
and every pinned digest is compared with the registry's digest for that tag.
`--fix` rewrites the tracked pins in images.txt from kubeadm and the registry
instead of checking them (the Renovate rule for coredns, etcd and pause is
disabled, so this is how they move).

kubeadm has no macOS build, so on a non-Linux host this runs it inside a
Linux container instead of requiring a local Linux toolchain; the project
already assumes a container engine is available (docs/operations.md
prerequisites).
"""

import argparse
import hashlib
import platform
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/docker/cluster.yaml"
MANAGEMENT_NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/management/cluster.yaml"
IMAGES_TXT = REPO_ROOT / "airgap/images.txt"

# depName, as both Renovate (images.txt) and kubeadm's own image list spell it.
TRACKED_COMPONENTS = {
    "registry.k8s.io/kube-apiserver",
    "registry.k8s.io/kube-controller-manager",
    "registry.k8s.io/kube-proxy",
    "registry.k8s.io/kube-scheduler",
    "registry.k8s.io/coredns/coredns",
    "registry.k8s.io/etcd",
    "registry.k8s.io/pause",
}

IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?"
)


def _topology_version(path: Path) -> str:
    text = path.read_text()
    match = re.search(
        r"^(?P<indent> *)topology:[ \t]*\n"
        r"(?:(?P=indent)[ \t]+.*\n|[ \t]*\n)*?"
        r"(?P=indent)[ \t]+version: v(?P<version>\d+\.\d+\.\d+)[ \t]*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        sys.exit(f"{path}: could not find a 'version: vX.Y.Z' pin under 'topology:'")
    return match.group("version")


def target_kubernetes_version() -> str:
    """The Kubernetes version this environment runs, from the kindest/node pin.

    The workload and management clusters must agree, since images.txt serves both.
    """
    workload = _topology_version(NODE_IMAGE_SOURCE)
    management = _topology_version(MANAGEMENT_NODE_IMAGE_SOURCE)
    if workload != management:
        sys.exit(
            f"topology version mismatch: {NODE_IMAGE_SOURCE} pins v{workload} "
            f"but {MANAGEMENT_NODE_IMAGE_SOURCE} pins v{management}"
        )
    return workload


def _kubeadm_script(version: str) -> str:
    base = f"https://dl.k8s.io/release/v{version}/bin/linux"
    return (
        "set -eu\n"
        'case "$(uname -m)" in\n'
        "  x86_64) arch=amd64 ;;\n"
        "  aarch64|arm64) arch=arm64 ;;\n"
        '  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;\n'
        "esac\n"
        f'curl -fsSL -o /usr/local/bin/kubeadm "{base}/$arch/kubeadm"\n'
        f'sum="$(curl -fsSL "{base}/$arch/kubeadm.sha256")"\n'
        'echo "$sum  /usr/local/bin/kubeadm" | sha256sum -c - >&2\n'
        "chmod +x /usr/local/bin/kubeadm\n"
        f"kubeadm config images list --kubernetes-version v{version}\n"
    )


def kubeadm_expected_images(version: str) -> dict:
    """{kubeadm image name: tag} via the real, checksum-verified kubeadm binary."""
    script = _kubeadm_script(version)
    if platform.system() == "Linux":
        command = ["bash", "-c", script]
    else:
        setup = "apt-get update -qq && apt-get install -y -qq curl ca-certificates\n"
        command = [
            "docker", "run", "--rm", "debian:bookworm-slim",
            "bash", "-c", setup + script,
        ]
    completed = subprocess.run(command, check=True, text=True, capture_output=True)

    expected = {}
    for line in completed.stdout.splitlines():
        match = IMAGE_REF.fullmatch(line.strip())
        if match is not None:
            expected[match.group("name")] = match.group("tag")
    return expected


MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def registry_digest(name: str, tag: str) -> str:
    """The sha256 digest the registry serves for name:tag (index digest for multi-arch)."""
    host, _, repo = name.partition("/")
    request = urllib.request.Request(
        f"https://{host}/v2/{repo}/manifests/{tag}",
        headers={"Accept": MANIFEST_ACCEPT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return "sha256:" + hashlib.sha256(response.read()).hexdigest()


def images_txt_pins() -> list:
    """[(section, line_number, name, tag, digest)] for every tracked component pin."""
    pins = []
    section = 0
    for number, line in enumerate(IMAGES_TXT.read_text().splitlines(), 1):
        code = line.strip()
        if code.startswith("# \u2500\u2500"):
            section += 1
            continue
        if not code or code.startswith("#"):
            continue
        match = IMAGE_REF.fullmatch(code)
        if match is None or match.group("name") not in TRACKED_COMPONENTS:
            continue
        pins.append(
            (section, number, match.group("name"), match.group("tag"), match.group("digest"))
        )
    return pins


def fix_images_txt(expected: dict) -> None:
    lines = IMAGES_TXT.read_text().splitlines(keepends=True)
    for _, number, name, _, _ in images_txt_pins():
        tag = expected[name]
        lines[number - 1] = f"{name}:{tag}@{registry_digest(name, tag)}\n"
    IMAGES_TXT.write_text("".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--fix", action="store_true", help="rewrite images.txt pins from kubeadm and the registry"
    )
    args = parser.parse_args()
    version = target_kubernetes_version()
    print(f"target Kubernetes version (kindest/node): v{version}")

    try:
        expected = kubeadm_expected_images(version)
    except subprocess.CalledProcessError as exc:
        print(
            f"could not determine kubeadm's expected images: {exc}\n{exc.stderr}",
            file=sys.stderr,
        )
        return 1

    missing_expected = TRACKED_COMPONENTS - set(expected)
    if missing_expected:
        sys.exit(
            "kubeadm config images list did not report: "
            + ", ".join(sorted(missing_expected))
        )

    if args.fix:
        fix_images_txt(expected)
        print(f"rewrote {IMAGES_TXT.name} k8s component pins for kubeadm v{version}")
        return 0

    pins = images_txt_pins()
    failures = []
    for section in sorted({pin[0] for pin in pins}):
        present = {pin[2] for pin in pins if pin[0] == section}
        for dep_name in sorted(TRACKED_COMPONENTS - present):
            failures.append(f"{dep_name}: not pinned in section {section} of {IMAGES_TXT.name}")
    if not pins:
        failures.append(f"no tracked component pins found in {IMAGES_TXT.name}")

    digests = {}
    for _, number, dep_name, tag, digest in pins:
        expected_tag = expected[dep_name]
        if tag != expected_tag:
            failures.append(
                f"{IMAGES_TXT.name}:{number}: {dep_name}:{tag} does not match "
                f"what kubeadm v{version} deploys ({dep_name}:{expected_tag})"
            )
            continue
        if (dep_name, tag) not in digests:
            digests[(dep_name, tag)] = registry_digest(dep_name, tag)
        if digest != digests[(dep_name, tag)]:
            failures.append(
                f"{IMAGES_TXT.name}:{number}: {dep_name}:{tag} digest {digest} does not "
                f"match the registry ({digests[(dep_name, tag)]})"
            )

    if failures:
        print("Air-gap kubeadm image version check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"Air-gap kubeadm image version check OK "
        f"({len(TRACKED_COMPONENTS)} components checked against kubeadm v{version})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
