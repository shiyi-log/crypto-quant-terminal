"""Causality, anchor completeness, and actual-position risk exits for Fnew."""

import copy
import unittest
from statistics import stdev

from strategy_fnew import BTC, ETH, TRX, load_fnew_specs
from strategy_fnew_signals import DAY_MS, TIMEFRAME_MS, _history, evaluate_fnew


def spec_for(number, *, k=1.5, paired_execution_enabled=False):
    return next(copy.deepcopy(spec) for spec in load_fnew_specs(
        paired_execution_enabled=paired_execution_enabled)
                if spec["canonical_strategy_id"].startswith(f"s{number}_")
                and spec["max_loss_per_trade_usdt"] == 50
                and spec["params"]["risk_stop_atr_mult"] == k)


def candles(count=120, *, timeframe="1h", start=10 * DAY_MS, quote=True):
    result = [{"timestamp": start + index * TIMEFRAME_MS[timeframe],
               "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
               "volume": 100.0, "extras": {"closed": True}}
              for index in range(count)]
    if quote:
        for row in result:
            row["extras"]["quote_volume"] = 10_000.0
    return result


def set_price(row, close, *, opening=None):
    row["close"] = close
    row["open"] = close if opening is None else opening
    row["high"] = max(row["high"], row["open"], close)
    row["low"] = min(row["low"], row["open"], close)
    return row


def market_input(spec, rows, *, spot=None):
    result = {pair: copy.deepcopy(rows) for pair in spec["data_universe"]}
    if spot is not None:
        result["spot"] = {pair: copy.deepcopy(spot)
                          for pair in spec["data_feeds"]["spot"]["pairs"]}
    return result


def position(rows, side=1, *, entry_price=100.0, entry_atr=1.0, **extras):
    return {"side": side, "entry_price": entry_price,
            "opened_candle_ms": rows[-1]["timestamp"], "entry_atr": entry_atr, **extras}


def basis_input(*, last_basis=0.02):
    spec = spec_for(33)
    future, spot = candles(), candles()
    for index, row in enumerate(future):
        basis = 0.001 if index % 2 else -0.001
        set_price(row, 100 * (1 + basis))
    set_price(future[-1], 100 * (1 + last_basis))
    return spec, future, market_input(spec, future, spot=spot)


def vwap_input(*, last_price=105.0, quote=True):
    spec = spec_for(34)
    rows = candles(100, timeframe="15m", start=10 * DAY_MS - 80 * TIMEFRAME_MS["15m"],
                   quote=quote)
    set_price(rows[-1], last_price, opening=100.0)
    return spec, rows, market_input(spec, rows)


class FnewHistoryTests(unittest.TestCase):
    def test_impossible_quote_base_price_cannot_create_vwap_signal(self):
        spec, rows, _ = vwap_input(last_price=100)
        rows[-1]["extras"]["quote_volume"] = 1
        result = evaluate_fnew(spec, market_input(spec, rows))
        self.assertFalse(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "invalid_quote_volume")
        self.assertIsNone(result["signals"][BTC]["target_side"])

    def test_completed_suffix_preserves_real_quote_volume(self):
        rows = candles(4)
        rows[0]["extras"]["closed"] = False
        rows[1]["extras"].pop("closed")
        history, error = _history(rows, None, bar_ms=TIMEFRAME_MS["1h"])
        self.assertIsNone(error)
        self.assertEqual([row["timestamp"] for row in history],
                         [row["timestamp"] for row in rows[2:]])
        self.assertEqual(history[-1]["extras"]["quote_volume"], 10_000)

    def test_future_invalid_price_cannot_contaminate_causal_history(self):
        rows = candles(4)
        rows[-1]["close"] = float("nan")
        history, error = _history(rows, rows[-1]["timestamp"], bar_ms=TIMEFRAME_MS["1h"])
        self.assertIsNone(error)
        self.assertEqual(len(history), 3)

    def test_gap_retains_suffix_without_cross_gap_indicator(self):
        rows = candles(5)
        del rows[2]
        history, error = _history(rows, None, bar_ms=TIMEFRAME_MS["1h"])
        self.assertIsNone(error)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["timestamp"], rows[-2]["timestamp"])

    def test_duplicate_unaligned_and_invalid_fields_fail_closed(self):
        changes = ("duplicate", "unaligned", "price", "quote")
        expected = ("duplicate_closed_candle_timestamp", "invalid_candle_timestamp",
                    "invalid_closed_candle_ohlcv", "invalid_quote_volume")
        for change, reason in zip(changes, expected, strict=True):
            with self.subTest(change=change):
                rows = candles(5)
                if change == "duplicate":
                    rows.append(copy.deepcopy(rows[-1]))
                elif change == "unaligned":
                    rows[-1]["timestamp"] += 1
                elif change == "price":
                    rows[-1]["close"] = 200
                else:
                    rows[-1]["extras"]["quote_volume"] = float("inf")
                history, error = _history(rows, None, bar_ms=TIMEFRAME_MS["1h"])
                self.assertEqual(history, [])
                self.assertEqual(error, reason)


