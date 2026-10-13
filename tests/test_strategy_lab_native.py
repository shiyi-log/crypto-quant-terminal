"""Native timeframe strategy-lab cycle contracts."""

from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from strategy_lab_service import (MAX_CLOSE_DELAY_MS, NATIVE_WARMUP_REFRESH_MS,
                                  StrategyLabService)


def candle(timestamp):
    return {"timestamp": timestamp, "open": 100, "high": 101, "low": 99,
            "close": 100, "volume": 10, "extras": {"closed": True}}


class NativeCycleTests(TestCase):
    def native_service(self, timeframe):
        pair = "BTC/USDT:USDT"
        interval = {"15m": 900_000, "1h": 3_600_000}[timeframe]
        spec = {"strategy_id": f"native_{timeframe}", "timeframe": timeframe,
                "execution_universe": [pair], "universe": [pair]}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=2 * interval,
                                     status_path=Path(temporary.name) / "status.json")
        rows = [candle(index * interval) for index in range(3)]
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: rows}
        service.simulation.preview_positions.return_value = {spec["strategy_id"]: {}}
        signal = {"strategy_id": spec["strategy_id"], "rule_hash": "test",
                  "ready": True, "warmup": {"ready": True},
                  "signals": {pair: {"target_side": None}}}
        return service, rows, signal, interval

    def test_non_five_minute_account_uses_native_window_and_processes(self):
        pair = "BTC/USDT:USDT"
        spec = {"strategy_id": "s12_surge_expansion_breakout_100x_maker",
                "timeframe": "15m", "execution_universe": [pair],
                "universe": [pair], "params_frozen": {"VR": 10.0,
                "window": 96, "valid_bars": 24, "H": 48},
                "position_budget": {"leverage": 100}}
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=1_800_001,
                                     status_path=status_path)
        rows = [candle(0), candle(900_000), candle(1_800_000)]
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: rows}
        service.simulation.preview_positions.return_value = {spec["strategy_id"]: {}}
        signal = {"strategy_id": spec["strategy_id"], "rule_hash": "test",
                  "ready": True, "warmup": {"ready": True}, "signals":
                  {pair: {"target_side": None}}}
        with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
            service._cycle_native(1_800_001)
        self.assertEqual(evaluate.call_count, 2)
        service.simulation.process_batch.assert_called_once()
        self.assertEqual(service.simulation.process_batch.call_args.kwargs["timeframe"], "15m")
        self.assertEqual(service.status["native_timeframes"]["15m"]["status"], "observing")

    def test_native_bootstrap_does_not_emit_blocked_placeholder(self):
        pair = "BTC/USDT:USDT"
        spec = {"strategy_id": "s12_surge_expansion_breakout_100x_maker",
                "timeframe": "15m", "execution_universe": [pair],
                "universe": [pair], "params_frozen": {"VR": 10.0,
                "window": 96, "valid_bars": 24, "H": 48}}
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=900_000,
                                     status_path=status_path)
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: [candle(0)]}
        evaluations = {}
        signal = {"strategy_id": spec["strategy_id"], "rule_hash": "test",
                  "ready": False, "warmup": {"ready": False}, "signals":
                  {pair: {"target_side": None}}}
        with patch("strategy_signals.evaluate", return_value=signal):
            service._native_bootstrap(evaluations)
        self.assertEqual(evaluations[spec["strategy_id"]]["warmup"]["ready"], False)
        self.assertNotEqual(evaluations[spec["strategy_id"]]["warmup"].get("reason"),
                            "awaiting_native_timeframe_adapter")

    def test_native_only_cycle_preserves_waiting_status(self):
        pair = "BTC/USDT:USDT"
        spec = {"strategy_id": "s12_surge_expansion_breakout_100x_maker",
                "timeframe": "15m", "execution_universe": [pair],
                "universe": [pair], "params_frozen": {"VR": 10.0,
                "window": 96, "valid_bars": 24, "H": 48},
                "position_budget": {"leverage": 100}}
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=900_000,
                                     status_path=status_path)
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: []}
        service.cycle(900_001)
        self.assertEqual(service.status["status"], "waiting_new_closed_candle")

    def test_native_empty_tail_becomes_explicitly_stale_after_startup_grace(self):
        pair = "BTC/USDT:USDT"
        spec = {"strategy_id": "s12_surge_expansion_breakout_100x_maker",
                "timeframe": "15m", "execution_universe": [pair],
                "universe": [pair], "params_frozen": {"VR": 10.0,
                "window": 96, "valid_bars": 24, "H": 48},
                "position_budget": {"leverage": 100}}
        status_path = Path(tempfile.mkdtemp()) / "status.json"
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=900_000,
                                     status_path=status_path)
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: []}
        interval = 900_000
        boundary = service.boundaries["15m"]
        service.cycle(boundary + interval + MAX_CLOSE_DELAY_MS + 1)
        self.assertEqual(service.status["native_timeframes"]["15m"]["status"],
                         "data_stale")
        self.assertEqual(service.status["status"], "data_stale")

    def test_processed_native_close_current_until_successor_close_deadline(self):
        for timeframe in ("15m", "1h"):
            with self.subTest(timeframe=timeframe):
                service, _rows, signal, interval = self.native_service(timeframe)
                service.native_last_processed[timeframe] = 2 * interval
                with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
                    for now in (3 * interval + MAX_CLOSE_DELAY_MS + 1,
                                4 * interval + MAX_CLOSE_DELAY_MS):
                        service.cycle(now)
                        self.assertEqual(service.status["status"], "observing")
                        self.assertEqual(service.status["native_timeframes"][timeframe]["status"],
                                         "observing")
                    service.cycle(4 * interval + MAX_CLOSE_DELAY_MS + 1)
                self.assertEqual(service.status["status"], "data_stale")
                self.assertEqual(evaluate.call_count, 2)
                service.simulation.update_warmup.assert_called_once()
                self.assertTrue(service.simulation.update_warmup.call_args.kwargs["diagnostic_only"])
                service.simulation.preview_positions.assert_not_called()
                service.simulation.process_batch.assert_not_called()

    def test_unprocessed_native_close_keeps_strict_acceptance_deadline(self):
        for timeframe in ("15m", "1h"):
            for delay, accepted in ((MAX_CLOSE_DELAY_MS, True),
                                    (MAX_CLOSE_DELAY_MS + 1, False)):
                with self.subTest(timeframe=timeframe, delay=delay):
                    service, _rows, signal, interval = self.native_service(timeframe)
                    with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
                        service.cycle(3 * interval + delay)
                    self.assertEqual(service.status["status"],
                                     "observing" if accepted else "data_stale")
                    self.assertEqual(evaluate.call_count, 1 + int(accepted))
                    self.assertEqual(service.simulation.preview_positions.call_count, int(accepted))
                    self.assertEqual(service.simulation.process_batch.call_count, int(accepted))
                    self.assertEqual(service.native_last_processed.get(timeframe),
                                     2 * interval if accepted else None)

    def test_timely_successor_resumes_existing_position_management_without_replay(self):
        pair = "BTC/USDT:USDT"
        for timeframe in ("15m", "1h"):
            with self.subTest(timeframe=timeframe):
                service, rows, signal, interval = self.native_service(timeframe)
                position = {pair: {"side": 1, "entry_price": 100, "margin": 90}}
                service.simulation.preview_positions.return_value = {
                    signal["strategy_id"]: position}
                with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
                    service.cycle(3 * interval + MAX_CLOSE_DELAY_MS + 1)
                    self.assertEqual(service.status["status"], "data_stale")
                    service.simulation.process_batch.assert_not_called()
                    rows.append(candle(3 * interval))
                    service.cycle(4 * interval + 1000)
                self.assertEqual(evaluate.call_count, 3)
                self.assertEqual(evaluate.call_args.kwargs["positions"], position)
                service.simulation.process_batch.assert_called_once()
                submitted = service.simulation.process_batch.call_args.args[0]
                self.assertEqual(submitted[pair][-1]["timestamp"], 3 * interval)
                self.assertEqual(service.native_last_processed[timeframe], 3 * interval)
                self.assertEqual(service.status["status"], "observing")

    def test_stale_native_period_does_not_block_other_period_position_management(self):
        pair = "BTC/USDT:USDT"
        service, _rows, signal, interval = self.native_service("1h")
        stale = {"strategy_id": "stale_15m", "timeframe": "15m",
                 "execution_universe": [pair], "universe": [pair]}
        service.specs.insert(0, stale)
        service.boundaries["15m"] = 2 * interval
        service.load_strategy_inputs = lambda *_args, **_kwargs: {
            pair: [candle(2 * interval)]}
        position = {pair: {"side": -1, "entry_price": 100, "margin": 90}}
        service.simulation.preview_positions.return_value = {signal["strategy_id"]: position}
        with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
            service.cycle(3 * interval + 1000)
        self.assertEqual(service.status["native_timeframes"]["15m"]["status"], "data_stale")
        self.assertEqual(service.status["native_timeframes"]["1h"]["status"], "observing")
        self.assertEqual(service.status["status"], "observing")
        self.assertEqual(evaluate.call_count, 3)
        self.assertEqual(evaluate.call_args.kwargs["positions"], position)
        service.simulation.process_batch.assert_called_once()
        self.assertEqual(service.simulation.process_batch.call_args.kwargs["timeframe"], "1h")

    def test_warmup_refreshes_before_session_boundary_and_during_stale_period(self):
        pair = "BTC/USDT:USDT"
        interval = 3_600_000
        spec = {"strategy_id": "s16_weekly_seasonality", "timeframe": "1h",
                "execution_universe": [pair], "universe": [pair]}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = StrategyLabService(Mock(), Mock(), [spec], now_ms=800 * interval,
                                     status_path=Path(temporary.name) / "status.json")
        rows = [candle(index * interval) for index in range(773, 800)]
        service.load_strategy_inputs = lambda *_args, **_kwargs: {pair: rows}

        service.cycle(800 * interval + 1000)
        first = service.simulation.update_warmup.call_args
        self.assertEqual(first.args[0][spec["strategy_id"]]["warmup"]["valid_bars"], 27)
        self.assertEqual(service.status["status"], "waiting_new_closed_candle")

        rows.append(candle(800 * interval))
        now = 801 * interval + MAX_CLOSE_DELAY_MS + 1
        service.cycle(now)
        second = service.simulation.update_warmup.call_args
        self.assertEqual(second.args[0][spec["strategy_id"]]["warmup"]["valid_bars"], 28)
        self.assertEqual(second.kwargs["observed_at_ms"], now)
        self.assertEqual(service.status["status"], "data_stale")

        # Filling older missing input makes indicators ready while execution
        # must still reject this close after its acceptance deadline.
        rows[:0] = [candle(index * interval) for index in range(700, 773)]
        service.cycle(now + NATIVE_WARMUP_REFRESH_MS)
        warmup = service.simulation.update_warmup.call_args.args[0][spec["strategy_id"]]["warmup"]
        self.assertEqual(warmup["valid_bars"], 101)
        self.assertTrue(warmup["ready"])
        self.assertEqual(warmup["reason"], "ready")
        self.assertEqual(service.status["status"], "data_stale")
        self.assertEqual(service.simulation.update_warmup.call_count, 3)
        service.simulation.preview_positions.assert_not_called()
        service.simulation.process_batch.assert_not_called()
        self.assertNotIn("1h", service.native_last_processed)

    def test_unchanged_diagnostics_are_throttled_and_not_written_again(self):
        service, rows, signal, interval = self.native_service("15m")
        service.native_last_processed["15m"] = 2 * interval
        now = 3 * interval + 1000
        with patch("strategy_signals.evaluate", return_value=signal) as evaluate:
            service.cycle(now)
            service.cycle(now + 1000)
            self.assertEqual(evaluate.call_count, 1)
            service.cycle(now + NATIVE_WARMUP_REFRESH_MS)
            self.assertEqual(evaluate.call_count, 2)
            rows.append(candle(3 * interval))
            service.native_last_processed["15m"] = 3 * interval
            service.cycle(4 * interval + 1000)
            self.assertEqual(evaluate.call_count, 3)
        service.simulation.update_warmup.assert_called_once()
        service.simulation.process_batch.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
