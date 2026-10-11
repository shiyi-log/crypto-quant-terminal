from __future__ import annotations

import copy
import importlib.util
import math
import random
import unittest
from unittest.mock import patch

import strategy_pair_signals as pairs


HOUR = 3_600_000


def candles(prices, *, start=0, width=0.002):
    return [{"timestamp": start + index * HOUR, "open": price,
             "high": price * (1 + width), "low": price * (1 - width),
             "close": price, "volume": 100.0, "extras": {"closed": True}}
            for index, price in enumerate(prices)]


def spread_history(count=520, shock=0.025):
    rng = random.Random(937)
    base, btc, eth, trx = 4.0, [], [], []
    noise = 0.0
    for index in range(count):
        base += rng.gauss(0, 0.003)
        noise = 0.2 * noise + rng.gauss(0, 0.0007)
        btc.append(math.exp(0.3 + 1.2 * base + noise + (shock if index == count - 1 else 0)))
        eth.append(math.exp(base))
        trx.append(math.exp(-5.5 + 0.8 * base + rng.gauss(0, 0.001)))
    return {pairs.BTC: candles(btc), pairs.ETH: candles(eth), pairs.TRX: candles(trx)}


def correlation_history():
    # The previous 168 returns are perfectly correlated; the current 168
    # returns alternate in opposite directions, while BTC is stronger.
    a, b = [100.0], [100.0]
    for index in range(168):
        ret = 0.002 if index % 2 else -0.001
        a.append(a[-1] * (1 + ret))
        b.append(b[-1] * (1 + ret))
    for index in range(168):
        ar = 0.003 if index % 2 else -0.001
        br = -0.002 if index % 2 else 0.001
        a.append(a[-1] * (1 + ar))
        b.append(b[-1] * (1 + br))
    return {pairs.BTC: candles(a), pairs.ETH: candles(b)}


def spec(canonical):
    return {"strategy_id": canonical + "_Fnew_R50_k1.5_maker",
            "canonical_strategy_id": canonical, "params": copy.deepcopy(pairs.DEFAULTS[canonical]),
            "execution_universe": [pairs.BTC, pairs.ETH, pairs.TRX] if canonical == pairs.S32
            else [pairs.BTC, pairs.ETH], "min_warmup_bars": 1, "rule_hash": "frozen-new-rule"}


def positions_from_signal(result, histories, *, entry_rho=None):
    legs = result["group"]["legs"]
    evidence = result["signals"][legs[0]]["evidence"]
    entries = {pair: histories[pair][-1]["close"] for pair in legs}
    shared = {"pair_group": evidence["pair_group"], "pair_legs": legs,
              "pair_sides": evidence["pair_sides"], "pair_entry_prices": entries,
              "entry_pair_atr_return": evidence["entry_pair_atr_return"],
              "entry_pair_coefficients": evidence.get("pair_coefficients"),
              "pair_entry_rho": evidence.get("pair_entry_rho", entry_rho),
              "risk_stop_atr_mult": 1.5, "group_id": "same-execution-group"}
    return {pair: {**copy.deepcopy(shared), "direction": side,
                   "side": "long" if side == 1 else "short", "entry_price": entries[pair],
                   "quantity": 100 / entries[pair], "entry_notional": 100,
                   "opened_candle_ms": histories[pair][-1]["timestamp"],
                   "entry_evidence": copy.deepcopy(evidence)}
            for pair, side in zip(legs, evidence["pair_sides"], strict=True)}


