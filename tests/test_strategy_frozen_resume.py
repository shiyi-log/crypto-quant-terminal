"""Research edits must not change a persisted paper account on restart."""
import copy
import unittest
from unittest.mock import MagicMock

from strategy_lab_service import resume_frozen_specs
from strategy_signals import BTC, ETH, normalize_spec
from strategy_simulation import execution_identity


# Executable fields from an actual pre-ATR account, read on 2026-10-12.
# Its stored warmup and hashes are historical facts, not new default values.
FROZEN_S07 = {
    "strategy_id": "s07_surge_continuation_B15m_100x_maker",
    "name": "订单流失衡延续",
    "mechanism": "主动买卖失衡达极端时，假设力量延续，顺失衡方向入场。",
    "params": {"H": 4, "I": 0.3, "volume_window": 20, "min_volume_multiple": 0.6},
    "timeframe": "15m", "universe": [BTC, ETH], "min_warmup_bars": 100,
    "initial_capital": 10000.0, "fee_rate_per_side": 0.0002,
    "position_budget": {"sizing": "剩余预算 / 剩余槽位", "leverage": 100,
                        "max_per_coin": 1, "max_positions": 2, "target_exposure": 0.3,
                        "leverage_convention": "A：保证金不变，名义 ×100，逐根强平"},
    "schema_version": "multi-strategy-signal-v1",
    "algorithm_version": "closed5m-prior-baseline-actual-position-v1",
    "closed_only": True, "fill_model": "next_closed_bar_open_proxy",
    "funding": "unknown", "slippage": "unknown",
    "history_policy": "official_rest_indicator_only",
    "atr_risk_policy": "fixed_entry_evidence_atr_close_trigger_next_open",
    "baseline_policy": "exclude_current_completed_candle",
    "true_range_policy": "arithmetic_mean_previous_true_ranges",
    "annualization_bars": 105120, "closed_freshness_ms": 90000,
    "holding_bars_policy": "inclusive_actual_open_candle", "same_side_rebalance": False,
    "flow_imbal_convention": "recompute_2_taker_buy_base_over_volume_minus_1",
    "input_revision_policy": "first_accepted_closed_version",
    "standard_deviation_ddof": 1,
    "orderflow_threshold_policy": "decimal_stored_inputs_no_epsilon",
    "execution_universe": [BTC, ETH], "research_universe": [BTC, ETH],
    "research_timeframe": "15m",
    "rule_hash": "5a571e3ea7b8f07f3e195ee0df000892027bdec11632f3dfd7ed5408dc07183d",
}
FROZEN_EXECUTION_HASH = "fc9fc13bf7c22077484165fdf5bbaaa5c04b7e0ac0b892cf4bfa349b01aa2f54"


def stored_account(definition):
    identity = execution_identity(definition)
    return {"strategy_id": definition["strategy_id"], "definition": copy.deepcopy(definition),
            "rule_hash": identity["rule_hash"], "execution_hash": identity["execution_hash"]}


def mock_store(rows):
    store = MagicMock()
    conn = store._connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = rows
    return store, conn


def modified_request(definition, *, params=None, **updates):
    requested = copy.deepcopy(definition)
    requested.pop("rule_hash")
    requested.update(updates)
    if params:
        requested["params"].update(params)
    return {**requested, **normalize_spec(requested)}


