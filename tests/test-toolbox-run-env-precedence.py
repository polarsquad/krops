#!/usr/bin/env python3
"""scripts/toolbox-run.sh must load .env before resolving the engine/socket,
and process environment must win over .env (issue #257)."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts/toolbox-run.sh"

STUB_TEMPLATE = """#!/usr/bin/env sh
case "$1" in
  info) exit 0 ;;
  --version) echo "ENGINE_NAME version 1.0.0" ;;
  context) exit 0 ;;
  run)
    shift
    echo "ARGV: $*" >> "$STUB_LOG"
    echo "CONTAINER_ENGINE=$CONTAINER_ENGINE" >> "$STUB_LOG"
    echo "ENGINE_SOCK=$ENGINE_SOCK" >> "$STUB_LOG"
    echo "GITHUB_USER=[$GITHUB_USER]" >> "$STUB_LOG"
    echo "---" >> "$STUB_LOG"
    exit 0
    ;;
  *) exit 1 ;;
esac
"""


def make_sandbox(tmp: Path, env_lines: list[str]) -> Path:
    """A fake repo root with a copy of the real wrapper (so its self-relative
    REPO_ROOT computation lands in the sandbox) plus a synthetic .env."""
    scripts_dir = tmp / "scripts"
    scripts_dir.mkdir()
    wrapper = scripts_dir / "toolbox-run.sh"
    shutil.copy(WRAPPER, wrapper)
    os.chmod(wrapper, 0o755)
    (tmp / ".env").write_text("\n".join(env_lines) + "\n")
    return wrapper


def install_stub(bin_dir: Path, name: str) -> None:
    path = bin_dir / name
    path.write_text(STUB_TEMPLATE.replace("ENGINE_NAME", name))
    os.chmod(path, 0o755)


def run_wrapper(tmp: Path, wrapper: Path, extra_env: dict):
    """Run the wrapper against stub docker/podman; returns (result, log)."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    install_stub(bin_dir, "docker")
    install_stub(bin_dir, "podman")
    log = tmp / "stub.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_LOG"] = str(log)
    # Never let a real ambient CONTAINER_ENGINE/GITHUB_USER leak into the test.
    for leaky in ("CONTAINER_ENGINE", "ENGINE_SOCK", "GITHUB_USER", "TOOLBOX_IMAGE"):
        env.pop(leaky, None)
    env.update(extra_env)
    result = subprocess.run(
        ["bash", str(wrapper), "bootstrap"],
        cwd=tmp, env=env, capture_output=True, text=True, check=False,
    )
    seen = log.read_text() if log.exists() else ""
    return result, seen


def expect_ok(result: subprocess.CompletedProcess, seen: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"wrapper exited {result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if "ARGV:" not in seen:
        raise AssertionError(f"wrapper never invoked the engine 'run'\nstderr={result.stderr}")


def test_dotenv_engine_takes_effect_when_unset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="podman"'])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)
        if "CONTAINER_ENGINE=podman" not in seen:
            raise AssertionError(f".env CONTAINER_ENGINE was not applied: {seen}")


def test_process_env_wins_over_dotenv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="podman"'])
        result, seen = run_wrapper(tmp, wrapper, {"CONTAINER_ENGINE": "docker"})
        expect_ok(result, seen)
        if "CONTAINER_ENGINE=docker" not in seen:
            raise AssertionError(f"process env CONTAINER_ENGINE was overridden by .env: {seen}")


def test_engine_and_socket_are_consistent() -> None:
    """The socket resolved for the container must match the .env-selected
    engine, not a stale docker-default resolved before .env loaded."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="podman"'])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)
        if "ENGINE_SOCK=/run/podman/podman.sock" not in seen:
            raise AssertionError(f"ENGINE_SOCK did not match the podman engine: {seen}")


def test_quoted_value_is_stripped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="docker"', 'GITHUB_USER="git"'])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)
        if "GITHUB_USER=[git]" not in seen:
            raise AssertionError(f"quoted .env value was not stripped correctly: {seen}")


def test_unquoted_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="docker"', "GITHUB_USER=git"])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)
        if "GITHUB_USER=[git]" not in seen:
            raise AssertionError(f"unquoted .env value was not applied: {seen}")


def test_empty_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="docker"', "GITHUB_USER="])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)
        if "GITHUB_USER=[]" not in seen:
            raise AssertionError(f"empty .env value handling broke: {seen}")


def test_malformed_leading_digit_key_is_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="docker"', '1BAD="oops"'])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)


def test_line_without_equals_is_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wrapper = make_sandbox(tmp, ['CONTAINER_ENGINE="docker"', "just some garbage text"])
        result, seen = run_wrapper(tmp, wrapper, {})
        expect_ok(result, seen)


def main() -> int:
    tests = [
        test_dotenv_engine_takes_effect_when_unset,
        test_process_env_wins_over_dotenv,
        test_engine_and_socket_are_consistent,
        test_quoted_value_is_stripped,
        test_unquoted_value,
        test_empty_value,
        test_malformed_leading_digit_key_is_skipped,
        test_line_without_equals_is_skipped,
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
    print("toolbox-run.sh .env precedence OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
