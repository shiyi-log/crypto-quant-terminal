"""行情采集回归测试：使用固定消息和内存存储，不连接交易所、不启动交易。"""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from market_stream import CollectorAlreadyRunning, CollectorInstanceLock, FileSpool, MarketCollector, StreamConfig, normalize_message, normalize_pair, parse_config


def trade(trade_id=42):
    return {"stream": "btcusdt@aggTrade", "data": {
        "e": "aggTrade", "E": 1700000000020, "s": "BTCUSDT", "a": trade_id,
        "p": "61234.12345678", "q": "0.00100000", "T": 1700000000000,
        "m": True, "f": 100, "l": 103,
    }}


def book():
    return {"stream": "btcusdt@depth20@100ms", "data": {
        "e": "depthUpdate", "s": "BTCUSDT", "E": 1700000000020,
        "T": 1700000000000, "U": 10, "u": 15, "pu": 9,
        "b": [["61000", "2"]], "a": [["61001", "3"]],
    }}


class MemoryStore:
    def __init__(self):
        self.ticks, self.books, self.documents = {}, {}, {}
        self.fail = False
        self.fail_book_once = False
        self.tick_batch_sizes = []

    def upsert_ticks(self, exchange, market, pair, rows):
        if self.fail:
            raise ConnectionError("测试数据库不可用")
        self.tick_batch_sizes.append(len(rows))
        for row in rows:
            self.ticks[(exchange, market, pair, row["trade_id"])] = row

    def insert_orderbooks(self, exchange, market, pair, rows):
        if self.fail or self.fail_book_once:
            self.fail_book_once = False
            raise ConnectionError("测试盘口事务失败")
        for row in rows:
            self.books[(exchange, market, pair, row["timestamp"], row["last_update_id"])] = row

    def put_document(self, key, payload):
        if self.fail:
            raise ConnectionError("测试数据库不可用")
        self.documents[key] = payload


class NormalizationTests(unittest.TestCase):
    def test_pair_formats_and_validation(self):
        self.assertEqual(normalize_pair("btcusdt"), "BTC/USDT:USDT")
        self.assertEqual(normalize_pair("BTC/USDT:USDT"), "BTC/USDT:USDT")
        self.assertEqual(normalize_pair("BTCUSDT", "spot"), "BTC/USDT")
        for value in ("BTCUSDT/../../", "ETHBTC", ""):
            with self.assertRaises(ValueError):
                normalize_pair(value)

    def test_current_official_partitioned_endpoints(self):
        connections = dict(StreamConfig().connections())
        self.assertIn("fstream.binance.com/market/stream?", connections["trades"])
        self.assertIn("@aggTrade", connections["trades"])
        self.assertIn("fstream.binance.com/public/stream?", connections["orderbooks"])
        self.assertIn("@depth20@100ms", connections["orderbooks"])
        spot = StreamConfig(pairs=("BTC/USDT",), market="spot").connections()
        self.assertIn("@trade/", spot[0][1])

    def test_aggregate_trade_preserves_precision_and_raw_ids(self):
        event = normalize_message(trade(), StreamConfig(), 1700000000100)
        self.assertEqual(event["market"], "futures")
        self.assertEqual(event["pair"], "BTC/USDT:USDT")
        self.assertEqual(event["row"]["price"], "61234.12345678")
        self.assertEqual(event["row"]["trade_id"], "42")
        self.assertTrue(event["row"]["extras"]["aggregated"])
        self.assertEqual(event["row"]["extras"]["raw"]["f"], 100)

    def test_spot_raw_trade_uses_t_not_a(self):
        message = trade()
        message["data"].update(e="trade", t=99)
        message["stream"] = "btcusdt@trade"
        config = StreamConfig(pairs=("BTC/USDT",), market="spot")
        event = normalize_message(message, config, 1700000000100)
        self.assertEqual(event["row"]["trade_id"], "99")
        self.assertFalse(event["row"]["extras"]["aggregated"])

    def test_futures_book_is_partial_snapshot(self):
        event = normalize_message(book(), StreamConfig(), 1700000000100)
        self.assertEqual(event["row"]["last_update_id"], "15")
        self.assertEqual(event["row"]["timestamp"], 1700000000000)
        self.assertEqual(event["row"]["extras"]["book_type"], "partial_snapshot")
        self.assertEqual(event["row"]["bids"], [["61000", "2"]])

    def test_spot_book_uses_receive_time_explicitly(self):
        payload = {"lastUpdateId": 333, "bids": [["10", "2"]] * 25, "asks": [["11", "3"]]}
        message = {"stream": "btcusdt@depth20@100ms", "data": payload}
        config = StreamConfig(pairs=("BTC/USDT",), market="spot")
        event = normalize_message(message, config, 1700000000100)
        self.assertEqual(event["row"]["timestamp"], 1700000000100)
        self.assertEqual(event["row"]["extras"]["timestamp_source"], "received")
        self.assertEqual(len(event["row"]["bids"]), 20)

    def test_invalid_market_data_is_rejected(self):
        for price in ("NaN", "Infinity", "-1", "bad"):
            message = trade()
            message["data"]["p"] = price
            with self.assertRaises(ValueError):
                normalize_message(message, StreamConfig(), 100)
        for timestamp in (-1, "1.5", True):
            message = trade()
            message["data"]["T"] = timestamp
            with self.assertRaises(ValueError):
                normalize_message(message, StreamConfig(), 100)
        message = book()
        del message["data"]["u"]
        with self.assertRaises(ValueError):
            normalize_message(message, StreamConfig(), 100)

    def test_cli_and_environment(self):
        with patch.dict("os.environ", {"QUANT_STREAM_ENABLED": "false", "QUANT_STREAM_PAIRS": "SOL/USDT,ETHUSDT"}):
            config = parse_config([])
            self.assertFalse(config.enabled)
            self.assertEqual(config.pairs, ("SOL/USDT:USDT", "ETH/USDT:USDT"))
            self.assertEqual(parse_config(["--pairs", "BTCUSDT", "--market", "spot"]).pairs, ("BTC/USDT",))


