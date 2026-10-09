"""K 线异步归档回归测试；内存数据库，绝不连接交易所或生产数据库。"""

from copy import deepcopy
import threading
import time
import unittest

from candle_archive import CandleArchiveWriter


def candle(timestamp, close=100.0, *, event=None, snapshot=None, closed=False):
    extras = {"closed": closed}
    if event is not None:
        extras["live_event_timestamp"] = event
    if snapshot is not None:
        extras["live_snapshot_timestamp"] = snapshot
    return {
        "timestamp": timestamp, "open": 99.0, "high": 102.0,
        "low": 98.0, "close": close, "volume": 10.0, "extras": extras,
    }


class MemoryStore:
    """记录实际事务、模拟数据库等待或失败，不假定归档器的内部结构。"""

    def __init__(self, *, blocked=False):
        self.rows = {}
        self.calls = []
        self.attempt_times = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.fail = False
        self.lock = threading.Lock()

    def upsert_candles(self, exchange, market, pair, timeframe, candle_type,
                       rows, source_key=None):
        values = deepcopy(list(rows))
        key = (exchange, market, pair, timeframe, candle_type)
        with self.lock:
            self.attempt_times.append(time.monotonic())
            self.calls.append((key, values, source_key))
        self.entered.set()
        self.release.wait()
        if self.fail:
            raise ConnectionError("postgresql://private-user:secret@private-host/private-db")
        with self.lock:
            for row in values:
                self.rows[(*key, row["timestamp"])] = row
        return len(values)

    def close(self):
        pass


