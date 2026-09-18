#!/usr/bin/env python3
"""Verify the collapsed CAPI-provider Renovate managers still cover all 4
providers (issue #354).

Each of the 3 customManagers that render core/bootstrap-kubeadm/
control-plane-kubeadm/infrastructure-docker now matches all four providers
through a single depName alternation instead of one manager per provider.
A broken alternation (e.g. a typo'd branch, or a missing `|`) would silently
stop extracting one provider while the others kept working, and none of the
existing tests distinguish providers once depNameTemplate has collapsed them
all to "kubernetes-sigs/cluster-api" -- so this test counts the annotated
`# renovate:` sites in source and asserts Renovate extracts exactly that
many dependencies per file/datasource pair.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from renovate_harness import run_renovate

REPO_ROOT = Path(__file__).resolve().parents[2]

PROVIDERS = ["core", "bootstrap-kubeadm", "control-plane-kubeadm", "infrastructure-docker"]
PROVIDER_DEP_RE = re.compile(
    r"depName=kubernetes-sigs/cluster-api-(?:" + "|".join(PROVIDERS) + r")\b"
)

# (file, datasource) -> expected match count, derived from what each file
# actually contains: 2 release-attachment sites (asset + metadata.yaml) per
# provider in zarf.yaml, 1 rendered-version site per provider in zarf.yaml,
# 1 staged-path site per provider in clusterctl-providers.yaml.
CASES = [
    ("airgap/zarf.yaml", "github-release-attachments", len(PROVIDERS) * 2),
    ("airgap/zarf.yaml", "github-releases", len(PROVIDERS)),
    ("airgap/files/clusterctl-providers.yaml", "github-releases", len(PROVIDERS)),
]


def main() -> int:
    files = sorted({file for file, _, _ in CASES})
    errors = []

    for file in files:
        text = (REPO_ROOT / file).read_text()
        annotated = len(PROVIDER_DEP_RE.findall(text))
        expected = sum(count for f, _, count in CASES if f == file)
        if annotated != expected:
            errors.append(
                f"{file}: expected {expected} annotated CAPI-provider sites in source, found {annotated} "
                "(this test's own CASES table is out of sync with the file, fix the table)"
            )

    result = run_renovate(files)
    if result.returncode:
        print("CAPI provider manager coverage check failed", file=sys.stderr)
        print(f"exit code: {result.returncode}", file=sys.stderr)
        result.print_diagnostics()
        return 1

    for file, datasource, expected in CASES:
        matched = [
            dep
            for dep in result.deps_by_file[file]
            if dep.get("datasource") == datasource
            and PROVIDER_DEP_RE.search(dep.get("replaceString") or "")
        ]
        if len(matched) != expected:
            errors.append(
                f"{file} ({datasource}): expected {expected} extracted CAPI-provider dependencies, "
                f"got {len(matched)} -- the depName alternation may be dropping a provider"
            )

    if errors:
        print("CAPI provider manager coverage check failed", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        result.print_diagnostics()
        return 1

    for file, datasource, expected in CASES:
        print(f"{file} ({datasource}): {expected} CAPI-provider dependencies extracted, all 4 providers covered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
