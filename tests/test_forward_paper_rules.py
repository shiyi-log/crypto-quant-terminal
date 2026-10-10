"""Synthetic forward contracts for frozen filters and TP/SL recovery."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from forward_paper_runner import ForwardPaperRunner  # noqa: E402


PAIR = "RISK/USDT:USDT"
NOW = datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc)


def candles(periods=8):
    index = pd.date_range("2025-01-01", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame({"open": np.full(periods, 100.0),
                         "high": np.full(periods, 100.1),
                         "low": np.full(periods, 99.9),
                         "close": np.full(periods, 100.0)}, index=index)


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def always_long(close, **_):
    return pd.Series(1.0, index=close.index)


class ForwardPaperRuleTests(unittest.TestCase):
    def runner(self, output, variants):
        return ForwardPaperRunner(output, variants, top_n=1, max_open=1,
                                  wallet=1_000.0, clock=lambda: NOW)

    def test_advanced_rules_change_runner_identity_and_require_new_directory(self):
        base = {"variant_id": "risk", "stop_loss": {"kind": "fixed_pct", "value": 1.0}}
        changed = {**base, "stop_loss": {"kind": "fixed_pct", "value": 2.0}}
        with tempfile.TemporaryDirectory() as tmp:
            first = self.runner(Path(tmp) / "first", [base])
            other = self.runner(Path(tmp) / "other", [changed])
            self.assertNotEqual(first.run_id, other.run_id)
            self.assertNotEqual(first.variants[0]["rule_hash"], other.variants[0]["rule_hash"])
            with self.assertRaisesRegex(ValueError, "new output directory"):
                self.runner(Path(tmp) / "first", [changed])

    def test_missing_high_low_is_recorded_and_blocks_risk_replay(self):
        rule = {"variant_id": "risk", "stop_loss": {"kind": "fixed_pct", "value": 1.0}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, [rule])
            result = runner.update({PAIR: candles()[["open", "close"]]})
            reason = f"missing_required_ohlc:{PAIR}:high,low"
            self.assertFalse(result["data_ready"])
            self.assertFalse(result["replay_completed"])
            self.assertFalse(result["strategy_usable"])
            self.assertEqual(result["fill_count"], 0)
            self.assertIn(reason, result["data_readiness_reasons"])
            self.assertIn(reason, rows(Path(tmp) / "manifest.jsonl")[-1]["data_readiness_reasons"])
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            self.assertIn(reason, checkpoint["data_readiness_reasons"])
            self.assertEqual(checkpoint["required_price_columns"], ["open", "high", "low", "close"])

    def test_invalid_ohlc_bounds_are_unavailable(self):
        frame = candles()
        frame.iloc[3, frame.columns.get_loc("high")] = 99.0
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, [{"variant_id": "risk", "range_filter": {
                "kind": "adx", "window": 2, "warmup_bars": 2, "threshold": 25.0}}])
            result = runner.update({PAIR: frame})
            self.assertFalse(result["data_ready"])
            self.assertIn(f"invalid_ohlc_bounds:{PAIR}", result["data_readiness_reasons"])
            self.assertEqual(result["decision_count"], 0)

    def test_partial_snapshot_cannot_erase_high_low_frozen_after_last_ready(self):
        frame = candles()
        rule = {"variant_id": "risk", "stop_loss": {"kind": "fixed_pct", "value": 1.0}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:3], "QUIET/USDT:USDT": frame.iloc[:3]})
            runner.update({PAIR: frame.iloc[:5], "QUIET/USDT:USDT": frame.iloc[:0]})
            runner.update({PAIR: frame.iloc[:6][["open", "close"]],
                           "QUIET/USDT:USDT": frame.iloc[:0]})
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            frozen = checkpoint["frozen_prefixes"][PAIR]
            self.assertEqual(frozen["columns"], ["open", "high", "low", "close"])
            self.assertEqual(frozen["through_utc"], frame.index[4].isoformat())
            self.assertEqual(frozen["column_sets"]["open,close"]["through_utc"],
                             frame.index[5].isoformat())

            changed = frame.copy()
            changed.iloc[4, changed.columns.get_loc("high")] = 120.0
            restarted = self.runner(tmp, [rule])
            with self.assertRaisesRegex(ValueError, "prefix conflict"):
                restarted.update({PAIR: changed.iloc[:7], "QUIET/USDT:USDT": frame.iloc[:7]})

    def test_partial_price_changes_are_rejected_before_checkpoint_contamination(self):
        frame = candles()
        rule = {"variant_id": "risk", "stop_loss": {"kind": "fixed_pct", "value": 1.0}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:5]})
            paths = [Path(tmp) / name for name in ("manifest.jsonl", "checkpoint.json")]
            original = {path.name: path.read_bytes() for path in paths}
            changed = frame.iloc[:6][["open", "close"]].copy()
            changed.iloc[4, changed.columns.get_loc("close")] = 100.5
            with self.assertRaisesRegex(ValueError, "prefix conflict"):
                runner.update({PAIR: changed})
            self.assertEqual(original, {path.name: path.read_bytes() for path in paths})
            # A rejected incomplete observation must not poison the next
            # correct complete snapshot's immutable evidence.
            self.assertTrue(runner.update({PAIR: frame.iloc[:6]})["data_ready"])

    def test_new_open_close_observations_remain_frozen_when_high_low_are_missing(self):
        frame = candles()
        rule = {"variant_id": "risk", "stop_loss": {"kind": "fixed_pct", "value": 1.0}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:3]})
            runner.update({PAIR: frame.iloc[:6][["open", "close"]]})
            changed = frame.copy()
            changed.iloc[5, changed.columns.get_loc("close")] = 100.05
            with self.assertRaisesRegex(ValueError, "prefix conflict"):
                self.runner(tmp, [rule]).update({PAIR: changed.iloc[:7]})

    def test_pending_trailing_exit_survives_partial_snapshot_and_restart(self):
        frame = candles(6)
        frame.iloc[2] = [100.0, 105.0, 99.9, 104.0]
        frame.iloc[3] = [104.0, 106.0, 104.0, 105.5]
        frame.iloc[4] = [105.5, 106.0, 104.0, 104.5]
        frame.iloc[5] = [103.0, 103.2, 102.8, 103.0]
        rule = {"variant_id": "risk", "take_profit": {"kind": "trailing_pct", "value": 1.0},
                "stop_loss": {"kind": "fixed_pct", "value": 10.0}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("forward_paper_runner.engine.signals", side_effect=always_long), \
                patch("forward_paper_runner.engine.strength", side_effect=always_long):
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:2]})
            runner.update({PAIR: frame.iloc[:5]})
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            position = checkpoint["engine_states"]["risk"]["positions"][PAIR]
            self.assertTrue(position["pending_exit"])
            self.assertEqual(position["pending_exit_reason"], "trailing_take_profit")
            self.assertAlmostEqual(position["risk_state"]["trailing_extreme"], 106.0)
            self.assertAlmostEqual(position["risk_state"]["trailing_level"], 104.94)
            frozen_trigger = position["pending_trigger"]
            frozen_risk = position["risk_state"]
            entry = rows(Path(tmp) / "fills.jsonl")[0]

            unavailable = runner.update({PAIR: frame.iloc[:5][["open", "close"]]})
            self.assertFalse(unavailable["data_ready"])
            restarted = self.runner(tmp, [rule])
            recovered = restarted.update({PAIR: frame})
            fills = rows(Path(tmp) / "fills.jsonl")
            entries = [row for row in fills if row["action"] == "entry"]
            exits = [row for row in fills if row["action"] == "exit"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(len(exits), 1)
            exit_fill = exits[0]
            self.assertEqual(exit_fill["position_id"], entry["position_id"])
            self.assertEqual(exit_fill["paper_trade_id"], entry["paper_trade_id"])
            self.assertEqual(exit_fill["filled_at_utc"], frame.index[5].isoformat())
            self.assertEqual(exit_fill["price"], 103.0)
            self.assertEqual(exit_fill["exit_reason"], "trailing_take_profit")
            self.assertEqual({key: exit_fill["trigger"][key] for key in frozen_trigger}, frozen_trigger)
            self.assertEqual(exit_fill["trigger"]["execution_gap"]["observed_open"], 103.0)
            self.assertEqual(exit_fill["risk_state"], frozen_risk)
            self.assertEqual(recovered["variants"]["risk"]["closed_trade_count"], 1)
            diag = recovered["variants"]["risk"]["diagnostics"]
            self.assertEqual(diag["open_positions"], [])
            self.assertEqual(recovered["variants"]["risk"]["open_position_count"], 0)
            self.assertAlmostEqual(diag["ending_cash"], 1_000.0 + exit_fill["profit_abs"])
            self.assertTrue(recovered["replay_completed"])
            # Replaying after another restart must not duplicate the exit.
            repeated = self.runner(tmp, [rule]).update({PAIR: frame})
            self.assertEqual(repeated["fill_count"], recovered["fill_count"])

    def test_trailing_boundary_candle_is_not_reprocessed_after_recovery(self):
        frame = candles(5)
        frame.iloc[2] = [100.0, 100.1, 99.9, 100.0]
        frame.iloc[3] = [100.0, 110.0, 99.9, 109.0]
        frame.iloc[4] = [109.0, 110.5, 108.95, 110.0]
        rule = {"variant_id": "risk", "take_profit": {"kind": "trailing_pct", "value": 1.0}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("forward_paper_runner.engine.signals", side_effect=always_long), \
                patch("forward_paper_runner.engine.strength", side_effect=always_long):
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:2]})
            runner.update({PAIR: frame.iloc[:4]})
            position = runner.state["engine_states"]["risk"]["positions"][PAIR]
            self.assertFalse(position["pending_exit"])
            self.assertAlmostEqual(position["risk_state"]["trailing_level"], 108.9)
            runner.update({PAIR: frame.iloc[:4][["open", "close"]]})
            restarted = self.runner(tmp, [rule])
            result = restarted.update({PAIR: frame})
            fills = rows(Path(tmp) / "fills.jsonl")
            self.assertEqual([row["action"] for row in fills], ["entry"])
            self.assertEqual(result["variants"]["risk"]["open_position_count"], 1)
            self.assertEqual(result["variants"]["risk"]["diagnostics"]["risk_exit_triggers"], 0)
            current = restarted.state["engine_states"]["risk"]["positions"][PAIR]
            self.assertFalse(current["pending_exit"])
            self.assertAlmostEqual(current["risk_state"]["trailing_extreme"], 110.5)

    def test_atr_levels_remain_entry_frozen_across_an_unobserved_interval(self):
        frame = candles(6)
        frame.iloc[3] = [100.0, 100.15, 99.85, 100.0]
        # This broad candle is learned only after recovery. It cannot change
        # the position's entry ATR or cause a backfilled historical exit.
        frame.iloc[4] = [100.0, 105.0, 95.0, 100.0]
        frame.iloc[5] = [100.0, 100.3, 99.7, 100.0]
        rule = {"variant_id": "risk", "take_profit": {
            "kind": "atr_multiple", "value": 3.0, "atr_window": 2},
            "stop_loss": {"kind": "atr_multiple", "value": 2.0, "atr_window": 2}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("forward_paper_runner.engine.signals", side_effect=always_long), \
                patch("forward_paper_runner.engine.strength", side_effect=always_long):
            runner = self.runner(tmp, [rule])
            runner.update({PAIR: frame.iloc[:2]})
            runner.update({PAIR: frame.iloc[:4]})
            position = runner.state["engine_states"]["risk"]["positions"][PAIR]
            frozen_risk = position["risk_state"]
            self.assertAlmostEqual(frozen_risk["entry_atr"]["2"], 0.2)
            self.assertAlmostEqual(frozen_risk["take_profit_level"], 100.6)
            self.assertAlmostEqual(frozen_risk["stop_loss_level"], 99.6)

            runner.update({PAIR: frame.iloc[:5][["open", "close"]]})
            restarted = self.runner(tmp, [rule])
            recovered = restarted.update({PAIR: frame})
            restored = restarted.state["engine_states"]["risk"]["positions"][PAIR]
            self.assertEqual(restored["risk_state"], frozen_risk)
            self.assertFalse(restored["pending_exit"])
            self.assertEqual(restarted.state["engine_states"]["risk"]["risk_continuity"], "unknown_gap")
            self.assertEqual(recovered["variants"]["risk"]["closed_trade_count"], 0)
            self.assertEqual([row["action"] for row in rows(Path(tmp) / "fills.jsonl")], ["entry"])


if __name__ == "__main__":
    unittest.main()
