#!/usr/bin/env python3
"""Offline unit test of the cert-manager group's boundaries using Renovate's engine.

See docs/dependencies.md#managed-surfaces for why cert-manager's chart pin
and its image tags must land in the same Renovate group.
"""

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

    def test_zarf_yaml_chart_version_is_actually_extracted(self):
        # The grouping test above only proves the *rule* groups depName
        # "cert-manager"/datasource "helm" correctly; it can't catch the
        # custom manager regex itself silently breaking and extracting
        # nothing. Verify extraction against the real annotated line.
        result = run_renovate(["airgap/zarf.yaml"])
        self.assertIn("cert-manager", result.dep_names("airgap/zarf.yaml"))
        deps = [
            dep for dep in result.deps_by_file["airgap/zarf.yaml"]
            if dep.get("depName") == "cert-manager"
        ]
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0].get("datasource"), "helm")


if __name__ == "__main__":
    unittest.main()
