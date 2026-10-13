"""Exchange event timestamp gap metrics for the market collector."""

from pathlib import Path
import tempfile
import unittest

from market_stream import MarketCollector, StreamConfig


class GapMetricTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = StreamConfig(
            spool_dir=Path(self.temporary.name) / "spool",
            gap_threshold_ms=60_000,
        )
        self.collector = MarketCollector(self.config, object())

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, kind, timestamp, *, timeframe=None):
        event = {
            "kind": kind,
            "market": "futures",
            "pair": "BTC/USDT:USDT",
            "row": {"timestamp": timestamp},
        }
        if timeframe is not None:
            event["timeframe"] = timeframe
        return event

    def test_kline_interval_boundary_is_not_a_gap(self):
        self.collector.record_event_gap(self.event("kline", 0, timeframe="5m"))
        self.collector.record_event_gap(self.event("kline", 300_000, timeframe="5m"))

        metrics = self.collector.status["gap_metrics"][
            "kline:futures:BTC/USDT:USDT:5m"
        ]
        self.assertEqual(metrics["gap_count"], 0)
        self.assertEqual(self.collector.status["gap_count"], 0)

    def test_kline_gap_is_recorded_with_candle_interval(self):
        self.collector.record_event_gap(self.event("kline", 0, timeframe="5m"))
        self.collector.record_event_gap(self.event("kline", 420_000, timeframe="5m"))

        metrics = self.collector.status["gap_metrics"][
            "kline:futures:BTC/USDT:USDT:5m"
        ]
        self.assertEqual(metrics["gap_count"], 1)
        self.assertEqual(metrics["max_gap_ms"], 120_000)
        self.assertEqual(metrics["last_gap"]["expected_interval_ms"], 300_000)
        self.assertEqual(self.collector.status["max_gap_ms"], 120_000)
        self.assertTrue(self.collector.status["data_gap_possible"])
        self.assertEqual(self.collector.status["connection_gap_metrics"]["trades"]["gap_count"], 1)

    def test_tick_and_orderbook_use_event_timestamp_and_keep_identities_separate(self):
        self.collector.record_event_gap(self.event("tick", 1_000))
        self.collector.record_event_gap(self.event("tick", 61_001))
        self.collector.record_event_gap(self.event("orderbook", 2_000))
        self.collector.record_event_gap(self.event("orderbook", 63_001))

        ticks = self.collector.status["gap_metrics"]["tick:futures:BTC/USDT:USDT"]
        books = self.collector.status["gap_metrics"]["orderbook:futures:BTC/USDT:USDT"]
        self.assertEqual(ticks["gap_count"], 1)
        self.assertEqual(books["gap_count"], 1)
        self.assertEqual(ticks["last_gap"]["gap_ms"], 60_001)
        self.assertEqual(books["last_gap"]["gap_ms"], 61_001)
        self.assertEqual(self.collector.status["connection_gap_metrics"]["trades"]["gap_count"], 1)
        self.assertEqual(self.collector.status["connection_gap_metrics"]["orderbooks"]["gap_count"], 1)

    def test_out_of_order_event_does_not_create_or_hide_a_gap(self):
        self.collector.record_event_gap(self.event("tick", 100_000))
        self.collector.record_event_gap(self.event("tick", 50_000))
        self.collector.record_event_gap(self.event("tick", 161_000))

        metrics = self.collector.status["gap_metrics"]["tick:futures:BTC/USDT:USDT"]
        self.assertEqual(metrics["gap_count"], 1)
        self.assertEqual(metrics["last_gap"]["previous_event_timestamp"], 100_000)
        self.assertEqual(metrics["last_event_timestamp"], 161_000)

    def test_gap_threshold_must_be_positive_integer(self):
        with self.assertRaises(ValueError):
            StreamConfig(gap_threshold_ms=0)
        with self.assertRaises(ValueError):
            StreamConfig(gap_threshold_ms=1.5)
        with self.assertRaises(ValueError):
            StreamConfig(gap_threshold_ms=True)


if __name__ == "__main__":
    unittest.main()
