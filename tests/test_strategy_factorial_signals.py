"""Contracts for immutable factor IDs and their canonical signal adapter."""

import copy
import unittest

from strategy_factorial import BTC, DESIGN_VERSION, ETH, load_factorial_specs
from strategy_factorial_signals import (
    FACTORIAL_STOP_POLICY,
    evaluate_factorial,
    factorial_rule_hash,
    normalize_factorial_spec,
)
from strategy_signals import LEGACY_DEFAULTS, TIMEFRAME_MS


def spec_for(canonical="s07_surge_continuation", *, risk=50, k=1.5,
             params=None, timeframe=None):
    number = int(canonical[1:3])
    arm = "B15m" if number <= 11 else "Cgen2" if number <= 21 else "Dgen3"
    timeframe = timeframe or ("15m" if number <= 11 else "1d")
    frozen = {**LEGACY_DEFAULTS[canonical][0], "atr_window": 14,
              "risk_stop_atr_mult": k, **(params or {})}
    if number == 8:
        frozen["stop_atr_mult"] = k
    execution = [ETH] if number == 10 else [BTC, ETH]
    return {"strategy_id": f"{canonical}_{arm}_R{risk}_k{k:.1f}_100x_maker",
            "canonical_strategy_id": canonical, "factorial_design_version": DESIGN_VERSION,
            "risk_sizing_mode": "entry_atr", "max_loss_per_trade_usdt": risk,
            "params": frozen, "timeframe": timeframe, "min_warmup_bars": 16,
            "universe": [BTC, ETH], "execution_universe": execution,
            "research_universe": [BTC, ETH], "data_universe": [BTC, ETH],
            "position_budget": {"leverage": 100}, "execution_enabled": True,
            "execution_blockers": [], "blocked_execution_reason": None}


def candles(count=80, *, timeframe="15m", prices=None):
    prices = prices or [100.0] * count
    return [{"timestamp": i * TIMEFRAME_MS[timeframe], "open": close,
             "close": close, "high": close + 1, "low": close - 1,
             "volume": 100.0, "extras": {"closed": True, "taker_buy_base": 80.0}}
            for i, close in enumerate(prices)]


def change_close(rows, close):
    rows[-1].update(close=close, high=max(rows[-1]["high"], close),
                    low=min(rows[-1]["low"], close))
    return rows


def position(rows, side=1, **extra):
    return {"side": side, "entry_price": 100.0,
            "opened_candle_ms": rows[-1]["timestamp"], "entry_atr": 1.0, **extra}


