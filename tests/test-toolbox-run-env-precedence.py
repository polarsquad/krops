#!/usr/bin/env python3
"""scripts/toolbox-run.sh must load .env before resolving the engine/socket
(issue #257), and must resolve .env the way mise's env_file does: .env wins
over the process environment, with the same line syntax. Otherwise
`scripts/toolbox-run.sh` and `mise run bootstrap` hand the container
different values for the same checkout."""
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
    echo "ENGINE=ENGINE_NAME" > "$STUB_LOG"
    env >> "$STUB_LOG"
    exit 0
    ;;
  *) exit 1 ;;
esac
"""

# One case per mise env_file behavior the wrapper must reproduce; the values
# are what mise 2026.9.12 exports for these lines.
PARSE_CASES = {
    "export KP_EXPORT=exported": ("KP_EXPORT", "exported"),
    "KP_COMMENT=bar # note": ("KP_COMMENT", "bar"),
    "KP_HASH=bar#nospace": ("KP_HASH", "bar#nospace"),
    'KP_DQ="quoted # kept" # trailing': ("KP_DQ", "quoted # kept"),
    "KP_SQ='single' # c": ("KP_SQ", "single"),
    "KP_TRAIL=trail   ": ("KP_TRAIL", "trail"),
    "  KP_LEAD=leading": ("KP_LEAD", "leading"),
    "KP_SPACED = spaced": ("KP_SPACED", "spaced"),
    'KP_EQ="a=b"': ("KP_EQ", "a=b"),
    "KP_EMPTY=": ("KP_EMPTY", ""),
}
TEST_KEYS = ["CONTAINER_ENGINE", "ENGINE_SOCK", "GITHUB_USER", "TOOLBOX_IMAGE"] + [
    key for key, _ in PARSE_CASES.values()
]


def run_wrapper(env_lines: list[str], extra_env=None) -> dict:
    """Run a sandboxed copy of the wrapper against stub docker/podman and
    return the environment the engine's `run` saw (plus ENGINE)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Copy the wrapper so its self-relative REPO_ROOT lands in the sandbox.
        (tmp / "scripts").mkdir()
        wrapper = tmp / "scripts/toolbox-run.sh"
        shutil.copy(WRAPPER, wrapper)
        (tmp / ".env").write_text("\n".join(env_lines) + "\n")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name in ("docker", "podman"):
            stub = bin_dir / name
            stub.write_text(STUB_TEMPLATE.replace("ENGINE_NAME", name))
            os.chmod(stub, 0o755)
        log = tmp / "stub.log"
        env = {k: v for k, v in os.environ.items() if k not in TEST_KEYS}
        env.update(PATH=f"{bin_dir}:{env['PATH']}", STUB_LOG=str(log))
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(wrapper), "bootstrap"],
            cwd=tmp, env=env, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not log.exists():
            raise AssertionError(
                f"wrapper exited {result.returncode} without running the engine\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return dict(line.split("=", 1) for line in log.read_text().splitlines() if "=" in line)


def expect(seen: dict, key: str, want: str) -> None:
    if seen.get(key) != want:
        raise AssertionError(f"{key}: want {want!r}, got {seen.get(key)!r}")


def test_dotenv_engine_takes_effect() -> None:
    seen = run_wrapper(['CONTAINER_ENGINE="podman"'])
    expect(seen, "ENGINE", "podman")
    expect(seen, "CONTAINER_ENGINE", "podman")


def test_dotenv_wins_over_process_env() -> None:
    seen = run_wrapper(['CONTAINER_ENGINE="podman"', "GITHUB_USER=from-dotenv"],
                       {"CONTAINER_ENGINE": "docker", "GITHUB_USER": "from-shell"})
    expect(seen, "ENGINE", "podman")
    expect(seen, "GITHUB_USER", "from-dotenv")


def test_process_env_used_when_dotenv_silent() -> None:
    seen = run_wrapper(["CONTAINER_ENGINE=docker"], {"GITHUB_USER": "from-shell"})
    expect(seen, "GITHUB_USER", "from-shell")


def test_engine_and_socket_are_consistent() -> None:
    """The socket must match the .env-selected engine, not a docker default
    resolved before .env loaded."""
    seen = run_wrapper(['CONTAINER_ENGINE="podman"'])
    expect(seen, "ENGINE_SOCK", "/run/podman/podman.sock")


def test_line_syntax_matches_mise() -> None:
    seen = run_wrapper(["CONTAINER_ENGINE=docker", *PARSE_CASES])
    for key, want in PARSE_CASES.values():
        expect(seen, key, want)


def test_malformed_lines_are_skipped() -> None:
    seen = run_wrapper(["CONTAINER_ENGINE=docker", '1BAD="oops"', "just some garbage text"])
    leaked = [k for k in seen if k.startswith("1BAD") or k.startswith("just")]
    if leaked:
        raise AssertionError(f"malformed .env lines were exported: {leaked}")


def test_mise_agrees() -> None:
    """Cross-check PARSE_CASES and precedence against the real mise, when
    installed (CI's renovate job has it; the expectations above stand alone)."""
    mise = shutil.which("mise")
    if not mise:
        print("   (skipped: mise not on PATH)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / ".env").write_text("\n".join([*PARSE_CASES, "GITHUB_USER=from-dotenv"]) + "\n")
        (tmp / "mise.toml").write_text(
            '[settings]\nenv_file = ".env"\n[tasks.dump]\nrun = "env"\n'
        )
        env = {k: v for k, v in os.environ.items() if k not in TEST_KEYS}
        env.update(GITHUB_USER="from-shell", MISE_AUTO_INSTALL="0")
        subprocess.run([mise, "trust", "-q", str(tmp)], env=env, check=True, capture_output=True)
        out = subprocess.run(
            [mise, "run", "-q", "dump"], cwd=tmp, env=env,
            capture_output=True, text=True, check=True,
        ).stdout
    seen = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    for key, want in PARSE_CASES.values():
        expect(seen, key, want)
    expect(seen, "GITHUB_USER", "from-dotenv")


def main() -> int:
    tests = [
        test_dotenv_engine_takes_effect,
        test_dotenv_wins_over_process_env,
        test_process_env_used_when_dotenv_silent,
        test_engine_and_socket_are_consistent,
        test_line_syntax_matches_mise,
        test_malformed_lines_are_skipped,
        test_mise_agrees,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok - {test.__name__}")
        except (AssertionError, subprocess.CalledProcessError) as exc:
            print(f"FAILED: {test.__name__}: {exc}", file=sys.stderr)
            failed += 1
    if failed:
        return 1
    print("toolbox-run.sh .env precedence OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