class CandleArchiveWriterTests(unittest.TestCase):
    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.assertTrue(predicate(), "异步归档没有在限定时间内完成")

    def writer(self, store=None, **kwargs):
        store = store or MemoryStore()
        writer = CandleArchiveWriter(lambda: store, **kwargs)
        self.addCleanup(writer.close, timeout=2.0)
        # 先释放故障和阻塞，再关闭线程；即使断言失败也不遗留阻塞 worker。
        self.addCleanup(setattr, store, "fail", False)
        self.addCleanup(store.release.set)
        return writer, store

    def offer(self, writer, rows, *, market="futures", timeframe="1m",
              pair="BTC/USDT:USDT", exchange="binance", candle_type="futures",
              source_key=None):
        return writer.offer(exchange, market, pair, timeframe, candle_type,
                            rows, source_key=source_key)

    def test_constructor_and_empty_offer_do_not_create_store_or_worker(self):
        calls = []
        writer = CandleArchiveWriter(lambda: calls.append(threading.current_thread()))
        self.addCleanup(writer.close, timeout=2.0)
        self.assertFalse(writer.status["running"])
        self.assertTrue(self.offer(writer, []))
        self.assertEqual(calls, [])
        self.assertFalse(writer.status["running"])
        self.assertTrue(writer.close(timeout=0.1))
        self.assertTrue(writer.status["closed"])
        self.assertFalse(self.offer(writer, [candle(1000)]))
        self.assertFalse(self.offer(writer, []))
        self.assertEqual(calls, [])

    def test_store_factory_runs_only_on_daemon_worker(self):
        store = MemoryStore()
        factory_threads = []

        def factory():
            factory_threads.append(threading.current_thread())
            return store

        writer = CandleArchiveWriter(factory, flush_interval=30.0)
        self.addCleanup(writer.close, timeout=2.0)
        self.assertEqual(factory_threads, [])
        self.assertTrue(self.offer(writer, [candle(1000)], source_key="live:BTC"))
        self.assertTrue(writer.close(timeout=2.0))
        self.assertTrue(factory_threads)
        self.assertTrue(all(thread.daemon for thread in factory_threads))
        self.assertTrue(all(thread is not threading.current_thread() for thread in factory_threads))
        self.assertEqual(store.calls[0][2], "live:BTC")
        self.assertEqual(writer.status["pending_datasets"], 0)
        self.assertEqual(writer.status["pending_rows"], 0)
        self.assertFalse(writer.status["running"])

    def test_offer_is_nonblocking_while_database_transaction_is_blocked(self):
        writer, store = self.writer(MemoryStore(blocked=True), flush_interval=0.01)
        self.assertTrue(self.offer(writer, [candle(1000)]))
        self.assertTrue(store.entered.wait(1.0))
        started = time.monotonic()
        self.assertTrue(self.offer(writer, [candle(2000)]))
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(writer.status["pending_datasets"], 1)
        self.assertEqual(writer.status["pending_rows"], 2)
        store.release.set()
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual({key[-1] for key in store.rows}, {1000, 2000})

    def test_close_flushes_immediately_before_normal_flush_interval(self):
        writer, store = self.writer(flush_interval=30.0)
        self.assertTrue(self.offer(writer, [candle(1000)]))
        started = time.monotonic()
        self.assertTrue(writer.close(timeout=1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(writer.status["pending_rows"], 0)

    def test_close_timeout_is_bounded_and_keeps_blocked_batch_for_drain(self):
        writer, store = self.writer(MemoryStore(blocked=True), flush_interval=0.01)
        self.assertTrue(self.offer(writer, [candle(1000)]))
        self.assertTrue(store.entered.wait(1.0))
        started = time.monotonic()
        self.assertFalse(writer.close(timeout=0.02))
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(writer.status["closed"])
        self.assertEqual(writer.status["pending_rows"], 1)
        self.assertFalse(self.offer(writer, [candle(2000)]))
        store.release.set()
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual({key[-1] for key in store.rows}, {1000})
        self.assertEqual(writer.status["pending_rows"], 0)

    def test_failed_write_keeps_rows_and_retries_with_backoff(self):
        writer, store = self.writer(flush_interval=0.01)
        store.fail = True
        self.assertTrue(self.offer(writer, [candle(1000)]))
        self.wait_until(lambda: writer.status["failures"] >= 1)
        self.assertEqual(writer.status["pending_datasets"], 1)
        self.assertEqual(writer.status["pending_rows"], 1)
        self.assertEqual(writer.status["last_error"], "ConnectionError")
        self.wait_until(lambda: len(store.attempt_times) >= 2)
        self.assertGreaterEqual(store.attempt_times[1] - store.attempt_times[0], 0.08)
        store.fail = False
        self.wait_until(lambda: writer.status["pending_rows"] == 0)
        self.assertEqual(len(store.rows), 1)
        self.assertGreaterEqual(writer.status["failures"], 1)
        self.assertTrue(writer.close(timeout=2.0))

    def test_factory_failure_is_retried_without_losing_pending_rows(self):
        store = MemoryStore()
        attempts = []

        def factory():
            attempts.append(threading.current_thread())
            if len(attempts) == 1:
                raise ConnectionError("private connection details")
            return store

        writer = CandleArchiveWriter(factory, flush_interval=0.01)
        self.addCleanup(writer.close, timeout=2.0)
        self.assertTrue(self.offer(writer, [candle(1000)]))
        self.wait_until(lambda: writer.status["failures"] >= 1)
        self.assertEqual(writer.status["pending_rows"], 1)
        self.assertEqual(writer.status["last_error"], "ConnectionError")
        self.wait_until(lambda: len(store.rows) == 1)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertTrue(writer.close(timeout=2.0))

    def test_live_event_version_wins_over_stale_event_and_unversioned_rest(self):
        writer, store = self.writer(flush_interval=30.0)
        for row in (candle(1000, 100), candle(1000, 120, event=3000),
                    candle(1000, 110, event=2000), candle(1000, 90)):
            self.assertTrue(self.offer(writer, [row]))
        self.assertEqual(writer.status["pending_rows"], 1)
        self.assertTrue(writer.close(timeout=2.0))
        saved = next(iter(store.rows.values()))
        self.assertEqual(saved["close"], 120)
        self.assertEqual(saved["extras"]["live_event_timestamp"], 3000)

    def test_newer_rest_snapshot_can_correct_live_candle_but_stale_snapshot_cannot(self):
        writer, store = self.writer(flush_interval=30.0)
        rows = (candle(1000, 120, event=3000),
                candle(1000, 90, snapshot=2500, closed=True),
                candle(1000, 130, snapshot=4000, closed=True),
                candle(1000, 110, event=3500))
        for row in rows:
            self.assertTrue(self.offer(writer, [row]))
        self.assertTrue(writer.close(timeout=2.0))
        saved = next(iter(store.rows.values()))
        self.assertEqual(saved["close"], 130)
        self.assertEqual(saved["extras"]["live_snapshot_timestamp"], 4000)
        self.assertTrue(saved["extras"]["closed"])

    def test_closed_candle_wins_at_equal_event_version(self):
        writer, store = self.writer(flush_interval=30.0)
        for row in (candle(1000, 100, event=3000),
                    candle(1000, 120, event=3000, closed=True),
                    candle(1000, 110, event=3000)):
            self.assertTrue(self.offer(writer, [row]))
        self.assertTrue(writer.close(timeout=2.0))
        saved = next(iter(store.rows.values()))
        self.assertEqual(saved["close"], 120)
        self.assertTrue(saved["extras"]["closed"])

    def test_successful_inflight_revision_does_not_discard_newer_offer(self):
        writer, store = self.writer(MemoryStore(blocked=True), flush_interval=0.01)
        self.assertTrue(self.offer(writer, [candle(1000, 100, event=2000)]))
        self.assertTrue(store.entered.wait(1.0))
        self.assertTrue(self.offer(writer, [candle(1000, 120, event=3000)]))
        self.assertEqual(writer.status["pending_rows"], 1)
        store.release.set()
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual(next(iter(store.rows.values()))["close"], 120)
        self.assertGreaterEqual(len(store.calls), 2)
        self.assertEqual(writer.status["pending_rows"], 0)

    def test_closed_previous_candle_and_new_current_candle_are_both_preserved(self):
        writer, store = self.writer(flush_interval=30.0)
        self.assertTrue(self.offer(writer, [candle(1000, event=1900, closed=True)]))
        self.assertTrue(self.offer(writer, [candle(2000, event=2100)]))
        self.assertEqual(writer.status["pending_rows"], 2)
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual({key[-1] for key in store.rows}, {1000, 2000})
        old = next(row for row in store.rows.values() if row["timestamp"] == 1000)
        self.assertTrue(old["extras"]["closed"])

    def test_dataset_identity_isolates_exchange_market_pair_timeframe_and_type(self):
        writer, store = self.writer(flush_interval=30.0)
        variants = ({}, {"exchange": "okx"}, {"market": "spot"},
                    {"pair": "ETH/USDT:USDT"}, {"timeframe": "5m"},
                    {"candle_type": "mark"})
        for number, variant in enumerate(variants):
            self.assertTrue(self.offer(writer, [candle(1000, close=100 + number)], **variant))
        self.assertEqual(writer.status["pending_datasets"], len(variants))
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual(len(store.rows), len(variants))
        self.assertEqual({row["close"] for row in store.rows.values()}, set(range(100, 106)))

    def test_dataset_capacity_rejects_new_dataset_but_existing_dataset_can_update(self):
        writer, store = self.writer(flush_interval=30.0, max_datasets=2)
        self.assertTrue(self.offer(writer, [candle(1000)], pair="BTC/USDT:USDT"))
        self.assertTrue(self.offer(writer, [candle(1000)], pair="ETH/USDT:USDT"))
        self.assertFalse(self.offer(writer, [candle(1000)], pair="SOL/USDT:USDT"))
        self.assertTrue(self.offer(writer, [candle(1000, 120, event=3000)]))
        self.assertEqual(writer.status["pending_datasets"], 2)
        self.assertEqual(writer.status["pending_rows"], 2)
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual({key[2] for key in store.rows}, {"BTC/USDT:USDT", "ETH/USDT:USDT"})
        btc = next(row for key, row in store.rows.items() if key[2] == "BTC/USDT:USDT")
        self.assertEqual(btc["close"], 120)

    def test_row_capacity_rejects_entire_offer_without_losing_prior_rows(self):
        writer, store = self.writer(flush_interval=30.0)
        self.assertTrue(self.offer(writer, [candle(timestamp) for timestamp in range(2000)]))
        # 超限批次同时含旧根更新：拒绝须原子，不能只应用其更新部分。
        self.assertFalse(self.offer(writer, [candle(0, 999), candle(2000)]))
        self.assertEqual(writer.status["pending_rows"], 2000)
        self.assertTrue(self.offer(writer, [candle(1, 120, event=3000)]))
        self.assertTrue(writer.close(timeout=2.0))
        self.assertEqual({key[-1] for key in store.rows}, set(range(2000)))
        first = next(row for row in store.rows.values() if row["timestamp"] == 0)
        updated = next(row for row in store.rows.values() if row["timestamp"] == 1)
        self.assertEqual(first["close"], 100)
        self.assertEqual(updated["close"], 120)


if __name__ == "__main__":
    unittest.main()