class FactorialIdentityTests(unittest.TestCase):
    def test_all_261_definitions_normalize_idempotently(self):
        specs = load_factorial_specs(normalizer=normalize_factorial_spec)
        self.assertEqual(len(specs), 261)
        self.assertEqual(len({item["rule_hash"] for item in specs}), 261)
        for spec in specs:
            with self.subTest(strategy=spec["strategy_id"]):
                normalized = normalize_factorial_spec(spec)
                self.assertEqual(normalize_factorial_spec(normalized), normalized)
                self.assertEqual(spec["rule_hash"], normalized["rule_hash"])

    def test_risk_and_width_axes_have_distinct_bound_hashes(self):
        hashes = {factorial_rule_hash(spec_for(risk=risk, k=k))
                  for risk in (50, 200, 1000) for k in (1.0, 1.5, 2.5)}
        self.assertEqual(len(hashes), 9)

    def test_identity_metadata_and_risk_parameter_mismatches_rejected(self):
        raw = spec_for()
        changes = [
            {"canonical_strategy_id": "s02_dual_thrust"},
            {"max_loss_per_trade_usdt": 200},
            {"risk_sizing_mode": "fixed_percent"},
            {"params": {**raw["params"], "risk_stop_atr_mult": 2.5}},
            {"strategy_id": raw["strategy_id"].replace("B15m", "Dgen3")},
            {"factorial_design_version": "different"},
            {"factorial_stop_policy": "intrabar"},
            {"factorial_factors": {"strategy": raw["canonical_strategy_id"],
                                   "risk_budget": "R200", "stop_width_k": 1.5}},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_factorial_spec({**raw, **change})

    def test_stale_hash_cannot_hide_parameter_universe_fee_or_stop_policy_change(self):
        frozen = normalize_factorial_spec(spec_for())
        changes = [{"params": {**frozen["params"], "I": 0.4}},
                   {"research_universe": [ETH, BTC]},
                   {"fee_rate_per_side": 0.0003}, {"initial_capital": 20000},
                   {"signal_rule_hash": "invented"},
                   {"execution_enabled": False},
                   {"factorial_stop_policy": "changed"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_factorial_spec({**frozen, **change})

    def test_params_frozen_conflict_and_fractional_warmup_are_rejected(self):
        raw = spec_for()
        with self.assertRaisesRegex(ValueError, "disagree"):
            normalize_factorial_spec({**raw, "params_frozen": {**raw["params"], "I": 0.8}})
        with self.assertRaisesRegex(ValueError, "integer"):
            normalize_factorial_spec({**raw, "min_warmup_bars": 16.5})
        # Numeric representation normalization must remain idempotent.
        raw["params"]["min_volume_multiple"] = 1
        raw["params_frozen"] = copy.deepcopy(raw["params"])
        normalized = normalize_factorial_spec(raw)
        self.assertEqual(normalize_factorial_spec({**raw, **normalized}), normalized)

    def test_unknown_canonical_and_paused_execution_pairs_rejected(self):
        raw = spec_for()
        with self.assertRaisesRegex(ValueError, "canonical"):
            normalize_factorial_spec({**raw, "strategy_id": raw["strategy_id"].replace(
                "surge_continuation", "unknown"), "canonical_strategy_id": "s07_unknown"})
        with self.assertRaisesRegex(ValueError, "paused"):
            normalize_factorial_spec({**raw, "execution_universe": ["SOL/USDT:USDT"]})

    def test_incomplete_strategies_cannot_be_enabled_by_metadata(self):
        for canonical in ("s04_btc_eth_pairs", "s19_multi_coin_breadth",
                          "s24_cross_sectional_lowvol"):
            with self.subTest(canonical=canonical):
                raw = spec_for(canonical)
                normalized = normalize_factorial_spec(raw)
                self.assertFalse(normalized["execution_enabled"])
                result = evaluate_factorial(raw, {})
                self.assertFalse(result["ready"])
                self.assertTrue(result["execution_blockers"])
                self.assertEqual(result["warmup"]["reason"],
                                 normalized["blocked_execution_reason"])
                self.assertTrue(all(signal["target_side"] is None
                                    for signal in result["signals"].values()))


class FactorialSignalTests(unittest.TestCase):
    def test_canonical_single_entry_captures_prior_atr_and_factor_identity(self):
        for side, bought in ((1, 80.0), (-1, 20.0)):
            with self.subTest(side=side):
                rows = candles()
                rows[-1]["extras"]["taker_buy_base"] = bought
                rows[-1].update(high=150, low=50)
                raw = spec_for()
                result = evaluate_factorial(raw, {BTC: rows, ETH: copy.deepcopy(rows)})
                self.assertEqual(result["strategy_id"], raw["strategy_id"])
                for signal in result["signals"].values():
                    self.assertEqual(signal["target_side"], side)
                    self.assertEqual(signal["evidence"]["atr"], 2.0)
                    self.assertEqual(signal["evidence"]["entry_atr"], 2.0)
                    self.assertEqual(signal["evidence"]["factorial_stop_policy"],
                                     FACTORIAL_STOP_POLICY)

    def test_s08_mapping_retains_risk_key_and_existing_take_profit(self):
        raw = spec_for("s08_wick_rejection", params={"W": 0.6})
        normalized = normalize_factorial_spec(raw)
        self.assertEqual(normalized["params"]["risk_stop_atr_mult"], 1.5)
        self.assertEqual(normalized["params"]["stop_atr_mult"], 1.5)
        self.assertEqual(normalized["params"]["tp_atr_mult"], 1.5)
        rows = candles()
        rows[-1].update(open=100, close=100.2, high=100.3, low=97)
        entry = evaluate_factorial(raw, {BTC: rows, ETH: rows})
        self.assertEqual(entry["signals"][BTC]["target_side"], 1)
        self.assertEqual(entry["signals"][BTC]["evidence"]["atr"], 2.0)
        for side in (1, -1):
            stopped = change_close(copy.deepcopy(rows), 100 - side * 1.5)
            pos = position(stopped, side)
            result = evaluate_factorial(raw, {BTC: stopped, ETH: stopped}, {BTC: pos})
            self.assertEqual(result["signals"][BTC]["reason"], "stop_loss_close_confirmed")
            self.assertEqual(result["signals"][BTC]["target_side"], 0)

    def test_s10_source_is_validated_but_only_eth_can_be_ordered(self):
        raw = spec_for("s10_lead_lag_btc_eth")
        for side in (1, -1):
            with self.subTest(side=side):
                btc = change_close(candles(), 100 + side)
                eth = candles()
                eth[-1].update(high=150, low=50)
                result = evaluate_factorial(raw, {BTC: btc, ETH: eth}, {BTC: {"invalid": True}})
                self.assertTrue(result["ready"])
                self.assertEqual(list(result["signals"]), [ETH])
                self.assertEqual(list(result["warmup"]["by_pair"]), [ETH])
                self.assertIn(BTC, result["warmup"]["context_by_pair"])
                signal = result["signals"][ETH]
                self.assertEqual(signal["target_side"], side)
                self.assertEqual(signal["evidence"]["source_pair"], BTC)
                self.assertEqual(signal["evidence"]["atr"], 2.0)
        missing = evaluate_factorial(raw, {ETH: candles()})
        self.assertFalse(missing["ready"])
        self.assertIsNone(missing["signals"][ETH]["target_side"])
        late_btc = candles()[:-1]
        mismatch = evaluate_factorial(raw, {BTC: late_btc, ETH: candles()})
        self.assertFalse(mismatch["ready"])
        self.assertEqual(mismatch["warmup"]["reason"], "pair_timestamps_not_aligned")

    def test_external_s10_s30_stops_are_inclusive_frozen_and_symmetric(self):
        for canonical, timeframe in (("s10_lead_lag_btc_eth", "15m"),
                                     ("s30_dual_asset_confirmation", "1d")):
            for risk in (50, 200, 1000):
                for k in (1.0, 1.5, 2.5):
                    for side in (1, -1):
                        with self.subTest(canonical=canonical, risk=risk, k=k, side=side):
                            raw = spec_for(canonical, risk=risk, k=k)
                            rows = change_close(candles(timeframe=timeframe), 100 - side * k)
                            rows[-1].update(high=180, low=20)
                            positions = {ETH: position(rows, side)}
                            result = evaluate_factorial(raw, {BTC: rows, ETH: rows}, positions)
                            signal = result["signals"][ETH]
                            self.assertEqual(signal["target_side"], 0)
                            self.assertEqual(signal["reason"], "stop_loss_close_confirmed")
                            self.assertEqual(signal["evidence"]["stop_level"], 100 - side * k)
                            self.assertEqual(signal["evidence"]["entry_atr"], 1.0)

    def test_missing_frozen_atr_overrides_even_a_mechanism_exit(self):
        for canonical, timeframe in (("s10_lead_lag_btc_eth", "15m"),
                                     ("s30_dual_asset_confirmation", "1d")):
            raw = spec_for(canonical, params={"H": 1})
            rows = candles(timeframe=timeframe)
            for bad in (None, 0, -1, float("nan"), "bad", True):
                with self.subTest(canonical=canonical, bad=bad):
                    result = evaluate_factorial(raw, {BTC: rows, ETH: rows},
                                                {ETH: position(rows, entry_atr=bad)})
                    signal = result["signals"][ETH]
                    self.assertIsNone(signal["target_side"])
                    self.assertEqual(signal["reason"], "frozen_entry_atr_unavailable")

    def test_external_stop_uses_validated_position_and_frozen_evidence_fallback(self):
        raw = spec_for("s10_lead_lag_btc_eth")
        rows = change_close(candles(), 98.5)
        for side in ("long", "1", 1.0):
            with self.subTest(side=side):
                pos = position(rows, side, entry_atr=None, entry_evidence={"atr": 1.0})
                result = evaluate_factorial(raw, {BTC: rows, ETH: rows}, {ETH: pos})
                self.assertEqual(result["signals"][ETH]["reason"], "stop_loss_close_confirmed")
        invalid = evaluate_factorial(raw, {BTC: rows, ETH: rows},
                                     {ETH: position(rows, side=True)})
        self.assertFalse(invalid["ready"])
        self.assertEqual(invalid["signals"][ETH]["reason"], "invalid_position")

    def test_s30_entry_atr_is_per_leg_and_one_leg_stop_does_not_flatten_partner(self):
        raw = spec_for("s30_dual_asset_confirmation", params={"channel": 3})
        btc = candles(timeframe="1d", prices=[100 + i * 0.1 for i in range(80)])
        eth = candles(timeframe="1d", prices=[100 + i * 0.2 for i in range(80)])
        for row in eth[:-1]:
            row.update(high=row["close"] + 2, low=row["close"] - 2)
        btc[-1].update(high=190, low=30)
        eth[-1].update(high=190, low=30)
        result = evaluate_factorial(raw, {BTC: btc, ETH: eth})
        self.assertTrue(all(signal["target_side"] == 1 for signal in result["signals"].values()))
        self.assertEqual(result["signals"][BTC]["evidence"]["atr"], 2.0)
        self.assertEqual(result["signals"][ETH]["evidence"]["atr"], 4.0)
        self.assertEqual(normalize_factorial_spec(raw)["risk_budget_scope"], "per_trade_leg")
        btc = change_close(candles(timeframe="1d"), 98.5)
        eth = candles(timeframe="1d", prices=[100 + i * 0.1 for i in range(80)])
        result = evaluate_factorial(raw, {BTC: btc, ETH: eth},
                                    {BTC: position(btc), ETH: position(eth, entry_price=107)})
        self.assertEqual(result["signals"][BTC]["reason"], "stop_loss_close_confirmed")
        self.assertIsNone(result["signals"][ETH]["target_side"])
        # The original held partner prevents immediate entry of the empty leg.
        held = evaluate_factorial(raw, {BTC: btc, ETH: eth},
                                  {ETH: position(eth, entry_price=107)})
        self.assertNotIn(held["signals"][BTC]["target_side"], (-1, 1))


if __name__ == "__main__":
    unittest.main()
