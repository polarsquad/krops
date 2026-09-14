#!/usr/bin/env python3
"""The toolbox entrypoint must point the Podman client at the mounted socket (#255)."""
import os, stat, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "bootstrap-rs/toolbox-entrypoint.sh"

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bin = tmp / "bin"; bin.mkdir()
        env_log = tmp / "podman_env.log"
        # Fake podman: record CONTAINER_HOST, succeed for info/network.
        (bin / "podman").write_text(
            "#!/usr/bin/env sh\n"
            f'echo "CONTAINER_HOST=$CONTAINER_HOST" >> {env_log}\n'
            "exit 0\n"
        )
        os.chmod(bin / "podman", 0o755)
        # Fake krops-bootstrap so the entrypoint's final exec is a no-op.
        (bin / "krops-bootstrap").write_text("#!/usr/bin/env sh\nexit 0\n")
        os.chmod(bin / "krops-bootstrap", 0o755)
        env = dict(os.environ, CONTAINER_ENGINE="podman", PATH=f"{bin}:{os.environ['PATH']}")
        env.pop("CONTAINER_HOST", None)
        subprocess.run(["sh", str(ENTRYPOINT), "teardown"], env=env,
                       cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seen = env_log.read_text().splitlines() if env_log.exists() else []
    if not seen:
        print("entrypoint never invoked podman", file=sys.stderr); return 1
    if "CONTAINER_HOST=unix:///var/run/docker.sock" not in seen:
        print(f"podman client not pointed at the mounted socket: {seen}", file=sys.stderr); return 1
    print("entrypoint podman CONTAINER_HOST OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
