#!/usr/bin/env python3
"""Regression: the digest gate must reject out-of-length SHA-256 digests (#267)."""
import shutil, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "airgap/tests/test-airgap-image-digests.py"

def run_gate(root: Path) -> subprocess.CompletedProcess:
    # Binds the copied gate's REPO_ROOT (its own file location) to root.
    (root / "airgap/tests").mkdir(parents=True, exist_ok=True)
    shutil.copy(GATE, root / "airgap/tests/test-airgap-image-digests.py")
    return subprocess.run(
        [sys.executable, str(root / "airgap/tests/test-airgap-image-digests.py"), "--all"],
        capture_output=True,
        text=True,
        check=False,
    )

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "airgap").mkdir()
        (root / "airgap/zarf.yaml").write_text(
            "components:\n  - name: ok\n    images:\n"
            f"      - ghcr.io/example/ok:v1@sha256:{'b'*64}\n")
        (root / "airgap/images.txt").write_text(
            f"ghcr.io/example/toolong:v1@sha256:{'a'*65}\n"
            f"ghcr.io/example/tooshort:v1@sha256:{'c'*63}\n"
            f"ghcr.io/example/toolongdigestonly@sha256:{'d'*65}\n"
            f"ghcr.io/example/tooshortdigestonly@sha256:{'e'*63}\n"
            f"ghcr.io/example/digestonly@sha256:{'f'*64}\n")
        (root / "airgap/manifests").mkdir()
        (root / "airgap/manifests/flux-instance.yaml").write_text("kind: FluxInstance\n")
        (root / "airgap/scripts").mkdir(exist_ok=True)
        (root / "airgap/scripts/noop.sh").write_text("#!/usr/bin/env sh\ntrue\n")
        r = run_gate(root)
        out = r.stdout + r.stderr
    if r.returncode == 0:
        print("gate ACCEPTED an out-of-length digest token (should reject)", file=sys.stderr)
        return 1
    expected = [
        ("tagged, 65-char digest (too long)", "ghcr.io/example/toolong:v1"),
        ("tagged, 63-char digest (too short)", "ghcr.io/example/tooshort:v1"),
        ("digest-only, 65-char digest (too long)", f"sha256:{'d'*65}"),
        ("digest-only, 63-char digest (too short)", f"sha256:{'e'*63}"),
        ("digest-only, valid 64-char digest", f"ghcr.io/example/digestonly@sha256:{'f'*64}"),
    ]
    for label, needle in expected:
        if needle not in out:
            print(f"gate failed but did not name the offender for: {label}", file=sys.stderr)
            return 1
    print("digest token boundary OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