class PairATRTests(unittest.TestCase):
    def test_signed_envelope_and_gap_true_range(self):
        inputs = {
            pairs.BTC: [{"timestamp": 0, "close": 100, "high": 100, "low": 100},
                        {"timestamp": HOUR, "close": 101, "high": 102, "low": 99}],
            pairs.ETH: [{"timestamp": 0, "close": 100, "high": 100, "low": 100},
                        {"timestamp": HOUR, "close": 99, "high": 101, "low": 98}],
        }
        self.assertAlmostEqual(pairs.compute_pair_atr_return(
            inputs, [pairs.BTC, pairs.ETH], [1, -1], {pairs.BTC: 100, pairs.ETH: 100}, 1), 0.06)
        self.assertAlmostEqual(pairs.compute_pair_atr_return(
            inputs, [pairs.BTC, pairs.ETH], [-1, 1], {pairs.BTC: 100, pairs.ETH: 100}, 1), 0.06)
        # Both previous prices and the bounded current range use actual entry
        # denominators; a jump outside the envelope contributes additional TR.
        inputs[pairs.BTC][0].update(close=110, high=110, low=110)
        self.assertAlmostEqual(pairs.compute_pair_atr_return(
            inputs, [pairs.BTC, pairs.ETH], [1, -1], {pairs.BTC: 100, pairs.ETH: 100}, 1), 0.12)

    def test_actual_entry_denominators_recompute_risk(self):
        histories = spread_history()
        legs = [pairs.BTC, pairs.ETH]
        inputs = {pair: histories[pair][-15:] for pair in legs}
        ref = {pair: histories[pair][-1]["close"] for pair in legs}
        old = pairs.compute_pair_atr_return(inputs, legs, [1, -1], ref, 14)
        shifted = {pair: price * 2 for pair, price in ref.items()}
        self.assertAlmostEqual(pairs.compute_pair_atr_return(inputs, legs, [1, -1], shifted, 14), old / 2)

    def test_invalid_pair_range_contract(self):
        histories = spread_history()
        legs = [pairs.BTC, pairs.ETH]
        inputs = {pair: histories[pair][-15:] for pair in legs}
        entries = {pair: histories[pair][-1]["close"] for pair in legs}
        for invalid in ([1, 1], [True, -1], [1], [0, 0]):
            with self.subTest(sides=invalid), self.assertRaises(ValueError):
                pairs.compute_pair_atr_return(inputs, legs, invalid, entries, 14)
        inputs[pairs.ETH][-1]["timestamp"] += HOUR
        with self.assertRaises(ValueError):
            pairs.compute_pair_atr_return(inputs, legs, [1, -1], entries, 14)


class CointegrationTests(unittest.TestCase):
    def evaluate(self, histories=None):
        histories = histories or spread_history()
        with patch.object(pairs, "_engle_granger", return_value=(-5.0, 0.001, [-3.5, -2.9, -2.6])):
            return pairs.evaluate_pair_fnew(spec(pairs.S32), histories)

    def test_training_excludes_current_and_exactly_one_pair_is_selected(self):
        histories = spread_history()
        snapshots = copy.deepcopy(histories)
        calls = []

        def eg(y, x, params):
            calls.append((y, x))
            return -5.0, 0.001, [-3.5, -2.9, -2.6]

        with patch.object(pairs, "_engle_granger", side_effect=eg):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), histories)
        self.assertTrue(result["ready"])
        selected = result["group"]["legs"]
        self.assertEqual(len(selected), 2)
        self.assertEqual(sum(signal["target_side"] is not None for signal in result["signals"].values()), 2)
        self.assertEqual(sum(result["signals"][pair]["target_side"] for pair in selected), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], [math.log(row["close"]) for row in histories[pairs.BTC][-501:-1]])
        evidence = result["signals"][selected[0]]["evidence"]
        coefficients = evidence["pair_coefficients"]
        self.assertEqual(coefficients["training_end_ms"], histories[pairs.BTC][-2]["timestamp"])
        self.assertEqual(coefficients["training_bars"], 500)
        self.assertEqual(coefficients["test"], "engle_granger_mackinnon_cointegration_distribution")
        self.assertTrue(evidence["equal_notional"])
        self.assertFalse(evidence["beta_hedged"])
        self.assertEqual(histories, snapshots)

    def test_current_shock_cannot_change_fitted_coefficients(self):
        a = self.evaluate(spread_history(shock=0.025))
        b = self.evaluate(spread_history(shock=-0.025))
        candidates_a = a["signals"][pairs.BTC]["evidence"]["pair_candidates"]
        candidates_b = b["signals"][pairs.BTC]["evidence"]["pair_candidates"]
        self.assertEqual([candidate["coefficients"] for candidate in candidates_a],
                         [candidate["coefficients"] for candidate in candidates_b])
        self.assertNotEqual([candidate["zscore"] for candidate in candidates_a],
                            [candidate["zscore"] for candidate in candidates_b])

    def test_non_cointegrated_pairs_never_enter(self):
        with patch.object(pairs, "_engle_granger", return_value=(-1.0, 0.8, [-3.5, -2.9, -2.6])):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), spread_history())
        self.assertTrue(result["ready"])
        self.assertIsNone(result["group"])
        self.assertTrue(all(signal["target_side"] is None for signal in result["signals"].values()))

    def test_missing_true_dependency_blocks_new_entries(self):
        with patch.object(pairs, "_engle_granger", side_effect=pairs.CointegrationDependencyUnavailable()):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), spread_history())
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["reason"], "cointegration_dependency_unavailable")

    def test_ties_use_lexicographic_pair_order(self):
        def candidate(legs, histories, params):
            return {"legs": list(legs), "zscore": 3, "cointegrated": True, "coefficients": {}}

        with patch.object(pairs, "_fit_candidate", side_effect=candidate):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), spread_history())
        self.assertEqual(result["group"]["legs"], [pairs.BTC, pairs.ETH])

    @unittest.skipUnless(importlib.util.find_spec("statsmodels"), "statsmodels unavailable")
    def test_real_engle_granger_detects_stationary_residual(self):
        histories = spread_history()
        candidate = pairs._fit_candidate((pairs.BTC, pairs.ETH), histories, pairs.DEFAULTS[pairs.S32])
        self.assertTrue(candidate["cointegrated"])
        self.assertLess(candidate["coefficients"]["eg_pvalue"], 0.05)
        self.assertLess(candidate["coefficients"]["eg_statistic"],
                        candidate["coefficients"]["eg_critical_values"][1])

    def test_perfect_linear_residual_is_not_a_tradeable_zscore(self):
        histories = {pair: candles([100 * math.exp(index * 0.001) for index in range(520)])
                     for pair in (pairs.BTC, pairs.ETH, pairs.TRX)}
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), histories)
        self.assertTrue(result["ready"])
        self.assertIsNone(result["group"])
        self.assertEqual(len(result["signals"][pairs.BTC]["evidence"]["pair_candidates_rejected"]), 3)


