"""Offline contracts for the orderflow-to-futures field backfill."""

import unittest

from tools.backfill_orderflow_fields import merge_missing, source_extras


class OrderflowBackfillTests(unittest.TestCase):
    def test_source_uses_quote_as_canonical_and_keeps_base_separate(self):
        extras = source_extras({
            "volume": 10.0, "quote_volume": 100.0,
            "taker_buy_base": 6.0, "taker_buy_quote": 40.0,
            "num_trades": 12,
        })
        self.assertAlmostEqual(extras["flow_imbal"], -0.2)
        self.assertAlmostEqual(extras["flow_imbal_q"], -0.2)
        self.assertAlmostEqual(extras["flow_imbal_base"], 0.2)
        self.assertEqual(extras["num_trades"], 12)

    def test_merge_only_fills_missing_fields_and_preserves_existing_values(self):
        merged = merge_missing(
            {"quote_volume": 99.0, "custom": "keep", "flow_imbal": 0.1},
            {"quote_volume": 100.0, "num_trades": 12, "flow_imbal": 0.2},
        )
        self.assertEqual(merged["quote_volume"], 99.0)
        self.assertEqual(merged["flow_imbal"], 0.1)
        self.assertEqual(merged["num_trades"], 12)
        self.assertEqual(merged["custom"], "keep")

    def test_merge_returns_none_when_target_is_complete(self):
        self.assertIsNone(merge_missing({"num_trades": 12}, {"num_trades": 13}))


if __name__ == "__main__":
    unittest.main()
