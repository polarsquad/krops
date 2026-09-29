#!/usr/bin/env python3
"""Offline unit test of the cert-manager group's boundaries using Renovate's engine.

See docs/dependencies.md#managed-surfaces for why cert-manager's chart pin
and its image tags must land in the same Renovate group.
"""

import re
import sys
import unittest

from renovate_harness import apply_package_rules, run_renovate


class CertManagerGroupingTest(unittest.TestCase):
    def test_chart_and_images_join_the_same_group(self):
        cases = [
            ("cert-manager", "helm", "bootstrap.toml", True),
            ("cert-manager", "helm", "pivot.sh", True),
            ("cert-manager", "helm", "airgap/zarf.yaml", True),
            ("cluster-api-operator", "helm", "bootstrap.toml", True),
            ("quay.io/jetstack/cert-manager-controller", "docker", "airgap/images.txt", True),
            ("quay.io/jetstack/cert-manager-cainjector", "docker", "airgap/images.txt", True),
            ("quay.io/jetstack/cert-manager-webhook", "docker", "airgap/images.txt", True),
            ("quay.io/jetstack/cert-manager-startupapicheck", "docker", "airgap/images.txt", True),
            ("quay.io/jetstack/cert-manager-controller", "docker", "airgap/zarf.yaml", True),
            # guards: must not join by accident
            ("registry.k8s.io/kube-apiserver", "docker", "airgap/images.txt", False),
            ("quay.io/jetstack/cert-manager-acmesolver", "docker", "airgap/images.txt", False),
            ("kube-prometheus-stack", "helm", "bootstrap.toml", False),
        ]
        dependencies = [
            {
                "depName": name, "packageName": name,
                "datasource": datasource, "packageFile": package_file,
            }
            for name, datasource, package_file, _ in cases
        ]
        results = apply_package_rules(dependencies)
        self.assertEqual(len(results), len(cases))
        for case, result in zip(cases, results):
            with self.subTest(dependency=case[:3]):
                self.assertEqual(result["groupName"] == "platform-charts", case[3])

    def test_helm_lookup_strips_v_prefix(self):
        # charts.jetstack.io publishes versions as "v1.21.x". Without
        # extractVersion, Renovate proposes "v1.21.x" as newValue: bare pins
        # (bootstrap.toml, pivot.sh, HelmReleases) would get the wrong "v"
        # prefix; airgap/zarf.yaml's template prepends a literal "v" to
        # newValue, so it would produce "vv1.21.x".
        # Verify that the packageRule carries extractVersion and that it
        # actually strips the prefix.
        # The packageRule has no matchFileNames, so one case covers every pin
        # surface; per-file cases would be duplicates.
        result = apply_package_rules([
            {"depName": "cert-manager", "packageName": "cert-manager",
             "datasource": "helm", "packageFile": "bootstrap.toml"},
        ])[0]
        extract_version = result.get("extractVersion")
        self.assertIsNotNone(
            extract_version,
            msg="extractVersion is not set; Renovate would write a "
                "'v'-prefixed version back to a bare pin",
        )
        # Translate Python-compatible named groups and verify the
        # pattern actually strips the leading 'v'.
        pattern = re.sub(r"\(\?<(\w+)>", r"(?P<\1>", extract_version)
        m = re.fullmatch(pattern, "v1.21.3")
        self.assertIsNotNone(m, msg=f"extractVersion {extract_version!r} did not match 'v1.21.3'")
        self.assertEqual(m.group("version"), "1.21.3")

    def test_zarf_yaml_chart_version_is_actually_extracted(self):
        # The grouping test above only proves the *rule* groups depName
        # "cert-manager"/datasource "helm" correctly; it can't catch the
        # custom manager regex itself silently breaking and extracting
        # nothing. Verify extraction and the lookup-side newValue against the
        # real annotated line, using a downgraded version so there is always
        # an update available to check.
        #
        # Note: run_renovate uses --dry-run=lookup, which does not render the
        # autoReplaceStringTemplate, so the write-back path
        # (version: v{{{newValue}}}) is not exercised here.
        def downgrade(path, text):
            # Pin to an old version so Renovate proposes an update and we
            # can assert the proposed newValue has no leading 'v'. Use a
            # regex so this keeps working after the next cert-manager bump.
            replaced, n = re.subn(
                r"(# renovate:.*depName=cert-manager\n\s+version: )v\S+",
                r"\g<1>v1.0.0",
                text,
            )
            if path == "airgap/zarf.yaml":
                self.assertEqual(
                    n, 1,
                    msg=f"expected exactly 1 cert-manager version pin in {path}, matched {n}; "
                        "the annotation format may have changed",
                )
            return replaced

        result = run_renovate(["airgap/zarf.yaml"], transform=downgrade)
        self.assertIn("cert-manager", result.dep_names("airgap/zarf.yaml"))
        deps = [
            dep for dep in result.deps_by_file["airgap/zarf.yaml"]
            if dep.get("depName") == "cert-manager"
        ]
        self.assertEqual(len(deps), 1)
        dep = deps[0]
        self.assertEqual(dep.get("datasource"), "helm")
        # The zarf manager regex consumes the literal 'v' before the capture
        # group, so currentValue is always bare regardless of extractVersion.
        current = dep.get("currentValue", "")
        self.assertFalse(
            current.startswith("v"),
            msg=f"currentValue {current!r} has a leading 'v'; "
                "the custom manager regex should capture without it",
        )
        # The lookup must propose a bare newValue (extractVersion strips the
        # 'v' from the datasource's "v1.x.y" before Renovate writes it back).
        # There should always be an update since v1.0.0 is well below any real release.
        updates = dep.get("updates", [])
        if not updates:
            result.print_diagnostics(sys.stderr)
        self.assertTrue(
            updates,
            msg="no updates proposed for cert-manager v1.0.0 -- "
                "lookup failed? (see Renovate diagnostics above)",
        )
        for update in updates:
            new_value = update.get("newValue", "")
            self.assertFalse(
                new_value.startswith("v"),
                msg=f"proposed newValue {new_value!r} has a leading 'v'; "
                    "extractVersion is not stripping it from the datasource lookup",
            )


if __name__ == "__main__":
    unittest.main()
