"""Structural-stop execution: frozen evidence, clamp, and identity isolation."""

from __future__ import annotations

import hashlib
import json
import unittest
import copy
from unittest.mock import patch

from strategy_simulation import (PAIRS, _structural_audit_fields,
                                 _structural_stop_plan, apply_closed_batch,
                                 execution_identity, initial_state)


def _rule_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


class StructuralStopPlanTests(unittest.TestCase):
    def setUp(self):
        self.rule_patch = patch("strategy_simulation._rule_hash", side_effect=_rule_hash)
        self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)
        self.model = execution_identity({
            "strategy_id": "structural-test",
            "timeframe": "15m",
            "universe": list(PAIRS),
            "position_budget": {"leverage": 100},
            "risk_sizing_mode": "structural",
            "max_loss_per_trade_usdt": 50,
            "stop_policy_version": "structural-stop-v1",
            "stop_clamp_policy": "liquidation_distance_fraction",
            "stop_liquidation_clamp_factor": .7,
            "notional_policy": "frozen_signal",
            "experiment_id": "structural-stop-test",
        })["execution"]

    def evidence(self, *, direction=1, raw_stop=98):
        raw_distance = abs(100 - raw_stop)
        liquidation_distance = .01
        maximum_distance = 100 * liquidation_distance * .7
        clamped_distance = min(raw_distance, maximum_distance)
        return {
            "risk_sizing_mode": "structural",
            "entry_price": 100,
            "raw_stop_level": raw_stop,
            "raw_stop_distance": raw_distance,
            "stop_spec_id": "range-low-v1",
            "stop_policy_version": "structural-stop-v1",
            "structure_anchor_candle_ms": 600_000,
            "structure_lookback": 10,
            "structure_inputs_hash": "inputs-hash",
            "stop_direction": direction,
            "frozen_notional": 100,
            "frozen_quantity": 1,
            "notional_policy": "frozen_signal",
            "stop_execution_mode": "close_confirmed",
            "stop_clamp_policy": "liquidation_distance_fraction",
            "clamp_ratio": .7,
            "liquidation_distance": liquidation_distance,
            "clamped_stop_level": 100 - direction * clamped_distance,
            "clamped_stop_distance": clamped_distance,
            "clamp_flag": raw_distance > maximum_distance,
            "signal_id": "signal-1",
            "signal_candle_ms": 900_000,
            "raw_stop_distance_pct": abs(100 - raw_stop) / 100,
        }

    def test_directly_uses_frozen_stop_and_clamps_without_resizing(self):
        plan = _structural_stop_plan(
            {"direction": 1, "evidence": self.evidence()}, 100, self.model)
        self.assertEqual(plan["mode"], "structural")
        self.assertEqual(plan["raw_stop_level"], 98)
        self.assertAlmostEqual(plan["stop_level"], 99.3)
        self.assertTrue(plan["clamp_flag"])
        self.assertEqual(plan["frozen_notional"], 100)
        self.assertEqual(plan["frozen_quantity"], 1)
        self.assertEqual(plan["execution_raw_distance"], 2)
        self.assertEqual(plan["execution_clamped_distance"], .7)

    def test_fill_gap_preserves_signal_fields_and_separates_execution_fields(self):
        plan = _structural_stop_plan(
            {"direction": 1, "evidence": self.evidence()}, 101, self.model)
        self.assertEqual(plan["raw_stop_level"], 98)
        self.assertEqual(plan["raw_stop_distance"], 2)
        self.assertEqual(plan["signal_raw_stop_distance"], 2)
        self.assertEqual(plan["execution_raw_distance"], 3)
        self.assertAlmostEqual(plan["execution_clamped_distance"], .707)
        self.assertAlmostEqual(plan["stop_level"], 101 - .707)

    def test_fill_through_frozen_stop_fails_closed(self):
        self.assertIsNone(_structural_stop_plan(
            {"direction": 1, "evidence": self.evidence()}, 97, self.model))
        self.assertIsNone(_structural_stop_plan(
            {"direction": -1, "evidence": self.evidence(direction=-1, raw_stop=102)},
            103, self.model))

    def test_short_direction_and_invalid_side_fail_closed(self):
        plan = _structural_stop_plan(
            {"direction": -1, "evidence": self.evidence(direction=-1, raw_stop=102)},
            100, self.model)
        self.assertAlmostEqual(plan["stop_level"], 100.7)
        invalid = _structural_stop_plan(
            {"direction": 1, "evidence": self.evidence(raw_stop=102)}, 100, self.model)
        self.assertIsNone(invalid)

    def test_frozen_quantity_must_match_frozen_notional(self):
        evidence = self.evidence()
        evidence["frozen_quantity"] = 2
        self.assertIsNone(_structural_stop_plan(
            {"direction": 1, "evidence": evidence}, 100, self.model))

    def test_dual_limit_is_secondary_and_auditable(self):
        model = execution_identity({
            "strategy_id": "structural-dual",
            "timeframe": "15m",
            "universe": list(PAIRS),
            "position_budget": {"leverage": 100},
            "risk_sizing_mode": "structural",
            "max_loss_per_trade_usdt": 50,
            "stop_policy_version": "structural-stop-v1",
            "stop_clamp_policy": "liquidation_distance_fraction",
            "stop_liquidation_clamp_factor": .7,
            "notional_policy": "dual_limit",
            "experiment_id": "structural-dual-test",
        })["execution"]
        plan = _structural_stop_plan(
            {"direction": 1, "evidence": dict(self.evidence(), notional_policy="dual_limit")},
            100, model)
        self.assertIn("notional_adjusted_for_safety", plan)
        self.assertLessEqual(plan["notional_adjusted_for_safety"], plan["frozen_notional"])
        self.assertFalse(plan["frozen_quantity_primary"])
        self.assertEqual(plan["notional_sizing_role"], "secondary_dual_limit")
        self.assertEqual(_structural_audit_fields(plan)["raw_stop_distance"], 2)

    def test_structural_evidence_semantics_fail_closed(self):
        cases = (
            {"structure_anchor_candle_ms": 900_000},
            {"structure_lookback": True},
            {"structure_inputs_hash": ""},
            {"signal_id": ""},
            {"signal_candle_ms": 901_000},
            {"clamped_stop_distance": None},
            {"clamped_stop_level": 98},
            {"clamp_flag": False},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                evidence = self.evidence()
                evidence.update(changes)
                order = {"direction": 1, "evidence": evidence}
                if "signal_candle_ms" in changes:
                    order["signal_candle_ms"] = 900_000
                self.assertIsNone(_structural_stop_plan(order, 100, self.model))

    def test_frozen_signal_is_the_primary_quantity_basis(self):
        plan = _structural_stop_plan(
            {"direction": 1, "evidence": self.evidence()}, 100, self.model)
        self.assertTrue(plan["frozen_quantity_primary"])
        self.assertEqual(plan["notional_sizing_role"], "primary_frozen_quantity")

    def test_structural_risk_scope_names_the_primary_quantity(self):
        self.assertEqual(self.model["risk_scope"], "signal_frozen_quantity")
        self.assertIn("frozen_signal_quantity", self.model["risk_sizing"])

    def test_structural_clamp_factor_is_fixed(self):
        for factor in (.8, 1.0):
            with self.assertRaises(ValueError):
                execution_identity({
                    "strategy_id": f"structural-factor-{factor}",
                    "timeframe": "15m", "universe": list(PAIRS),
                    "position_budget": {"leverage": 100},
                    "risk_sizing_mode": "structural",
                    "stop_clamp_policy": "liquidation_distance_fraction",
                    "stop_liquidation_clamp_factor": factor,
                    "experiment_id": "structural-factor-test",
                })

    def test_structural_audit_fields_are_top_level_on_open_and_close(self):
        spec = {
            "strategy_id": "structural-events",
            "timeframe": "15m",
            "universe": list(PAIRS),
            "position_budget": {"leverage": 100},
            "risk_sizing_mode": "structural",
            "stop_policy_version": "structural-stop-v1",
            "stop_clamp_policy": "liquidation_distance_fraction",
            "stop_liquidation_clamp_factor": .7,
            "notional_policy": "frozen_signal",
            "experiment_id": "structural-events-test",
        }
        def bars(timestamp, price=100):
            row = {"timestamp": timestamp, "open": price, "high": price + .1,
                   "low": price - .1, "close": price, "volume": 10,
                   "extras": {"closed": True}}
            return {pair: copy.deepcopy(row) for pair in PAIRS}
        def signal(timestamp, target):
            info = self.evidence()
            return {
                "strategy_id": spec["strategy_id"], "rule_hash": _rule_hash(spec),
                "ready": True, "warmup": {"ready": True, "valid_bars": 10,
                                           "required_bars": 1},
                "signals": {
                    PAIRS[0]: {"target_side": target, "reason": "test_signal",
                               "candle_utc": timestamp, "evidence": info,
                               "size_multiplier": 1},
                    PAIRS[1]: {"target_side": None, "reason": "hold",
                               "candle_utc": timestamp, "evidence": {},
                               "size_multiplier": 1},
                }, "group": None,
            }
        state = initial_state(spec, 900_000)
        state, _ = apply_closed_batch(state, spec, bars(900_000),
                                      signal(900_000, 1), 1_801_000, 900_000)
        state, events = apply_closed_batch(state, spec, bars(1_800_000),
                                           signal(1_800_000, 1), 2_701_000, 900_000)
        opened = next(item["payload"] for item in events
                      if item["event_type"] == "position_opened")
        for key in ("raw_stop_level", "clamped_stop_level", "clamped_stop_distance",
                    "liquidation_distance", "risk_sizing_mode"):
            self.assertIn(key, opened)
        state, _ = apply_closed_batch(state, spec, bars(2_700_000),
                                      signal(2_700_000, 0), 3_601_000, 900_000)
        state, events = apply_closed_batch(state, spec, bars(3_600_000),
                                           signal(3_600_000, 0), 4_501_000, 900_000)
        closed = next(item["payload"] for item in events
                      if item["event_type"] == "trade_closed")
        for key in ("raw_stop_level", "clamped_stop_level", "clamped_stop_distance",
                    "liquidation_distance", "risk_sizing_mode"):
            self.assertIn(key, closed)

    def test_structural_identity_is_opt_in_and_distinct(self):
        legacy = {"strategy_id": "legacy", "timeframe": "15m", "universe": list(PAIRS),
                  "position_budget": {"leverage": 100}, "params": {"n": 3}}
        structural = dict(legacy, strategy_id="structural-test", risk_sizing_mode="structural",
                          max_loss_per_trade_usdt=50, stop_policy_version="structural-stop-v1",
                          notional_policy="frozen_signal", experiment_id="structural-stop-test")
        self.assertNotEqual(execution_identity(legacy)["execution_hash"],
                            execution_identity(structural)["execution_hash"])


if __name__ == "__main__":
    unittest.main()
