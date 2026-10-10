#!/usr/bin/env python3
"""
Test that AWS credentials are no longer stored in static SOPS files.

Ensures:
1. No aws-credentials.sops.yaml files exist under mgmt/aws/
2. AWS credential secrets are created imperatively by bootstrap-rs
3. AWS credential secrets are created imperatively by bootstrap.sh/pivot.sh
"""

import re
import sys
from pathlib import Path


def test_no_sops_credential_files():
    """Assert no aws-credentials.sops.yaml files exist under mgmt/aws/."""
    repo_root = Path(__file__).parent.parent
    mgmt_aws = repo_root / "mgmt" / "aws"

    sops_files = list(mgmt_aws.glob("**/aws-credentials.sops.yaml"))
    assert len(sops_files) == 0, (
        f"Found SOPS credential files that should not exist: {sops_files}. "
        "AWS credentials should be created imperatively from ambient credentials."
    )
    print("✓ No aws-credentials.sops.yaml files found under mgmt/aws/")


def test_kustomizations_dont_reference_sops_credentials():
    """Assert kustomization.yaml files don't list aws-credentials.sops.yaml."""
    repo_root = Path(__file__).parent.parent
    mgmt_aws = repo_root / "mgmt" / "aws"

    for kustomization in mgmt_aws.glob("**/kustomization.yaml"):
        content = kustomization.read_text()
        assert "aws-credentials.sops.yaml" not in content, (
            f"{kustomization} still references aws-credentials.sops.yaml. "
            "AWS credentials should be created imperatively."
        )
    print("✓ No kustomization.yaml files reference aws-credentials.sops.yaml")


def test_bootstrap_rs_creates_aws_secrets():
    """Assert bootstrap-rs creates AWS credential secrets imperatively."""
    repo_root = Path(__file__).parent.parent
    main_rs = repo_root / "bootstrap-rs" / "src" / "main.rs"

    content = main_rs.read_text()

    # Check that create_aws_credential_secrets function exists
    assert "async fn create_aws_credential_secrets" in content, (
        "bootstrap-rs should have a create_aws_credential_secrets function"
    )

    # Extract the function body to ensure secrets are created within it
    fn_match = re.search(
        r'async fn create_aws_credential_secrets\s*\([^)]*\)\s*(?:->.*?)?\s*\{',
        content
    )
    assert fn_match, "Could not find create_aws_credential_secrets function signature"

    # Find the function body (from opening brace to closing brace, accounting for nesting)
    fn_start = fn_match.end()
    brace_count = 1
    fn_end = fn_start
    for i, char in enumerate(content[fn_start:]):
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                fn_end = fn_start + i
                break
    create_fn_body = content[fn_start:fn_end]

    # Check that it creates capa-system secret within the function
    assert re.search(r'"capa-system"', create_fn_body), (
        "create_aws_credential_secrets should create aws-credentials secret in capa-system"
    )

    # Check that it creates ack-system secret within the function
    assert re.search(r'"ack-system"', create_fn_body), (
        "create_aws_credential_secrets should create aws-credentials secret in ack-system"
    )

    # Check that the function is called in run_bootstrap
    assert "create_aws_credential_secrets(None" in content, (
        "bootstrap-rs should call create_aws_credential_secrets in the bootstrap phase"
    )

    # Check that the function is called in run_pivot
    assert "create_aws_credential_secrets(Some(kc)" in content, (
        "bootstrap-rs should call create_aws_credential_secrets in the pivot phase"
    )

    print("✓ bootstrap-rs creates AWS credential secrets imperatively")


def test_bootstrap_sh_creates_aws_secrets():
    """Assert bootstrap.sh creates AWS credential secrets imperatively."""
    repo_root = Path(__file__).parent.parent
    bootstrap_sh = repo_root / "bootstrap.sh"

    content = bootstrap_sh.read_text()

    # Check that it creates CAPA secret
    assert 'kubectl create secret generic aws-credentials' in content, (
        "bootstrap.sh should create aws-credentials secret"
    )

    # Check for both namespaces
    capa_section = 'capa-system' in content
    ack_section = 'ack-system' in content
    assert capa_section and ack_section, (
        "bootstrap.sh should create aws-credentials in both capa-system and ack-system"
    )

    print("✓ bootstrap.sh creates AWS credential secrets imperatively")


def test_pivot_sh_creates_aws_secrets():
    """Assert pivot.sh creates AWS credential secrets imperatively."""
    repo_root = Path(__file__).parent.parent
    pivot_sh = repo_root / "pivot.sh"

    content = pivot_sh.read_text()

    # Check that it creates secrets with kubectl
    assert 'kubectl --kubeconfig "$MGMT_KUBECONFIG" create secret generic aws-credentials' in content, (
        "pivot.sh should create aws-credentials secret"
    )

    # Check for both namespaces
    capa_section = 'capa-system' in content
    ack_section = 'ack-system' in content
    assert capa_section and ack_section, (
        "pivot.sh should create aws-credentials in both capa-system and ack-system"
    )

    print("✓ pivot.sh creates AWS credential secrets imperatively")


