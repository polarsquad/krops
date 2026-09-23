#!/usr/bin/env python3
"""toolbox-run.sh's exec must not be truncated by a comment inside its
backslash continuation (#357): the image and CLI args must always reach the
container engine, and CLOUDSDK_CONFIG must be forwarded exactly once, fixed
to the repo-local path (never from PASS_ENV, so an operator value can't
override it). An operator value for an immutable key must also warn on
stderr instead of silently doing nothing."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts/toolbox-run.sh"


def run_once(extra_env: dict):
    """Run the wrapper against a fake docker in an isolated fake repo;
    returns (argv, stderr)."""
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
        env.update(extra_env)
        result = subprocess.run(
            ["bash", str(wrapper), "bootstrap", "local-host"],
            env=env, cwd=fake_repo, stdout=subprocess.DEVNULL, capture_output=False,
            stderr=subprocess.PIPE, text=True, check=False,
        )
        argv = argv_log.read_text().splitlines() if argv_log.exists() else []
        return argv, result.stderr


def test_image_and_cli_args_reach_docker() -> None:
    argv, _ = run_once({})
    if "krops-toolbox:test" not in argv:
        raise AssertionError(f"image argument dropped from the exec: {argv}")
    if "local-host" not in argv:
        raise AssertionError(f"CLI args dropped from the exec: {argv}")
    cloudsdk = [a for a in argv if a.startswith("CLOUDSDK_CONFIG")]
    if cloudsdk != ["CLOUDSDK_CONFIG=/workspace/.gcloud"]:
        raise AssertionError(
            f"CLOUDSDK_CONFIG must be forwarded exactly once, fixed to the repo-local path: {argv}"
        )
    # The image must come after the last -e (so it's not swallowed as a flag
    # value) and the CLI args must come after the image.
    image_idx = argv.index("krops-toolbox:test")
    cli_idx = argv.index("local-host")
    if not (image_idx < cli_idx):
        raise AssertionError(f"argument order wrong: {argv}")


def test_operator_override_of_immutable_key_warns_and_is_ignored() -> None:
    argv, stderr = run_once({"CLOUDSDK_CONFIG": "/home/operator/.gcloud"})
    cloudsdk = [a for a in argv if a.startswith("CLOUDSDK_CONFIG")]
    if cloudsdk != ["CLOUDSDK_CONFIG=/workspace/.gcloud"]:
        raise AssertionError(
            f"an operator-set CLOUDSDK_CONFIG must still be overridden by the fixed value: {argv}"
        )
    if "CLOUDSDK_CONFIG" not in stderr or "/home/operator/.gcloud" not in stderr:
        raise AssertionError(
            f"overriding an immutable key must warn on stderr instead of silently doing nothing: {stderr!r}"
        )


def main() -> int:
    tests = [
        test_image_and_cli_args_reach_docker,
        test_operator_override_of_immutable_key_warns_and_is_ignored,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok - {test.__name__}")
        except AssertionError as exc:
            print(f"FAILED: {test.__name__}: {exc}", file=sys.stderr)
            failed += 1
    if failed:
        return 1
    print("toolbox-run.sh exec OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
