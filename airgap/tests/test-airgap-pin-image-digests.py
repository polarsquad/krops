#!/usr/bin/env python3
"""Exercise pin_image_digests and copy_registry_secret from resolve-clusterctl.sh.

pin_image_digests rewrites rendered provider manifests so pods reference the digest bundled
in images.txt, and must fail loudly when an image is left unpinned.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "airgap/scripts/resolve-clusterctl.sh"
DIGEST = "sha256:" + "a" * 64
IMAGES = f"registry.k8s.io/cluster-api/cluster-api-controller:v1.14.2@{DIGEST}\n# comment\nlocalhost:5001/x:latest\n"


def run(manifest: str) -> tuple[subprocess.CompletedProcess, str]:
    with tempfile.TemporaryDirectory() as tmp:
        images, rendered = Path(tmp, "images.txt"), Path(tmp, "m.yaml")
        images.write_text(IMAGES)
        rendered.write_text(manifest)
        # Only the function: sourcing the whole script needs a staged clusterctl.
        body = SCRIPT.read_text().split("pin_image_digests()", 1)[1]
        cmd = f"pin_image_digests(){body}\nIMAGES_TXT={images} pin_image_digests {rendered}"
        result = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False)
        return result, rendered.read_text()


def check_copy_registry_secret() -> None:
    """The secret is copied into each namespace, or the function fails."""
    body = SCRIPT.read_text().split("copy_registry_secret()", 1)[1].split("\n}\n", 1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp, "kubectl")
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "apply" ]; then cat >> "$OUT"; else printf %s "$AUTH"; fi\n'
        )
        fake.chmod(0o755)
        out = Path(tmp, "applied.yaml")
        env = {"PATH": f"{tmp}:/usr/bin:/bin", "OUT": str(out)}
        cmd = f"copy_registry_secret(){body}\n}}\ncopy_registry_secret ns-a ns-b"
        ok = subprocess.run(["sh", "-c", cmd], env={**env, "AUTH": "e30="}, capture_output=True, text=True, check=False)
        applied = out.read_text() if out.exists() else ""
        if ok.returncode != 0 or applied.count(".dockerconfigjson: e30=") != 2 or "namespace: ns-b" not in applied:
            sys.exit(f"copy_registry_secret failed: rc={ok.returncode} {ok.stderr!r} {applied!r}")
        empty = subprocess.run(["sh", "-c", cmd], env={**env, "AUTH": ""}, capture_output=True, text=True, check=False)
        if empty.returncode == 0:
            sys.exit("copy_registry_secret must fail when the source secret is empty")


def main() -> None:
    ok, out = run("      - image: registry.k8s.io/cluster-api/cluster-api-controller:v1.14.2\n")
    if ok.returncode != 0 or f"cluster-api-controller:v1.14.2@{DIGEST}" not in out:
        sys.exit(f"pinning failed: rc={ok.returncode} stderr={ok.stderr!r} out={out!r}")

    bad, _ = run("image: example.com/other:1.0\n")
    if bad.returncode == 0 or "unpinned" not in bad.stderr:
        sys.exit("an unpinned image must fail pin_image_digests")
    check_copy_registry_secret()
    print("pin_image_digests and copy_registry_secret checks OK")


if __name__ == "__main__":
    main()