class BasisSignalTests(unittest.TestCase):
    def test_both_directions_use_prior_delta_sample_sigma(self):
        for last_basis, side in ((0.02, -1), (-0.02, 1)):
            with self.subTest(side=side):
                spec, rows, feeds = basis_input(last_basis=last_basis)
                before = copy.deepcopy(feeds)
                result = evaluate_fnew(spec, feeds, as_of_utc=rows[-1]["timestamp"] + 3_600_000)
                self.assertTrue(result["ready"])
                tail_bases = [row["close"] / 100 - 1 for row in rows[-102:]]
                deltas = [b - a for a, b in zip(tail_bases, tail_bases[1:])]
                for signal in result["signals"].values():
                    self.assertEqual(signal["target_side"], side)
                    self.assertAlmostEqual(signal["evidence"]["basis_sigma"], stdev(deltas[:-1]))
                    self.assertEqual(signal["evidence"]["atr"], 2.0)
                    self.assertEqual(signal["evidence"]["pnl_exposure"],
                                     "unhedged_perpetual;not_pure_basis_return")
                self.assertEqual(before, feeds)

    def test_missing_stale_shifted_and_partial_spot_never_forward_fill(self):
        for defect in ("missing", "shifted", "partial", "gap"):
            with self.subTest(defect=defect):
                spec, rows, feeds = basis_input()
                spot_pair = spec["data_feeds"]["spot"]["pair_map"][BTC]
                if defect == "missing":
                    del feeds["spot"][spot_pair]
                elif defect == "shifted":
                    for row in feeds["spot"][spot_pair]:
                        row["timestamp"] -= 3_600_000
                elif defect == "partial":
                    feeds["spot"][spot_pair][-1]["extras"]["closed"] = False
                else:
                    del feeds["spot"][spot_pair][-20]
                result = evaluate_fnew(spec, feeds)
                self.assertFalse(result["ready"])
                self.assertTrue(all(signal["target_side"] is None
                                    for signal in result["signals"].values()))
                self.assertFalse(result["basis_feed_ready"])

    def test_simultaneously_old_feeds_are_stale_at_observation_time(self):
        spec, rows, feeds = basis_input()
        result = evaluate_fnew(spec, feeds, as_of_utc=rows[-1]["timestamp"] + 7_200_000)
        self.assertFalse(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "fnew_closed_history_stale")

    def test_zero_sigma_is_no_entry_and_reverted_delta_closes_existing(self):
        spec = spec_for(33)
        rows = candles()
        feeds = market_input(spec, rows, spot=rows)
        no_entry = evaluate_fnew(spec, feeds)
        self.assertEqual(no_entry["signals"][BTC]["reason"], "basis_delta_sigma_zero")
        for side in (-1, 1):
            result = evaluate_fnew(spec, feeds, {BTC: position(rows, side)})
            self.assertEqual(result["signals"][BTC]["target_side"], 0)
            self.assertEqual(result["signals"][BTC]["reason"], "basis_delta_reverted")

    def test_spot_outage_preserves_actual_perpetual_risk_exits(self):
        spec, rows, feeds = basis_input()
        feeds["spot"] = {}
        set_price(rows[-1], 98.5)
        feeds.update(market_input(spec, rows))
        result = evaluate_fnew(spec, feeds, {BTC: position(rows)})
        self.assertTrue(result["ready"])
        self.assertEqual(result["warmup"]["reason"], "ready_for_risk_exits_only")
        self.assertEqual(result["signals"][BTC]["target_side"], 0)
        self.assertEqual(result["signals"][BTC]["reason"], "stop_loss_close_confirmed")
        self.assertIsNone(result["signals"][ETH]["target_side"])
        self.assertIsNone(result["signals"][TRX]["target_side"])
        evidence = result["signals"][BTC]["evidence"]
        self.assertFalse(evidence["basis_feed_ready"])
        self.assertEqual(evidence["entry_atr"], 1.0)

    def test_spot_outage_still_allows_holding_period_exit(self):
        spec, rows, feeds = basis_input()
        feeds["spot"] = {}
        pos = position(rows, entry_atr=100)
        pos["opened_candle_ms"] -= (spec["params"]["H"] - 1) * TIMEFRAME_MS["1h"]
        result = evaluate_fnew(spec, feeds, {BTC: pos})
        self.assertTrue(result["ready"])
        self.assertEqual(result["signals"][BTC]["reason"], "holding_period_elapsed")
        self.assertEqual(result["signals"][BTC]["target_side"], 0)


