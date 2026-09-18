#!/usr/bin/env python3
"""toolbox-run.sh's exec must not be truncated by a comment inside its
backslash continuation (#357): the image and CLI args must always reach the
container engine, and CLOUDSDK_CONFIG must be forwarded twice, repo-local
value last."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts/toolbox-run.sh"

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # toolbox-run.sh always cds to its own repo root ("$(dirname
        # "$0")/.."); copy it into an isolated fake repo so the run (and the
        # .kube/ dir it creates) never touches the real checkout.
        fake_repo = tmp / "repo"
        (fake_repo / "scripts").mkdir(parents=True)
        wrapper = fake_repo / "scripts/toolbox-run.sh"
        shutil.copy(WRAPPER, wrapper)
        os.chmod(wrapper, 0o755)
        bin_dir = tmp / "bin"; bin_dir.mkdir()
        argv_log = tmp / "docker_argv.log"
        # Fake docker: record every arg it's called with, one per line per
        # invocation, then succeed.
        (bin_dir / "docker").write_text(
            "#!/usr/bin/env sh\n"
            f'for a in "$@"; do echo "$a" >> {argv_log}; done\n'
            f'echo -- >> {argv_log}\n'
            "exit 0\n"
        )
        os.chmod(bin_dir / "docker", 0o755)
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            CONTAINER_ENGINE="docker",
            TOOLBOX_IMAGE="krops-toolbox:test",
        )
        subprocess.run(
            ["bash", str(wrapper), "bootstrap", "local-host"],
            env=env, cwd=fake_repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not argv_log.exists():
            print("docker was never invoked", file=sys.stderr); return 1
        argv = argv_log.read_text().splitlines()

    if "krops-toolbox:test" not in argv:
        print(f"image argument dropped from the exec: {argv}", file=sys.stderr); return 1
    if "local-host" not in argv:
        print(f"CLI args dropped from the exec: {argv}", file=sys.stderr); return 1
    if argv.count("CLOUDSDK_CONFIG=/workspace/.gcloud") != 1:
        print(f"repo-local CLOUDSDK_CONFIG override missing: {argv}", file=sys.stderr); return 1
    # The image must come after the last -e (so it's not swallowed as a flag
    # value) and the CLI args must come after the image.
    image_idx = argv.index("krops-toolbox:test")
    cli_idx = argv.index("local-host")
    if not (image_idx < cli_idx):
        print(f"argument order wrong: {argv}", file=sys.stderr); return 1

    print("toolbox-run.sh exec OK: image and CLI args reach docker")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
