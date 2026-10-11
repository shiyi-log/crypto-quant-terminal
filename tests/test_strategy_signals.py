"""Executable causal contracts for independent forward strategy signals."""
import copy
import math
import unittest
from datetime import datetime, timezone
from statistics import stdev

from strategy_signals import (
    ALGORITHM_VERSION,
    BAR_MS,
    BTC,
    LEGACY_DEFAULTS as DEFAULTS,
    ETH,
    _single,
    evaluate,
    normalize_spec,
    strategy_rule_hash,
)

START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def candles(count=750, prices=None):
    prices = prices or [100.0] * count
    return [{"timestamp": START + index * BAR_MS, "open": close, "high": close + 0.3,
             "low": close - 0.3, "close": close, "volume": 100.0,
             "extras": {"closed": True, "taker_buy_base": 50.0}}
            for index, close in enumerate(prices)]


def latest(rows, *, close=None, open_price=None, high=None, low=None,
           volume=None, bought=None):
    row = rows[-1]
    if close is not None:
        row["close"] = close
    if open_price is not None:
        row["open"] = open_price
    row["high"] = high if high is not None else max(row["high"], row["open"], row["close"])
    row["low"] = low if low is not None else min(row["low"], row["open"], row["close"])
    if volume is not None:
        row["volume"] = volume
    if bought is not None:
        row["extras"]["taker_buy_base"] = bought
    return rows


def account(strategy_id, btc, eth=None, positions=None, **kwargs):
    return evaluate({"strategy_id": strategy_id}, {BTC: btc, ETH: eth or copy.deepcopy(btc)},
                    positions, **kwargs)


def position(rows, side=1, entry=100.0, held=1, **kwargs):
    return {"side": side, "entry_price": entry,
            "opened_candle_ms": rows[-1]["timestamp"] - (held - 1) * BAR_MS, **kwargs}


