#!/usr/bin/env python3
"""Regression gate for the krops.io/* tagging standard (issue #381)."""
import re
import subprocess
import sys
import pathlib
import yaml

REPO = pathlib.Path(__file__).parent.parent
REQUIRED_TAGS = [
    "krops.io/run-id",
    "krops.io/revision",
    "krops.io/expires-at",
    "krops.io/run-kind",
]
CAPA_TAG_RE = re.compile(r"^[a-zA-Z0-9\s_.:=+\-@/]+$")


def kustomize_build(path):
    """Build a kustomize overlay and return parsed YAML documents."""
    r = subprocess.run(
        ["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=True
    )
    return list(yaml.safe_load_all(r.stdout))


errors = []

# (a) CAPA control planes have the four tags
CLUSTER_OVERLAYS = [
    "mgmt/aws/clusters/eu-north-1/staging",
    "mgmt/aws/clusters/eu-north-1/management",
    "mgmt/aws/clusters/eu-west-1/staging",
]
for overlay in CLUSTER_OVERLAYS:
    docs = kustomize_build(REPO / overlay)
    for doc in docs:
        if doc and doc.get("kind") == "AWSManagedControlPlane":
            tags = doc.get("spec", {}).get("additionalTags", {})
            for k in REQUIRED_TAGS:
                if k not in tags:
                    errors.append(
                        f"{overlay}: AWSManagedControlPlane missing additionalTags[{k!r}]"
                    )

# (b) ACK CRs have the four tags
ACK_OVERLAYS = [
    "mgmt/aws/infrastructure/workload-resources",
    "mgmt/aws/infrastructure/aws-global-iam",
]
ACK_KINDS = {"Bucket", "DBInstance", "Role", "User"}
for overlay in ACK_OVERLAYS:
    docs = kustomize_build(REPO / overlay)
    for doc in docs:
        if not doc:
            continue
        if doc.get("kind") not in ACK_KINDS:
            continue
        spec = doc.get("spec", {})
        # Bucket uses spec.tagging.tagSet (list of {key, value}), others use spec.tags (list of {key, value})
        if doc["kind"] == "Bucket":
            tag_list = spec.get("tagging", {}).get("tagSet", [])
        else:
            tag_list = spec.get("tags", [])
        found_keys = {t["key"] for t in tag_list if isinstance(t, dict)}
        for k in REQUIRED_TAGS:
            if k not in found_keys:
                errors.append(
                    f"{overlay}: {doc['kind']} {doc.get('metadata',{}).get('name')} missing tag {k!r}"
                )

# (c) Every ${KROPS_*} placeholder in manifests is quoted and has := default
MANIFEST_DIRS = [
    "mgmt/aws/clusters",
    "mgmt/aws/infrastructure/workload-resources",
    "mgmt/aws/infrastructure/aws-global-iam",
]
PLACEHOLDER_RE = re.compile(r"\$\{KROPS_\w+")
QUOTED_PLACEHOLDER_RE = re.compile(r'"\$\{KROPS_\w+:=[^}]+\}"')
for d in MANIFEST_DIRS:
    for f in (REPO / d).rglob("*.yaml"):
        if "flux-ks" in f.name:
            continue
        text = f.read_text()
        for m in PLACEHOLDER_RE.finditer(text):
            line = text[max(0, m.start() - 50) : m.end() + 50]
            if not QUOTED_PLACEHOLDER_RE.search(line):
                errors.append(
                    f"{f.relative_to(REPO)}: unquoted or missing-default placeholder near: {line.strip()!r}"
                )

# (d) Simulate Flux substitution; values must be strings and fit CAPA limits
SAMPLE_VALUES = {
    "KROPS_RUN_ID": "gha-123",
    "KROPS_REVISION": "a" * 40,
    "KROPS_EXPIRES_AT": "2026-09-28T09:15:00Z",
    "KROPS_RUN_KIND": "scheduled",
}
DEFAULT_VALUES = {
    "KROPS_RUN_ID": "manual",
    "KROPS_REVISION": "unknown",
    "KROPS_EXPIRES_AT": "never",
    "KROPS_RUN_KIND": "manual",
}
for vals, label in [(SAMPLE_VALUES, "sample"), (DEFAULT_VALUES, "default")]:
    for overlay in CLUSTER_OVERLAYS:
        docs = kustomize_build(REPO / overlay)
        for doc in docs:
            if not doc or doc.get("kind") != "AWSManagedControlPlane":
                continue
            # Simulate substitution
            import json

            raw = json.dumps(doc)
            for var, val in vals.items():
                raw = re.sub(
                    r'"\$\{' + var + r':=[^}]+\}"',
                    json.dumps(val),
                    raw,
                )
            doc2 = json.loads(raw)
            tags = doc2.get("spec", {}).get("additionalTags", {})
            for k in REQUIRED_TAGS:
                v = tags.get(k, "")
                if not isinstance(v, str):
                    errors.append(
                        f"{overlay} ({label}): {k} value is not a string: {v!r}"
                    )
                if not CAPA_TAG_RE.match(v):
                    errors.append(
                        f"{overlay} ({label}): {k}={v!r} fails CAPA tag regex"
                    )
                if len(k) > 128 or len(str(v)) > 256:
                    errors.append(
                        f"{overlay} ({label}): {k}={v!r} exceeds CAPA length limits"
                    )

# (e) Every flux-ks.yaml whose rendered output has a taggable kind includes substituteFrom
TAGGABLE_KINDS = {"AWSManagedControlPlane", "Bucket", "DBInstance", "Role", "User"}
for flux_ks_path in (REPO / "mgmt/aws").rglob("flux-ks.yaml"):
    flux_docs = list(yaml.safe_load_all(flux_ks_path.read_text()))
    for ks in flux_docs:
        if not ks or ks.get("kind") != "Kustomization":
            continue
        path = ks.get("spec", {}).get("path", "")
        overlay_abs = REPO / path.lstrip("/")
        if not overlay_abs.exists():
            continue
        try:
            rendered = kustomize_build(overlay_abs)
        except subprocess.CalledProcessError:
            continue
        has_taggable = any(
            d and d.get("kind") in TAGGABLE_KINDS for d in rendered
        )
        if not has_taggable:
            continue
        substitute_from = (
            ks.get("spec", {}).get("postBuild", {}).get("substituteFrom", [])
        )
        has_run_cm = any(
            s.get("kind") == "ConfigMap"
            and s.get("name") == "krops-run"
            and s.get("optional") is True
            for s in substitute_from
        )
        if not has_run_cm:
            errors.append(
                f"{flux_ks_path.relative_to(REPO)}: Kustomization {ks.get('metadata',{}).get('name')} covers taggable kinds but lacks substituteFrom krops-run optional:true"
            )

# (f) Constants appear literally in run_tags.rs
run_tags_text = (REPO / "bootstrap-rs/src/run_tags.rs").read_text()
for literal in ["krops-run", "KROPS_RUN_ID", "KROPS_REVISION", "KROPS_EXPIRES_AT", "KROPS_RUN_KIND"]:
    if literal not in run_tags_text:
        errors.append(f"bootstrap-rs/src/run_tags.rs: missing literal {literal!r}")

# (g) pivot.sh never generates run tags; it copies them from kind
pivot_text = (REPO / "pivot.sh").read_text()
for forbidden in ["_KROPS_RUN_ID", "date -u +%Y%m%dT", "KROPS_RUN_TTL"]:
    if forbidden in pivot_text:
        errors.append(f"pivot.sh: must not generate run tags (found {forbidden!r})")
if "get configmap krops-run" not in pivot_text:
    errors.append("pivot.sh: must read krops-run ConfigMap from kind (missing 'get configmap krops-run')")
if not re.search(r'--context\s+\S+\s+get configmap krops-run|get configmap krops-run.*--context', pivot_text):
    errors.append("pivot.sh: must use explicit --context when reading from kind (missing '--context' in krops-run read)")

if errors:
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)
    sys.exit(1)
print(f"OK: all {len(REQUIRED_TAGS)} tags present on all taggable kinds")
