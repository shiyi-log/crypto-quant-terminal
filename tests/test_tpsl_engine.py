import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shiyi/DeepSeek/量化/bot")
import event_backtest as EB
import paper_rules


class TpslEngineTests(unittest.TestCase):
    def setUp(self):
        self.idx = pd.date_range("2026-01-01", periods=8, freq="D")
        self.states = pd.Series([0, 1, 1, 1, 1, 1, 1, 1], index=self.idx, dtype=float)
        self.strengths = pd.Series([0, 2, 2, 2, 2, 2, 2, 2], index=self.idx, dtype=float)

    def data(self, opens=None, highs=None, lows=None):
        opens = np.asarray(opens if opens is not None else [100] * 8, dtype=float)
        closes = opens.copy()
        highs = np.asarray(highs if highs is not None else opens + 1, dtype=float)
        lows = np.asarray(lows if lows is not None else opens - 1, dtype=float)
        return {"BTC/USDT:USDT": pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes}, index=self.idx)}

    def execute(self, data, **kwargs):
        with patch.object(EB, "signals", return_value=self.states), \
                patch.object(EB, "strength", return_value=self.strengths):
            return EB.run_v2(data, top_n=1, max_open=1, exposure=.5, **kwargs)

    def test_take_profit_is_triggered_on_completed_bar_and_filled_next_open(self):
        # Entry at index 2. Index 2 high crosses the TP; index 3 open is the fill.
        data = self.data(opens=[100, 100, 100, 103, 103, 103, 103, 103],
                         highs=[101, 101, 103, 104, 104, 104, 104, 104])
        trades, _, _, diag = self.execute(
            data, take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None})
        row = trades.iloc[0]
        self.assertEqual(row.exit_reason, "take_profit")
        self.assertEqual(row.close_date, self.idx[3])
        self.assertEqual(row.close_rate, 103.0)
        self.assertGreater(diag["risk_exit_triggers"], 0)

    def test_execution_open_gap_triggers_and_fills_at_open(self):
        # The completed candle stays below the TP.  The next execution open
        # crosses it, so the gap branch must fill at 102 rather than 101.
        data = self.data(opens=[100, 100, 100, 102, 102, 102, 102, 102],
                         highs=[100.2, 100.5, 100.5, 102.5, 102.5, 102.5, 102.5, 102.5],
                         lows=[99.8, 99.5, 99.5, 101.5, 101.5, 101.5, 101.5, 101.5])
        trades, _, _, _ = self.execute(
            data, take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None})
        row = trades.iloc[0]
        self.assertEqual(row.exit_reason, "take_profit")
        self.assertEqual(row.close_rate, 102.0)
        self.assertEqual(row.trigger["trigger_level"], 101.0)
        self.assertTrue(row.trigger["gap_at_open"])
        self.assertEqual(row.trigger["observed_price"], 102.0)

    def test_same_bar_stop_loss_wins_and_short_uses_opposite_extremes(self):
        states = self.states.copy(); states.iloc[1:] = -1
        strengths = self.strengths.copy(); strengths.iloc[1:] = 2
        data = self.data(opens=[100, 100, 100, 90, 90, 90, 90, 90],
                         highs=[101, 101, 103, 110, 100, 100, 100, 100],
                         lows=[99, 99, 97, 80, 80, 80, 80, 80])
        with patch.object(EB, "signals", return_value=states), \
                patch.object(EB, "strength", return_value=strengths):
            trades, _, _, diag = EB.run_v2(
                data, top_n=1, max_open=1, exposure=.5,
                take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None},
                stop_loss={"kind": "fixed_pct", "value": 1.0, "atr_window": None})
        row = trades.iloc[0]
        self.assertEqual(row.exit_reason, "stop_loss")
        self.assertTrue(row.trigger["dual_touch"])
        self.assertGreaterEqual(diag["dual_touch_stop_loss_first"], 1)

    def test_short_fixed_take_profit_uses_the_low_and_fills_next_open(self):
        states = self.states.copy(); states.iloc[1:] = -1
        strengths = self.strengths.copy(); strengths.iloc[1:] = 2
        data = self.data(
            opens=[100, 100, 100, 100, 100, 100, 100, 100],
            highs=[101, 101, 101, 101, 101, 101, 101, 101],
            lows=[99, 99, 98, 99, 99, 99, 99, 99],
        )
        with patch.object(EB, "signals", return_value=states), \
                patch.object(EB, "strength", return_value=strengths):
            trades, _, _, _ = EB.run_v2(
                data, top_n=1, max_open=1, exposure=.5,
                take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None})

        row = trades.iloc[0]
        self.assertEqual(row.exit_reason, "take_profit")
        self.assertEqual(row.trigger["observed_price"], 98.0)
        self.assertEqual(row.trigger["trigger_level"], 99.0)
        self.assertEqual(row.close_date, self.idx[3])
        self.assertEqual(row.close_rate, 100.0)

    def test_missing_execution_open_keeps_pending_exit(self):
        data = self.data(opens=[100, 100, 100, np.nan, 102, 102, 102, 102],
                         highs=[101, 101, 103, 104, 104, 104, 104, 104])
        trades, _, _, diag = self.execute(
            data, take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None})
        row = trades.iloc[0]
        self.assertEqual(row.exit_reason, "take_profit")
        self.assertEqual(row.close_date, self.idx[4])
        self.assertGreater(diag["pending_exit_days"], 0)

    def test_exit_fill_does_not_release_slot_until_next_open(self):
        states = pd.DataFrame({
            "AAA/USDT:USDT": [0, 1, 1, 0, 0, 0, 0, 0],
            "BBB/USDT:USDT": [0, 0, 1, 1, 1, 1, 1, 1],
        }, index=self.idx, dtype=float)
        strengths = pd.DataFrame({
            "AAA/USDT:USDT": [0, 2, 2, 0, 0, 0, 0, 0],
            "BBB/USDT:USDT": [0, 0, 1, 1, 1, 1, 1, 1],
        }, index=self.idx, dtype=float)
        data = self.data(highs=[101, 101, 103, 101, 101, 101, 101, 101])
        data["BBB/USDT:USDT"] = data["BTC/USDT:USDT"].copy()
        with patch.object(EB, "signals", side_effect=[states[c] for c in states]), \
                patch.object(EB, "strength", side_effect=[strengths[c] for c in strengths]):
            events = []
            _, _, _, _ = EB.run_v2(
                data, top_n=2, max_open=1, exposure=.5,
                take_profit={"kind": "fixed_pct", "value": 1.0, "atr_window": None},
                event_sink=events)
        # use top_n=2 but max_open=1; the high strength AAA opens first.
        fills = [e for e in events if e.get("event_type") == "fill" and e.get("action") == "entry"]
        b_fills = [e for e in fills if e["coin"] == "BBB"]
        self.assertTrue(b_fills)
        self.assertEqual(pd.Timestamp(b_fills[0]["filled_at_utc"]).tz_localize(None), self.idx[4])

    def test_range_filter_warmup_is_fail_closed_and_records_raw_evidence(self):
        data = self.data()
        events = []
        trades, _, _, diag = self.execute(
            data, event_sink=events,
            range_filter={"kind": "channel_width_pct", "window": 3,
                          "threshold": 1, "op": "gt", "warmup_bars": 3})
        decisions = [e for e in events if e.get("event_type") == "decision"]
        self.assertGreater(len(decisions), 0)
        candidates = [c for e in decisions for c in e.get("candidates", [])]
        self.assertTrue(any(c["range_filter"]["ready"] is False for c in candidates))
        self.assertGreater(diag["range_filter_not_ready"], 0)
        self.assertTrue(trades.empty)

    def test_trailing_gap_uses_retracement_direction_and_execution_open(self):
        """A gap through trailing TP is a reversal, not a profitable advance."""
        tp = {"kind": "trailing_pct", "value": 1.0, "atr_window": None}
        sl = {"kind": "fixed_pct", "value": 20.0, "atr_window": None}

        long_state = paper_rules.initial_risk(100.0, 1, tp, sl, {})
        # Long trailing level starts at 99.0: an open above it is still
        # profitable continuation, while an open below it is a retracement.
        self.assertIsNone(paper_rules.gap_touch(
            long_state, 1, 100.5, tp, sl, "2026-01-02T00:00:00Z"))
        long_gap = paper_rules.gap_touch(
            long_state, 1, 98.5, tp, sl, "2026-01-02T00:00:00Z")
        self.assertEqual(long_gap["reason"], "trailing_take_profit")
        self.assertEqual(long_gap["observed_open"], 98.5)
        self.assertTrue(long_gap["gap_at_open"])

        short_state = paper_rules.initial_risk(100.0, -1, tp, sl, {})
        # Short trailing level starts at 101.0: an open below it is still
        # profitable continuation, while an open above it is a retracement.
        self.assertIsNone(paper_rules.gap_touch(
            short_state, -1, 99.5, tp, sl, "2026-01-02T00:00:00Z"))
        short_gap = paper_rules.gap_touch(
            short_state, -1, 101.5, tp, sl, "2026-01-02T00:00:00Z")
        self.assertEqual(short_gap["reason"], "trailing_take_profit")
        self.assertEqual(short_gap["observed_open"], 101.5)
        self.assertTrue(short_gap["gap_at_open"])


if __name__ == "__main__":
    unittest.main()