class VwapSignalTests(unittest.TestCase):
    def test_raw_quote_volume_wins_over_ohlc_proxy_for_both_sides(self):
        for close, side in ((105.0, -1), (95.0, 1)):
            with self.subTest(side=side):
                spec, rows, feeds = vwap_input(last_price=close)
                result = evaluate_fnew(spec, feeds)
                for signal in result["signals"].values():
                    self.assertEqual(signal["target_side"], side)
                    self.assertEqual(signal["evidence"]["vwap"], 100)
                    self.assertEqual(signal["evidence"]["vwap_source"], "quote_volume")
                    self.assertEqual(signal["evidence"]["vwap_bars"], 20)
                    self.assertEqual(signal["evidence"]["atr"], 2)

    def test_quote_missing_fallback_is_explicit_and_weighted(self):
        spec, rows, feeds = vwap_input(quote=False)
        rows[-2]["volume"] = 1_000
        set_price(rows[-2], 99)
        feeds = market_input(spec, rows)
        result = evaluate_fnew(spec, feeds)
        day_rows = rows[-20:]
        expected = sum((row["high"] + row["low"] + row["close"]) / 3 * row["volume"]
                       for row in day_rows) / sum(row["volume"] for row in day_rows)
        evidence = result["signals"][BTC]["evidence"]
        self.assertAlmostEqual(evidence["vwap"], expected)
        self.assertEqual(evidence["vwap_source"], "hlc3_volume_proxy")
        self.assertEqual(evidence["vwap_proxy_rows"], 20)

    def test_missing_day_start_or_gap_cannot_create_a_fake_anchor(self):
        spec = spec_for(34)
        for defect in ("missing_start", "gap"):
            with self.subTest(defect=defect):
                rows = candles(90, timeframe="15m", start=10 * DAY_MS)
                if defect == "missing_start":
                    rows = rows[1:]
                else:
                    del rows[20]
                set_price(rows[-1], 150)
                result = evaluate_fnew(spec, market_input(spec, rows))
                self.assertEqual(result["signals"][BTC]["reason"], "vwap_session_start_missing")
                self.assertIsNone(result["signals"][BTC]["target_side"])

    def test_zero_session_volume_prevents_entry(self):
        spec, rows, _ = vwap_input()
        for row in rows[-20:]:
            row["volume"] = 0
            row["extras"]["quote_volume"] = 0
        result = evaluate_fnew(spec, market_input(spec, rows))
        self.assertEqual(result["signals"][BTC]["reason"], "vwap_session_volume_zero")
        self.assertIsNone(result["signals"][BTC]["target_side"])

    def test_daily_boundary_rejects_entries_and_closes_open_anchor_position(self):
        spec = spec_for(34)
        rows = candles(180, timeframe="15m", start=10 * DAY_MS - 84 * 900_000)
        set_price(rows[-1], 105)
        feeds = market_input(spec, rows)
        empty = evaluate_fnew(spec, feeds)
        self.assertEqual(empty["signals"][BTC]["reason"], "vwap_session_end_no_entry")
        self.assertIsNone(empty["signals"][BTC]["target_side"])
        pos = position(rows, entry_evidence={"vwap_day_ms": 10 * DAY_MS})
        opened = evaluate_fnew(spec, feeds, {BTC: pos})
        self.assertEqual(opened["signals"][BTC]["reason"], "vwap_anchor_session_elapsed")
        self.assertEqual(opened["signals"][BTC]["target_side"], 0)

    def test_anchor_crossing_closes_even_when_close_jumps_over_exit_band(self):
        spec, rows, feeds = vwap_input(last_price=101)
        pos = position(rows, entry_price=98, entry_evidence={"vwap_day_ms": 10 * DAY_MS})
        result = evaluate_fnew(spec, feeds, {BTC: pos})
        self.assertEqual(result["signals"][BTC]["reason"], "vwap_anchor_reached")
        self.assertEqual(result["signals"][BTC]["target_side"], 0)