def test_teardown_rs_deletes_aws_secrets():
    """Assert teardown.rs deletes AWS credential secrets from both namespaces."""
    repo_root = Path(__file__).parent.parent
    teardown_rs = repo_root / "bootstrap-rs" / "src" / "teardown.rs"

    content = teardown_rs.read_text()

    # Check that it deletes from both namespaces
    assert '"capa-system"' in content and '"ack-system"' in content, (
        "teardown.rs should delete aws-credentials from both capa-system and ack-system"
    )

    print("✓ teardown.rs deletes AWS credential secrets from both namespaces")


def test_teardown_sh_deletes_aws_secrets():
    """Assert teardown.sh deletes AWS credential secrets from both namespaces."""
    repo_root = Path(__file__).parent.parent
    teardown_sh = repo_root / "teardown.sh"

    content = teardown_sh.read_text()

    # Check that it deletes from capa-system
    assert 'kubectl delete secret aws-credentials -n capa-system' in content, (
        "teardown.sh should delete aws-credentials from capa-system"
    )

    # Check that it deletes from ack-system
    assert 'kubectl delete secret aws-credentials -n ack-system' in content, (
        "teardown.sh should delete aws-credentials from ack-system"
    )

    print("✓ teardown.sh deletes AWS credential secrets from both namespaces")


def test_flux_kustomizations_have_no_orphan_decryption():
    """Assert decryption blocks are removed from capa-system and ack-controllers Kustomizations."""
    repo_root = Path(__file__).parent.parent

    # Check capa-system in capi-providers/flux-ks.yaml
    capi_flux = repo_root / "mgmt" / "aws" / "capi-providers" / "flux-ks.yaml"
    capi_content = capi_flux.read_text()
    capi_docs = capi_content.split('\n---')

    capa_doc = None
    for doc in capi_docs:
        if 'name: capa-system' in doc:
            capa_doc = doc
            break

    assert capa_doc, "Could not find capa-system Kustomization in capi-providers/flux-ks.yaml"
    assert 'decryption:' not in capa_doc, (
        "capa-system Kustomization should not have a decryption block"
    )

    # Ensure no sops files exist under capa-system
    capa_sops = list((repo_root / "mgmt" / "aws" / "capi-providers" / "capa-system").glob("*.sops.yaml"))
    assert len(capa_sops) == 0, (
        f"Found .sops.yaml files under capa-system that should not exist: {capa_sops}"
    )

    # Check ack-controllers in infrastructure/flux-ks.yaml
    infra_flux = repo_root / "mgmt" / "aws" / "infrastructure" / "flux-ks.yaml"
    infra_content = infra_flux.read_text()
    infra_docs = infra_content.split('\n---')

    ack_doc = None
    for doc in infra_docs:
        if 'name: ack-controllers' in doc:
            ack_doc = doc
            break

    assert ack_doc, "Could not find ack-controllers Kustomization in infrastructure/flux-ks.yaml"
    assert 'decryption:' not in ack_doc, (
        "ack-controllers Kustomization should not have a decryption block"
    )

    # Ensure no sops files exist under ack-controllers
    ack_sops = list((repo_root / "mgmt" / "aws" / "infrastructure" / "ack-controllers").glob("*.sops.yaml"))
    assert len(ack_sops) == 0, (
        f"Found .sops.yaml files under ack-controllers that should not exist: {ack_sops}"
    )

    print("✓ No orphan decryption blocks in capa-system or ack-controllers Kustomizations")


def test_migration_runbook_documented():
    """Assert migration runbook is documented in docs/aws-iam.md."""
    repo_root = Path(__file__).parent.parent
    aws_iam_md = repo_root / "docs" / "aws-iam.md"

    content = aws_iam_md.read_text()

    # Check for key migration runbook commands
    assert 'flux suspend kustomization capa-system' in content, (
        "Migration runbook should include: flux suspend kustomization capa-system"
    )

    assert 'flux suspend kustomization ack-controllers' in content, (
        "Migration runbook should include: flux suspend kustomization ack-controllers"
    )

    assert 'kustomize.toolkit.fluxcd.io/prune=disabled' in content, (
        "Migration runbook should include the prune-disable annotation"
    )

    assert 'flux resume kustomization' in content, (
        "Migration runbook should include: flux resume kustomization"
    )

    print("✓ Migration runbook is properly documented in docs/aws-iam.md")


if __name__ == "__main__":
    try:
        test_no_sops_credential_files()
        test_kustomizations_dont_reference_sops_credentials()
        test_bootstrap_rs_creates_aws_secrets()
        test_bootstrap_sh_creates_aws_secrets()
        test_pivot_sh_creates_aws_secrets()
        test_teardown_rs_deletes_aws_secrets()
        test_teardown_sh_deletes_aws_secrets()
        test_flux_kustomizations_have_no_orphan_decryption()
        test_migration_runbook_documented()
        print("\n✓ All tests passed!")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}", file=sys.stderr)
        sys.exit(1)
