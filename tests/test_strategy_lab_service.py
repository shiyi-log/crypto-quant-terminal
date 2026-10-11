"""Causality at the history/live boundary and visible service failure states."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from strategy_lab_service import (BAR_MS, MAX_CLOSE_DELAY_MS, PAIRS, StrategyLabService,
                                  merge_closed_history, parse_warmup_klines,
                                  read_service_status)


def candle(stamp, closed=True):
    return {"timestamp": stamp, "open": 100, "high": 102, "low": 99, "close": 101,
            "volume": 20, "extras": {"closed": closed, "taker_buy_base": 12}}


class HistoryBoundaryTests(unittest.TestCase):
    def test_rest_warmup_explicit_close_times_and_no_future_rows(self):
        rows = [[t, "100", "102", "99", "101", "20", t + BAR_MS - 1,
                 "2000", 100, "12", "1200", 0] for t in (0, BAR_MS, 2 * BAR_MS)]
        result = parse_warmup_klines(rows, 2 * BAR_MS, 999999)
        self.assertEqual([r["timestamp"] for r in result], [0, BAR_MS])
        self.assertTrue(all(r["extras"]["closed"] is True for r in result))
        self.assertEqual(result[-1]["extras"]["taker_buy_base"], 12)

    def test_gap_or_incomplete_history_fails_instead_of_interpolating(self):
        def row(t):
            return [t, 100, 102, 99, 101, 20, t + BAR_MS - 1, 2000, 100, 12, 1200]
        with self.assertRaisesRegex(ValueError, "continuous"):
            parse_warmup_klines([row(0), row(2 * BAR_MS)], 3 * BAR_MS, 999999)
        with self.assertRaisesRegex(ValueError, "boundary"):
            parse_warmup_klines([row(0)], 3 * BAR_MS, 999999)

    def test_future_rest_cannot_substitute_for_missing_live_close(self):
        history = [candle(0), candle(BAR_MS), candle(2 * BAR_MS)]
        live = [candle(BAR_MS), candle(2 * BAR_MS, False), candle(3 * BAR_MS)]
        merged = merge_closed_history(history, live, 3 * BAR_MS, 2 * BAR_MS)
        self.assertEqual([r["timestamp"] for r in merged], [0, BAR_MS])

    def test_later_database_revision_cannot_change_accepted_closed_input(self):
        accepted = candle(2 * BAR_MS)
        revised = dict(accepted, close=100)
        merged = merge_closed_history([candle(BAR_MS)], [revised, candle(3 * BAR_MS)],
                                       4 * BAR_MS, 2 * BAR_MS, {2 * BAR_MS: accepted})
        self.assertEqual(next(r for r in merged if r["timestamp"] == 2 * BAR_MS)["close"], 101)
        self.assertEqual(merged[-1]["timestamp"], 3 * BAR_MS)


class ServiceCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.engine = Mock(), Mock()
        self.service = StrategyLabService(self.store, self.engine,
            [{"strategy_id": "test"}], now_ms=2 * BAR_MS,
            status_path=Path(self.temp.name) / "status.json")
        self.service.history = {p: [candle(0), candle(BAR_MS)] for p in PAIRS}

    def test_history_only_cycle_never_evaluates_or_trades(self):
        self.store.get_candles.return_value = [candle(BAR_MS)]
        with patch("strategy_signals.evaluate") as evaluate:
            self.service.cycle(3 * BAR_MS)
        evaluate.assert_not_called()
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["status"], "waiting_new_closed_candle")

    def test_missing_leg_blocks_all_orders(self):
        self.store.get_candles.side_effect = [[candle(2 * BAR_MS)], [candle(BAR_MS)]]
        with patch("strategy_signals.evaluate") as evaluate:
            self.service.cycle(3 * BAR_MS)
        evaluate.assert_not_called()
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["status"], "waiting_aligned_data")

    def test_new_closed_bar_signals_use_positions_after_pending_open_fills(self):
        self.store.get_candles.return_value = [candle(2 * BAR_MS)]
        after_fill = {"test": {PAIRS[0]: {"side": 1, "entry_price": 100}}}
        self.engine.preview_positions.return_value = after_fill
        with patch("strategy_signals.evaluate", return_value={"ready": True}) as evaluate:
            self.service.cycle(3 * BAR_MS)
            self.service.cycle(3 * BAR_MS + 1000)
        evaluate.assert_called_once()
        self.assertEqual(evaluate.call_args.kwargs["positions"], after_fill["test"])
        self.engine.positions_by_strategy.assert_not_called()
        self.engine.process_batch.assert_called_once()
        self.assertEqual(self.engine.process_batch.call_args.kwargs["session_boundary_ms"], 2 * BAR_MS)

    def test_stale_candle_does_not_generate_delayed_orders(self):
        self.store.get_candles.return_value = [candle(2 * BAR_MS)]
        with patch("strategy_signals.evaluate") as evaluate:
            self.service.cycle(6 * BAR_MS)
        evaluate.assert_not_called()
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["status"], "data_stale")

    def test_processed_close_remains_current_until_next_close_deadline(self):
        self.store.get_candles.return_value = [candle(2 * BAR_MS)]
        self.service.last_processed = 2 * BAR_MS
        with patch("strategy_signals.evaluate") as evaluate:
            for now in (3 * BAR_MS + MAX_CLOSE_DELAY_MS + 1,
                        4 * BAR_MS + MAX_CLOSE_DELAY_MS):
                self.service.cycle(now)
                self.assertEqual(self.service.status["status"], "observing")
            self.service.cycle(4 * BAR_MS + MAX_CLOSE_DELAY_MS + 1)
        evaluate.assert_not_called()
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["status"], "data_stale")

    def test_unprocessed_close_still_rejected_after_acceptance_deadline(self):
        self.store.get_candles.return_value = [candle(2 * BAR_MS)]
        with patch("strategy_signals.evaluate") as evaluate:
            self.service.cycle(3 * BAR_MS + MAX_CLOSE_DELAY_MS + 1)
        evaluate.assert_not_called()
        self.engine.process_batch.assert_not_called()
        self.assertIsNone(self.service.last_processed)
        self.assertEqual(self.service.status["status"], "data_stale")

    def test_heartbeat_must_be_fresh_and_process_alive(self):
        path = Path(self.temp.name) / "health.json"
        path.write_text(json.dumps({"heartbeat_at": 1000, "pid": 42,
                                    "running": True, "status": "observing"}))
        with patch("strategy_lab_service.os.kill"):
            self.assertTrue(read_service_status(path, 1001)["running"])
            self.assertFalse(read_service_status(path, 100000)["running"])
        with patch("strategy_lab_service.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(read_service_status(path, 1001)["running"])


if __name__ == "__main__":
    unittest.main()