class CascadeRiskTests(unittest.TestCase):
    def test_invalid_quote_blocks_anchor_entry_but_not_valid_price_risk_exit(self):
        spec, rows, _ = vwap_input(last_price=98.5)
        rows[-1]["extras"]["quote_volume"] = 1
        result = evaluate_fnew(spec, market_input(spec, rows), {BTC: position(rows)})
        self.assertTrue(result["ready"])
        self.assertFalse(result["entry_ready"])
        self.assertEqual(result["signals"][BTC]["target_side"], 0)
        self.assertEqual(result["signals"][BTC]["reason"], "stop_loss_close_confirmed")
        self.assertEqual(result["signals"][BTC]["evidence"]["quote_volume_validation_error"],
                         "invalid_quote_volume")
        self.assertEqual(result["signals"][BTC]["candle_utc"], rows[-1]["timestamp"])

    def test_other_pair_outage_and_own_short_suffix_do_not_block_frozen_stop(self):
        spec = spec_for(35)
        for defect in ("other_pair_outage", "own_short_suffix"):
            with self.subTest(defect=defect):
                rows = candles(80, timeframe="15m")
                set_price(rows[-1], 98.5)
                feeds = market_input(spec, rows)
                if defect == "other_pair_outage":
                    feeds[ETH] = []
                else:
                    feeds[BTC] = rows[-1:]
                result = evaluate_fnew(spec, feeds, {BTC: position(rows)})
                self.assertTrue(result["ready"])
                self.assertFalse(result["entry_ready"])
                self.assertEqual(result["warmup"]["reason"], "ready_for_risk_exits_only")
                self.assertEqual(result["signals"][BTC]["target_side"], 0)
                self.assertEqual(result["signals"][BTC]["reason"], "stop_loss_close_confirmed")
                self.assertIsNone(result["signals"][ETH]["target_side"])

    def test_current_zero_atr_does_not_block_existing_horizon_exit(self):
        for number in (33, 34, 35):
            with self.subTest(number=number):
                spec = spec_for(number)
                rows = candles(120, timeframe=spec["timeframe"])
                for row in rows:
                    row.update(high=100, low=100)
                pos = position(rows)
                pos["opened_candle_ms"] -= (spec["params"]["H"] - 1) * TIMEFRAME_MS[spec["timeframe"]]
                if number == 34:
                    pos["entry_evidence"] = {"vwap_day_ms": rows[-1]["timestamp"] // DAY_MS * DAY_MS}
                result = evaluate_fnew(spec, market_input(spec, rows,
                                                       spot=rows if number == 33 else None), {BTC: pos})
                self.assertEqual(result["signals"][BTC]["target_side"], 0)
                self.assertEqual(result["signals"][BTC]["reason"], "holding_period_elapsed")

    def test_incomplete_or_empty_vwap_session_cannot_block_independent_risk_exits(self):
        spec = spec_for(34)
        for defect in ("missing_start_horizon", "empty_new_day"):
            with self.subTest(defect=defect):
                rows = candles(89, timeframe="15m", start=10 * DAY_MS + 900_000)
                pos = position(rows, entry_evidence={"vwap_day_ms": 10 * DAY_MS})
                if defect == "missing_start_horizon":
                    pos["opened_candle_ms"] -= (spec["params"]["H"] - 1) * 900_000
                    expected = "holding_period_elapsed"
                else:
                    for row in rows:
                        row["volume"] = 0
                        row["extras"]["quote_volume"] = 0
                    pos["entry_evidence"]["vwap_day_ms"] -= DAY_MS
                    expected = "vwap_anchor_session_elapsed"
                result = evaluate_fnew(spec, market_input(spec, rows), {BTC: pos})
                self.assertEqual(result["signals"][BTC]["target_side"], 0)
                self.assertEqual(result["signals"][BTC]["reason"], expected)

    def test_three_bodies_with_atr_move_enter_continuation_without_wick_filter(self):
        spec = spec_for(35)
        for side in (-1, 1):
            with self.subTest(side=side):
                rows = candles(80, timeframe="15m")
                for row, previous, close in zip(rows[-3:], (100, 100 + side, 100 + 2 * side),
                                                (100 + side, 100 + 2 * side, 100 + 10 * side),
                                                strict=True):
                    set_price(row, close, opening=previous)
                result = evaluate_fnew(spec, market_input(spec, rows))
                signal = result["signals"][BTC]
                self.assertEqual(signal["target_side"], side)
                self.assertEqual(signal["reason"], "cascade_continuation_proxy")
                self.assertIn("no_observed_liquidation_feed", signal["evidence"]["liquidation_data"])
                self.assertIn("no_wick_exhaustion_filter", signal["evidence"]["comparison_to_s21"])

    def test_mixed_body_or_insufficient_move_cannot_trigger(self):
        spec = spec_for(35)
        for kind in ("mixed", "small"):
            with self.subTest(kind=kind):
                rows = candles(80, timeframe="15m")
                for index, row in enumerate(rows[-3:]):
                    set_price(row, 100 + (index + 1) / 10, opening=100 + index / 10)
                if kind == "mixed":
                    rows[-2]["open"] = rows[-2]["close"] + 0.1
                    set_price(rows[-1], 110, opening=100)
                result = evaluate_fnew(spec, market_input(spec, rows))
                self.assertIsNone(result["signals"][BTC]["target_side"])

    def test_all_single_rules_use_frozen_entry_atr_for_each_width_and_direction(self):
        for number in (33, 34, 35):
            for k in (1.0, 1.5, 2.5):
                for side in (-1, 1):
                    with self.subTest(number=number, k=k, side=side):
                        spec = spec_for(number, k=k)
                        rows = candles(120, timeframe=spec["timeframe"])
                        set_price(rows[-1], 100 - side * k)
                        # Current broad range cannot move a frozen stop.
                        rows[-1].update(high=150, low=50)
                        feeds = market_input(spec, rows, spot=candles(120) if number == 33 else None)
                        result = evaluate_fnew(spec, feeds, {BTC: position(rows, side)})
                        signal = result["signals"][BTC]
                        self.assertEqual(signal["target_side"], 0)
                        self.assertEqual(signal["reason"], "stop_loss_close_confirmed")
                        self.assertEqual(signal["evidence"]["stop_level"], 100 - side * k)
                        self.assertEqual(signal["evidence"]["entry_atr"], 1)

    def test_missing_frozen_atr_does_not_replace_it_with_current_atr(self):
        spec = spec_for(35)
        rows = candles(80, timeframe="15m")
        set_price(rows[-1], 80)
        pos = position(rows)
        del pos["entry_atr"]
        result = evaluate_fnew(spec, market_input(spec, rows), {BTC: pos})
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["reason"], "frozen_entry_atr_unavailable")

    def test_held_position_exits_at_horizon_and_zero_atr_rejects_entry(self):
        spec = spec_for(35)
        rows = candles(80, timeframe="15m")
        pos = position(rows)
        pos["opened_candle_ms"] -= (spec["params"]["H"] - 1) * 900_000
        result = evaluate_fnew(spec, market_input(spec, rows), {BTC: pos})
        self.assertEqual(result["signals"][BTC]["reason"], "holding_period_elapsed")
        for row in rows:
            row.update(high=100, low=100)
        result = evaluate_fnew(spec, market_input(spec, rows))
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["reason"], "entry_atr_unavailable_or_invalid")