class SpoolTests(unittest.TestCase):
    def test_atomic_batch_can_be_reloaded_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = FileSpool(Path(directory))
            events = [normalize_message(trade(), StreamConfig(), 100)]
            path = spool.persist(events)
            restarted = FileSpool(Path(directory))
            self.assertEqual(restarted.load(restarted.pending()[0]), events)
            restarted.acknowledge(path)
            self.assertEqual(restarted.pending(), [])

    def test_orphan_temporary_file_is_never_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = FileSpool(Path(directory))
            path = Path(directory) / "interrupted.tmp"
            path.write_text('{"version":1,"events":', encoding="utf-8")
            self.assertEqual(spool.pending(), [path])
            with self.assertRaises(json.JSONDecodeError):
                spool.load(path)
            self.assertTrue(path.exists())


class InstanceLockTests(unittest.TestCase):
    def test_other_process_is_rejected_then_lock_released(self):
        script = """
from pathlib import Path
import sys
from market_stream import CollectorAlreadyRunning, CollectorInstanceLock
try:
    with CollectorInstanceLock(Path(sys.argv[1])):
        print('独占锁已获取')
except CollectorAlreadyRunning as exc:
    print(str(exc))
    raise SystemExit(2)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".market_stream.lock"
            with CollectorInstanceLock(path):
                blocked = subprocess.run([sys.executable, "-c", script, str(path)],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(blocked.returncode, 2, blocked.stderr)
                self.assertIn("第二实例退出", blocked.stdout)
            allowed = subprocess.run([sys.executable, "-c", script, str(path)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue(path.exists())

    def test_abrupt_process_exit_releases_operating_system_lock(self):
        script = """
