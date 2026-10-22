# verify-package.sh: sourced library (not executed) that verifies a Zarf air-gap
# package offline. airgap_verify_package never calls exit.

airgap_verify_package() {
  local pkg=$1 sbom_dir=$2 doc sbom_found=0
  local VERIFY_ARGS=()
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

  # zarf verifies the signature and the checksum manifest (sboms.tar included)
  # and extracts the SBOM archive, but never parses the documents: a package
  # whose SBOM entries are not valid Syft JSON would extract cleanly. Decode
  # each document with syft's own decoder (vendored inside the zarf binary)
  # and fail unless at least one document was found and all decoded.
  if ! "$ZARF" package verify "$pkg" "${VERIFY_ARGS[@]}"; then
    echo "airgap_verify_package: signature or checksum verification failed: $pkg" >&2
    return 1
  fi
  if ! "$ZARF" package inspect sbom "$pkg" --output "$sbom_dir" --verify=always "${VERIFY_ARGS[@]}"; then
    echo "airgap_verify_package: SBOM extraction failed: $pkg" >&2
    return 1
  fi
  while IFS= read -r -d '' doc; do
    sbom_found=1
    if ! "$ZARF" tools sbom convert "$doc" -o syft-json >/dev/null; then
      echo "airgap_verify_package: SBOM document failed to decode as Syft: $doc" >&2
      return 1
    fi
  done < <(find "$sbom_dir" -type f -name '*.json' -print0)
  if [ "$sbom_found" -eq 0 ]; then
    echo "airgap_verify_package: no SBOM documents extracted to $sbom_dir" >&2
    return 1
  fi
  return 0
}
