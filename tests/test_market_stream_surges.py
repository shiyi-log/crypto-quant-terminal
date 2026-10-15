"""Closed-candle surge integration uses durable files without exchange access."""

import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_stream import FileSpool, MarketCollector, StreamConfig, normalize_message
from orderflow_detector import VolumeSurgeDetector
from tests.test_market_stream import MemoryStore, book, trade


PAIR = "BTC/USDT:USDT"
MISSING = object()


def kline(timestamp=600_000, quote_volume=40, *, closed=True, interval="5m"):
    return {"stream": f"btcusdt@kline_{interval}", "data": {
        "e": "kline", "E": timestamp + 300_000, "s": "BTCUSDT",
        "k": {"t": timestamp, "T": timestamp + 299_999, "s": "BTCUSDT", "i": interval,
              "o": "10", "h": "12", "l": "9", "c": "11", "v": "4",
              "q": str(quote_volume), "x": closed, "n": 7, "V": "3",
              "Q": str(quote_volume * .6)},
    }}


def archived_candle(timestamp, quote_volume, *, closed=True, live=True):
    extras = {"quote_volume": quote_volume}
    if closed is not MISSING:
        extras["closed"] = closed
    if live:
        extras["live_event_timestamp"] = timestamp + 299_999
    return {"timestamp": timestamp, "open": 10, "high": 12, "low": 9,
            "close": 11, "volume": "4", "extras": extras}


class SurgeStore(MemoryStore):
    """Model the persisted candle columns and canonical nested event contract."""

    def __init__(self):
        super().__init__()
        self.candles = {}
        self.history = {}
        self.candle_reads = []
        self.candle_writes = []
        self.surges = {}
        self.surge_reads = []
        self.surge_attempts = []
        self.price_candidate_attempts = []
        self.fail_surge = False

    def upsert_candles(self, exchange, market, pair, timeframe, candle_type, rows,
                       source_key=None):
        if self.fail:
            raise ConnectionError("测试 K 线数据库不可用")
        self.candle_writes.append((exchange, market, pair, timeframe, candle_type,
                                   copy.deepcopy(rows)))
        for row in rows:
            # DataStore persists OHLCV and extras, not arbitrary top-level fields.
            persisted = {name: copy.deepcopy(row[name]) for name in
                         ("timestamp", "open", "high", "low", "close", "volume", "extras")}
            self.candles[(exchange, market, pair, timeframe, candle_type,
                          row["timestamp"])] = persisted

    def get_candles(self, exchange, market, pair, timeframe, candle_type="futures",
                    limit=200, start=None, end=None):
        self.candle_reads.append((exchange, market, pair, timeframe, candle_type, limit))
        selected_history = (market, pair, candle_type)
        if selected_history in self.history:
            # Supplied history can be unsorted, unlike the real SQL read.
            return copy.deepcopy(self.history[selected_history])
        if candle_type == market and (market, pair) in self.history:
            return copy.deepcopy(self.history[(market, pair)])
        identity = (exchange, market, pair, timeframe, candle_type)
        rows = [row for key, row in self.candles.items() if key[:5] == identity]
        return copy.deepcopy(sorted(rows, key=lambda row: row["timestamp"])[-limit:])

    def append_orderflow_surge(self, event, source_id=None):
        self.surge_attempts.append(copy.deepcopy(event))
        if self.fail or self.fail_surge:
            raise ConnectionError("测试突增观测数据库不可用")
        if not isinstance(event.get("observation"), dict):
            raise ValueError("canonical sink requires the nested detector event")
        previous = self.surges.get(event["event_id"])
        if previous is not None and previous != event:
            raise ValueError("an event identity cannot change during replay")
        self.surges[event["event_id"]] = copy.deepcopy(event)
        return event["event_id"]

    def insert_surge_observation(self, observation):
        raise AssertionError("collector should prefer append_orderflow_surge")

    def append_price_candidate(self, event, source_id=None):
        self.price_candidate_attempts.append(copy.deepcopy(event))
        if self.fail:
            raise ConnectionError("测试价格候选数据库不可用")
        return event["event_id"]

    def read_orderflow_surges(self, market="futures", pair=None, timeframe="5m",
                              status=None, limit=100, include_revisions=False,
                              start=None, end=None, event_id=None):
        self.surge_reads.append({"market": market, "pair": pair,
                                 "timeframe": timeframe, "limit": limit})
        rows = [row for row in self.surges.values()
                if row["market"] == market and (pair is None or row["pair"] == pair)
                and row["timeframe"] == timeframe]
        return copy.deepcopy(rows[-limit:] if limit is not None else rows)


class CollectorSurgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SurgeStore()
        self.config = StreamConfig(
            pairs=(PAIR,), spool_dir=Path(self.temporary.name) / "spool",
            flush_interval=.01, retry_max=.01, shutdown_timeout=.03,
            status_interval=.01,
        )
        self.collector = MarketCollector(self.config, self.store,
                                         VolumeSurgeDetector(window_size=2))

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def seed(self, rows, *, now_ms=900_000):
        self.store.history[("futures", PAIR)] = rows
        with patch("market_stream.time.time", return_value=now_ms / 1000):
            await self.collector.seed_surge_detector()

    def pending_events(self, collector=None):
        collector = collector or self.collector
        return [event for path in collector.spool.pending()
                for event in collector.spool.load(path)]

    async def flush_all(self, collector=None):
        collector = collector or self.collector
        while collector.spool.pending():
            self.assertTrue(await collector.flush_spool())

    async def test_closed_candle_and_nested_observation_are_durable_before_return(self):
        await self.seed([archived_candle(0, 10, closed=True),
                         archived_candle(300_000, 10, closed=True)])
        with patch("market_stream.time.time", return_value=900):
            await self.collector.receive(kline())

        self.assertEqual(self.store.candles, {})
        self.assertEqual(self.store.surges, {})
        self.assertEqual(self.collector.queue.qsize(), 0)
        paths = self.collector.spool.pending()
        self.assertEqual(len(paths), 1)
        events = self.collector.spool.load(paths[0])
        self.assertEqual([event["kind"] for event in events],
                         ["kline", "price_candidate", "orderflow_surge"])
        candidate = events[1]["row"]
        self.assertEqual(candidate["observation"]["ratio_band"], "4to8")
        observation = events[2]["row"]
        self.assertEqual(observation["observation"]["baseline_quote_volume"], 10)
        self.assertEqual(observation["observation"]["threshold_band"], "4x")
        self.assertEqual(observation["observed_at"], 900_000)
        self.assertEqual(observation["status"], "unknown")
        restarted_spool = FileSpool(self.config.spool_dir)
        self.assertEqual(restarted_spool.load(restarted_spool.pending()[0]), events)

        await self.flush_all()
        self.assertEqual(self.store.surge_attempts, [observation])
        self.assertEqual(self.store.price_candidate_attempts, [candidate])
        self.assertEqual(self.store.surges[observation["event_id"]], observation)
        row = next(iter(self.store.candles.values()))
        self.assertEqual(row["extras"]["quote_volume"], 40)
        self.assertIs(row["extras"]["closed"], True)
        self.assertEqual(row["extras"]["num_trades"], 7)
        self.assertEqual(float(row["extras"]["taker_buy_base"]), 3)
        self.assertEqual(row["extras"]["taker_buy_quote"], 24)
        self.assertAlmostEqual(row["extras"]["flow_imbal"], .2)
        self.assertAlmostEqual(row["extras"]["flow_imbal_q"], .2)
        self.assertEqual(row["extras"]["flow_imbal_base"], .5)
        self.assertEqual(self.store.candle_writes[0][:5],
                         ("binance", "futures", PAIR, "5m", "futures"))
        self.assertEqual(self.store.candle_writes[1][:5],
                         ("binance", "futures", PAIR, "5m", "orderflow"))
        self.assertEqual(self.store.candle_writes[0][5], self.store.candle_writes[1][5])
        self.assertEqual(self.collector.status["surge_observed"], 1)
        self.assertEqual(self.collector.status["surge_persisted"], 1)

    async def test_repeated_close_emits_one_observation(self):
        await self.seed([archived_candle(0, 10, closed=True),
                         archived_candle(300_000, 10, closed=True)])
        await self.collector.receive(kline())
        await self.collector.receive(kline())
        self.assertEqual(sum(event["kind"] == "orderflow_surge"
                             for event in self.pending_events()), 1)
        self.collector.stop.set()
        await self.collector.writer()
        self.assertEqual(len(self.store.candles), 2)
        self.assertEqual(len(self.store.surges), 1)
        self.assertEqual(self.collector.spool.pending(), [])

    async def test_open_updates_do_not_archive_or_advance_detector(self):
        await self.seed([archived_candle(0, 10, closed=True),
                         archived_candle(300_000, 10, closed=True)])
        await self.collector.receive(kline(600_000, 1000, closed=False))
        self.assertEqual(self.collector.spool.pending(), [])
        await self.collector.receive(kline(600_000, 40))
        event = next(event for event in self.pending_events()
                     if event["kind"] == "orderflow_surge")
        self.assertEqual(event["row"]["observation"]["baseline_quote_volume"], 10)

    async def test_one_minute_candle_is_persisted_without_advancing_five_minute_detector(self):
        config = replace(self.config, kline_intervals=("1m", "5m"))
        collector = MarketCollector(config, self.store, VolumeSurgeDetector(window_size=2))
        await collector.receive(kline(600_000, 1000, interval="1m"))
        events = self.pending_events(collector)
        self.assertEqual([event["kind"] for event in events], ["kline"])
        self.assertEqual(events[0]["timeframe"], "1m")
        self.assertEqual(collector.status["surge_observed"], 0)
        await self.flush_all(collector)
        self.assertEqual(self.store.candle_writes[0][3], "1m")
        self.assertEqual(self.store.candle_writes[1][3], "1m")

    async def test_writer_history_can_warm_restarted_detector_from_persisted_extras(self):
        await self.collector.receive(kline(0, 10))
        await self.collector.receive(kline(300_000, 20))
        self.collector.stop.set()
        await self.collector.writer()
        self.assertEqual(len(self.store.candles), 4)
        self.assertEqual(self.store.surges, {})

        restarted = MarketCollector(self.config, self.store,
                                    VolumeSurgeDetector(window_size=2))
        await restarted.seed_surge_detector()
        self.assertEqual(self.store.candle_reads[-1],
                         ("binance", "futures", PAIR, "5m", "futures", 3))
        await restarted.receive(kline(600_000, 30))
        event = next(event for event in self.pending_events(restarted)
                     if event["kind"] == "orderflow_surge")
        self.assertEqual(event["row"]["observation"]["baseline_quote_volume"], 15)

    async def test_seed_ignores_explicit_open_rows_and_restores_replay_guard(self):
        await self.seed([archived_candle(600_000, 1000, closed=False),
                         archived_candle(300_000, 20, closed=True),
                         archived_candle(0, 10, closed=True)])
        await self.collector.receive(kline(300_000, 1000))
        await self.collector.receive(kline(150_000, 1000))
        await self.collector.receive(kline(600_000, 30))
        observations = [event["row"] for event in self.pending_events()
                        if event["kind"] == "orderflow_surge"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["observation"]["timestamp"], 600_000)
        self.assertEqual(observations[0]["observation"]["baseline_quote_volume"], 15)

    async def test_seed_accepts_elapsed_live_candles_and_excludes_current_candle(self):
        await self.seed([archived_candle(600_000, 1000),
                         archived_candle(0, 10),
                         archived_candle(300_000, 20, closed=True)], now_ms=650_000)
        await self.collector.receive(kline(600_000, 30))
        observation = next(event["row"]["observation"] for event in self.pending_events()
                           if event["kind"] == "orderflow_surge")
        self.assertEqual(observation["baseline_quote_volume"], 15)

    async def test_seed_uses_live_futures_candles_and_ignores_research_archive(self):
        self.store.history[("futures", PAIR, "orderflow")] = [
            archived_candle(0, 10, closed=True),
            archived_candle(300_000, 20, closed=True),
        ]
        self.store.history[("futures", PAIR, "futures")] = [
            archived_candle(300_000, 30, closed=True),
            archived_candle(600_000, 20, closed=True),
        ]
        await self.collector.seed_surge_detector()

        self.assertEqual(
            self.collector.status["surge_warmup"][PAIR]["source"],
            "binance_websocket_closed_kline",
        )
        self.assertEqual(self.collector.status["surge_warmup"][PAIR]["valid_bars"], 2)
        await self.collector.receive(kline(900_000, 60))
        observation = next(event["row"]["observation"] for event in self.pending_events()
                           if event["kind"] == "orderflow_surge")
        self.assertEqual(observation["baseline_quote_volume"], 25)

    async def test_seed_ignores_rows_without_live_event_marker(self):
        self.store.history[("futures", PAIR, "futures")] = [
            archived_candle(0, 10, closed=True, live=False),
            archived_candle(300_000, 20, closed=True, live=False),
        ]
        await self.collector.seed_surge_detector()

        warmup = self.collector.status["surge_warmup"][PAIR]
        self.assertIsNone(warmup["source"])
        self.assertEqual(warmup["valid_bars"], 0)
        self.assertFalse(warmup["ready"])
        await self.collector.receive(kline(600_000, 30))
        self.assertFalse(any(event["kind"] == "price_candidate"
                             for event in self.pending_events()))

    async def test_stale_archive_does_not_bridge_gap_to_live_baseline(self):
        self.store.history[("futures", PAIR, "orderflow")] = [
            archived_candle(0, 10, closed=True),
            archived_candle(300_000, 10, closed=True),
        ]
        self.store.history[("futures", PAIR, "futures")] = [
            archived_candle(900_000, 30, closed=True),
        ]
        await self.collector.seed_surge_detector()

        self.assertEqual(
            self.collector.status["surge_warmup"][PAIR]["source"],
            "binance_websocket_closed_kline",
        )
        self.assertEqual(self.collector.status["surge_warmup"][PAIR]["valid_bars"], 1)
        self.assertFalse(self.collector.status["surge_warmup"][PAIR]["ready"])
        await self.collector.receive(kline(1_200_000, 100))
        self.assertFalse(any(event["kind"] == "orderflow_surge"
                             for event in self.pending_events()))

    async def test_seed_keeps_newest_closed_history_in_chronological_order(self):
        await self.seed([archived_candle(300_000, 20, closed=True),
                         archived_candle(0, 1000, closed=True),
                         archived_candle(600_000, 10, closed=True)])
        await self.collector.receive(kline(900_000, 30))
        observation = next(event["row"]["observation"] for event in self.pending_events()
                           if event["kind"] == "orderflow_surge")
        self.assertEqual(observation["baseline_quote_volume"], 15)

    async def test_seed_restores_event_ids_through_market_aware_canonical_reader(self):
        for market, pair in (("futures", PAIR), ("spot", "BTC/USDT")):
            with self.subTest(market=market):
                store = SurgeStore()
                config = replace(self.config, market=market, pairs=(pair,),
                                 spool_dir=Path(self.temporary.name) / market)
                detector = VolumeSurgeDetector(window_size=2)
                detector.seed(pair, [10, 20], market=market, timestamps=[0, 300_000])
                persisted = detector.process(pair, "5m", {
                    "timestamp": 600_000, "quote_volume": 40, "closed": True,
                }, market=market, source_timestamp=900_000)
                store.surges[persisted["event_id"]] = persisted
                store.history[(market, pair)] = [archived_candle(0, 10, closed=True),
                                                archived_candle(300_000, 20, closed=True)]
                collector = MarketCollector(config, store, VolumeSurgeDetector(window_size=2))
                await collector.seed_surge_detector()
                await collector.receive(kline())
                expected_kinds = ["kline", "price_candidate"] if market == "futures" else ["kline"]
                self.assertEqual([event["kind"] for event in self.pending_events(collector)],
                                 expected_kinds)
                self.assertTrue(any(read["market"] == market and read["pair"] == pair
                                    and read["timeframe"] == "5m"
                                    for read in store.surge_reads))
                await self.flush_all(collector)
                self.assertEqual(store.surge_attempts, [])

    async def test_spot_closed_candle_never_emits_price_candidate(self):
        config = replace(self.config, market="spot", pairs=("BTC/USDT",),
                         spool_dir=Path(self.temporary.name) / "spot")
        store = SurgeStore()
        store.history[("spot", "BTC/USDT")] = [
            archived_candle(0, 10, closed=True), archived_candle(300_000, 10, closed=True),
        ]
        collector = MarketCollector(config, store, VolumeSurgeDetector(window_size=2))
        await collector.seed_surge_detector()
        await collector.receive(kline())
        events = self.pending_events(collector)
        self.assertEqual([event["kind"] for event in events], ["kline", "orderflow_surge"])
        self.assertEqual(collector.status["price_candidate_observed"], 0)

    def test_loader_rejects_spot_price_candidate(self):
        event = {
            "kind": "price_candidate", "market": "spot", "pair": "BTC/USDT",
            "timeframe": "5m", "row": {"event_id": "candidate", "observation": {},
            "method_version": "v1", "data_fingerprint": "fingerprint"},
        }
        with tempfile.TemporaryDirectory() as directory:
            spool = FileSpool(Path(directory))
            path = spool.persist([event])
            with self.assertRaisesRegex(ValueError, "只接受 futures 5m"):
                spool.load(path)
            self.assertTrue(path.exists())

    async def test_partial_commit_survives_restart_and_does_not_stop_market_ingestion(self):
        await self.seed([archived_candle(0, 10, closed=True),
                         archived_candle(300_000, 10, closed=True)])
        await self.collector.receive(kline())
        path = self.collector.spool.pending()[0]
        original = self.collector.spool.load(path)
        self.store.fail_surge = True
        self.assertFalse(await self.collector.flush_spool())
        self.assertTrue(path.exists())
        self.assertEqual(len(self.store.candles), 2)
        self.assertEqual(self.store.surges, {})
        self.assertEqual(self.collector.status["database_failures"], 1)
        self.assertIn("突增观测", self.collector.status["database_error"])
        self.assertFalse(self.collector.stop.is_set())

        await self.collector.receive(trade())
        await self.collector.receive(book())
        self.assertEqual(self.collector.queue.qsize(), 2)
        self.collector.stop.set()
        await self.collector.writer()
        self.assertEqual(self.collector.queue.qsize(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.collector.spool.load(path), original)
        self.assertEqual(self.collector.status["dropped"], 0)

        self.store.fail_surge = False
        restarted = MarketCollector(self.config, self.store,
                                    VolumeSurgeDetector(window_size=2))
        await self.flush_all(restarted)
        observation = next(event["row"] for event in original
                           if event["kind"] == "orderflow_surge")
        self.assertEqual(self.store.surges, {observation["event_id"]: observation})
        self.assertEqual(len(self.store.candles), 2)
        self.assertEqual((len(self.store.ticks), len(self.store.books)), (1, 1))
        self.assertEqual(restarted.status["dropped"], 0)

    async def test_closed_fact_disk_failure_is_visible_and_stops_collection(self):
        await self.seed([archived_candle(0, 10, closed=True),
                         archived_candle(300_000, 10, closed=True)])
        with patch.object(self.collector.spool, "persist", side_effect=OSError("测试磁盘已满")):
            with self.assertRaisesRegex(OSError, "测试磁盘已满"):
                await self.collector.receive(kline())
        self.assertTrue(self.collector.stop.is_set())
        self.assertEqual(self.collector.status["state"], "storage_error")
        self.assertGreater(self.collector.status["dropped"], 0)
        self.assertEqual(self.store.surges, {})


class SurgeSpoolValidationTests(unittest.TestCase):
    def test_malformed_closed_candle_or_nested_observation_is_rejected(self):
        config = StreamConfig(pairs=(PAIR,))
        candle_event = normalize_message(kline(), config, 900_000)
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed(PAIR, [10, 10], timestamps=[0, 300_000])
        observation = detector.process(PAIR, "5m", candle_event["candle"])
        surge_event = {"kind": "orderflow_surge", "market": "futures", "pair": PAIR,
                       "timeframe": "5m", "row": observation}
        with tempfile.TemporaryDirectory() as directory:
            spool = FileSpool(Path(directory))
            for event, required in ((candle_event, "close"), (surge_event, "observation")):
                with self.subTest(kind=event["kind"]):
                    malformed = copy.deepcopy(event)
                    del malformed["row"][required]
                    path = spool.persist([malformed])
                    with self.assertRaises(ValueError):
                        spool.load(path)
                    self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
