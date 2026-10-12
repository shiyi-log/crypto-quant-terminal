"""Offline checks for the explicit flow imbalance migration helper."""

import unittest

from tools.migrate_flow_imbal_semantics import migrate_extras


class FlowImbalanceMigrationTests(unittest.TestCase):
    def test_legacy_base_is_preserved_and_quote_becomes_canonical(self):
        migrated, status = migrate_extras({
            "volume": 10.0,
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": 0.2,
            "flow_imbal_q": -0.2,
        })
        self.assertEqual(status, "migrated")
        self.assertAlmostEqual(migrated["flow_imbal_base"], 0.2)
        self.assertAlmostEqual(migrated["flow_imbal"], -0.2)
        self.assertAlmostEqual(migrated["flow_imbal_q"], -0.2)

    def test_canonical_quote_is_idempotent(self):
        migrated, status = migrate_extras({
            "volume": 10.0,
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": -0.2,
            "flow_imbal_base": 0.2,
            "flow_imbal_q": -0.2,
        })
        self.assertEqual(status, "already_canonical")
        self.assertAlmostEqual(migrated["flow_imbal"], -0.2)

    def test_conflicting_value_is_not_migrated(self):
        migrated, status = migrate_extras({
            "volume": 10.0,
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": 0.9,
        })
        self.assertIsNone(migrated)
        self.assertEqual(status, "flow_formula_conflict")

    def test_missing_source_fields_are_reported(self):
        migrated, status = migrate_extras({"quote_volume": 100.0})
        self.assertIsNone(migrated)
        self.assertEqual(status, "missing_source_fields")

    def test_top_level_volume_does_not_leak_into_or_delete_extras(self):
        original = {
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": 0.2,
            "custom": {"keep": True},
        }
        migrated, status = migrate_extras(original, volume=10.0)
        self.assertEqual(status, "migrated")
        self.assertNotIn("volume", migrated)
        self.assertEqual(migrated["custom"], {"keep": True})
        self.assertEqual(original, {
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": 0.2,
            "custom": {"keep": True},
        })

    def test_existing_derived_field_conflict_is_not_overwritten(self):
        migrated, status = migrate_extras({
            "volume": 10.0,
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": -0.2,
            "flow_imbal_base": 0.99,
        })
        self.assertIsNone(migrated)
        self.assertEqual(status, "flow_formula_conflict")

    def test_invalid_existing_derived_field_is_not_overwritten(self):
        migrated, status = migrate_extras({
            "volume": 10.0,
            "quote_volume": 100.0,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 40.0,
            "flow_imbal": -0.2,
            "flow_imbal_q": "not-a-number",
        })
        self.assertIsNone(migrated)
        self.assertEqual(status, "flow_formula_conflict")


if __name__ == "__main__":
    unittest.main()
