"""Shared harness for Renovate integration tests.

Runs Renovate with --platform=local --dry-run=lookup against a temp fixture
that copies the real renovate.json5 plus selected repo files, parses the JSON
log event stream, and returns per-file dependency/update data for assertions.
No writes, no branches, no PRs (lookup only).

Node requirement: Renovate 44.50.1 declares Node ^24.11.0 in `engines`
(RegExp.escape). CI sets
up Node 24 in the validate.yml renovate job; locally run under a Node 24
toolchain, e.g. `mise x node@24 -- python3 tests/test-renovate-coverage.py`.

A test built on this harness is a file list (plus an optional per-file text
transform) and assertions against RenovateResult; no subprocess or parsing
logic lives in the tests (issue #97).
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]

# Renovate records the looked-up package files under `config` in this debug
# event, keyed by manager. The manager name (currently `regex`) is
# intentionally not hard-coded so a future custom-manager migration remains
# visible.
_PACKAGE_FILES_EVENT = "packageFiles with updates"


class RenovateResult:
    """Parsed outcome of one harness Renovate run."""

    def __init__(self, returncode, fixture_files):
        self.returncode = returncode
        self.deps_by_file = {path: [] for path in fixture_files}
        self.diagnostics = []

    def dep_names(self, package_file):
        """depName strings Renovate extracted from one fixture file."""
        return {
            dep["depName"]
            for dep in self.deps_by_file[package_file]
            if dep.get("depName")
        }

    def pin_digest_count(self, package_file):
        """Number of pinDigest updates (sha256) proposed for one file."""
        return sum(
            update.get("updateType") == "pinDigest"
            and update.get("newDigest", "").startswith("sha256:")
            for dep in self.deps_by_file[package_file]
            for update in dep.get("updates", [])
        )

    def deps_without_pin_digest(self, package_file, excluded_dep_names=None):
        """Extracted dependencies lacking a valid sha256 pinDigest update."""
        excluded_dep_names = set(excluded_dep_names or ())
        return [
            f"{dep.get('depName', '<unknown>')}:{dep.get('currentValue', '<unknown>')}"
            for dep in self.deps_by_file[package_file]
            if dep.get("depName") not in excluded_dep_names
            and not any(
                update.get("updateType") == "pinDigest"
                and update.get("newDigest", "").startswith("sha256:")
                and len(update["newDigest"]) == 71
                and all(char in "0123456789abcdef" for char in update["newDigest"][7:])
                for update in dep.get("updates", [])
            )
        ]

    def print_diagnostics(self, stream=None):
        stream = stream or sys.stderr
        if self.diagnostics:
            print("Renovate diagnostics:", file=stream)
            for diagnostic in self.diagnostics[-20:]:
                print(f"  - {diagnostic}", file=stream)


def run_renovate(fixture_files, transform=None, repo_root=REPO_ROOT):
    """Run Renovate over a fixture of repo files and parse the event stream.

    fixture_files: iterable of repo-relative paths to copy into the fixture.
    transform: optional callable (relative_path, text) -> text applied to each
        copied file (e.g. stripping digests to test pin proposals).
    Returns a RenovateResult with deps_by_file populated for every fixture
        file (empty list when Renovate extracted nothing from it).
    """
    fixture_files = sorted(fixture_files)
    env = os.environ.copy()
    env.update(
        {
            "LOG_FORMAT": "json",
            "LOG_LEVEL": "debug",
            "RENOVATE_ENABLED_MANAGERS": "custom.regex",
            "RENOVATE_INCLUDE_PATHS": json.dumps(fixture_files),
        }
    )

    with tempfile.TemporaryDirectory(prefix="renovate-harness-") as temp:
        fixture_root = Path(temp)
        shutil.copy2(repo_root / "renovate.json5", fixture_root / "renovate.json5")
        for relative in fixture_files:
            source = repo_root / relative
            target = fixture_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            text = source.read_text()
            if transform is not None:
                text = transform(relative, text)
            target.write_text(text)

        completed = subprocess.run(
            ["renovate", "--platform=local", "--dry-run=lookup"],
            check=False,
            cwd=fixture_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    return _parse(completed.returncode, completed.stdout, fixture_files)


def _parse(returncode, stdout, fixture_files):
    result = RenovateResult(returncode, fixture_files)
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("level", 0) >= 40:
            result.diagnostics.append(event.get("msg", line))
        if event.get("msg") != _PACKAGE_FILES_EVENT:
            continue

        for manager_files in event.get("config", {}).values():
            for package in manager_files:
                package_file = package.get("packageFile")
                if package_file in result.deps_by_file:
                    result.deps_by_file[package_file].extend(
                        package.get("deps", [])
                    )
    return result
