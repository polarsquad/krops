#!/usr/bin/env python3
"""Offline unit test of the cluster-api group's boundaries using Renovate's engine.

Widening the group to include the digest-pinned CAPI images (#354) risked
either under-matching (images stay split from their release-asset PRs, the
bug this widening fixed) or over-matching (an unrelated docker image, or
CAPZ's one-minor-at-a-time override, gets pulled in by accident).
"""

import unittest

from renovate_harness import apply_package_rules


class ClusterApiGroupingTest(unittest.TestCase):
    def test_cluster_api_family_and_its_images_group_together(self):
        cases = [
            # CAPI release/asset lookups (grouped before and after #354)
            ("kubernetes-sigs/cluster-api", "github-release-attachments", True),
            ("kubernetes-sigs/cluster-api", "github-releases", True),
            ("kubernetes-sigs/cluster-api-addon-provider-helm", "github-releases", True),
            ("kubernetes-sigs/cluster-api-provider-gcp", "github-releases", True),
            # digest-pinned images the #315/#354 CAPI providers deploy (grouped by #354)
            ("registry.k8s.io/cluster-api/cluster-api-controller", "docker", True),
            ("registry.k8s.io/cluster-api/kubeadm-bootstrap-controller", "docker", True),
            ("registry.k8s.io/cluster-api/kubeadm-control-plane-controller", "docker", True),
            ("gcr.io/k8s-staging-cluster-api/capd-manager", "docker", True),
            ("registry.k8s.io/cluster-api-helm/cluster-api-helm-controller", "docker", True),
            # must NOT join: CAPZ's one-minor-at-a-time override takes precedence
            ("kubernetes-sigs/cluster-api-provider-azure", "github-releases", False),
            # must NOT join: unrelated images/deps, including a registry.k8s.io
            # image outside the cluster-api/cluster-api-helm namespaces
            ("quay.io/jetstack/cert-manager-controller", "docker", False),
            ("registry.k8s.io/kube-apiserver", "docker", False),
            ("ghcr.io/fluxcd/source-controller", "docker", False),
        ]
        dependencies = [
            {
                "depName": name, "packageName": name, "datasource": datasource,
                "packageFile": "airgap/zarf.yaml",
            }
            for name, datasource, _ in cases
        ]
        results = apply_package_rules(dependencies)
        self.assertEqual(len(results), len(cases))
        for case, result in zip(cases, results):
            with self.subTest(dependency=case[:2]):
                self.assertEqual(result["groupName"] == "cluster-api", case[2])


if __name__ == "__main__":
    unittest.main()
