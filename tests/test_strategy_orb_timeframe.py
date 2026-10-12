"""ORB follows the account timeframe and the final completed UTC-day bar."""
import copy
import unittest

from strategy_factorial_signals import evaluate_factorial
from strategy_signals import BTC, DAY_MS, ETH, TIMEFRAME_MS, _orb, evaluate
from tests.test_strategy_factorial_signals import spec_for


def candles(timeframe, count=61):
    step = TIMEFRAME_MS[timeframe]
    return [{"timestamp": index * step, "open": 100.0, "close": 100.0,
             "high": 101.0, "low": 99.0, "volume": 100.0,
             "extras": {"closed": True}} for index in range(count)]


def change_close(rows, close, index=-1):
    rows[index].update(close=close, high=max(101.0, close), low=min(99.0, close))
    return rows


def signal(rows, timeframe="15m", *, protected=False, position=None):
    params = {"atr_window": 14, "risk_stop_atr_mult": 1.5} if protected else {}
    spec = {"strategy_id": "s01_orb_breakout", "timeframe": timeframe,
            "min_warmup_bars": 16, "params": params}
    positions = {BTC: position} if position else None
    result = evaluate(spec, {BTC: rows, ETH: copy.deepcopy(rows)}, positions,
                      as_of_utc=rows[-1]["timestamp"] + TIMEFRAME_MS[timeframe])
    if not result["ready"]:
        raise AssertionError(result["warmup"])
    return result["signals"][BTC]


class OrbTimeframeTests(unittest.TestCase):
    def test_first_breakout_uses_actual_spacing_in_both_risk_paths(self):
        for timeframe in ("5m", "15m", "1h"):
            for protected in (False, True):
                for side, close in ((1, 102.0), (-1, 98.0)):
                    with self.subTest(timeframe=timeframe, protected=protected, side=side):
                        result = signal(change_close(candles(timeframe), close), timeframe,
                                        protected=protected)
                        self.assertEqual(result["target_side"], side)
                        self.assertEqual(result["reason"], "first_session_breakout")
                        self.assertEqual(result["evidence"]["range_bars"], 12)
                        self.assertEqual(result["evidence"]["session_high"], 101.0)

    def test_ready_15m_session_without_breakout_is_not_a_missing_range(self):
        for protected in (False, True):
            result = signal(candles("15m"), protected=protected)
            self.assertIsNone(result["target_side"])
            self.assertEqual(result["reason"], "no_session_breakout")

    def test_prior_breakout_consumes_day_without_manufacturing_an_entry(self):
        for protected in (False, True):
            rows = change_close(candles("15m"), 102.0, index=-2)
            result = signal(rows, protected=protected)
            self.assertIsNone(result["target_side"])
            self.assertEqual(result["reason"], "daily_first_entry_already_consumed")

    def test_missing_midnight_range_remains_unready(self):
        result = signal(candles("15m")[3:])
        self.assertIsNone(result["target_side"])
        self.assertEqual(result["reason"], "session_range_not_ready")

    def test_day_end_flattens_only_on_actual_last_bar(self):
        for timeframe in ("5m", "15m", "1h"):
            count = DAY_MS // TIMEFRAME_MS[timeframe]
            for protected in (False, True):
                for is_last in (False, True):
                    with self.subTest(timeframe=timeframe, protected=protected, last=is_last):
                        rows = candles(timeframe, count if is_last else count - 1)
                        position = {"side": 1, "entry_price": 100.0, "entry_atr": 1.0,
                                    "opened_candle_ms": rows[-1]["timestamp"] - TIMEFRAME_MS[timeframe]}
                        result = signal(rows, timeframe, protected=protected, position=position)
                        self.assertEqual(result["target_side"], 0 if is_last else None)
                        self.assertEqual(result["reason"], "session_day_end" if is_last else "position_held")

    def test_last_15m_bar_cannot_open_even_on_first_breakout(self):
        for protected in (False, True):
            for is_last in (False, True):
                rows = change_close(candles("15m", 96 if is_last else 95), 102.0)
                result = signal(rows, protected=protected)
                self.assertEqual(result["target_side"], None if is_last else 1)
                self.assertEqual(result["reason"], "session_day_end_no_entry" if is_last
                                 else "first_session_breakout")

    def test_legacy_helper_default_retains_five_minute_behavior(self):
        rows = change_close(candles("5m"), 102.0)
        params = {"N_bars": 12}
        self.assertEqual(_orb(rows, params, None), _orb(rows, params, None, bar_ms=TIMEFRAME_MS["5m"]))
        self.assertEqual(_orb(rows, params, None)[0:2], (1, "first_session_breakout"))

    def test_current_factorial_15m_account_reaches_orb_with_actual_period(self):
        rows = change_close(candles("15m"), 102.0)
        spec = spec_for("s01_orb_breakout", risk=1000, k=1.0, timeframe="15m")
        result = evaluate_factorial(spec, {BTC: rows, ETH: copy.deepcopy(rows)},
                                    as_of_utc=rows[-1]["timestamp"] + TIMEFRAME_MS["15m"])
        self.assertTrue(result["ready"], result["warmup"])
        self.assertEqual(result["signals"][BTC]["target_side"], 1)
        self.assertEqual(result["signals"][BTC]["reason"], "first_session_breakout")
