# verify-package.sh: sourced library (not executed) that verifies a Zarf air-gap
# package offline. airgap_verify_package never calls exit.

airgap_verify_package() {
  local pkg=$1 sbom_dir=$2 doc sbom_found=0
  local VERIFY_ARGS=() ZARF_TMP
  local ZARF="${ZARF:-$(command -v zarf)}"
  if [ -n "${ZARF_VERIFY_KEY:-}" ]; then
    VERIFY_ARGS+=(--key "$ZARF_VERIFY_KEY")
  else
    # AIRGAP_VERIFY_WORKFLOW_REF: see docs/airgap.md#empirical-findings.
    VERIFY_ARGS+=(
      --certificate-identity
      "https://github.com/${AIRGAP_VERIFY_WORKFLOW_REF:-polarsquad/krops/.github/workflows/air-gapped.yml@refs/heads/main}"
      --certificate-oidc-issuer
      'https://token.actions.githubusercontent.com'
    )
  fi

  # zarf's verify and inspect-sbom each extract the package to a temp dir for
  # the checksum and SBOM checks; the extraction is a full unpack of the
  # package (1.7-2.5 GiB for the current 1.6 GiB package, measured on the
  # air-gapped nightly). It is part of the caller's disk budget, not a free
  # side effect, so point zarf's temp dir (global --tmpdir flag, zarf 0.88.0)
  # at a sibling of sbom_dir and remove it on every exit path. The default
  # /tmp/zarf-* was outside the update-rollback budget (ENOSPC, #612), and
  # zarf 0.88.0 also leaves the extraction dir behind when verification
  # fails, so the cleanup below is what reclaims the space on rejection.
  ZARF_TMP=$(mktemp -d "$(dirname "$sbom_dir")/zarf-verify.XXXXXX") || return 1
  VERIFY_ARGS+=(--tmpdir "$ZARF_TMP")

  # zarf verifies the signature and the checksum manifest (sboms.tar included)
  # and extracts the SBOM archive, but never parses the documents: a package
  # whose SBOM entries are not valid Syft JSON would extract cleanly. Decode
  # each document with syft's own decoder (vendored inside the zarf binary)
  # and fail unless at least one document was found and all decoded.
  if ! "$ZARF" package verify "$pkg" "${VERIFY_ARGS[@]}"; then
    rm -rf "$ZARF_TMP"
    echo "airgap_verify_package: signature or checksum verification failed: $pkg" >&2
    return 1
  fi
  if ! "$ZARF" package inspect sbom "$pkg" --output "$sbom_dir" --verify=always "${VERIFY_ARGS[@]}"; then
    rm -rf "$ZARF_TMP"
    echo "airgap_verify_package: SBOM extraction failed: $pkg" >&2
    return 1
  fi
  while IFS= read -r -d '' doc; do
    sbom_found=1
    if ! "$ZARF" tools sbom convert "$doc" -o syft-json >/dev/null; then
      rm -rf "$ZARF_TMP"
      echo "airgap_verify_package: SBOM document failed to decode as Syft: $doc" >&2
      return 1
    fi
  done < <(find "$sbom_dir" -type f -name '*.json' -print0)
  if [ "$sbom_found" -eq 0 ]; then
    rm -rf "$ZARF_TMP"
    echo "airgap_verify_package: no SBOM documents extracted to $sbom_dir" >&2
    return 1
  fi
  rm -rf "$ZARF_TMP"
  return 0
}
