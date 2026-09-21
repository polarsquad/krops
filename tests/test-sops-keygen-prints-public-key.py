#!/usr/bin/env python3
"""mise run sops-keygen must print the generated PUBLIC key (#423): age-keygen
1.3.x has no -p flag, so the old fallback printed an empty line."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Copy only the task file: mise reads mise.toml from cwd, so the run
        # (and the age.agekey it writes) stays inside the sandbox.
        shutil.copy(REPO_ROOT / "mise.toml", tmp / "mise.toml")
        env = dict(os.environ, MISE_AUTO_INSTALL="0", MISE_TRUSTED_CONFIG_PATHS=str(tmp))
        subprocess.run(["mise", "trust", "-q", str(tmp / "mise.toml")], env=env, check=False)
        result = subprocess.run(
            ["mise", "run", "sops-keygen"], cwd=tmp, env=env,
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print(f"sops-keygen failed: {result.stderr}", file=sys.stderr)
            return 1
        if not (tmp / "age.agekey").is_file():
            print("age.agekey was not written", file=sys.stderr)
            return 1
        if not re.search(r"^Public key: age1[0-9a-z]{58}$", result.stdout, re.M):
            print(f"public key line missing or empty:\n{result.stdout}", file=sys.stderr)
            return 1
        second = subprocess.run(
            ["mise", "run", "sops-keygen"], cwd=tmp, env=env,
            capture_output=True, text=True, check=False,
        )
        if second.returncode == 0:
            print("second run must refuse to overwrite age.agekey", file=sys.stderr)
            return 1
    print("sops-keygen prints the public key and refuses to overwrite OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
