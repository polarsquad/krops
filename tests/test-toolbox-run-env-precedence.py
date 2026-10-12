#!/usr/bin/env python3
"""scripts/toolbox-run.sh must load .env before resolving the engine/socket
(issue #257), and must resolve .env the way mise's env_file does: .env wins
over the process environment, for the supported single-line syntax. Otherwise
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
    # Simulate container engine: export -e KEY=VALUE args like a real engine
    # would inject them into the container's environment.
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -e)
          shift
          case "$1" in
            *=*)
              _k="${1%%=*}"
              _v="${1#*=}"
              export "${_k}=${_v}"
              ;;
          esac
          ;;
      esac
      shift
    done
    printf 'ENGINE=ENGINE_NAME\\0' > "$STUB_LOG"
    env -0 >> "$STUB_LOG"
    exit 0
    ;;
  *) exit 1 ;;
esac
"""

# One case per mise env_file behavior the wrapper must reproduce; the values
# are cross-checked against real mise (verified with 2026.10.5 and 2026.10.8).
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
PARSE_CASES.update({
    r"AGE_KEY_FILE=keys\ private.txt": ("AGE_KEY_FILE", "keys private.txt"),
    r'GITHUB_USER="foo\"bar"': ("GITHUB_USER", 'foo"bar'),
    "KP_PARENT=alice": ("KP_PARENT", "alice"),
    "OCI_REPOSITORY=ghcr.io/${KP_PARENT}/krops": ("OCI_REPOSITORY", "ghcr.io/alice/krops"),
    "KP_BARE=$KP_PARENT/path": ("KP_BARE", "alice/path"),
    'KP_DOUBLE="$KP_PARENT/${KP_PARENT}"': ("KP_DOUBLE", "alice/alice"),
    r'KP_DOUBLE_LITERAL="\$KP_PARENT"': ("KP_DOUBLE_LITERAL", "$KP_PARENT"),
    "KP_INHERITED=${KP_EXTERNAL}": ("KP_INHERITED", "inherited"),
    "KP_UNSET=${KP_NOT_SET}": ("KP_UNSET", ""),
    r"KP_LITERAL=\$KP_PARENT": ("KP_LITERAL", "$KP_PARENT"),
    "KP_SINGLE='${KP_PARENT}'": ("KP_SINGLE", "${KP_PARENT}"),
    r'KP_BACKSLASH="a\\b"': ("KP_BACKSLASH", r"a\b"),
    r'KP_NEWLINE="a\nb"': ("KP_NEWLINE", "a\nb"),
    r'KP_CONTROL="a\tb\rc"': ("KP_CONTROL", "a\tb\rc"),
    r'KP_TRAILING_NEWLINE="tail\n"': ("KP_TRAILING_NEWLINE", "tail\n"),
    r'KP_UNKNOWN="a\qb"': ("KP_UNKNOWN", r"a\qb"),
    'KP_QUOTED_SPACE="a " # note': ("KP_QUOTED_SPACE", "a "),
    r"KP_ESCAPED_COMMENT=foo\ # note": ("KP_ESCAPED_COMMENT", "foo "),
    "KP_ESCAPED_TRAIL=foo\\  ": ("KP_ESCAPED_TRAIL", "foo "),
    "KP_REPEAT=first": ("KP_REPEAT", "second"),
    "KP_REPEAT=second": ("KP_REPEAT", "second"),
    "KP_AFTER=$KP_REPEAT": ("KP_AFTER", "second"),
})

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
        env = {k: v for k, v in os.environ.items() if k not in TEST_KEYS and not k.startswith("KP_")}
        env.update(KP_EXTERNAL="inherited", PATH=f"{bin_dir}:{env['PATH']}", STUB_LOG=str(log), HOME=str(tmp / "home"))
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
        return dict(line.split("=", 1) for line in log.read_bytes().decode().split("\0") if "=" in line)


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


def test_shell_content_is_not_executed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "executed"
        command = f"$(touch {sentinel})"
        backticks = f"`touch {sentinel}`"
        seen = run_wrapper(["CONTAINER_ENGINE=docker", f"KP_COMMAND={command}",
                            f"KP_BACKTICKS={backticks}", "KP_LITERAL_REF=${KP_COMMAND}"])
        expect(seen, "KP_COMMAND", command)
        expect(seen, "KP_BACKTICKS", backticks)
        expect(seen, "KP_LITERAL_REF", command)
        if sentinel.exists():
            raise AssertionError("dotenv content executed a command")


def test_unsupported_single_quote_escape_is_skipped() -> None:
    seen = run_wrapper(["CONTAINER_ENGINE=docker", r"KP_UNSUPPORTED='foo\'bar'"])
    if "KP_UNSUPPORTED" in seen:
        raise AssertionError("unsupported single-quoted escape was exported")


def test_mise_agrees() -> None:
    """Cross-check PARSE_CASES and precedence against the real mise, when
    installed (CI's renovate job has it; the expectations above stand alone)."""
    mise = shutil.which("mise")
    if not mise:
        if "--require-mise" in sys.argv:
            raise AssertionError("mise is required but not on PATH")
        print("   (skipped: mise not on PATH)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / ".env").write_text("\n".join([*PARSE_CASES, "GITHUB_USER=from-dotenv"]) + "\n")
        (tmp / "mise.toml").write_text(
            '[settings]\nenv_file = ".env"\n[tasks.dump]\nrun = "env -0"\n'
        )
        env = {k: v for k, v in os.environ.items() if k not in TEST_KEYS and not k.startswith("KP_")}
        env.update(GITHUB_USER="from-shell", KP_EXTERNAL="inherited", MISE_AUTO_INSTALL="0",
                   MISE_CONFIG_DIR=str(tmp / "config"), MISE_DATA_DIR=str(tmp / "data"),
                   MISE_CACHE_DIR=str(tmp / "cache"), MISE_STATE_DIR=str(tmp / "state"))
        subprocess.run([mise, "trust", "-q", str(tmp)], env=env, check=True, capture_output=True)
        out = subprocess.run(
            [mise, "run", "-q", "dump"], cwd=tmp, env=env,
            capture_output=True, check=True,
        ).stdout.decode()
    seen = dict(line.split("=", 1) for line in out.split("\0") if "=" in line)
    for key, want in PARSE_CASES.values():
        expect(seen, key, "from-dotenv" if key == "GITHUB_USER" else want)
    expect(seen, "GITHUB_USER", "from-dotenv")


def main() -> int:
    tests = [
        test_dotenv_engine_takes_effect,
        test_dotenv_wins_over_process_env,
        test_process_env_used_when_dotenv_silent,
        test_engine_and_socket_are_consistent,
        test_line_syntax_matches_mise,
        test_malformed_lines_are_skipped,
        test_shell_content_is_not_executed,
        test_unsupported_single_quote_escape_is_skipped,
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