class StrategyRuleIdentityTests(unittest.TestCase):
    def test_ten_distinct_supported_identities_and_normalization_idempotent(self):
        self.assertGreaterEqual(len(DEFAULTS), 20)
        hashes = set()
        for strategy_id in DEFAULTS:
            with self.subTest(strategy_id=strategy_id):
                normalized = normalize_spec({"strategy_id": strategy_id})
                self.assertEqual(normalize_spec(normalized), normalized)
                self.assertEqual(strategy_rule_hash(normalized), normalized["rule_hash"])
                hashes.add(normalized["rule_hash"])
        self.assertEqual(len(hashes), len(DEFAULTS))

    def test_research_commentary_does_not_change_executable_identity(self):
        spec = {"strategy_id": "s07_surge_continuation"}
        described = {**spec, "known_evidence": "old report", "evaluation": "other report",
                     "distinct_from": "display description", "hypothesis_status": "hypothesis"}
        self.assertEqual(strategy_rule_hash(spec), strategy_rule_hash(described))

    def test_parameter_capital_fee_and_mechanism_change_hash(self):
        spec = {"strategy_id": "s02_dual_thrust"}
        baseline = strategy_rule_hash(spec)
        for update in ({"params": {"k": 0.7}}, {"initial_capital": 12_000},
                       {"fee_rate_per_side": 0.0003}, {"mechanism": "another frozen mechanism"},
                       {"min_warmup_bars": 100}, {"position_budget": {"target_exposure": 0.2}}):
            with self.subTest(update=update):
                self.assertNotEqual(baseline, strategy_rule_hash({**spec, **update}))

    def test_changed_executable_spec_cannot_reuse_stale_rule_hash(self):
        normalized = normalize_spec({"strategy_id": "s02_dual_thrust"})
        normalized["params"]["k"] = 0.7
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            normalize_spec(normalized)

    def test_cost_timing_input_and_atr_conventions_are_frozen(self):
        normalized = normalize_spec({"strategy_id": "s08_wick_rejection"})
        self.assertEqual(normalized["fill_model"], "next_closed_bar_open_proxy")
        self.assertEqual(normalized["fee_rate_per_side"], 0.0002)
        self.assertEqual(normalized["input_revision_policy"], "first_accepted_closed_version")
        self.assertEqual(normalized["history_policy"], "official_rest_indicator_only")
        self.assertEqual(normalized["closed_freshness_ms"], 90_000)
        self.assertEqual(normalized["holding_bars_policy"], "inclusive_actual_open_candle")
        self.assertEqual(normalized["atr_risk_policy"],
                         "fixed_entry_evidence_atr_close_trigger_next_open")
        self.assertEqual(normalized["slippage"], "unknown")
        self.assertEqual(normalized["funding"], "unknown")

    def test_unsupported_execution_changes_rejected(self):
        spec = {"strategy_id": "s07_surge_continuation"}
        for update in ({"closed_only": False}, {"fill_model": "same_close"},
                       {"flow_imbal_convention": {"mode": "stored"}},
                       {"params": {"unknown_threshold": 10}},
                       {"fee_rate_per_side": -0.1}, {"initial_capital": 0},
                       {"position_budget": {"leverage": 2}},
                       {"params": {"volume_window": True}},
                       {"params": {"I": float("nan")}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                normalize_spec({**spec, **update})

    def test_100x_variant_has_distinct_identity_and_requires_matching_leverage(self):
        base = normalize_spec({"strategy_id": "s06_volume_climax_reversal"})
        with self.assertRaisesRegex(ValueError, "_100x strategy identities require leverage 100"):
            normalize_spec({"strategy_id": "s06_volume_climax_reversal_100x"})
        variant = normalize_spec({"strategy_id": "s06_volume_climax_reversal_100x",
                                  "position_budget": {"leverage": 100}})
        self.assertEqual(variant["strategy_id"], "s06_volume_climax_reversal_100x")
        self.assertEqual(variant["position_budget"]["leverage"], 100)
        self.assertNotEqual(base["rule_hash"], variant["rule_hash"])

    def test_documented_recompute_mapping_and_flat_convention_hash_match(self):
        base = {"strategy_id": "s07_surge_continuation"}
        documented = {**base, "flow_imbal_convention": {"mode": "recompute", "note": "display"}}
        self.assertEqual(strategy_rule_hash(base), strategy_rule_hash(documented))


class ClosedHistoryContractTests(unittest.TestCase):
    def test_all_ten_unready_accounts_hold_without_fake_flat(self):
        for strategy_id in DEFAULTS:
            with self.subTest(strategy_id=strategy_id):
                required = normalize_spec({"strategy_id": strategy_id})["min_warmup_bars"]
                result = account(strategy_id, candles(required - 1))
                self.assertFalse(result["ready"])
                self.assertFalse(result["warmup"]["ready"])
                self.assertIsNone(result["signals"][BTC]["target_side"])
                self.assertIsNone(result["signals"][ETH]["target_side"])

    def test_open_candles_never_influence_decision_or_fingerprint(self):
        rows = latest(candles(), close=102, volume=1000)
        baseline = account("s06_volume_climax_reversal", rows)
        future = latest(candles(751), close=200, volume=100_000)[-1]
        future["extras"]["closed"] = False
        with_open = account("s06_volume_climax_reversal", rows + [future])
        self.assertEqual(with_open, baseline)

    def test_asof_excludes_future_closed_bars_without_lookahead(self):
        rows = latest(candles(), close=102, volume=1000)
        as_of = rows[-1]["timestamp"] + BAR_MS
        future = latest(candles(751), close=200, volume=100_000)[-1]
        self.assertEqual(account("s06_volume_climax_reversal", rows, as_of_utc=as_of),
                         account("s06_volume_climax_reversal", rows + [future], as_of_utc=as_of))

    def test_too_early_clock_cannot_use_uncompleted_close(self):
        rows = candles()
        result = account("s07_surge_continuation", rows,
                         as_of_utc=rows[-1]["timestamp"] + BAR_MS - 1)
        self.assertEqual(result["signals"][BTC]["candle_utc"], rows[-2]["timestamp"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["reason"], "closed_candle_stale")

    def test_stale_data_does_not_open_an_account(self):
        rows = latest(candles(), bought=80)
        result = account("s07_surge_continuation", rows,
                         as_of_utc=rows[-1]["timestamp"] + BAR_MS + 90_001)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["signals"][BTC]["target_side"])

    def test_gap_requires_new_contiguous_warmup(self):
        rows = candles()
        del rows[-20]
        result = account("s07_surge_continuation", rows)
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["valid_bars"], 19)

    def test_old_gap_does_not_poison_new_complete_contiguous_window(self):
        rows = candles()
        del rows[10]
        self.assertTrue(account("s07_surge_continuation", rows)["ready"])

    def test_duplicate_timestamp_is_refused(self):
        rows = candles()
        result = account("s07_surge_continuation", rows + [copy.deepcopy(rows[-1])])
        self.assertFalse(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "duplicate_closed_candle_timestamp")

    def test_missing_peer_blocks_both_accounts(self):
        rows = latest(candles(), bought=80)
        result = evaluate({"strategy_id": "s07_surge_continuation"}, {BTC: rows})
        self.assertFalse(result["ready"])
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["reason"], "account_peer_data_unready")

    def test_timestamp_alignment_required_for_all_accounts(self):
        btc, eth = candles(), candles()
        for row in eth:
            row["timestamp"] += BAR_MS
        result = account("s04_btc_eth_pairs", btc, eth)
        self.assertFalse(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "pair_timestamps_not_aligned")
        self.assertIsNone(result["signals"][ETH]["target_side"])

    def test_malformed_ohlcv_fails_closed(self):
        for field, value in (("high", 99), ("low", 101), ("volume", -1),
                             ("close", float("inf")), ("open", True)):
            rows = candles()
            rows[-1][field] = value
            with self.subTest(field=field):
                result = account("s07_surge_continuation", rows)
                self.assertFalse(result["ready"])
                self.assertEqual(result["signals"][BTC]["reason"], "invalid_closed_candle_ohlcv")

    def test_missing_closed_markers_never_become_signal_data(self):
        rows = candles()
        for row in rows:
            row["extras"].pop("closed")
        result = account("s07_surge_continuation", rows)
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["valid_bars"], 0)

    def test_input_records_and_positions_are_unchanged(self):
        rows = candles()
        positions = {BTC: position(rows, entry_evidence={"atr": 2.0})}
        before = copy.deepcopy((rows, positions))
        account("s08_wick_rejection", rows, positions=positions)
        self.assertEqual((rows, positions), before)

    def test_evidence_contains_actual_history_and_changes_with_input(self):
        rows = candles()
        first = account("s07_surge_continuation", rows)["signals"][BTC]["evidence"]
        rows[100]["volume"] = 101
        second = account("s07_surge_continuation", rows)["signals"][BTC]["evidence"]
        self.assertNotEqual(first["data_fingerprint"], second["data_fingerprint"])
        self.assertEqual(first["history_bars"], 750)
        self.assertEqual(first["history_start_ms"], START)
        self.assertEqual(first["algorithm_version"], ALGORITHM_VERSION)

    def test_invalid_actual_position_prevents_new_entry(self):
        rows = latest(candles(), bought=80)
        result = account("s07_surge_continuation", rows, positions={BTC: {"side": 1}})
        self.assertFalse(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "invalid_position")


