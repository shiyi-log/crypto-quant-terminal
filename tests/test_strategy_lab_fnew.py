"""Fnew population integration contracts for the strategy-lab service.

The Fnew factory, signal layer, data feeds and paper engine were already
implemented and unit tested.  These tests pin the missing piece: the service
must load the Fnew population, dispatch its normalization/evaluation, honour
the paired-execution blocker, and walk the nested ``spot`` feed that s33
declares without ever treating it as a perpetual leg.
"""

from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from strategy_fnew import fnew_rule_hash, load_fnew_specs, normalize_fnew_spec
from strategy_lab_service import (StrategyLabService, blocked_evaluation,
                                  evaluate_account, feed_series, is_fnew_spec,
                                  load_specs, normalize_account_spec,
                                  research_pause_reason)

BAR_15M = 900_000
BAR_1H = 3_600_000


def candle(timestamp):
    return {"timestamp": timestamp, "open": 100, "high": 101, "low": 99,
            "close": 100, "volume": 10,
            "extras": {"closed": True, "quote_volume": 1000}}


def fnew_spec(canonical):
    return next(spec for spec in load_fnew_specs()
                if spec["canonical_strategy_id"] == canonical)


class FnewPopulationTests(TestCase):
    def test_fnew_specs_join_legacy_population_without_collision(self):
        legacy = load_specs()
        fnew = load_fnew_specs()
        combined = legacy + fnew
        ids = [spec["strategy_id"] for spec in combined]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse({s["strategy_id"] for s in legacy}
                         & {s["strategy_id"] for s in fnew})
        self.assertEqual(sum(1 for s in fnew if is_fnew_spec(s)), 45)
        self.assertEqual(sum(1 for s in fnew if s["execution_enabled"] is False), 18)

    def test_paired_entries_are_blocked_until_explicitly_enabled(self):
        blocked = [s for s in load_fnew_specs() if s["execution_enabled"] is False]
        self.assertEqual({s["canonical_strategy_id"] for s in blocked},
                         {"s32_cointegration_spread_1h", "s36_correlation_regime_1h"})
        for spec in blocked:
            self.assertEqual(spec["blocked_execution_reason"],
                             "paired_atr_stop_policy_unimplemented")
        enabled = load_fnew_specs(paired_execution_enabled=True)
        self.assertTrue(all(s["execution_enabled"] for s in enabled))
        # Enabling is an identity change, so the frozen rule hash must move.
        self.assertNotEqual({s["rule_hash"] for s in blocked},
                            {s["rule_hash"] for s in enabled
                             if s["canonical_strategy_id"] in
                             {"s32_cointegration_spread_1h", "s36_correlation_regime_1h"}})

    def test_normalization_dispatches_to_fnew_normalizer(self):
        spec = fnew_spec("s34_vwap_anchor_reversion_15m")
        self.assertEqual(normalize_account_spec(spec), normalize_fnew_spec(spec))
        self.assertEqual(normalize_account_spec(spec)["rule_hash"], fnew_rule_hash(spec))

    def test_research_pause_uses_fnew_data_universe(self):
        spec = fnew_spec("s35_cascade_continuation_15m")
        self.assertIsNone(research_pause_reason(spec))
        widened = dict(spec, data_universe=["BTC/USDT:USDT", "DOGE/USDT:USDT"])
        self.assertEqual(research_pause_reason(widened),
                         "paused_research_context_required")


class FnewEvaluationDispatchTests(TestCase):
    def test_evaluate_account_dispatches_to_evaluate_fnew(self):
        spec = fnew_spec("s34_vwap_anchor_reversion_15m")
        canned = {"strategy_id": spec["strategy_id"], "rule_hash": spec["rule_hash"],
                  "ready": True, "warmup": {"ready": True}, "signals": {}}
        with patch("strategy_fnew_signals.evaluate_fnew", return_value=canned) as evaluate:
            self.assertIs(evaluate_account(spec, {"BTC/USDT:USDT": []}, positions={},
                                           as_of_utc=1_800_000_000_000), canned)
        self.assertEqual(evaluate.call_count, 1)

    def test_blocked_paired_account_never_reaches_the_signal_layer(self):
        spec = fnew_spec("s32_cointegration_spread_1h")
        with patch("strategy_fnew_signals.evaluate_fnew") as evaluate:
            result = evaluate_account(spec, {}, positions={}, as_of_utc=1_800_000_000_000)
        self.assertEqual(evaluate.call_count, 0)
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["reason"],
                         "paired_atr_stop_policy_unimplemented")
        self.assertEqual(result["rule_hash"], fnew_rule_hash(spec))

    def test_blocked_evaluation_uses_the_frozen_fnew_hash(self):
        spec = fnew_spec("s36_correlation_regime_1h")
        result = blocked_evaluation(spec, spec["blocked_execution_reason"])
        self.assertEqual(result["rule_hash"], spec["rule_hash"])


