#!/usr/bin/env python3
"""Require the pinned cert-manager version to support the pinned Kubernetes version.

cert-manager only supports a rolling window of Kubernetes versions per release
(https://cert-manager.io/docs/releases/); this repo hit that wall directly
when cert-manager v1.21.x (supports v1.33-v1.36) was paired with a
Renovate-bumped Kubernetes v1.37.0, breaking the daily air-gapped workflow
silently until root-caused (issue #322). This gate tracks that support
window from cert-manager's own published "Currently supported releases"
table -- there is no versioned/structured feed for it (cert-manager/cert-
manager#9123 asks upstream to formalize one), so this fetches the same
Markdown source that page renders from and parses it, rather than embedding
a compatibility table here that would silently go stale itself.

Only runs its network check when a change under `--changed-from` touches the
cert-manager pin (bootstrap.toml) or the Kubernetes pin (the kindest/node
version in mgmt/local-host/clusters/docker/cluster.yaml, the source
airgap/tests/test-airgap-kubeadm-images.py also treats as authoritative);
otherwise it's a no-op, since neither input changed.
"""

import argparse
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TOML = REPO_ROOT / "bootstrap.toml"
NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/docker/cluster.yaml"
TRIGGER_PATHS = {
    BOOTSTRAP_TOML.relative_to(REPO_ROOT).as_posix(),
    NODE_IMAGE_SOURCE.relative_to(REPO_ROOT).as_posix(),
}

# The live page (https://cert-manager.io/docs/releases/) renders this file;
# fetching the Markdown source directly avoids parsing rendered HTML.
SUPPORTED_RELEASES_URL = (
    "https://raw.githubusercontent.com/cert-manager/website/master/"
    "content/docs/releases/README.md"
)
DOCS_URL = "https://cert-manager.io/docs/releases/"

ROW_RE = re.compile(
    r"^\|\s*\[(?P<cert_manager>\d+\.\d+)\]\[\]\s*\|"  # | [1.21][] |
    r"[^|]*\|[^|]*\|"  # Release Date | End of Life |
    r"\s*(?P<k8s_min>\d+\.\d+)\s*→\s*(?P<k8s_max>\d+\.\d+)",  # 1.33 -> 1.36 ...
    re.MULTILINE,
)


def changed_paths(base: str | None) -> list[str]:
    if base:
        command = ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"]
        return subprocess.run(
            command, cwd=REPO_ROOT, check=True, text=True, capture_output=True
        ).stdout.splitlines()

    for candidate in ("origin/main", "main"):
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=REPO_ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if exists.returncode == 0:
            return subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{candidate}...HEAD"],
                cwd=REPO_ROOT, check=True, text=True, capture_output=True,
            ).stdout.splitlines()
    return []


def pinned_cert_manager_version() -> str:
    config = tomllib.loads(BOOTSTRAP_TOML.read_text())
    version = config.get("charts", {}).get("cert-manager")
    if version is None:
        sys.exit(f"{BOOTSTRAP_TOML}: charts.cert-manager is missing")
    return version


def pinned_kubernetes_version() -> str:
    text = NODE_IMAGE_SOURCE.read_text()
    match = re.search(r"^\s*version: v(?P<version>\d+\.\d+\.\d+)\s*$", text, re.MULTILINE)
    if match is None:
        sys.exit(f"{NODE_IMAGE_SOURCE}: could not find a 'version: vX.Y.Z' topology pin")
    return match.group("version")


def minor(version: str) -> tuple[int, int]:
    major, minor_, *_ = version.split(".")
    return int(major), int(minor_)


def supported_kubernetes_ranges() -> dict[str, tuple[str, str]]:
    """{cert-manager minor: (min k8s minor, max k8s minor)} for currently supported releases."""
    try:
        with urllib.request.urlopen(SUPPORTED_RELEASES_URL, timeout=15) as response:
            text = response.read().decode()
    except urllib.error.URLError as error:
        sys.exit(f"could not fetch {SUPPORTED_RELEASES_URL}: {error}")

    section = re.search(r"## Currently supported releases\n(.*?)\n##", text, re.DOTALL)
    if section is None:
        sys.exit(
            f"could not find the 'Currently supported releases' table in {SUPPORTED_RELEASES_URL}"
            " -- page structure may have changed"
        )

    ranges = {
        match.group("cert_manager"): (match.group("k8s_min"), match.group("k8s_max"))
        for match in ROW_RE.finditer(section.group(1))
    }
    if not ranges:
        sys.exit(
            f"parsed no release rows from {SUPPORTED_RELEASES_URL} -- table format may have changed"
        )
    return ranges


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--changed-from",
        metavar="GIT_REF",
        help="only check when this touches bootstrap.toml or the Kubernetes version pin",
    )
    args = parser.parse_args()

    changed = set(changed_paths(args.changed_from))
    if not changed & TRIGGER_PATHS:
        print(
            "cert-manager/Kubernetes support check skipped "
            "(neither the cert-manager nor the Kubernetes pin changed)"
        )
        return 0

    cert_manager_version = pinned_cert_manager_version()
    kubernetes_version = pinned_kubernetes_version()
    cert_manager_minor = ".".join(cert_manager_version.split(".")[:2])
    kubernetes_minor = ".".join(kubernetes_version.split(".")[:2])

    ranges = supported_kubernetes_ranges()
    supported_range = ranges.get(cert_manager_minor)
    if supported_range is None:
        print(
            f"cert-manager {cert_manager_minor} (bootstrap.toml) is not listed among the "
            f"currently supported releases at {DOCS_URL} -- it may be too new, too old, or "
            f"the pinned Kubernetes v{kubernetes_version} may be unsupported. Verify manually.",
            file=sys.stderr,
        )
        return 1

    k8s_min, k8s_max = supported_range
    if not (minor(k8s_min) <= minor(kubernetes_minor) <= minor(k8s_max)):
        print(
            f"cert-manager {cert_manager_minor} (bootstrap.toml) supports Kubernetes "
            f"{k8s_min} → {k8s_max}, but the pinned Kubernetes version is "
            f"v{kubernetes_version} ({DOCS_URL}).",
            file=sys.stderr,
        )
        return 1

    print(
        f"cert-manager/Kubernetes support check OK: cert-manager {cert_manager_minor} "
        f"supports Kubernetes {k8s_min} → {k8s_max}, pinned Kubernetes is "
        f"v{kubernetes_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
