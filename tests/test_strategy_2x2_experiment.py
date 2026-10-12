"""The 2x2 factory is opt-in and preserves signal-side invariants."""

import copy
import json
import unittest
from pathlib import Path

from strategy_2x2_experiment import (build_2x2_candidates,
                                     materialize_2x2_execution_spec,
                                     risk_capped_margin, risk_capped_notional)
from strategy_signals import LEGACY_LEVERAGE_CONVENTION, leverage_convention_for
from strategy_simulation import (LEGACY_BUDGET_RULE, RISK_CAP_BUDGET_RULE,
                                  execution_identity)
from strategy_2x2_scope import ARMS, FOCUS_STRATEGY_IDS


class Strategy2x2FactoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "research_multi_strategy" / "strategy_specs.json"
        cls.document = json.loads(path.read_text(encoding="utf-8"))
        cls.source = cls.document["strategies"]

    def test_factory_builds_disabled_four_arm_candidates_without_mutating_source(self):
        source = copy.deepcopy(self.source)
        candidates = build_2x2_candidates(source)
        self.assertEqual(len(candidates), len(FOCUS_STRATEGY_IDS) * len(ARMS))
        self.assertEqual(source, self.source)
        self.assertTrue(all(row["execution_enabled"] is False for row in candidates))
        self.assertTrue(all(row["blocked_execution_reason"] == "manual_activation_required"
                            for row in candidates))

    def test_each_family_shares_signal_identity_but_has_four_execution_plans(self):
        candidates = build_2x2_candidates(self.source)
        for family in FOCUS_STRATEGY_IDS:
            with self.subTest(family=family):
                rows = [row for row in candidates if row["variant_of"] == family]
                self.assertEqual(len(rows), 4)
                self.assertEqual(len({row["canonical_signal_rule_hash"] for row in rows}), 1)
                self.assertEqual(len({row["rule_hash"] for row in rows}), 1)
                self.assertEqual(len({row["execution_plan_hash"] for row in rows}), 4)
                self.assertEqual({row["arm_id"] for row in rows}, {arm.arm_id for arm in ARMS})

    def test_ids_and_overrides_make_factors_visible(self):
        candidates = build_2x2_candidates(self.source)
        for row in candidates:
            self.assertIn("_2x2_", row["strategy_id"])
            override = row["execution_override"]
            self.assertIn(override["leverage"], (30, 100))
            self.assertIn(override["stop_execution_mode"], ("close_confirmed", "intrabar"))
            self.assertIn(f"_L{override['leverage']}_", row["strategy_id"])
            if override["stop_execution_mode"] == "intrabar":
                self.assertIn("_intrabar", row["strategy_id"])
            else:
                self.assertIn("_close", row["strategy_id"])

    def test_risk_capped_notional_does_not_depend_on_leverage(self):
        # This is the sizing invariant for the proposed factor comparison:
        # leverage changes margin and liquidation distance, while the planned
        # notional remains risk-budget limited.
        for stop_ratio in (0.002, 0.005, 0.01):
            with self.subTest(stop_ratio=stop_ratio):
                base = risk_capped_notional(50, stop_ratio, 0.0004)
                margins = {leverage: risk_capped_margin(50, stop_ratio, 0.0004, leverage)
                           for leverage in (30, 100)}
                self.assertEqual(base, margins[30] * 30)
                self.assertEqual(base, margins[100] * 100)
                self.assertLess(margins[30], margins[100] * 4)

    def test_validation_rejects_changed_signal_parameters(self):
        candidates = build_2x2_candidates(self.source)
        candidates[1]["params"]["N"] = 999
        from strategy_2x2_experiment import validate_2x2_candidates

        with self.assertRaisesRegex(ValueError, "invariant params"):
            validate_2x2_candidates(candidates)

    def test_materializer_maps_override_to_execution_identity_without_enabling(self):
        candidates = build_2x2_candidates(self.source)
        materialized = [materialize_2x2_execution_spec(row) for row in candidates]
        self.assertEqual(len(materialized), 24)
        for row in materialized:
            identity = execution_identity(row)
            override = row["execution_override"]
            self.assertEqual(row["execution_enabled"], False)
            self.assertEqual(identity["rule_hash"], row["canonical_signal_rule_hash"])
            self.assertEqual(identity["execution"]["execution_variant"],
                             "risk-execution-2x2-v1")
            self.assertEqual(identity["execution"]["leverage"], override["leverage"])
            self.assertEqual(identity["execution"]["stop_execution_mode"],
                             override["stop_execution_mode"])

    def test_materialized_arms_have_distinct_execution_hashes_and_shared_signal_hash(self):
        candidates = build_2x2_candidates(self.source)
        materialized = [materialize_2x2_execution_spec(row) for row in candidates]
        for family in FOCUS_STRATEGY_IDS:
            rows = [row for row in materialized if row["variant_of"] == family]
            identities = [execution_identity(row) for row in rows]
            with self.subTest(family=family):
                self.assertEqual(len({item["rule_hash"] for item in identities}), 1)
                self.assertEqual(len({item["execution_hash"] for item in identities}), 4)
                models = {row["arm_id"]: identity["execution"]
                          for row, identity in zip(rows, identities)}
                self.assertEqual(models["A"]["leverage"], 100)
                self.assertEqual(models["B"]["leverage"], 30)
                self.assertEqual(models["C"]["stop_execution_mode"], "intrabar")
                self.assertEqual(models["D"]["leverage"], 30)
                self.assertEqual(models["D"]["stop_execution_mode"], "intrabar")

    def test_materialized_descriptions_match_arm_and_risk_sizing_contract(self):
        candidates = build_2x2_candidates(self.source)
        materialized = [materialize_2x2_execution_spec(row) for row in candidates]
        for row in materialized:
            with self.subTest(strategy_id=row["strategy_id"]):
                leverage = row["position_budget"]["leverage"]
                self.assertEqual(row["position_budget"]["leverage_convention"],
                                 leverage_convention_for(leverage))
                identity = execution_identity(row)
                self.assertEqual(identity["execution"]["budget_rule"], RISK_CAP_BUDGET_RULE)
                self.assertIn("independent of leverage", identity["execution"]["budget_rule"])

    def test_description_correction_is_hash_compatible_with_frozen_2x2_rows(self):
        candidates = build_2x2_candidates(self.source)
        for candidate in candidates:
            corrected = materialize_2x2_execution_spec(candidate)
            legacy = copy.deepcopy(corrected)
            legacy["position_budget"]["leverage_convention"] = LEGACY_LEVERAGE_CONVENTION
            corrected_identity = execution_identity(corrected)
            legacy_identity = execution_identity(legacy)
            with self.subTest(strategy_id=corrected["strategy_id"]):
                self.assertEqual(corrected_identity["rule_hash"], legacy_identity["rule_hash"])
                self.assertEqual(corrected_identity["execution_hash"],
                                 legacy_identity["execution_hash"])
                self.assertEqual(
                    corrected_identity["execution_hash"],
                    __import__("strategy_simulation")._hash(
                        {**corrected_identity["execution"], "budget_rule": LEGACY_BUDGET_RULE}))

    def test_materializer_does_not_mutate_candidate(self):
        candidate = build_2x2_candidates(self.source)[0]
        original = copy.deepcopy(candidate)
        materialize_2x2_execution_spec(candidate)
        self.assertEqual(candidate, original)

    def test_materializer_rejects_enabled_or_malformed_candidates(self):
        candidate = build_2x2_candidates(self.source)[0]
        enabled = copy.deepcopy(candidate)
        enabled["execution_enabled"] = True
        with self.assertRaisesRegex(ValueError, "remain disabled"):
            materialize_2x2_execution_spec(enabled)
        malformed = copy.deepcopy(candidate)
        malformed["execution_override"]["leverage"] = 30
        with self.assertRaisesRegex(ValueError, "does not match"):
            materialize_2x2_execution_spec(malformed)


if __name__ == "__main__":
    unittest.main()
