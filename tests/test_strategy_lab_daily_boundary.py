"""Daily startup causality using the real loader, evaluator and ledger transition."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from strategy_factorial import BTC, ETH, TRX
from strategy_lab_service import MAX_CLOSE_DELAY_MS, StrategyLabService
from strategy_signals import normalize_spec
from strategy_simulation import apply_closed_batch, initial_state
from tests.test_strategy_factorial_service import CandleStore, candle, factor_spec


DAY = 86_400_000
MIDNIGHT = int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp() * 1000)
BOUNDARY = MIDNIGHT - DAY


class DailyBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        legacy = normalize_spec({
            "strategy_id": "s22_dual_confirmation_trend_Dgen3_100x_maker",
            "timeframe": "1d", "universe": [BTC, ETH],
            "execution_universe": [BTC, ETH], "position_budget": {"leverage": 100},
        })
        factor = factor_spec(canonical="s22_dual_confirmation_trend", timeframe="1d",
                             pairs=(BTC, ETH, TRX))
        self.specs = [legacy, factor]
        self.store = CandleStore()
        self.store.rows = {
            (pair, "1d"): [candle(BOUNDARY - DAY * offset)
                            for offset in range(750, 0, -1)]
            for pair in (BTC, ETH, TRX)
        }
        self.engine = Mock()
        self.engine.preview_positions.return_value = {}
        self.service = StrategyLabService(
            self.store, self.engine, self.specs,
            now_ms=BOUNDARY + (17 * 60 + 25) * 60_000,
            status_path=Path(temporary.name) / "status.json")
        self.states = {spec["strategy_id"]: initial_state(spec, BOUNDARY)
                       for spec in self.specs}
        self.events = []

        def persist_in_memory(rows, signals, *, observed_at_ms, session_boundary_ms,
                              timeframe):
            self.assertEqual(timeframe, "1d")
            self.assertEqual(session_boundary_ms, BOUNDARY)
            for spec in self.specs:
                identity = spec["strategy_id"]
                if identity in signals:
                    self.states[identity], events = apply_closed_batch(
                        self.states[identity], spec, rows, signals[identity],
                        observed_at_ms, session_boundary_ms)
                    self.events.extend(events)

        self.engine.process_batch.side_effect = persist_in_memory

    def append_current_day(self, pairs=(BTC, ETH, TRX), *, closed=True):
        for pair in pairs:
            row = candle(BOUNDARY)
            row["extras"]["closed"] = closed
            self.store.rows[(pair, "1d")].append(row)

    def test_next_utc_midnight_creates_daily_decisions_once_with_late_trx(self):
        self.service.bootstrap()
        self.assertEqual(self.service.boundaries["1d"], BOUNDARY)
        self.service.cycle(MIDNIGHT - 1)
        self.assertEqual(self.events, [])
        self.engine.process_batch.assert_not_called()

        self.append_current_day((BTC, ETH))
        self.service.cycle(MIDNIGHT + 1000)
        legacy, factor = self.specs
        self.assertEqual(self.states[legacy["strategy_id"]]["last_candle_ms"], BOUNDARY)
        self.assertIsNone(self.states[factor["strategy_id"]]["last_candle_ms"])

        self.append_current_day((TRX,))
        self.service.cycle(MIDNIGHT + 15_000)
        self.service.cycle(MIDNIGHT + 30_000)
        decisions = [event for event in self.events if event["event_type"] == "decision"]
        self.assertEqual(Counter(event["strategy_id"] for event in decisions),
                         Counter({spec["strategy_id"]: 1 for spec in self.specs}))
        self.assertTrue(all(state["last_candle_ms"] == BOUNDARY
                            for state in self.states.values()))
        self.assertEqual(self.service.status["native_timeframes"]["1d"]["status"],
                         "observing")

    def test_midnight_does_not_accept_an_unclosed_daily_snapshot(self):
        self.append_current_day(closed=False)
        self.service.cycle(MIDNIGHT + 1000)
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["native_timeframes"]["1d"]["status"],
                         "waiting_new_closed_candle")

    def test_daily_close_missed_past_ninety_seconds_stays_stale(self):
        self.append_current_day()
        self.service.cycle(MIDNIGHT + MAX_CLOSE_DELAY_MS + 1)
        self.engine.process_batch.assert_not_called()
        self.assertEqual(self.service.status["native_timeframes"]["1d"]["status"],
                         "data_stale")


if __name__ == "__main__":
    unittest.main()
