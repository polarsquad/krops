#!/usr/bin/env python3
"""Offline check that every autoReplaceStringTemplate round-trips its match."""

import unittest

from renovate_harness import render_auto_replacements


class ReplaceTemplatesTest(unittest.TestCase):
    def test_no_op_update_reproduces_matched_text(self):
        results = render_auto_replacements()
        self.assertTrue(results, "no regex-manager deps with a replacement template extracted")
        for result in results:
            with self.subTest(manager=result["manager"], file=result["file"], dep=result["depName"]):
                self.assertEqual(result["rendered"], result["replaceString"])


if __name__ == "__main__":
    unittest.main()
