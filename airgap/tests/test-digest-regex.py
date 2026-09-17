#!/usr/bin/env python3
"""Regression: the digest gate must reject overlong SHA-256 digest tokens (#267)."""
import shutil, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "airgap/tests/test-airgap-image-digests.py"

def run_gate(root: Path) -> subprocess.CompletedProcess:
    # REPO_ROOT is derived from the gate's own location, so copy it into the
    # fixture's airgap/tests/ to bind the gate to the fixture tree.
    (root / "airgap/tests").mkdir(parents=True, exist_ok=True)
    shutil.copy(GATE, root / "airgap/tests/test-airgap-image-digests.py")
    return subprocess.run([sys.executable, str(root / "airgap/tests/test-airgap-image-digests.py"), "--all"],
                          capture_output=True, text=True, check=False)

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "airgap").mkdir()
        (root / "airgap/zarf.yaml").write_text(
            "components:\n  - name: ok\n    images:\n"
            f"      - ghcr.io/example/ok:v1@sha256:{'b'*64}\n")
        (root / "airgap/images.txt").write_text(
            f"ghcr.io/example/app:v1@sha256:{'a'*65}\n")
        (root / "airgap/scripts").mkdir(exist_ok=True)
        (root / "airgap/scripts/noop.sh").write_text("#!/usr/bin/env sh\ntrue\n")
        r = run_gate(root)
        out = r.stdout + r.stderr
    if r.returncode == 0:
        print("gate ACCEPTED a 65-char digest token (should reject)", file=sys.stderr); return 1
    if "ghcr.io/example/app:v1" not in out:
        print("gate failed but did not name the offending reference", file=sys.stderr); return 1
    print("digest token boundary OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