class PairDispatchTests(unittest.TestCase):
    def test_pair_risk_dispatch_needs_only_fresh_selected_two_marks(self):
        spec = spec_for(32, paired_execution_enabled=True)
        rows = candles(1)
        btc_rows = copy.deepcopy(rows)
        set_price(btc_rows[-1], 98)
        feeds = {BTC: btc_rows, ETH: rows, TRX: []}
        shared = {"pair_group": "frozen-pair", "pair_legs": [BTC, ETH],
                  "pair_sides": [1, -1], "pair_entry_prices": {BTC: 100, ETH: 100},
                  "entry_pair_atr_return": 0.01, "risk_stop_atr_mult": 1.5,
                  "group_id": "same-atomic-fill", "quantity": 1.0, "entry_notional": 100.0}
        positions = {pair: position(rows, side, **shared)
                     for pair, side in ((BTC, 1), (ETH, -1))}
        result = evaluate_fnew(spec, feeds, positions, as_of_utc=rows[-1]["timestamp"] + 3_600_000)
        self.assertTrue(result["ready"])
        self.assertTrue(result["execution_enabled"])
        self.assertEqual(result["execution_blockers"], [])
        for pair in (BTC, ETH):
            self.assertEqual(result["signals"][pair]["target_side"], 0)
            self.assertEqual(result["signals"][pair]["reason"], "pair_stop_loss_close_confirmed")
        self.assertIsNone(result["signals"][TRX]["target_side"])

    def test_blocked_pair_retains_diagnostic_but_never_instruction(self):
        spec = spec_for(36)
        prices_a, prices_b = [100.0], [100.0]
        for index in range(336):
            if index < 168:
                a = b = 0.002 if index % 2 else -0.001
            else:
                a = 0.003 if index % 2 else -0.001
                b = -0.002 if index % 2 else 0.001
            prices_a.append(prices_a[-1] * (1 + a))
            prices_b.append(prices_b[-1] * (1 + b))
        feeds = {BTC: candles(337, quote=False), ETH: candles(337, quote=False)}
        for pair, prices in ((BTC, prices_a), (ETH, prices_b)):
            for row, price in zip(feeds[pair], prices, strict=True):
                row.update(open=price, high=price * 1.001, low=price * 0.999, close=price)
        result = evaluate_fnew(spec, feeds)
        self.assertFalse(result["ready"])
        self.assertFalse(result["execution_enabled"])
        for signal in result["signals"].values():
            self.assertIsNone(signal["target_side"])
            self.assertIn(signal["evidence"]["research_candidate_side"], (-1, 1))
            self.assertEqual(signal["reason"], "paired_atr_stop_policy_unimplemented")


if __name__ == "__main__":
    unittest.main()
