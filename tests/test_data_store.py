"""统一数据层的隔离集成测试；绝不使用生产 schema。"""

import json
import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from data_store import DataStore, _number, _timestamp
from runtime_config import load_environment


load_environment()


class DataStoreValidationTests(unittest.TestCase):
    def test_invalid_numbers_and_timestamps(self):
        for value in (float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                _number(value, "price")
        for value in (-1, 1.2, True):
            with self.assertRaises(ValueError):
                _timestamp(value)
        self.assertEqual(_number("12.5", "price"), 12.5)
        self.assertEqual(_timestamp(1728000000000), 1728000000000)

    def test_no_hardcoded_database_credentials(self):
        # 构造对象不建立连接，缺少显式 DSN 时给出可操作的配置错误。
        with patch.dict(os.environ, {}, clear=True), patch("data_store.DataStore.initialize", autospec=True):
            store = DataStore(root=Path(tempfile.gettempdir()) / "no-quant-env")
            self.assertEqual(store.dsn, "")


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "需要 QUANT_TEST_DATABASE_URL 才运行 PostgreSQL 隔离集成测试")
class DataStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test_config = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production_dsn = os.environ.get("QUANT_DATABASE_URL")
        production_config = conninfo_to_dict(production_dsn) if production_dsn else {}
        # 即使隔离 schema，也拒绝误连生产数据库。比较位置时不输出连接凭据。
        def location(config):
            return tuple(config.get(name, "") for name in ("host", "port", "dbname"))
        database_name = test_config.get("dbname", "").lower()
        if not ("test" in database_name or database_name == "quant_ci") or (
            production_config and location(test_config) == location(production_config)
        ):
            raise RuntimeError("QUANT_TEST_DATABASE_URL 必须指向独立测试数据库")
        cls.schema = "quant_test_" + uuid.uuid4().hex
        cls.store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema)
        # 环境已配置时连接失败必须成为测试失败，不能转成 skip。
        cls.store.initialize()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.store.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def key(self, prefix):
        return prefix + ":" + uuid.uuid4().hex

    def candle(self, timestamp, close=100.0):
        return {"timestamp": timestamp, "open": 99.0, "high": 102.0,
                "low": 98.0, "close": close, "volume": 10.0, "extras": {"trades": 4}}

    def summary(self, pair):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT rows,first_timestamp,last_timestamp FROM candle_datasets
                WHERE exchange='binance' AND market='futures' AND pair=%s
                AND timeframe='1m' AND candle_type='futures'""", (pair,)).fetchone()
        return row

    def test_candle_summary_is_incremental_idempotent_and_handles_out_of_order(self):
        pair = self.key("summary")
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures",
            [self.candle(2000), self.candle(3000)])
        self.assertEqual(self.summary(pair), {"rows": 2, "first_timestamp": 2000, "last_timestamp": 3000})
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures",
            [self.candle(1000), self.candle(2000, 110), self.candle(4000), self.candle(4000, 120)])
        expected = {"rows": 4, "first_timestamp": 1000, "last_timestamp": 4000}
        self.assertEqual(self.summary(pair), expected)
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures",
            [self.candle(1000), self.candle(2000), self.candle(4000)])
        self.assertEqual(self.summary(pair), expected)
        self.assertEqual(self.store.upsert_candles("binance", "futures", pair, "1m", "futures", []), 0)
        self.assertEqual(self.summary(pair), expected)

    def test_missing_candle_summary_recovers_all_existing_history(self):
        pair = self.key("recover-summary")
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures",
            [self.candle(1000), self.candle(2000), self.candle(3000)])
        with self.store._connection() as conn:
            conn.execute("DELETE FROM candle_datasets WHERE pair=%s", (pair,))
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures", [self.candle(2000)])
        self.assertEqual(self.summary(pair), {"rows": 3, "first_timestamp": 1000, "last_timestamp": 3000})

    def test_live_candle_versions_guard_late_snapshots_and_accept_new_reconciliation(self):
        pair = self.key("versions")
        def row(price, event=None, snapshot=None, closed=False):
            candle = self.candle(1000, price)
            candle["extras"]["closed"] = closed
            if event is not None:
                candle["extras"]["live_event_timestamp"] = event
            if snapshot is not None:
                candle["extras"]["live_snapshot_timestamp"] = snapshot
            return candle
        def write(rows):
            return self.store.upsert_candles("binance", "futures", pair, "1m", "futures", rows)
        def latest_price():
            return self.store.get_candles("binance", "futures", pair, "1m")[0]["close"]
        write([row(120, event=2000), row(110, event=1900), row(100)])
        self.assertEqual(latest_price(), 120)
        self.assertEqual(write([row(10), row(20, snapshot=1950)]), 0)
        self.assertEqual(latest_price(), 120)
        write([row(130, snapshot=2100, closed=True)])
        self.assertEqual(latest_price(), 130)
        self.assertEqual(write([row(125, event=2100)]), 0)
        self.assertEqual(latest_price(), 130)
        self.assertEqual(self.summary(pair), {"rows": 1, "first_timestamp": 1000, "last_timestamp": 1000})

    def test_multitable_snapshot_rolls_back_and_removes_deleted_tables(self):
        source = self.key("snapshot")
        self.store.replace_external_snapshot(source, {"trades": [{"id": 1}], "orders": [{"id": 2}]})
        with self.assertRaises(ValueError):
            self.store.replace_external_snapshot(source, {"trades": [{"id": 3}], "orders": [None]})
        self.assertEqual(self.store.read_external(source, "trades"), [{"id": 1}])
        self.assertEqual(self.store.read_external(source, "orders"), [{"id": 2}])
        self.store.replace_external_snapshot(source, {"trades": [{"id": 4}]})
        self.assertEqual(self.store.read_external(source, "orders"), [])

    def test_z_password_compare_and_swap_preserves_other_users(self):
        self.store.save_users({"users": {"a": {"salt": "s", "hash": "old"}, "b": {"salt": "s", "hash": "b"}}})
        self.assertTrue(self.store.update_user("a", {"salt": "s2", "hash": "new"}, "old"))
        self.assertFalse(self.store.update_user("a", {"salt": "s3", "hash": "stale"}, "old"))
        users = self.store.load_users()["users"]
        self.assertEqual(users["a"]["hash"], "new")
        self.assertEqual(users["b"]["hash"], "b")

    def test_document_paths_are_literal_and_payload_roundtrips(self):
        prefix = self.key("documents_%")
        key = prefix + "/summary.json"
        self.store.put_document(key, {"中文": [1, None, True]}, source_path="/tmp/source", source_mtime_ns=123)
        self.assertEqual(self.store.get_document(key), {"中文": [1, None, True]})
        records = self.store.list_documents(prefix=prefix, suffix=".json")
        self.assertEqual([row["key"] for row in records], [key])
        self.assertEqual(records[0]["source_mtime_ns"], 123)
        self.assertEqual(self.store.get_document(self.key("missing"), []), [])

    def test_market_isolation_upsert_and_batch_duplicates(self):
        pair = self.key("BTC/USDT")
        self.assertEqual(self.store.upsert_candles("binance", "futures", pair, "1m", "futures",
            [self.candle(2000), self.candle(1000), self.candle(1000, 105)]), 2)
        self.store.upsert_candles("binance", "spot", pair, "1m", "spot", [self.candle(1000, 20)])
        self.store.upsert_candles("okx", "futures", pair, "1m", "futures", [self.candle(1000, 30)])
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures", [self.candle(1000, 110)])
        rows = self.store.get_candles("binance", "futures", pair, "1m")
        self.assertEqual([row["timestamp"] for row in rows], [1000, 2000])
        self.assertEqual(rows[0]["close"], 110)
        self.assertEqual(self.store.get_candles("binance", "spot", pair, "1m", "spot")[0]["close"], 20)
        self.assertEqual(self.store.get_candles("okx", "futures", pair, "1m")[0]["close"], 30)
        self.assertEqual(self.store.get_candles("binance", "futures", pair, "1m", limit=1)[0]["timestamp"], 2000)
        self.assertEqual(len(self.store.get_candles("binance", "futures", pair, "1m", start=1001)), 1)

    def test_batch_failure_rolls_back_previous_upserts(self):
        pair = self.key("rollback")
        self.store.upsert_candles("binance", "futures", pair, "1m", "futures", [self.candle(1000)])
        bad = self.candle(2000)
        bad["close"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.upsert_candles("binance", "futures", pair, "1m", "futures", [self.candle(1000, 999), bad])
        rows = self.store.get_candles("binance", "futures", pair, "1m")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 100)
        self.assertEqual(self.summary(pair), {"rows": 1, "first_timestamp": 1000, "last_timestamp": 1000})
        # 异常 COPY 后连接仍可复用。
        self.store.put_document(self.key("connection-alive"), {"ok": True})

    def test_events_replace_preserves_order_and_rolls_back(self):
        key = self.key("events")
        self.assertEqual(self.store.replace_events(key, [{"n": 1}, {"n": 2}, {"n": 3}]), 3)
        self.assertEqual(self.store.read_events(key, limit=2), [{"n": 2}, {"n": 3}])
        with self.assertRaises(TypeError):
            self.store.replace_events(key, [{"n": 9}, {"invalid": object()}])
        self.assertEqual(self.store.read_events(key), [{"n": 1}, {"n": 2}, {"n": 3}])
        self.store.replace_events(key, [{"n": 4}])
        self.assertEqual(self.store.read_events(key), [{"n": 4}])
        self.store.replace_events(key, [])
        self.assertEqual(self.store.read_events(key), [])

    def test_external_snapshots_are_atomic_and_keep_raw_columns(self):
        source = self.key("freqtrade")
        rows = [{"id": 1, "pair": "BTC/USDT", "is_open": True}, {"id": 2, "custom": {"x": 1}}]
        self.store.replace_external(source, "trades", rows)
        self.assertEqual(self.store.read_external(source, "trades"), rows)
        with self.assertRaises(ValueError):
            self.store.replace_external(source, "trades", [{"id": 3}, "invalid"])
        self.assertEqual(self.store.read_external(source, "trades"), rows)
        self.store.replace_external(source, "trades", [{"id": 2, "custom": {"x": 2}}])
        self.assertEqual(self.store.read_external(source, "trades"), [{"id": 2, "custom": {"x": 2}}])

    def test_users_migration_preserves_hash_and_never_resets(self):
        original = {"version": 1, "users": {"alice": {"salt": "abc", "hash": "original-hash",
                    "updated_at": "2026-10-09", "role": "admin"}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertTrue(self.store.migrate_users(path))
            self.assertEqual(self.store.load_users(), original)
            changed = {"users": {"alice": {"salt": "new-salt", "hash": "new-hash", "updated_at": "later"}}}
            self.store.save_users(changed)
            self.assertFalse(self.store.migrate_users(path))
            self.assertEqual(self.store.load_users(), changed)
            with self.assertRaises(ValueError):
                self.store.save_users({"users": {"alice": {"salt": "broken"}}})
            self.assertEqual(self.store.load_users(), changed)

    def test_failed_sync_does_not_advance_success_fingerprint(self):
        key = self.key("source")
        self.store.record_source(key, "/tmp/data", "feather", 10, 100, 5)
        self.store.record_source(key, "/tmp/data", "feather", 20, 200, 8, error="incomplete write")
        state = self.store.source_state(key)
        self.assertEqual((state["mtime_ns"], state["size"], state["row_count"]), (10, 100, 5))
        self.assertEqual(state["last_attempt_mtime_ns"], 20)
        self.assertEqual(state["error"], "incomplete write")
        self.store.record_source(key, "/tmp/data", "feather", 20, 200, 8)
        self.assertEqual(self.store.source_state(key)["row_count"], 8)
        self.assertIsNone(self.store.source_state(key)["error"])

    def test_stream_replay_is_idempotent_and_stats_are_safe(self):
        pair = self.key("stream")
        tick = {"trade_id": "42", "timestamp": 1000, "price": "10.1234", "quantity": "2.5", "buyer_maker": False}
        self.store.upsert_ticks("binance", "futures", pair, [tick, tick])
        self.store.upsert_ticks("binance", "futures", pair, [tick])
        self.store.upsert_ticks("binance", "futures", pair, [{**tick, "timestamp": 2000}])
        self.assertEqual(len(self.store.get_ticks("binance", "futures", pair)), 1)
        self.assertEqual(Decimal(self.store.get_ticks("binance", "futures", pair)[0]["quantity"]), Decimal("2.5"))
        book = {"timestamp": 1000, "last_update_id": 1, "bids": [["10", "2"]], "asks": [["11", "3"]]}
        self.store.insert_orderbooks("binance", "futures", pair, [book, book])
        self.store.insert_orderbooks("binance", "futures", pair, [book])
        self.assertEqual(len(self.store.get_orderbooks("binance", "futures", pair)), 1)
        self.store.set_sync_state({"running": True, "last_success_at": "old"})
        self.store.set_sync_state({"running": False})
        stats = self.store.stats()
        self.assertEqual(stats["engine"], "postgresql")
        self.assertEqual(stats["database"]["journal_mode"], "wal")
        self.assertEqual(stats["sync"]["last_success_at"], "old")
        self.assertFalse(stats["sync"]["running"])
        self.assertNotIn("password", stats["database"]["path"])
        streams = [row for row in stats["streams"] if row["pair"] == pair]
        self.assertEqual({row["kind"]: row["rows"] for row in streams}, {"ticks": 1, "orderbooks": 1})
        tick_summary = next(row for row in streams if row["kind"] == "ticks")
        self.assertEqual((tick_summary["first_timestamp"], tick_summary["last_timestamp"]), (2000, 2000))
        # 状态输出可直接通过 HTTP 编码为 JSON。
        json.dumps(stats)

    def test_series_and_artifacts(self):
        source = self.key("deribit")
        self.store.upsert_series(source, "dvol", "BTC", [{"timestamp": 1000, "value": 42.5}])
        self.store.upsert_series(source, "dvol", "BTC", [{"timestamp": 1000, "value": 43.5}])
        self.assertEqual(self.store.get_series(source, "dvol", "BTC")[0]["value"], 43.5)
        key = self.key("artifact")
        self.store.save_artifact(key, "/unread/model.bin", 10, 123, "model", {"version": 2})
        artifact = self.store.list_artifacts(key)[0]
        self.assertEqual(artifact["size"], 10)
        self.assertEqual(artifact["metadata"], {"version": 2})

    def test_decimal_precision_and_numeric_ids_with_same_timestamp(self):
        pair = self.key("precision")
        self.store.upsert_ticks("binance", "futures", pair, [
            {"trade_id": str(identity), "timestamp": 1000, "price": "0.123456789012345678",
             "quantity": "12345678901234567890.123456789012345678", "buyer_maker": True}
            for identity in (9, 10)
        ])
        rows = self.store.get_ticks("binance", "futures", pair)
        self.assertEqual([row["trade_id"] for row in rows], ["9", "10"])
        self.assertEqual(rows[0]["price"], "0.123456789012345678")
        self.assertEqual(rows[0]["quantity"], "12345678901234567890.123456789012345678")
        self.assertEqual(self.store.get_ticks("binance", "futures", pair, limit=1)[0]["trade_id"], "10")
        self.store.insert_orderbooks("binance", "futures", pair, [
            {"timestamp": 1000, "last_update_id": identity, "bids": [["1", "2"]], "asks": [["2", "1"]]}
            for identity in (9, 10)
        ])
        self.assertEqual([row["last_update_id"] for row in self.store.get_orderbooks("binance", "futures", pair)], ["9", "10"])
        self.assertEqual(self.store.get_orderbooks("binance", "futures", pair, limit=1)[0]["last_update_id"], "10")


if __name__ == "__main__":
    unittest.main()
