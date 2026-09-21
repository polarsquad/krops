#!/usr/bin/env python3
"""Guard: no ACK CRs (*.services.k8s.aws) committed or rendered under workload/ (issue #411).

ACK controllers and their CRs live on the management cluster only (issue
#346); workload clusters run no controllers and hold no credentials. An ACK
CR under workload/ would reconcile nothing and silently rot. Scans (1)
every committed *.yaml under workload/ and (2) the rendered output of every
workload kustomize overlay, failing on any document whose apiVersion ends
in .services.k8s.aws.

Pure stdlib: apiVersion/kind are always column-0 keys in Kubernetes
manifests, so a column-anchored line scan is exact for this guard.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = REPO_ROOT / "workload"
ACK_API = re.compile(r"^apiVersion:\s*[a-z0-9.-]+\.services\.k8s\.aws/\S+\s*$")
KIND = re.compile(r"^kind:\s*(\S+)\s*$")


def ack_docs(text, source):
    """Return one finding per ACK CR document in the YAML text."""
    findings = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not ACK_API.match(line):
            continue
        kind = "?"
        if i + 1 < len(lines):
            kind_match = KIND.match(lines[i + 1])
            if kind_match:
                kind = kind_match.group(1)
        findings.append(f"{source}: {kind} ({line.split(':', 1)[1].strip()})")
    return findings


def kustomize_cmd():
    if shutil.which("kubectl"):
        return ["kubectl", "kustomize"]
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    sys.exit("neither kubectl nor kustomize on PATH")


def main():
    failures = []
    for path in sorted(WORKLOAD.rglob("*.yaml")):
        findings = ack_docs(path.read_text(), str(path.relative_to(REPO_ROOT)))
        failures.extend(findings)
    base_cmd = kustomize_cmd()
    for kustomization in sorted(WORKLOAD.rglob("kustomization.yaml")):
        overlay = kustomization.parent
        rendered = subprocess.run(
            [*base_cmd, str(overlay)], check=True, capture_output=True, text=True
        ).stdout
        findings = ack_docs(rendered, f"{overlay.relative_to(REPO_ROOT)} (rendered)")
        failures.extend(findings)
    if failures:
        print(
            "ACK CRs (*.services.k8s.aws) belong under mgmt/ only; "
            "found under workload/:",
            file=sys.stderr,
        )
        for finding in failures:
            print(f"  {finding}", file=sys.stderr)
        sys.exit(1)
    print("OK: no ACK CRs committed or rendered under workload/")


if __name__ == "__main__":
    main()
