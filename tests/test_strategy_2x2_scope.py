"""Focused 2x2 execution scope is declarative and opt-in."""

import copy
import json
import unittest
from pathlib import Path

from strategy_2x2_scope import (
    ARMS,
    EXPERIMENT_ID,
    EXPERIMENT_STATUS,
    FOCUS_STRATEGY_IDS,
    build_scope_metadata,
    candidate_strategy_id,
    select_focus_specs,
)
from strategy_2x2_experiment import build_2x2_candidates


ROOT = Path(__file__).resolve().parents[1]


class Strategy2x2ScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = json.loads(
            (ROOT / "research_multi_strategy" / "strategy_specs.json").read_text(
                encoding="utf-8"
            )
        )
        cls.source_specs = document["strategies"]

    def test_scope_selects_six_explicit_families_without_mutating_source(self):
        source = copy.deepcopy(self.source_specs)
        selected = select_focus_specs(source)
        self.assertEqual([item["strategy_id"] for item in selected], list(FOCUS_STRATEGY_IDS))
        self.assertEqual(source, self.source_specs)
        self.assertEqual(len(selected), 6)

    def test_catalog_has_four_arms_per_family_and_is_disabled(self):
        catalog = build_scope_metadata(self.source_specs)
        self.assertEqual(catalog["experiment_id"], EXPERIMENT_ID)
        self.assertEqual(catalog["status"], EXPERIMENT_STATUS)
        self.assertEqual(catalog["scope"]["candidate_count"], 24)
        self.assertEqual(len(catalog["candidates"]), 24)
        self.assertTrue(all(item["execution_enabled"] is False
                            for item in catalog["candidates"]))
        self.assertEqual({item["arm_id"] for item in catalog["candidates"]},
                         {arm.arm_id for arm in ARMS})

    def test_factor_contrasts_are_explicit_and_ids_are_unique(self):
        catalog = build_scope_metadata(self.source_specs)
        candidates = catalog["candidates"]
        self.assertEqual(len({item["strategy_id"] for item in candidates}), 24)
        for base_id in FOCUS_STRATEGY_IDS:
            rows = [item for item in candidates if item["variant_of"] == base_id]
            self.assertEqual(len(rows), 4)
            self.assertEqual({item["leverage"] for item in rows}, {30, 100})
            self.assertEqual({item["stop_execution_mode"] for item in rows},
                             {"close_confirmed", "intrabar"})
            # The source JSON may not have computed hashes yet.  When a
            # normalized definition does provide one, every arm must retain
            # that same signal-side comparison key.
            self.assertEqual(len({item["signal_rule_hash"] for item in rows}), 1)

    def test_candidate_id_contains_both_execution_factors(self):
        self.assertEqual(candidate_strategy_id("s05_bollinger_fade", "A"),
                         "s05_bollinger_fade_2x2_A_L100_close")
        self.assertEqual(candidate_strategy_id("s17_overnight_range_fade", "D"),
                         "s17_overnight_range_fade_2x2_D_L30_intrabar")
        with self.assertRaises(ValueError):
            candidate_strategy_id("s01_orb_breakout", "A")
        with self.assertRaises(ValueError):
            candidate_strategy_id("s05_bollinger_fade", "Z")

    def test_intrabar_policy_is_ohlc_only_and_conservative(self):
        policy = build_scope_metadata(self.source_specs)["intrabar_policy"]
        self.assertEqual(policy["source"], "5m_ohlc")
        self.assertEqual(policy["same_candle_stop_target"], "stop_first_conservative")
        self.assertEqual(policy["gap_through_stop"], "next_open_price")
        self.assertFalse(policy["tick_data_used"])

    def test_candidate_factory_keeps_signal_hash_and_execution_factors_separate(self):
        candidates = build_2x2_candidates(self.source_specs)
        self.assertEqual(len(candidates), 24)
        for base_id in FOCUS_STRATEGY_IDS:
            rows = [row for row in candidates if row["variant_of"] == base_id]
            self.assertEqual(len({row["canonical_signal_rule_hash"] for row in rows}), 1)
            self.assertEqual(len({row["execution_plan_hash"] for row in rows}), 4)
            self.assertTrue(all(row["execution_enabled"] is False for row in rows))
            self.assertEqual({row["execution_override"]["leverage"] for row in rows},
                             {30, 100})


if __name__ == "__main__":
    unittest.main()