from pathlib import Path
import os
import sys
from market_stream import CollectorInstanceLock
with CollectorInstanceLock(Path(sys.argv[1])):
    os._exit(0)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".market_stream.lock"
            finished = subprocess.run([sys.executable, "-c", script, str(path)], timeout=10)
            self.assertEqual(finished.returncode, 0)
            with CollectorInstanceLock(path):
                self.assertTrue(path.exists())


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore()
        self.config = StreamConfig(spool_dir=Path(self.temporary.name) / "spool", flush_interval=0.01,
                                   shutdown_timeout=0.03, status_interval=0.01)
        self.collector = MarketCollector(self.config, self.store)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_database_failure_preserves_batch_and_replays_on_recovery(self):
        events = [normalize_message(trade(), self.config, 100)]
        await self.collector.persist_batch(events)
        self.store.fail = True
        self.assertFalse(await self.collector.flush_spool())
        self.assertEqual(len(self.collector.spool.pending()), 1)
        self.assertEqual(self.collector.status["database_failures"], 1)
        self.store.fail = False
        self.collector._retry_at = 0
        self.assertTrue(await self.collector.flush_spool())
        self.assertEqual(len(self.store.ticks), 1)
        self.assertEqual(self.collector.spool.pending(), [])

    async def test_partial_database_commit_is_idempotent_on_replay(self):
        events = [normalize_message(trade(), self.config, 100), normalize_message(book(), self.config, 100)]
        await self.collector.persist_batch(events)
        self.store.fail_book_once = True
        self.assertFalse(await self.collector.flush_spool())
        self.assertEqual(len(self.store.ticks), 1)
        self.collector._retry_at = 0
        self.assertTrue(await self.collector.flush_spool())
        self.assertEqual((len(self.store.ticks), len(self.store.books)), (1, 1))

    async def test_replay_keeps_original_market_when_cli_changes(self):
        await self.collector.persist_batch([normalize_message(trade(), self.config, 100)])
        restarted = MarketCollector(replace(self.config, market="spot", pairs=("BTC/USDT",)), self.store)
        await restarted.flush_spool()
        self.assertIn(("binance", "futures", "BTC/USDT:USDT", "42"), self.store.ticks)

    async def test_queue_overflow_persists_without_dropping(self):
        collector = MarketCollector(replace(self.config, queue_size=1), self.store)
        await collector.receive(trade(1))
        await collector.receive(trade(2))
        self.assertEqual(collector.queue.qsize(), 1)
        self.assertEqual(len(collector.spool.pending()), 1)
        self.assertEqual(collector.status["overflow_spooled"], 1)
        self.assertEqual(collector.status["dropped"], 0)

    async def test_queue_overflow_disk_error_is_visible_and_stops_collection(self):
        collector = MarketCollector(replace(self.config, queue_size=1), self.store)
        await collector.receive(trade(1))
        with patch.object(collector.spool, "persist", side_effect=OSError("磁盘已满")):
            with self.assertRaises(OSError):
                await collector.receive(trade(2))
        self.assertTrue(collector.stop.is_set())
        self.assertEqual(collector.status["state"], "storage_error")
        self.assertEqual(collector.status["dropped"], 1)

    async def test_invalid_message_drop_is_explicit(self):
        message = trade()
        message["data"]["p"] = "NaN"
        await self.collector.receive(message)
        self.assertEqual(self.collector.status["invalid_messages"], 1)
        self.assertEqual(self.collector.status["dropped"], 1)

    async def test_shutdown_flushes_batched_memory_queue(self):
        for trade_id in range(5):
            await self.collector.receive(trade(trade_id))
        self.collector.stop.set()
        await self.collector.writer()
        self.assertEqual(len(self.store.ticks), 5)
        self.assertEqual(self.store.tick_batch_sizes, [5])
        self.assertEqual(self.collector.spool.pending(), [])

    async def test_shutdown_with_unavailable_database_keeps_recoverable_spool(self):
        self.store.fail = True
        await self.collector.receive(trade())
        self.collector.stop.set()
        await self.collector.writer()
        self.assertEqual(len(self.collector.spool.pending()), 1)
        self.assertEqual(self.collector.queue.qsize(), 0)
        self.assertEqual(self.collector.status["dropped"], 0)

    async def test_status_remains_readable_without_database(self):
        self.store.fail = True
        await self.collector.receive(trade())
        await self.collector.publish_status()
        payload = json.loads((self.config.spool_dir.parent / "market_stream_status.json").read_text())
        self.assertEqual(payload["queue_length"], 1)
        self.assertEqual(payload["trade_type"], "aggregated")
        self.assertEqual(payload["book_type"], "partial_snapshot")

    async def test_corrupt_tmp_is_preserved_and_next_batch_is_replayed(self):
        damaged = self.config.spool_dir / "0000-interrupted.tmp"
        original = b'{"version":1,"events":['
        damaged.write_bytes(original)
        await self.collector.persist_batch([normalize_message(trade(), self.config, 100)])
        self.assertTrue(await self.collector.flush_spool())
        quarantined = self.collector.spool.corrupt_files()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), original)
        self.assertFalse(damaged.exists())
        self.assertTrue(await self.collector.flush_spool())
        self.assertEqual(len(self.store.ticks), 1)
        self.assertFalse(self.collector.stop.is_set())
        snapshot = self.store.documents["market_stream_status.json"]
        self.assertEqual(snapshot["corrupt_batches"], 1)
        self.assertTrue(snapshot["data_gap_possible"])
        self.assertIn(".corrupt", snapshot["storage_warning"])
        sidecar = json.loads((self.config.spool_dir.parent / "market_stream_status.json").read_text())
        self.assertEqual(sidecar["corrupt_files"], [quarantined[0].name])

    async def test_complete_orphan_tmp_can_be_replayed(self):
        await self.collector.persist_batch([normalize_message(trade(), self.config, 100)])
        path = self.collector.spool.pending()[0]
        path.rename(path.with_suffix(".tmp"))
        self.assertTrue(await self.collector.flush_spool())
        self.assertEqual(len(self.store.ticks), 1)
        self.assertEqual(self.collector.spool.pending(), [])
        self.assertEqual(self.collector.spool.corrupt_files(), [])

    async def test_corrupt_warning_survives_collector_restart(self):
        damaged = self.config.spool_dir / "0000-invalid.json"
        damaged.write_text("[]", encoding="utf-8")
        await self.collector.flush_spool()
        restarted = MarketCollector(self.config, self.store)
        await restarted.publish_status()
        snapshot = self.store.documents["market_stream_status.json"]
        self.assertEqual(snapshot["corrupt_batches"], 1)
        self.assertTrue(snapshot["storage_warning"])

    async def test_second_collector_exits_before_network_or_status_work(self):
        with CollectorInstanceLock(self.config.spool_dir.parent / ".market_stream.lock"):
            with self.assertRaises(CollectorAlreadyRunning):
                await self.collector.run()
        self.assertEqual(self.store.documents, {})
        self.assertFalse((self.config.spool_dir.parent / "market_stream_status.json").exists())


if __name__ == "__main__":
    unittest.main()
