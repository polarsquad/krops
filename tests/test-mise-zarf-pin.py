#!/usr/bin/env python3
"""Verify the zarf mise pin's asset_pattern resolves to a real release asset (issue #324).

mise's github backend needs asset_pattern to match the exact filename of a
release asset; see docs/dependencies.md's "Intentional differences" for why
this pin is written the way it is. This actually installs zarf via mise and
checks the reported version matches the pin, rather than just re-checking
the template string.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"


def main() -> int:
    tools = tomllib.loads(MISE_TOML.read_text()).get("tools", {})
    pinned_version = tools.get("github:zarf-dev/zarf", {}).get("version")
    if not pinned_version:
        print("zarf mise pin FAILED: no github:zarf-dev/zarf version pin found in mise.toml")
        return 1

    result = subprocess.run(
        ["mise", "x", "--", "zarf", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"zarf mise pin FAILED: `mise x -- zarf version` exited {result.returncode}")
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return 1

    installed_version = result.stdout.strip()
    if installed_version != f"v{pinned_version}":
        print(
            f"zarf mise pin FAILED: installed zarf reports '{installed_version}', "
            f"pinned version is '{pinned_version}'"
        )
        return 1

    print(f"zarf mise pin OK ({pinned_version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
