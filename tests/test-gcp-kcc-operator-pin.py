#!/usr/bin/env python3
"""Regression guard for the KCC operator OCIRepository pin (issue #550).

The OCIRepository pins both ref.tag and ref.digest (since #546); the
digest takes precedence, so Flux pulls the chart by digest and a forced
re-push of the tag cannot change the artifact, and the cosign
matchOIDCIdentity block adds the publisher-identity check on top. No
renderer or manager validates that an edit keeps the digest line or the
verify block, so this offline guard fails on their removal or on a
changed publisher identity. Only the URL, issuer, and subject literals
are hardcoded; tag and digest values must not be pinned here because
Renovate's native Flux manager bumps both together in one edit. Requires
PyYAML (mise's python provides it via `uv run`).
"""

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
OCIREPO = REPO_ROOT / "mgmt/gcp/infrastructure/kcc-operator/ocirepository.yaml"

# Stable couplings: the URL is the Renovate depName/lookupName; the issuer
# and subject are the keyless-cosign identity of the chart's publish
# workflow.
EXPECTED_URL = "oci://ghcr.io/lioramilbaum/config-connector-helm/config-connector"
EXPECTED_ISSUER = "https://token.actions.githubusercontent.com"
EXPECTED_SUBJECT = "https://github.com/lioramilbaum/config-connector-helm/.github/workflows/publish.yaml@refs/heads/main"

TAG_RE = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def main() -> int:
    failures = []
    docs = [d for d in yaml.safe_load_all(OCIREPO.read_text()) if d]
    if len(docs) != 1 or docs[0].get("kind") != "OCIRepository":
        print(f"expected exactly 1 OCIRepository in {OCIREPO.relative_to(REPO_ROOT)}, got {len(docs)}")
        return 1
    spec = docs[0].get("spec", {})
    ref = spec.get("ref", {})

    if spec.get("url") != EXPECTED_URL:
        failures.append(f"spec.url must be {EXPECTED_URL} (Renovate depName), got {spec.get('url')!r}")
    tag = ref.get("tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        failures.append(f"spec.ref.tag must be a semver tag ({TAG_RE.pattern}), got {tag!r}")
    digest = ref.get("digest")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        failures.append("spec.ref.digest must be a sha256 digest; without it the pin is tag-mutable (issue #550)")

    verify = spec.get("verify", {})
    if verify.get("provider") != "cosign":
        failures.append(f"spec.verify.provider must be cosign, got {verify.get('provider')!r}")
    identities = verify.get("matchOIDCIdentity")
    if not isinstance(identities, list) or len(identities) != 1:
        failures.append(f"spec.verify.matchOIDCIdentity must be a single entry, got {identities!r}")
    else:
        ident = identities[0]
        if ident.get("issuer") != EXPECTED_ISSUER:
            failures.append(f"matchOIDCIdentity issuer must be {EXPECTED_ISSUER}, got {ident.get('issuer')!r}")
        if ident.get("subject") != EXPECTED_SUBJECT:
            failures.append(f"matchOIDCIdentity subject must be {EXPECTED_SUBJECT}, got {ident.get('subject')!r}")

    if failures:
        print("gcp kcc operator pin FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"gcp kcc operator pin OK (tag {tag}, digest {digest[:19]}…, cosign OIDC identity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
