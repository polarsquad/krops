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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Renovate records the looked-up package files under `config` in this debug
# event, keyed by manager. The manager name (currently `regex`) is
# intentionally not hard-coded so a future custom-manager migration remains
# visible.
_PACKAGE_FILES_EVENT = "packageFiles with updates"

# Shared across test scripts so repeat lookups hit cache, not GitHub (AGENTS.md).
RENOVATE_CACHE_DIR = Path(tempfile.gettempdir()) / "krops-renovate-harness-cache"


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

    def deps_without_pin_digest(
        self, package_file, excluded_dep_names=None, allowed_datasources=None
    ):
        """Extracted dependencies lacking a valid sha256 pinDigest update."""
        excluded_dep_names = set(excluded_dep_names or ())
        allowed_datasources = set(allowed_datasources or ())
        return [
            f"{dep.get('depName', '<unknown>')}:{dep.get('currentValue', '<unknown>')}"
            for dep in self.deps_by_file[package_file]
            if dep.get("depName") not in excluded_dep_names
            and (
                not allowed_datasources
                or dep.get("datasource") in allowed_datasources
            )
            and not any(
                update.get("updateType") == "pinDigest"
                and update.get("newDigest", "").startswith("sha256:")
                and len(update["newDigest"]) == 71
                and all(char in "0123456789abcdef" for char in update["newDigest"][7:])
                for update in dep.get("updates", [])
            )
        ]

    def pin_digests_changing_tag(self, package_file, excluded_dep_names=None):
        """pinDigest updates whose newValue differs from the current tag."""
        excluded_dep_names = set(excluded_dep_names or ())
        return [
            f"{dep.get('depName')}:{dep.get('currentValue')} -> {update.get('newValue')}"
            for dep in self.deps_by_file[package_file]
            if dep.get("depName") not in excluded_dep_names
            for update in dep.get("updates", [])
            if update.get("updateType") == "pinDigest"
            and update.get("newValue") != dep.get("currentValue")
        ]

    def inconsistent_pin_digests(self, package_files):
        """Image refs given different pinDigest digests across the files."""
        digests = {}
        for package_file in package_files:
            for dep in self.deps_by_file[package_file]:
                for update in dep.get("updates", []):
                    if update.get("updateType") == "pinDigest":
                        key = f"{dep.get('depName')}:{dep.get('currentValue')}"
                        digests.setdefault(key, {})[package_file] = update.get("newDigest")
        return {
            key: by_file
            for key, by_file in digests.items()
            if len(set(by_file.values())) > 1
        }

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
    env.setdefault("RENOVATE_CACHE_DIR", str(RENOVATE_CACHE_DIR))
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


def apply_package_rules(dependencies, repo_root=REPO_ROOT):
    """Apply the real config with Renovate's rule engine, offline (Node >=24.11).

    Resolve modules beside the installed CLI so local and CI tests use the same
    Renovate version without maintaining a second JavaScript dependency install.
    """
    executable = shutil.which("renovate")
    if executable is None:
        raise RuntimeError("renovate must be installed and on PATH")
    renovate_root = Path(executable).resolve().parent.parent
    script = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
const [root, configPath] = process.argv.slice(1);
const require = createRequire(`${root}/package.json`);
const JSON5 = require('json5');
const { applyPackageRules } = await import(
    pathToFileURL(`${root}/dist/util/package-rules/index.js`).href
);
const { packageRules } = JSON5.parse(fs.readFileSync(configPath, 'utf8'));
const dependencies = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];
for (const dependency of dependencies) {
    const result = await applyPackageRules({ ...dependency, packageRules });
    results.push({
        groupName: result.groupName ?? null,
        separateMajorMinor: result.separateMajorMinor ?? null,
    });
}
process.stdout.write(JSON.stringify(results));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         str(renovate_root), str(repo_root / "renovate.json5")],
        input=json.dumps(dependencies), text=True, capture_output=True,
        check=True, env={**os.environ, "LOG_LEVEL": "fatal"},
    )
    return json.loads(completed.stdout)


def render_auto_replacements(repo_root=REPO_ROOT):
    """Render every regex manager's autoReplaceStringTemplate, offline.

    Each dep extracted from the tracked files is rendered as a no-op update
    (newValue/newDigest equal to the current ones) through the same compile
    call Renovate's auto-replace uses. Only Renovate's fixed match fields reach
    the template context, so a template that relies on any other capture group
    renders differently from the matched text.
    """
    executable = shutil.which("renovate")
    if executable is None:
        raise RuntimeError("renovate must be installed and on PATH")
    renovate_root = Path(executable).resolve().parent.parent
    script = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
const [root, repoRoot] = process.argv.slice(1);
const require = createRequire(`${root}/package.json`);
const JSON5 = require('json5');
const load = (path) => import(pathToFileURL(`${root}/dist/${path}`).href);
const { extractPackageFile } = await load('modules/manager/custom/regex/index.js');
const { compile } = await load('util/template/index.js');
const { matchRegexOrGlobList } = await load('util/string-match.js');
const { customManagers } = JSON5.parse(
    fs.readFileSync(`${repoRoot}/renovate.json5`, 'utf8'),
);
const files = execFileSync('git', ['ls-files'], { cwd: repoRoot, encoding: 'utf8' })
    .split('\n').filter(Boolean);
const results = [];
for (const manager of customManagers) {
    if (manager.customType !== 'regex' || !manager.autoReplaceStringTemplate) continue;
    const config = { matchStringsStrategy: 'any', ...manager };
    for (const file of files.filter((f) => matchRegexOrGlobList(f, manager.managerFilePatterns))) {
        const content = fs.readFileSync(`${repoRoot}/${file}`, 'utf8');
        const extracted = await extractPackageFile(content, file, config);
        for (const dep of extracted?.deps ?? []) {
            const upgrade = {
                ...config, ...dep,
                newValue: dep.currentValue, newDigest: dep.currentDigest,
            };
            results.push({
                manager: manager.description ?? manager.matchStrings[0],
                file,
                depName: dep.depName ?? null,
                replaceString: dep.replaceString,
                rendered: compile(manager.autoReplaceStringTemplate, upgrade, false),
            });
        }
    }
}
process.stdout.write(JSON.stringify(results));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         str(renovate_root), str(repo_root)],
        text=True, capture_output=True,
        check=True, env={**os.environ, "LOG_LEVEL": "fatal"},
    )
    return json.loads(completed.stdout)