class CorrelationTests(unittest.TestCase):
    def test_nonoverlapping_baseline_and_strong_leg_direction(self):
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), correlation_history())
        self.assertTrue(result["ready"])
        self.assertEqual(result["signals"][pairs.BTC]["target_side"], 1)
        self.assertEqual(result["signals"][pairs.ETH]["target_side"], -1)
        evidence = result["signals"][pairs.BTC]["evidence"]
        self.assertAlmostEqual(evidence["prior_rho"], 1)
        self.assertAlmostEqual(evidence["rho"], -1)
        self.assertEqual(evidence["rho_compare_lag"], 168)
        self.assertTrue(evidence["correlation_is_not_a_traded_asset"])
        self.assertEqual(pairs.required_pair_bars(spec(pairs.S36)), 337)

    def test_high_correlation_is_a_required_condition(self):
        histories = correlation_history()
        with patch.object(pairs, "_correlation", side_effect=[-0.5, 0.6]):
            result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "no_correlation_regime_trigger")
        self.assertIsNone(result["signals"][pairs.BTC]["target_side"])

    def test_correlation_constant_return_variance_fails_closed(self):
        histories = {pair: candles([100] * 337) for pair in (pairs.BTC, pairs.ETH)}
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        self.assertTrue(result["ready"])
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "correlation_return_variance_zero")
        self.assertIsNone(result["signals"][pairs.BTC]["target_side"])

    def test_zero_pair_atr_blocks_trigger_without_crashing_evaluator(self):
        histories = correlation_history()
        for rows in histories.values():
            reference = rows[-16]["close"]
            for row in rows[-15:]:
                row.update(open=reference, high=reference, low=reference, close=reference)
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "pair_entry_atr_unavailable")
        self.assertTrue(all(signal["target_side"] is None for signal in result["signals"].values()))
        histories = spread_history()
        for rows in histories.values():
            reference = rows[-16]["close"]
            for row in rows[-15:]:
                row.update(open=reference, high=reference, low=reference, close=reference)
        candidate = {"legs": [pairs.BTC, pairs.ETH], "zscore": 3, "cointegrated": True,
                     "coefficients": {}}
        with patch.object(pairs, "_fit_candidate", return_value=candidate):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), histories)
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "pair_entry_atr_unavailable")
        self.assertTrue(all(signal["target_side"] is None for signal in result["signals"].values()))


