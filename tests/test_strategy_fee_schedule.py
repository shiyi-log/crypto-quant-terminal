"""Demo fee defaults must not rewrite frozen paper economics or history."""

import copy
import unittest
from unittest.mock import patch

from strategy_fee_schedule import (
    DEMO_FEE_MODEL_VERSION,
    FEE_BASIS,
    LEGACY_FEE_MODEL_VERSION,
    LEGACY_TAKER_FEE_PER_SIDE,
    MAKER_FEE_PER_SIDE,
    TAKER_FEE_PER_SIDE,
)
from strategy_simulation import (
    BAR_MS,
    EXECUTION_MODEL,
    LEGACY_EXECUTION_MODEL,
    PAIRS,
    apply_closed_batch,
    execution_identity,
    initial_state,
)
from tests.test_strategy_frozen_resume import FROZEN_EXECUTION_HASH, FROZEN_S07


class StrategyFeeScheduleTests(unittest.TestCase):
    def test_demo_rates_distinguish_quoted_maker_and_verified_taker(self):
        self.assertEqual(MAKER_FEE_PER_SIDE, .0002)
        self.assertEqual(TAKER_FEE_PER_SIDE, .0004)
        self.assertEqual(LEGACY_TAKER_FEE_PER_SIDE, .0005)
        self.assertIn("maker_0.0002_commissionRate_quote_requires_actual_fills", FEE_BASIS)
        self.assertIn("taker_0.0004_verified", FEE_BASIS)
        self.assertIn("manual_smoke_20261012_035400", FEE_BASIS)

    def test_frozen_maker_execution_hash_and_definition_are_unchanged(self):
        definition = copy.deepcopy(FROZEN_S07)
        before = copy.deepcopy(definition)
        identity = execution_identity(definition)
        self.assertEqual(identity["execution_hash"], FROZEN_EXECUTION_HASH)
        self.assertEqual(identity["rule_hash"], definition["rule_hash"])
        self.assertEqual(identity["execution"]["fee_rate_per_side"], .0002)
        self.assertEqual(identity["execution"]["taker_fee_per_side"], .0005)
        self.assertNotIn("fee_model_version", identity["execution"])
        self.assertEqual(definition, before)

    def test_frozen_explicit_taker_keeps_its_original_execution_hash_and_rate(self):
        definition = copy.deepcopy(FROZEN_S07)
        definition["fee_rate_per_side"] = .0005
        definition["rule_hash"] = "d798ac304bb04c8f92542e37edcfdcb8fa22b05514e98f3555f378e0f527267d"
        identity = execution_identity(definition)
        self.assertEqual(identity["execution_hash"],
                         "a3d6f34bf5551986cd2570cd7eb021ccaf6b230ebf63c9cf3d9ba1e32a1df5aa")
        self.assertEqual(identity["execution"]["fee_rate_per_side"], .0005)

    def test_missing_version_and_fee_keep_the_historical_maker_default(self):
        definition = copy.deepcopy(FROZEN_S07)
        definition.pop("fee_rate_per_side")
        identity = execution_identity(definition)
        self.assertEqual(identity["execution_hash"], FROZEN_EXECUTION_HASH)
        self.assertEqual(identity["execution"]["fee_rate_per_side"], .0002)
        self.assertEqual(identity["execution"]["fee_basis"],
                         "maker_limit_orders_observed_in_live_trades")

    def test_current_model_changes_only_version_and_fee_schedule_metadata(self):
        legacy = copy.deepcopy(LEGACY_EXECUTION_MODEL)
        current = copy.deepcopy(EXECUTION_MODEL)
        current.pop("fee_model_version")
        current["taker_fee_per_side"] = legacy["taker_fee_per_side"]
        current["fee_basis"] = legacy["fee_basis"]
        self.assertEqual(current, legacy)
        self.assertEqual(EXECUTION_MODEL["fee_model_version"], DEMO_FEE_MODEL_VERSION)
        self.assertEqual(EXECUTION_MODEL["taker_fee_per_side"], TAKER_FEE_PER_SIDE)
        self.assertEqual(EXECUTION_MODEL["fee_rate_per_side"], MAKER_FEE_PER_SIDE)
        self.assertEqual(EXECUTION_MODEL["slippage"], "unknown")

    def test_demo_version_is_opt_in_and_hash_bound(self):
        legacy = copy.deepcopy(FROZEN_S07)
        current = dict(legacy, fee_model_version=DEMO_FEE_MODEL_VERSION)
        with patch("strategy_simulation._rule_hash", return_value=legacy["rule_hash"]):
            before = execution_identity(legacy)
            after = execution_identity(current)
        self.assertNotEqual(before["execution_hash"], after["execution_hash"])
        self.assertEqual(after["execution"]["fee_model_version"], DEMO_FEE_MODEL_VERSION)
        self.assertEqual(after["execution"]["taker_fee_per_side"], .0004)
        self.assertEqual(after["execution"]["fee_rate_per_side"], .0002)
        self.assertEqual(after["execution"]["fee_basis"], FEE_BASIS)

    def test_explicit_legacy_version_keeps_legacy_execution_identity(self):
        definition = dict(FROZEN_S07, fee_model_version=LEGACY_FEE_MODEL_VERSION)
        with patch("strategy_simulation._rule_hash", return_value=FROZEN_S07["rule_hash"]):
            identity = execution_identity(definition)
        self.assertEqual(identity["execution_hash"], FROZEN_EXECUTION_HASH)

    def test_unknown_or_invalid_version_is_rejected(self):
        for version in ("binance-demo-unknown", None, True, {}, 1):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "unsupported fee_model_version"):
                    execution_identity(dict(FROZEN_S07, fee_model_version=version))

    def test_demo_version_rejects_an_unsourced_custom_fee(self):
        with self.assertRaisesRegex(ValueError, "Demo fee_model_version"):
            execution_identity(dict(FROZEN_S07, fee_model_version=DEMO_FEE_MODEL_VERSION,
                                    fee_rate_per_side=.0003))

    def test_returned_execution_cannot_mutate_the_legacy_or_current_models(self):
        for version, original in ((LEGACY_FEE_MODEL_VERSION, LEGACY_EXECUTION_MODEL),
                                  (DEMO_FEE_MODEL_VERSION, EXECUTION_MODEL)):
            with self.subTest(version=version):
                before = copy.deepcopy(original)
                with patch("strategy_simulation._rule_hash", return_value="test-rule"):
                    identity = execution_identity(dict(FROZEN_S07, fee_model_version=version))
                identity["execution"]["liquidation"]["enabled"] = False
                self.assertEqual(original, before)

    def test_existing_state_cannot_adopt_a_new_fee_model_under_the_same_identity(self):
        definition = copy.deepcopy(FROZEN_S07)
        state = initial_state(definition, BAR_MS)
        before = copy.deepcopy(state)
        changed = dict(definition, fee_model_version=DEMO_FEE_MODEL_VERSION)
        with patch("strategy_simulation._rule_hash", return_value=definition["rule_hash"]):
            with self.assertRaisesRegex(ValueError, "rule/execution change requires a new strategy identity"):
                apply_closed_batch(state, changed, {}, {}, 2 * BAR_MS, BAR_MS)
        self.assertEqual(state, before)

    def test_actual_paper_fee_deduction_uses_each_frozen_contract(self):
        for version, fee in ((LEGACY_FEE_MODEL_VERSION, .0005),
                             (DEMO_FEE_MODEL_VERSION, .0004),
                             (DEMO_FEE_MODEL_VERSION, .0002)):
            with self.subTest(version=version, fee=fee):
                definition = {"strategy_id": "test-fee-contract", "timeframe": "5m",
                              "universe": list(PAIRS), "fee_model_version": version,
                              "fee_rate_per_side": fee}
                with patch("strategy_simulation._rule_hash", return_value="test-rule"):
                    state = initial_state(definition, BAR_MS)
                    for index, target in ((1, 1), (2, None), (3, 0), (4, None)):
                        stamp = index * BAR_MS
                        candle = {"timestamp": stamp, "open": 100., "high": 100.5,
                                  "low": 99.5, "close": 100., "volume": 10,
                                  "extras": {"closed": True}}
                        signal = {"strategy_id": definition["strategy_id"],
                                  "rule_hash": "test-rule", "ready": True,
                                  "signals": {PAIRS[0]: {"target_side": target}}}
                        state, events = apply_closed_batch(
                            state, definition, {pair: [copy.deepcopy(candle)] for pair in PAIRS},
                            signal, stamp + BAR_MS + 1000, BAR_MS)
                        if index == 2:
                            position = state["positions"][PAIRS[0]]
                            notional = position["entry_price"] * position["quantity"]
                            self.assertAlmostEqual(position["entry_fee"], notional * fee)
                    trade = next(item["payload"] for item in events
                                 if item["event_type"] == "trade_closed")
                    self.assertEqual(state["positions"], {})
                    self.assertAlmostEqual(trade["fees"], 2 * notional * fee)
                    self.assertAlmostEqual(state["fees_paid"], trade["fees"])
                    self.assertAlmostEqual(trade["net_pnl"], -trade["fees"])


if __name__ == "__main__":
    unittest.main()
