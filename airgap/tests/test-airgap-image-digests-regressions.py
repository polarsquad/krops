#!/usr/bin/env python3
"""Offline regressions for air-gap image-source coverage (issue #170)."""
import contextlib
import importlib.util
import io
import itertools
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).with_name("test-airgap-image-digests.py")
DIGEST = "sha256:" + "a" * 64
PINNED = f"registry.example.com/base:1.0.0@{DIGEST}"
NEW_REF = f"registry.example.com/new:2.0.0@{DIGEST}"
LEGACY_REF = "registry.example.com/legacy:0.0.1"
MODULES = itertools.count()


def zarf_inventory(ref):
    # Exact indentation the gate's zarf.yaml parser expects.
    return f"components:\n  - name: fixture\n    images:\n      - {ref}\n"


class Fixture:
    """A throwaway git repository shaped like the krops airgap layout."""

    def __init__(self, root, legacy=False):
        self.root = Path(root)
        for sub in (
            "airgap/tests",
            "airgap/scripts/nested",
            "airgap/values",
            "airgap/rendered",
            "airgap/archives",
            "airgap/sbom",
            "airgap/manifests",
        ):
            (self.root / sub).mkdir(parents=True)
        inventory = PINNED + ("\n" + LEGACY_REF if legacy else "") + "\n"
        (self.root / "airgap" / "images.txt").write_text(inventory)
        (self.root / "airgap" / "zarf.yaml").write_text(zarf_inventory(PINNED))
        (self.root / "airgap" / "manifests" / "flux-instance.yaml").write_text(
            f"image: {PINNED}\n"
        )
        (self.root / "airgap" / "scripts" / "build.sh").write_text(
            f"#!/bin/sh\nIMAGE={PINNED}\n"
        )
        (self.root / "airgap" / "scripts" / "nested" / "helper.sh").write_text(
            f"#!/bin/sh\nIMAGE={PINNED}\n"
        )
        # Committed content under every documented exclusion prefix.
        (self.root / "airgap" / "values" / "legacy.yaml").write_text(
            f"image: {PINNED}\n"
        )
        (self.root / "airgap" / "rendered" / "out.yaml").write_text(
            f"image: {PINNED}\n"
        )
        (self.root / "airgap" / "sbom" / "scan.json").write_text(
            f'"image": "{PINNED}"\n'
        )
        # The gate itself lives under the ignored airgap/tests/ prefix and
        # contains image references (LOCAL_EXCEPTIONS) in its own source.
        shutil.copy(SOURCE, self.root / "airgap" / "tests" / SOURCE.name)
        (self.root / "README.md").write_text("# fixture\n")
        self._git("init", "-q", "-b", "main")
        self.commit("base")

    def _git(self, *args):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.com",
                *args,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self._git("add", "-A")
        self._git("commit", "-qm", message)

    def branch(self, name):
        self._git("checkout", "-qb", name)

    def run(self, *args):
        spec = importlib.util.spec_from_file_location(
            f"image_digests_{next(MODULES)}", self.root / "airgap" / "tests" / SOURCE.name
        )
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(
            sys, "argv", [str(self.root / "airgap" / "tests" / SOURCE.name)] + list(args)
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = gate.main()
        return result, stdout.getvalue(), stderr.getvalue()


class NewSourceDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_repository_passes_every_mode(self):
        for args in ([], ["--all"], ["--changed-from", "main"]):
            with self.subTest(args=args):
                result, _, stderr = self.fixture.run(*args)
                self.assertEqual(result, 0, stderr)

    def test_untracked_yaml_is_rejected_with_line_and_reference(self):
        self.fixture.write("airgap/new-list.yaml", f"images:\n  - {NEW_REF}\n")
        result, _, stderr = self.fixture.run()
        self.assertEqual(result, 1)
        self.assertIn(
            f"airgap/new-list.yaml:2: uncovered image-bearing source "
            f"(first reference: {NEW_REF})",
            stderr,
        )
        self.assertIn("IGNORED_AIRGAP_PREFIXES", stderr)

    def test_untracked_python_is_rejected(self):
        self.fixture.write("airgap/new-list.py", f'IMAGE = "{NEW_REF}"\n')
        result, _, stderr = self.fixture.run()
        self.assertEqual(result, 1)
        self.assertIn("airgap/new-list.py:1: uncovered image-bearing source", stderr)

    def test_all_mode_rejects_unregistered_source(self):
        self.fixture.write("airgap/new-list.yaml", f"images:\n  - {NEW_REF}\n")
        for committed in (False, True):
            with self.subTest(committed=committed):
                if committed:
                    self.fixture.commit("add new list")
                result, _, stderr = self.fixture.run("--all")
                self.assertEqual(result, 1)
                self.assertIn("airgap/new-list.yaml:2: uncovered image-bearing source", stderr)

    def test_committed_yaml_is_rejected_in_changed_from_mode(self):
        self.fixture.branch("work")
        self.fixture.write("airgap/new-list.yaml", f"images:\n  - {NEW_REF}\n")
        self.fixture.commit("add new list")
        result, _, stderr = self.fixture.run("--changed-from", "main")
        self.assertEqual(result, 1)
        self.assertIn("airgap/new-list.yaml:2: uncovered image-bearing source", stderr)

    def test_all_mode_enumerates_nested_registered_scripts(self):
        self.fixture.write(
            "airgap/scripts/nested/helper.sh",
            "#!/bin/sh\nIMAGE=registry.example.com/nested:0.0.1\n",
        )
        result, _, stderr = self.fixture.run("--all")
        self.assertEqual(result, 1)
        self.assertIn(
            "airgap/scripts/nested/helper.sh:2: unpinned image: "
            "registry.example.com/nested:0.0.1",
            stderr,
        )

    def test_documented_exclusions_are_not_flagged(self):
        # Untracked content under an excluded prefix must pass too.
        self.fixture.write("airgap/values/untracked.yaml", f"image: {NEW_REF}\n")
        for args in ([], ["--all"]):
            with self.subTest(args=args):
                result, _, stderr = self.fixture.run(*args)
                self.assertEqual(result, 0, stderr)

    def test_generated_config_artifact_tree_is_not_flagged(self):
        # build-config-artifact.sh leaves a gitignored generated copy of the
        # manifests under airgap/config-artifact/ (e.g. the verbatim
        # kindnet.yaml with a tagged, unpinned image); the working-tree walk
        # must not flag it as an uncovered source (issue #170 review).
        self.fixture.write(
            "airgap/config-artifact/mgmt/local-host/addons/cni/kindnet.yaml",
            "image: docker.io/kindest/kindnetd:v20260528-9350166c\n",
        )
        for args in ([], ["--all"]):
            with self.subTest(args=args):
                result, _, stderr = self.fixture.run(*args)
                self.assertEqual(result, 0, stderr)

    def test_new_source_without_references_passes(self):
        self.fixture.write("airgap/notes.yaml", "count: 1\n")
        for args in ([], ["--all"]):
            with self.subTest(args=args):
                result, _, stderr = self.fixture.run(*args)
                self.assertEqual(result, 0, stderr)


class LegacyPinTests(unittest.TestCase):
    """Changed-file mode must not force migration of untouched legacy pins."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self._tmp.name), legacy=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_changed_mode_skips_untouched_legacy_pins(self):
        self.fixture.branch("work")
        build = self.fixture.root / "airgap" / "scripts" / "build.sh"
        self.fixture.write("airgap/scripts/build.sh", build.read_text() + "# tweak\n")
        self.fixture.commit("tweak build.sh")
        result, _, stderr = self.fixture.run("--changed-from", "main")
        self.assertEqual(result, 0, stderr)

    def test_changed_mode_still_enforces_changed_registered_sources(self):
        self.fixture.branch("work")
        self.fixture.write(
            "airgap/images.txt",
            (self.fixture.root / "airgap" / "images.txt").read_text()
            + f"ghcr.io/example/added:1.0.0@{DIGEST}\n",
        )
        self.fixture.commit("add pinned image")
        result, _, stderr = self.fixture.run("--changed-from", "main")
        self.assertEqual(result, 1)
        self.assertIn(
            f"airgap/images.txt:2: unpinned image: {LEGACY_REF}", stderr
        )

    def test_all_mode_enforces_legacy_pins(self):
        result, _, stderr = self.fixture.run("--all")
        self.assertEqual(result, 1)
        self.assertIn(f"airgap/images.txt:2: unpinned image: {LEGACY_REF}", stderr)


if __name__ == "__main__":
    unittest.main()