class FrozenPairTests(unittest.TestCase):
    def setUp(self):
        self.histories = spread_history()
        with patch.object(pairs, "_engle_granger", return_value=(-5, 0.001, [-3.5, -2.9, -2.6])):
            self.entry = pairs.evaluate_pair_fnew(spec(pairs.S32), self.histories)
        self.positions = positions_from_signal(self.entry, self.histories)
        self.legs = self.entry["group"]["legs"]

    def current_marks(self, *, adverse=False):
        marks = {}
        for pair in self.legs:
            position = self.positions[pair]
            price = position["entry_price"]
            if adverse and pair == self.legs[0]:
                price *= 1 - 0.1 * position["direction"]
            marks[pair] = candles([price], start=position["opened_candle_ms"] + HOUR)
        return marks

    def test_pair_stop_closes_both_and_does_not_need_unselected_trx(self):
        marks = self.current_marks(adverse=True)
        with patch.object(pairs, "_engle_granger", side_effect=AssertionError("do not refit")):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), marks, self.positions)
        self.assertTrue(result["ready"])
        self.assertEqual(result["warmup"]["required_bars"], 1)
        for pair in self.legs:
            self.assertEqual(result["signals"][pair]["target_side"], 0)
            self.assertEqual(result["signals"][pair]["reason"], "pair_stop_loss_close_confirmed")
            self.assertAlmostEqual(result["signals"][pair]["evidence"]["pair_return"], -0.1)

    def test_frozen_coefficients_survive_and_no_rolling_refit_is_used(self):
        marks = self.current_marks()
        with patch.object(pairs, "_fit_candidate", side_effect=AssertionError("do not refit")):
            result = pairs.evaluate_pair_fnew(spec(pairs.S32), marks, self.positions)
        for pair in self.legs:
            self.assertIsNone(result["signals"][pair]["target_side"])
            self.assertEqual(result["signals"][pair]["reason"], "paired_position_held")
            self.assertTrue(result["signals"][pair]["evidence"]["frozen_coefficients_used"])

    def test_convergence_uses_frozen_zscore_and_closes_both(self):
        marks = self.current_marks()
        coeff = self.positions[self.legs[0]]["entry_pair_coefficients"]
        y, x = coeff["dependent_pair"], coeff["independent_pair"]
        target = math.exp(coeff["intercept"] + coeff["beta"] * math.log(marks[x][-1]["close"])
                          + coeff["residual_mean"])
        marks[y] = candles([target], start=marks[y][-1]["timestamp"])
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), marks, self.positions)
        for pair in self.legs:
            self.assertEqual(result["signals"][pair]["target_side"], 0)
            self.assertEqual(result["signals"][pair]["reason"], "cointegration_spread_converged")

    def test_invalid_pair_metadata_always_exits_entire_group(self):
        variations = ("side", "direction", "opened_candle_ms", "group_id", "pair_sides", "pair_entry_prices")
        for field in variations:
            positions = copy.deepcopy(self.positions)
            if field == "side":
                positions[self.legs[1]][field] = positions[self.legs[0]][field]
            elif field == "direction":
                positions[self.legs[1]][field] *= -1
            elif field == "opened_candle_ms":
                positions[self.legs[1]][field] -= HOUR
            elif field == "group_id":
                positions[self.legs[1]][field] = "wrong-group"
            elif field == "pair_sides":
                positions[self.legs[1]][field] = [1, 1]
            else:
                positions[self.legs[1]][field][self.legs[0]] *= 2
            with self.subTest(field=field):
                result = pairs.evaluate_pair_fnew(spec(pairs.S32), self.current_marks(), positions)
                self.assertEqual([result["signals"][pair]["target_side"] for pair in self.legs], [0, 0])
                self.assertEqual(result["signals"][self.legs[0]]["reason"], "atomic_position_inconsistent")

    def test_missing_leg_and_missing_frozen_atr_exit_both(self):
        positions = {self.legs[0]: self.positions[self.legs[0]]}
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), self.current_marks(), positions)
        self.assertEqual([result["signals"][pair]["target_side"] for pair in self.legs], [0, 0])
        positions = copy.deepcopy(self.positions)
        for position in positions.values():
            position["entry_pair_atr_return"] = None
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), self.current_marks(), positions)
        self.assertEqual(result["signals"][self.legs[0]]["reason"], "pair_frozen_risk_unavailable")

    def test_actual_unequal_notionals_cannot_hide_behind_shared_risk_metadata(self):
        positions = copy.deepcopy(self.positions)
        positions[self.legs[0]]["quantity"] *= 10
        positions[self.legs[0]]["entry_notional"] *= 10
        marks = self.current_marks()
        for pair in self.legs:
            direction = positions[pair]["direction"]
            multiplier = 1 - 0.01 * direction if pair == self.legs[0] else 1 + 0.01 * direction
            marks[pair] = candles([positions[pair]["entry_price"] * multiplier],
                                  start=marks[pair][-1]["timestamp"])
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), marks, positions)
        self.assertEqual([result["signals"][pair]["target_side"] for pair in self.legs], [0, 0])
        self.assertEqual(result["signals"][self.legs[0]]["reason"], "atomic_position_inconsistent")

    def test_malformed_unhashable_pair_legs_exit_actual_legs(self):
        positions = copy.deepcopy(self.positions)
        for position in positions.values():
            position["pair_legs"] = [{"pair": self.legs[0]}, self.legs[1]]
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), self.current_marks(), positions)
        self.assertEqual([result["signals"][pair]["target_side"] for pair in self.legs], [0, 0])
        self.assertEqual(result["signals"][self.legs[0]]["reason"], "atomic_position_inconsistent")

    def test_marked_notional_may_change_without_changing_frozen_equal_dollars(self):
        positions = copy.deepcopy(self.positions)
        positions[self.legs[0]]["notional"] = 120
        positions[self.legs[1]]["notional"] = 95
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), self.current_marks(), positions)
        self.assertEqual(result["signals"][self.legs[0]]["reason"], "paired_position_held")

    def test_holding_limit_is_group_wide(self):
        marks = self.current_marks()
        for pair in self.legs:
            marks[pair][-1]["timestamp"] += 72 * HOUR
        result = pairs.evaluate_pair_fnew(spec(pairs.S32), marks, self.positions)
        self.assertEqual(result["signals"][self.legs[0]]["reason"], "holding_period_elapsed")
        self.assertEqual([result["signals"][pair]["target_side"] for pair in self.legs], [0, 0])

    def test_correlation_exit_is_relative_to_frozen_entry_rho(self):
        histories = correlation_history()
        entry = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        positions = positions_from_signal(entry, histories)
        # Supply a 0.051 rebound, keeping all current price/risk information.
        with patch.object(pairs, "_correlation", return_value=-0.949):
            result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories, positions)
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "correlation_regime_rebounded")
        self.assertEqual([result["signals"][pair]["target_side"] for pair in (pairs.BTC, pairs.ETH)], [0, 0])

    def test_correlation_current_mark_stop_survives_missing_window(self):
        histories = correlation_history()
        entry = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        positions = positions_from_signal(entry, histories)
        stamp = histories[pairs.BTC][-1]["timestamp"] + HOUR
        marks = {pair: candles([position["entry_price"] * (0.9 if pair == pairs.BTC else 1)], start=stamp)
                 for pair, position in positions.items()}
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), marks, positions)
        self.assertEqual(result["signals"][pairs.BTC]["reason"], "pair_stop_loss_close_confirmed")


