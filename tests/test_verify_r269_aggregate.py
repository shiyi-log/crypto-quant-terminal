"""Unit checks for the independent R259 aggregate-loss verifier."""

import unittest

from tools.verify_r269_aggregate import Trade, summarize


class R259AggregateVerifierTests(unittest.TestCase):
    def test_detects_single_compliant_aggregate_breach(self):
        report = summarize([
            Trade("s08_R50", 1000, -38.0, 50.0),
            Trade("s08_R50", 1000, -31.8, 50.0),
        ])
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["single_breach_count"], 0)
        self.assertEqual(report["aggregate_breach_count"], 1)
        self.assertEqual(report["single_compliant_aggregate_breach_count"], 1)
        self.assertFalse(report["aggregate_limit_satisfied"])

    def test_compliant_group_passes_and_different_caps_do_not_merge(self):
        report = summarize([
            Trade("s08_R50", 1000, -20.0, 50.0),
            Trade("s08_R50", 1000, 10.0, 50.0),
            Trade("s08_R50", 1000, -20.0, 200.0),
        ])
        self.assertEqual(report["group_count"], 2)
        self.assertEqual(report["single_breach_count"], 0)
        self.assertEqual(report["aggregate_breach_count"], 0)
        self.assertTrue(report["aggregate_limit_satisfied"])

    def test_single_breach_is_not_counted_as_aggregate_only(self):
        report = summarize([
            Trade("s03_R50", 1000, -60.0, 50.0),
            Trade("s03_R50", 1000, 1.0, 50.0),
        ])
        self.assertEqual(report["single_breach_count"], 1)
        self.assertEqual(report["aggregate_breach_count"], 1)
        self.assertEqual(report["single_compliant_aggregate_breach_count"], 0)


if __name__ == "__main__":
    unittest.main()