class ExplicitAtrRiskTests(unittest.TestCase):
    def test_atr_risk_requires_explicit_opt_in_and_changes_the_rule_hash(self):
        legacy = normalize_spec({"strategy_id": "s02_dual_thrust"})
        protected = normalize_spec({"strategy_id": "s02_dual_thrust",
                                    "params": {"risk_stop_atr_mult": 1.5}})
        self.assertNotIn("risk_stop_atr_mult", legacy["params"])
        self.assertEqual(protected["params"]["atr_window"], 14)
        self.assertEqual(protected["params"]["risk_stop_atr_mult"], 1.5)
        self.assertNotEqual(legacy["rule_hash"], protected["rule_hash"])
        self.assertEqual(normalize_spec(legacy), legacy)
        self.assertEqual(normalize_spec(protected), protected)
        changed = copy.deepcopy(legacy)
        changed["params"]["risk_stop_atr_mult"] = 1.5
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            normalize_spec(changed)

    def test_explicit_warmup_can_use_the_mathematical_minimum(self):
        cases = (
            ("s03_tsmom_vol_target", {"L": 96, "W": 96}, 98),
            ("s04_btc_eth_pairs", {"L": 32}, 33),
            ("s05_bollinger_fade", {"N": 32}, 33),
            ("s06_volume_climax_reversal", {"volume_window": 96}, 97),
            ("s09_trend_pullback", {"ma_slow": 96, "S": 16}, 113),
            ("s14_vol_compression_release", {"atr_window": 14,
                                             "compression_lookback": 100}, 116),
            ("s16_weekly_seasonality", {}, 3),
            ("s18_drawdown_recovery", {"N": 168}, 170),
        )
        for strategy_id, params, required in cases:
            with self.subTest(strategy_id=strategy_id):
                normalized = normalize_spec({"strategy_id": strategy_id,
                                             "params": params,
                                             "min_warmup_bars": 1})
                self.assertEqual(normalized["min_warmup_bars"], required)
                stored = normalize_spec({"strategy_id": strategy_id,
                                         "params": params,
                                         "min_warmup_bars": required + 20})
                self.assertEqual(stored["min_warmup_bars"], required + 20)
                self.assertEqual(normalize_spec(stored), stored)

    def test_atr_opt_in_extends_short_mechanism_warmup(self):
        protected = normalize_spec({"strategy_id": "s16_weekly_seasonality",
                                    "params": {"risk_stop_atr_mult": 1.5,
                                               "atr_window": 14},
                                    "min_warmup_bars": 3})
        self.assertEqual(protected["min_warmup_bars"], 16)

    def test_both_sides_stop_at_the_frozen_entry_atr_boundary(self):
        params = normalize_spec({"strategy_id": "s02_dual_thrust",
                                 "params": {"risk_stop_atr_mult": 1.5}})["params"]
        for side, close, stop in ((1, 97.0, 97.0), (-1, 103.0, 103.0)):
            with self.subTest(side=side):
                rows = latest(candles(40), close=close, high=150, low=50)
                pos = {"side": side, "entry_price": 100.0, "held_bars": 2,
                       "entry_atr": 2.0, "entry_evidence": {"atr": 20.0}}
                target, reason, evidence, _ = _single("s02_dual_thrust", rows, params, pos)
                self.assertEqual((target, reason), (0, "stop_loss_close_confirmed"))
                self.assertEqual(evidence["stop_level"], stop)
                self.assertEqual(evidence["entry_atr"], 2.0)
                self.assertEqual(evidence["risk_stop_atr_mult"], 1.5)

    def test_intrabar_touch_does_not_override_close_confirmation(self):
        params = normalize_spec({"strategy_id": "s07_surge_continuation",
                                 "params": {"risk_stop_atr_mult": 1.5}})["params"]
        for side, close in ((1, 98.0), (-1, 102.0)):
            with self.subTest(side=side):
                rows = latest(candles(120), close=close, high=110, low=90,
                              bought=80 if side == 1 else 20)
                pos = {"side": side, "entry_price": 100.0, "held_bars": 1,
                       "entry_evidence": {"atr": 2.0}}
                target, reason, evidence, _ = _single("s07_surge_continuation", rows, params, pos)
                self.assertIsNone(target)
                self.assertEqual(reason, "position_held")
                self.assertEqual(evidence["stop_level"], 100.0 - side * 3.0)

    def test_missing_entry_atr_is_not_replaced_by_current_volatility(self):
        params = normalize_spec({"strategy_id": "s02_dual_thrust",
                                 "params": {"risk_stop_atr_mult": 1.5}})["params"]
        rows = latest(candles(40), close=90.0)
        for missing in (None, 0, float("nan")):
            with self.subTest(entry_atr=missing):
                pos = {"side": 1, "entry_price": 100.0, "held_bars": 2,
                       "entry_atr": missing}
                target, reason, _, _ = _single("s02_dual_thrust", rows, params, pos)
                self.assertIsNone(target)
                self.assertEqual(reason, "frozen_entry_atr_unavailable")

    def test_legacy_position_without_entry_atr_keeps_its_mechanism_exit(self):
        rows = latest(candles(), close=99.5)
        signal = account("s02_dual_thrust", rows,
                         positions={BTC: position(rows, held=2)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "opposite_range_breakout")
        self.assertNotIn("stop_level", signal["evidence"])


class TenMechanismTests(unittest.TestCase):
    def test_s01_fixed_session_range_first_breakout_only(self):
        rows = latest(candles(61), close=101)
        signal = account("s01_orb_breakout", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertAlmostEqual(signal["evidence"]["session_high"], 100.3)
        # A later breakout is not another entry, including after a previous
        # paper position has closed. The day's historical first is consumed.
        extra = copy.deepcopy(rows[-1])
        extra["timestamp"] += BAR_MS
        later = account("s01_orb_breakout", rows + [extra])["signals"][BTC]
        self.assertIsNone(later["target_side"])
        self.assertEqual(later["reason"], "daily_first_entry_already_consumed")

    def test_s01_session_first_n_bars_cannot_be_partial(self):
        rows = latest(candles(100), close=101)[20:]
        signal = account("s01_orb_breakout", rows)["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "session_range_not_ready")

    def test_s01_day_end_flat_and_reverse_range_close(self):
        rows = latest(candles(288), close=101)
        signal = account("s01_orb_breakout", rows,
                         positions={BTC: position(rows, held=20)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "session_day_end")
        rows = latest(candles(100), close=99)
        signal = account("s01_orb_breakout", rows,
                         positions={BTC: position(rows, held=10)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "opposite_session_breakout")

    def test_s02_current_open_center_and_prior_true_range(self):
        rows = latest(candles(), open_price=105, close=105.4, high=150, low=90)
        signal = account("s02_dual_thrust", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertAlmostEqual(signal["evidence"]["range_mean"], 0.6)
        self.assertAlmostEqual(signal["evidence"]["upper"], 105.3)
        self.assertEqual(signal["evidence"]["center_open"], 105)

    def test_s02_reverse_breakout_closes_actual_position(self):
        rows = latest(candles(), close=99.5)
        signal = account("s02_dual_thrust", rows,
                         positions={BTC: position(rows, held=2)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "opposite_range_breakout")

    def test_s03_momentum_and_prior_annualized_volatility_size(self):
        prices = [100 + 0.02 * index + 0.5 * math.sin(index) for index in range(750)]
        rows = candles(prices=prices)
        signal = account("s03_tsmom_vol_target", rows)["signals"][BTC]
        returns = [prices[index] / prices[index - 1] - 1 for index in range(461, 749)]
        rv = stdev(returns) * math.sqrt(365 * 288)
        self.assertEqual(signal["target_side"], 1)
        self.assertAlmostEqual(signal["evidence"]["realized_vol_annual"], rv)
        self.assertAlmostEqual(signal["size_multiplier"], min(0.2 / rv, 1))
        self.assertEqual(signal["evidence"]["rebalance"], "entry_or_flip_only")

    def test_s03_multiplier_never_exceeds_one(self):
        prices = [100 + 0.01 * index + 0.0001 * math.sin(index) for index in range(750)]
        signal = account("s03_tsmom_vol_target", candles(prices=prices))["signals"][BTC]
        self.assertEqual(signal["size_multiplier"], 1)

    def test_s03_zero_variance_is_not_divided_by_zero(self):
        signal = account("s03_tsmom_vol_target", candles())["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "realized_volatility_zero")

    def test_s03_zero_variance_still_exits_when_existing_direction_changes(self):
        for close in (90, 100):
            rows = latest(candles(), close=close)
            signal = account("s03_tsmom_vol_target", rows,
                             positions={BTC: position(rows, held=2)})["signals"][BTC]
            with self.subTest(close=close):
                self.assertEqual(signal["target_side"], 0)
                self.assertEqual(signal["reason"], "momentum_reversed_without_volatility")

    def test_s11_requires_momentum_direction_and_bollinger_pullback(self):
        # Positive long-horizon momentum alone is insufficient; the current
        # close must also be at/below the lower band.
        prices = [100 + index * 0.02 for index in range(750)]
        rows = candles(prices=prices)
        signal = account("s11_tsmom_bollinger_pullback", rows)["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "no_momentum_bollinger_alignment")
        self.assertGreater(signal["evidence"]["momentum"], 0)
        self.assertEqual(signal["evidence"]["combination"],
                         "momentum_direction_and_bollinger_pullback")

        # The same positive trend with a sufficiently deep pullback produces
        # a long target and uses the volatility-targeted entry size.
        # The frozen Z=2 threshold requires a pullback of roughly 2.0 USDT
        # for this steadily rising fixture; keep the long-horizon momentum
        # positive while making the Bollinger condition unambiguous.
        prices[-1] = prices[-2] - 3.0
        rows = candles(prices=prices)
        signal = account("s11_tsmom_bollinger_pullback", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertEqual(signal["reason"], "momentum_confirmed_bollinger_pullback")
        self.assertLessEqual(signal["size_multiplier"], 1.0)

    def test_s11_exits_on_mean_reversion_or_momentum_reversal(self):
        prices = [100 + index * 0.02 for index in range(750)]
        rows = candles(prices=prices)
        pos = position(rows, side=1, entry=100)
        latest(rows, close=rows[-2]["close"] - 1.5)
        held = account("s11_tsmom_bollinger_pullback", rows,
                       positions={BTC: pos})["signals"][BTC]
        self.assertIsNone(held["target_side"])
        latest(rows, close=rows[-2]["close"] + 0.2)
        exited = account("s11_tsmom_bollinger_pullback", rows,
                         positions={BTC: pos})["signals"][BTC]
        self.assertEqual(exited["target_side"], 0)
        self.assertEqual(exited["reason"], "mean_reversion_complete")

    def test_s04_atomic_equal_notional_both_directions(self):
        for btc_close, sides in ((102, (1, -1)), (98, (-1, 1))):
            result = account("s04_btc_eth_pairs", latest(candles(), close=btc_close), candles())
            with self.subTest(btc_close=btc_close):
                self.assertEqual(result["group"], {"atomic": True, "legs": [BTC, ETH]})
                self.assertEqual(tuple(result["signals"][pair]["target_side"] for pair in (BTC, ETH)),
                                 sides)
                self.assertTrue(result["signals"][BTC]["evidence"]["equal_notional"])

    def test_s04_whole_pair_exit_and_orphan_recovery(self):
        rows = candles()
        positions = {BTC: position(rows), ETH: position(rows, side=-1)}
        result = account("s04_btc_eth_pairs", rows, positions=positions)
        self.assertEqual([result["signals"][pair]["target_side"] for pair in (BTC, ETH)], [0, 0])
        self.assertEqual(result["signals"][BTC]["reason"], "relative_strength_converged")
        result = account("s04_btc_eth_pairs", rows, positions={BTC: position(rows)})
        self.assertEqual([result["signals"][pair]["target_side"] for pair in (BTC, ETH)], [0, 0])
        self.assertEqual(result["signals"][BTC]["reason"], "atomic_position_inconsistent")

    def test_s05_deviation_uses_previous_window_and_exits_on_mean(self):
        prices = [99 if index % 2 else 101 for index in range(750)]
        rows = latest(candles(prices=prices), close=97)
        signal = account("s05_bollinger_fade", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertEqual(signal["evidence"]["prior_mean"], 100)
        latest(rows, close=100)
        signal = account("s05_bollinger_fade", rows,
                         positions={BTC: position(rows, entry=97)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "mean_reversion_complete")

    def test_s06_prior_volume_median_and_contrarian_price_direction(self):
        for close, target in ((102, -1), (98, 1)):
            rows = latest(candles(), close=close, volume=1000)
            signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
            with self.subTest(close=close):
                self.assertEqual(signal["target_side"], target)
                self.assertEqual(signal["evidence"]["baseline_volume"], 100)
                self.assertEqual(signal["evidence"]["volume_ratio"], 10)
                self.assertEqual(signal["evidence"]["mv_window"], 3)

    def test_s06_uses_three_bar_move_not_single_bar_move(self):
        # A one-bar move can be extreme while the three-bar displacement is
        # small; the corrected rule must not use the former.
        prices = [100.0] * 750
        prices[-4:] = [100.0, 100.0, 99.0, 100.1]
        rows = latest(candles(prices=prices), volume=800)
        signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertAlmostEqual(signal["evidence"]["price_move_3"], 0.001)

        # Conversely, a three-bar move can cross the threshold while the
        # latest one-bar move does not.
        prices[-4:] = [100.0, 100.2, 100.5, 101.2]
        rows = latest(candles(prices=prices), volume=800)
        signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], -1)
        self.assertAlmostEqual(signal["evidence"]["price_move_3"], 0.012)

    def test_s06_exact_price_and_volume_thresholds_are_inclusive(self):
        rows = candles(prices=[2049.0] * 750)
        latest(rows, close=2069.49, volume=800)
        signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], -1)
        self.assertEqual(signal["evidence"]["price_move"], 0.01)
        latest(rows, close=2069.4899999)
        self.assertIsNone(account("s06_volume_climax_reversal", rows)["signals"][BTC]["target_side"])

    def test_s06_current_volume_is_excluded_from_prior_median(self):
        rows = candles(prices=[100.0] * 750)
        for row in rows[-289:-1]:
            row["volume"] = 100.0
        latest(rows, close=101.0, volume=800.0)
        signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
        self.assertEqual(signal["evidence"]["baseline_volume"], 100.0)
        self.assertEqual(signal["evidence"]["volume_ratio"], 8.0)
        self.assertEqual(signal["target_side"], -1)

    def test_s06_even_window_median_does_not_round_exact_volume_boundary_away(self):
        rows = candles(prices=[2049.0] * 750)
        for index, row in enumerate(rows[-289:-1]):
            row["volume"] = 1200.101 if index < 144 else 1200.102
        latest(rows, close=2069.49, volume=9600.812)
        signal = account("s06_volume_climax_reversal", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], -1)
        self.assertEqual(signal["evidence"]["baseline_volume"], 1200.1015)

    def test_s07_recompute_bought_fraction_ignores_stored_imbalance(self):
        rows = latest(candles(), bought=75)
        rows[-1]["extras"]["flow_imbal"] = -1
        signal = account("s07_surge_continuation", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertEqual(signal["evidence"]["flow_imbal"], 0.5)
        latest(rows, bought=25)
        self.assertEqual(account("s07_surge_continuation", rows)["signals"][BTC]["target_side"], -1)

    def test_s07_low_volume_and_invalid_taker_amount_do_not_enter(self):
        rows = latest(candles(), volume=10, bought=8)
        signal = account("s07_surge_continuation", rows)["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "orderflow_volume_filter_not_met")
        latest(rows, volume=100, bought=101)
        signal = account("s07_surge_continuation", rows)["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "taker_volume_unavailable")

    def test_s07_exact_threshold_is_inclusive_without_admitting_subthreshold(self):
        for bought, side in ((60, 1), (40, -1), (59.999999999, None),
                             (40.000000001, None)):
            rows = latest(candles(), bought=bought)
            signal = account("s07_surge_continuation", rows)["signals"][BTC]
            with self.subTest(bought=bought):
                self.assertEqual(signal["target_side"], side)

    def test_s07_reversal_exit_works_even_below_entry_volume_filter(self):
        rows = latest(candles(), volume=10, bought=2)
        signal = account("s07_surge_continuation", rows,
                         positions={BTC: position(rows)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "orderflow_direction_reversed")

    def test_s08_long_lower_wick_and_short_upper_wick(self):
        for values, expected in (((100, 101, 101.1, 96), 1), ((101, 100, 106, 99.9), -1)):
            op, close, hi, lo = values
            rows = latest(candles(), open_price=op, close=close, high=hi, low=lo)
            signal = account("s08_wick_rejection", rows)["signals"][BTC]
            with self.subTest(values=values):
                self.assertEqual(signal["target_side"], expected)
                self.assertAlmostEqual(signal["evidence"]["atr"], 0.6)

    def test_s08_risk_uses_frozen_atr_close_not_high_low_touch(self):
        rows = latest(candles(), open_price=100, close=99, high=110, low=90)
        pos = position(rows, entry_evidence={"atr": 2})
        signal = account("s08_wick_rejection", rows, positions={BTC: pos})["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["evidence"]["stop_level"], 98)
        self.assertEqual(signal["evidence"]["take_profit_level"], 103)
        latest(rows, close=97)
        signal = account("s08_wick_rejection", rows, positions={BTC: pos})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "stop_loss_close_confirmed")
        latest(rows, close=103)
        signal = account("s08_wick_rejection", rows, positions={BTC: pos})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "take_profit_close_confirmed")

    def test_s08_missing_frozen_atr_cannot_recompute_from_later_bars(self):
        rows = latest(candles(), close=90)
        signal = account("s08_wick_rejection", rows,
                         positions={BTC: position(rows)})["signals"][BTC]
        self.assertIsNone(signal["target_side"])
        self.assertEqual(signal["reason"], "frozen_entry_atr_unavailable")

    def test_s08_exact_wick_geometry_threshold_and_close_levels(self):
        rows = candles(prices=[2000.79] * 750)
        latest(rows, open_price=2000.79, high=2001.23, low=2000.13, close=2001.03)
        signal = account("s08_wick_rejection", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertEqual(signal["evidence"]["lower_wick_ratio"], 0.6)
        for entry, atr, close, reason in (
                (2000.01, 0.13, 1999.88, "stop_loss_close_confirmed"),
                (2000.13, 0.1, 2000.28, "take_profit_close_confirmed")):
            latest(rows, close=close)
            pos = position(rows, entry=entry, entry_evidence={"atr": atr})
            signal = account("s08_wick_rejection", rows, positions={BTC: pos})["signals"][BTC]
            with self.subTest(reason=reason):
                self.assertEqual(signal["target_side"], 0)
                self.assertEqual(signal["reason"], reason)
                self.assertEqual(signal["evidence"]["implementation_revision"],
                                 "decimal-threshold-correction-v2")

    def test_s09_prior_slow_slope_and_fast_ma_pullback(self):
        prices = [100 + index * 0.01 for index in range(750)]
        rows = candles(prices=prices)
        fast = sum(prices[-25:-1]) / 24
        latest(rows, close=fast, open_price=fast)
        signal = account("s09_trend_pullback", rows)["signals"][BTC]
        self.assertEqual(signal["target_side"], 1)
        self.assertAlmostEqual(signal["evidence"]["slow_slope"], 0.48)
        self.assertAlmostEqual(signal["evidence"]["prior_fast_mean"], fast)
        latest(rows, close=100)
        signal = account("s09_trend_pullback", rows,
                         positions={BTC: position(rows)})["signals"][BTC]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "slow_trend_broken")

    def test_s10_only_eth_trades_btc_source_and_exits_after_actual_h(self):
        btc, eth = latest(candles(), close=101), candles()
        result = account("s10_lead_lag_btc_eth", btc, eth)
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["reason"], "signal_source_only")
        self.assertEqual(result["signals"][ETH]["target_side"], 1)
        self.assertAlmostEqual(result["signals"][ETH]["evidence"]["btc_return"], 0.01)
        result = account("s10_lead_lag_btc_eth", btc, eth,
                         positions={ETH: position(eth, held=3)})
        self.assertEqual(result["signals"][ETH]["target_side"], 0)
        self.assertEqual(result["signals"][ETH]["reason"], "holding_period_elapsed")

    def test_actual_holding_limit_counts_entry_candle_once(self):
        rows = candles()
        result = account("s07_surge_continuation", rows,
                         positions={BTC: position(rows, held=6)})
        self.assertEqual(result["signals"][BTC]["target_side"], 0)
        self.assertEqual(result["signals"][BTC]["evidence"]["held_bars"], 6)
        self.assertEqual(result["signals"][BTC]["reason"], "holding_period_elapsed")


if __name__ == "__main__":
    unittest.main()