class FrozenStrategyResumeTests(unittest.TestCase):
    def test_historical_rule_and_execution_hashes_still_match(self):
        before = copy.deepcopy(FROZEN_S07)
        normalized = normalize_spec(FROZEN_S07)
        identity = execution_identity(FROZEN_S07)
        self.assertEqual(normalized["rule_hash"], FROZEN_S07["rule_hash"])
        self.assertEqual(identity["execution_hash"], FROZEN_EXECUTION_HASH)
        self.assertEqual(normalized["min_warmup_bars"], 100)
        self.assertNotIn("risk_stop_atr_mult", normalized["params"])
        self.assertEqual(FROZEN_S07, before)

    def test_changed_research_resumes_the_original_definition_and_defers_edit(self):
        row = stored_account(FROZEN_S07)
        requested = modified_request(FROZEN_S07, params={"risk_stop_atr_mult": 1.5},
                                     min_warmup_bars=21)
        before = copy.deepcopy((row, requested))
        store, conn = mock_store([row])
        selected, deferred = resume_frozen_specs(store, [requested])
        self.assertEqual(selected, [FROZEN_S07])
        self.assertEqual(deferred, [FROZEN_S07["strategy_id"]])
        self.assertEqual((row, requested), before)
        self.assertIsNot(selected[0], row["definition"])
        selected[0]["params"]["H"] = 999
        self.assertEqual(row["definition"]["params"]["H"], 4)
        self.assertEqual(conn.execute.call_args.args[1], ([FROZEN_S07["strategy_id"]],))
        self.assertEqual(conn.execute.call_count, 1)

    def test_execution_only_change_is_deferred_even_when_rule_hash_matches(self):
        row = stored_account(FROZEN_S07)
        requested = copy.deepcopy(FROZEN_S07)
        requested["execution_universe"] = [BTC]
        self.assertEqual(normalize_spec(requested)["rule_hash"], row["rule_hash"])
        store, _ = mock_store([row])
        selected, deferred = resume_frozen_specs(store, [requested])
        self.assertEqual(selected, [FROZEN_S07])
        self.assertEqual(deferred, [FROZEN_S07["strategy_id"]])

    def test_unmodified_existing_definition_is_not_deferred(self):
        store, _ = mock_store([stored_account(FROZEN_S07)])
        selected, deferred = resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])
        self.assertEqual(selected, [FROZEN_S07])
        self.assertEqual(deferred, [])

    def test_new_identity_is_selected_alongside_frozen_account_in_request_order(self):
        new = normalize_spec({"strategy_id": "s02_dual_thrust_v2",
                              "params": {"risk_stop_atr_mult": 1.5},
                              "position_budget": {"leverage": 100}})
        changed = modified_request(FROZEN_S07, params={"H": 5})
        store, _ = mock_store([stored_account(FROZEN_S07)])
        selected, deferred = resume_frozen_specs(store, [new, changed])
        self.assertEqual(selected, [new, FROZEN_S07])
        self.assertEqual(deferred, [FROZEN_S07["strategy_id"]])

    def test_corrupted_stored_rule_hash_cannot_resume(self):
        row = stored_account(FROZEN_S07)
        row["rule_hash"] = "wrong"
        store, _ = mock_store([row])
        with self.assertRaisesRegex(ValueError, "persisted frozen strategy identity mismatch"):
            resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])

    def test_corrupted_stored_execution_hash_cannot_resume(self):
        row = stored_account(FROZEN_S07)
        row["execution_hash"] = "wrong"
        store, _ = mock_store([row])
        with self.assertRaisesRegex(ValueError, "persisted frozen strategy identity mismatch"):
            resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])

    def test_changed_persisted_params_cannot_reuse_the_stored_rule_hash(self):
        row = stored_account(FROZEN_S07)
        row["definition"]["params"]["H"] = 5
        store, _ = mock_store([row])
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])

    def test_changed_persisted_execution_pairs_cannot_reuse_execution_hash(self):
        row = stored_account(FROZEN_S07)
        row["definition"]["execution_universe"] = [BTC]
        store, _ = mock_store([row])
        with self.assertRaisesRegex(ValueError, "persisted frozen strategy identity mismatch"):
            resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])

    def test_definition_for_a_different_account_cannot_resume_under_another_row_id(self):
        other = normalize_spec({"strategy_id": "s02_dual_thrust_v2",
                                "position_budget": {"leverage": 100}})
        row = stored_account(other)
        row["strategy_id"] = FROZEN_S07["strategy_id"]
        store, _ = mock_store([row])
        with self.assertRaisesRegex(ValueError, "persisted frozen strategy identity mismatch"):
            resume_frozen_specs(store, [copy.deepcopy(FROZEN_S07)])


if __name__ == "__main__":
    unittest.main()