class NestedSpotFeedTests(TestCase):
    def test_feed_series_flattens_nested_spot_mapping(self):
        loaded = {"BTC/USDT:USDT": [candle(0)],
                  "spot": {"BTC/USDT": [candle(0)]}}
        names = dict(feed_series(loaded))
        self.assertEqual(set(names), {"BTC/USDT:USDT", "spot/BTC/USDT"})
        self.assertEqual(names["spot/BTC/USDT"], [candle(0)])

    def test_context_candles_accepts_aligned_spot_and_rejects_a_shifted_tail(self):
        aligned = {"BTC/USDT:USDT": [candle(0), candle(BAR_1H)],
                   "spot": {"BTC/USDT": [candle(0), candle(BAR_1H)]}}
        self.assertEqual(StrategyLabService._context_candles(aligned), aligned)
        shifted = {"BTC/USDT:USDT": [candle(0), candle(BAR_1H)],
                   "spot": {"BTC/USDT": [candle(0)]}}
        self.assertEqual(StrategyLabService._context_candles(shifted), {})

    def test_warmup_refresh_survives_a_nested_spot_feed(self):
        spec = fnew_spec("s34_vwap_anchor_reversion_15m")
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=4 * BAR_15M,
                                     status_path=status_path)
        loaded = {"BTC/USDT:USDT": [candle(0), candle(BAR_15M)],
                  "spot": {"BTC/USDT": [candle(0), candle(BAR_15M)]}}
        canned = {"strategy_id": spec["strategy_id"], "rule_hash": spec["rule_hash"],
                  "ready": True, "warmup": {"ready": True, "valid_bars": 2},
                  "signals": {}}
        with patch("strategy_fnew_signals.evaluate_fnew", return_value=canned):
            service._refresh_native_warmup(spec, loaded, now=4 * BAR_15M,
                                           interval=BAR_15M)
        service.simulation.update_warmup.assert_called_once()

    def test_bootstrap_reports_the_spot_series_separately(self):
        spec = fnew_spec("s34_vwap_anchor_reversion_15m")
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=4 * BAR_15M,
                                     status_path=status_path)
        service.strategy_history[spec["strategy_id"]] = {
            "BTC/USDT:USDT": [candle(0), candle(BAR_15M)],
            "spot": {"BTC/USDT": [candle(0)]}}
        bars = {name: len(rows) for name, rows in
                feed_series(service.strategy_history[spec["strategy_id"]])}
        self.assertEqual(bars, {"BTC/USDT:USDT": 2, "spot/BTC/USDT": 1})


class FnewInputLoadingTests(TestCase):
    def service_for(self, spec):
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=4 * BAR_1H,
                                     status_path=status_path)
        return service

    def test_fnew_inputs_use_the_fnew_loader(self):
        spec = fnew_spec("s35_cascade_continuation_15m")
        service = self.service_for(spec)
        loaded = {"BTC/USDT:USDT": [candle(0)]}
        with patch("strategy_fnew_data.load_fnew_candles", return_value=loaded) as loader:
            self.assertEqual(service.load_strategy_inputs(spec, now_ms=4 * BAR_1H), loaded)
            self.assertEqual(loader.call_count, 1)
            # A second request for the same feed contract is cached.
            service.load_strategy_inputs(spec, now_ms=4 * BAR_1H)
            self.assertEqual(loader.call_count, 1)

    def test_blocked_paired_account_loads_no_input_at_all(self):
        spec = fnew_spec("s32_cointegration_spread_1h")
        service = self.service_for(spec)
        with patch("strategy_fnew_data.load_fnew_candles") as loader:
            self.assertEqual(service.load_strategy_inputs(spec, now_ms=4 * BAR_1H), {})
        self.assertEqual(loader.call_count, 0)

    def test_basis_and_non_basis_feeds_never_share_a_cache_entry(self):
        """s33 needs spot; a perpetual-only mechanism must not reuse its window."""
        basis = fnew_spec("s33_basis_momentum_1h")
        service = self.service_for(basis)
        perpetual_only = fnew_spec("s35_cascade_continuation_15m")
        loaded = {"BTC/USDT:USDT": [candle(0)], "spot": {"BTC/USDT": [candle(0)]}}
        with patch("strategy_fnew_data.load_fnew_candles", return_value=loaded) as loader:
            service.load_strategy_inputs(basis, now_ms=4 * BAR_1H)
            service.load_strategy_inputs(perpetual_only, now_ms=4 * BAR_1H)
        self.assertEqual(loader.call_count, 2)


class FnewNativeCycleTests(TestCase):
    def test_native_cycle_evaluates_fnew_and_passes_only_trade_legs(self):
        spec = fnew_spec("s34_vwap_anchor_reversion_15m")
        pair = "BTC/USDT:USDT"
        spec = dict(spec, execution_universe=[pair], data_universe=[pair],
                    params_frozen=spec["params_frozen"])
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=1_800_001,
                                     status_path=status_path)
        rows = [candle(0), candle(BAR_15M), candle(2 * BAR_15M)]
        service.load_strategy_inputs = lambda *_a, **_k: {pair: rows}
        service.simulation.preview_positions.return_value = {spec["strategy_id"]: {}}
        signal = {"strategy_id": spec["strategy_id"], "rule_hash": spec["rule_hash"],
                  "ready": True, "warmup": {"ready": True},
                  "signals": {pair: {"target_side": None}}}
        with patch("strategy_fnew_signals.evaluate_fnew", return_value=signal) as evaluate:
            # Observe the close of the last bar: the 90s acceptance deadline
            # must be met or the cycle legitimately reports data_stale.
            service._cycle_native(2 * BAR_15M + BAR_15M + 1)
        self.assertGreaterEqual(evaluate.call_count, 1)
        service.simulation.process_batch.assert_called_once()
        candles_arg = service.simulation.process_batch.call_args.args[0]
        self.assertEqual(set(candles_arg), {pair})
        self.assertEqual(service.simulation.process_batch.call_args.kwargs["timeframe"],
                         "15m")


class LegacyPathUntouchedTests(TestCase):
    def test_legacy_specs_keep_their_own_normalizer_and_hash(self):
        legacy = load_specs()
        for spec in legacy:
            self.assertFalse(is_fnew_spec(spec))
            self.assertEqual(normalize_account_spec(spec)["rule_hash"],
                             spec["rule_hash"])
