"""Opt-in cost-gated accounts remain separate from frozen controls."""

import unittest

from strategy_fee_schedule import DEMO_FEE_MODEL_VERSION, MAKER_FEE_PER_SIDE
from strategy_lab_service import load_cost_adjusted_specs, load_specs, normalize_account_spec
from strategy_simulation import execution_identity
from strategy_signals import BAR_MS, BTC, ETH, evaluate


class CostAdjustedPopulationTests(unittest.TestCase):
    def test_factory_adds_three_parallel_research_identities(self):
        legacy = load_specs()
        adjusted = load_cost_adjusted_specs()
        self.assertEqual({spec["variant_of"] for spec in adjusted}, {
            "s04_btc_eth_pairs", "s05_bollinger_fade", "s08_wick_rejection",
        })
        self.assertEqual(len(adjusted), 3)
        self.assertTrue(all(spec["strategy_id"].endswith("_Cost4bp")
                            for spec in adjusted))
        self.assertTrue(all(spec["params"]["min_expected_move_bps"] == 4.0
                            for spec in adjusted))
        self.assertTrue(all(spec["fee_model_version"] == DEMO_FEE_MODEL_VERSION
                            for spec in adjusted))
        self.assertTrue(all(spec["fee_rate_per_side"] == MAKER_FEE_PER_SIDE
                            for spec in adjusted))
        legacy_ids = {spec["strategy_id"] for spec in legacy}
        self.assertFalse(legacy_ids & {spec["strategy_id"] for spec in adjusted})

    def test_old_controls_keep_hash_and_new_execution_contract_is_distinct(self):
        legacy = {spec["variant_of"]: spec for spec in load_specs()
                  if spec.get("variant_of") in {
                      "s04_btc_eth_pairs", "s05_bollinger_fade", "s08_wick_rejection",
                  }}
        adjusted = {spec["variant_of"]: spec for spec in load_cost_adjusted_specs()}
        for canonical in adjusted:
            with self.subTest(canonical=canonical):
                old_identity = execution_identity(legacy[canonical])
                new_identity = execution_identity(adjusted[canonical])
                self.assertNotEqual(legacy[canonical]["rule_hash"],
                                    adjusted[canonical]["rule_hash"])
                self.assertNotEqual(old_identity["execution_hash"],
                                    new_identity["execution_hash"])
                self.assertEqual(new_identity["execution"]["fee_model_version"],
                                 DEMO_FEE_MODEL_VERSION)
                self.assertEqual(new_identity["execution"]["fee_rate_per_side"],
                                 MAKER_FEE_PER_SIDE)

    def test_factory_output_is_idempotently_normalizable(self):
        for spec in load_cost_adjusted_specs():
            with self.subTest(strategy_id=spec["strategy_id"]):
                normalized = normalize_account_spec(spec)
                self.assertEqual(normalized["rule_hash"], spec["rule_hash"])
                self.assertEqual(normalized["params"]["min_expected_move_bps"], 4.0)

    def test_move_above_threshold_keeps_the_original_s05_entry(self):
        rows = []
        for index in range(100):
            close = 99.0 if index % 2 else 101.0
            rows.append({"timestamp": index * 900_000, "open": close,
                         "high": close + 0.5, "low": close - 0.5,
                         "close": close, "volume": 100.0,
                         "extras": {"closed": True}})
        rows[-1]["close"] = 95.0
        rows[-1]["open"] = 95.0
        rows[-1]["high"] = 95.5
        rows[-1]["low"] = 94.5
        spec = next(item for item in load_cost_adjusted_specs()
                    if item["variant_of"] == "s05_bollinger_fade")
        result = evaluate(spec, {BTC: rows, ETH: [dict(row) for row in rows]})
        self.assertEqual(result["signals"][BTC]["target_side"], 1)
        self.assertGreater(result["signals"][BTC]["evidence"]["expected_move_bps"], 4.0)


if __name__ == "__main__":
    unittest.main()