class CausalValidationTests(unittest.TestCase):
    def test_incomplete_future_and_stale_bars_cannot_enter(self):
        histories = correlation_history()
        observation = histories[pairs.BTC][-1]["timestamp"] + HOUR
        future = copy.deepcopy(histories)
        for pair in future:
            future[pair] += candles([1_000_000], start=observation)
        self.assertEqual(pairs.evaluate_pair_fnew(spec(pairs.S36), future, as_of_ms=observation),
                         pairs.evaluate_pair_fnew(spec(pairs.S36), histories, as_of_ms=observation))
        stale = pairs.evaluate_pair_fnew(spec(pairs.S36), histories, as_of_ms=observation + 90_001)
        self.assertFalse(stale["ready"])
        self.assertEqual(stale["warmup"]["reason"], "closed_candle_stale")

    def test_gaps_and_unaligned_native_rows_cannot_enter(self):
        histories = correlation_history()
        histories[pairs.BTC].pop(-2)
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        self.assertFalse(result["ready"])
        histories = correlation_history()
        for row in histories[pairs.ETH]:
            row["timestamp"] += HOUR
        result = pairs.evaluate_pair_fnew(spec(pairs.S36), histories)
        self.assertEqual(result["warmup"]["reason"], "pair_timestamps_not_aligned")

    def test_invalid_parameters_fail_explicitly(self):
        for key, value in (("rho_exit_mode", "absolute"), ("rho_window", True), ("risk_stop_atr_mult", math.nan)):
            definition = spec(pairs.S36)
            definition["params"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                pairs.evaluate_pair_fnew(definition, correlation_history())


if __name__ == "__main__":
    unittest.main()
