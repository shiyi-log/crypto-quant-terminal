"""验证真实 HTTP 分发使用隔离数据库，不触碰交易账户和模型进程。"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

import psycopg

from data_store import DataStore


@unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "需要隔离 PostgreSQL 测试库")
class AuthDatabaseIntegrationTests(unittest.TestCase):
    """用真实服务验证鉴权、市场隔离、数据读取以及故障响应。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="quant_http_")
        cls.schema = "http_test_" + uuid.uuid4().hex
        cls.store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema)
        cls.store.initialize()
        spec = importlib.util.spec_from_file_location(
            "quant_auth_database_test", Path(__file__).resolve().parents[1] / "auth_service.py"
        )
        cls.auth = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temp.name}):
            spec.loader.exec_module(cls.auth)
        cls.store.save_users({"users": {"admin": {"salt": "00", "hash": "test"}}})
        cls.store.put_document("research_summary.json", {"generated_at": "隔离测试", "models": []})
        cls.store.put_document("research_progress.json", {"round": 1, "percent": 25})
        cls.store.replace_events("research_trials.jsonl", [{"run_id": "test", "t_period": 2.1}])
        for market, pair, price in [("spot", "BTC/USDT", 100), ("futures", "BTC/USDT:USDT", 200)]:
            cls.store.upsert_candles("binance", market, pair, "1h", market, [{
                "timestamp": 1700000000000, "open": price, "high": price,
                "low": price, "close": price, "volume": 1,
            }])
        cls.store_patch = patch.object(cls.auth, "get_store", return_value=cls.store)
        cls.store_patch.start()
        cls.server = cls.auth.ThreadingHTTPServer(("127.0.0.1", 0), cls.auth.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.token = cls.auth.make_token("admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.store_patch.stop()
        cls.store.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(cls.schema)))
        cls.temp.cleanup()

    def request(self, path, authorized=True, body=None):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_auto_login_remains_compatible_with_database_users(self):
        code, payload = self.request("/auth/auto-login", authorized=False, body={})
        self.assertEqual(code, 200)
        self.assertTrue(payload["auto"])
        self.assertEqual(self.auth.verify_token(payload["access_token"]), "admin")

    def test_database_status_requires_authentication(self):
        self.assertEqual(self.request("/api/locals/database/status", authorized=False)[0], 401)
        code, payload = self.request("/api/locals/database/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload["engine"], "postgresql")
        self.assertNotIn("password", payload["database"]["path"])

    def test_history_queries_keep_spot_and_futures_separate(self):
        for pair, price in [("BTC/USDT", 100), ("BTC/USDT:USDT", 200)]:
            code, payload = self.request("/api/locals/ohlcv?timeframe=1h&pair=" + urllib.parse.quote(pair))
            self.assertEqual(code, 200)
            self.assertEqual(payload["data"][0][4], price)
            self.assertTrue(payload["source"].startswith("postgresql:"))

    def test_pair_case_and_settlement_validation(self):
        code, payload = self.request("/api/locals/ohlcv?pair=btc%2Fusdt%3Ausdt&timeframe=1h")
        self.assertEqual(code, 200)
        self.assertEqual(payload["pair"], "BTC/USDT:USDT")
        for path in ("/api/locals/ohlcv", "/api/locals/klines", "/api/locals/database/ticks"):
            self.assertEqual(self.request(path + "?pair=BTC%2FUSDT%3ABTC")[0], 400)
        self.assertEqual(self.request("/api/locals/database/ticks?market=spot&pair=BTC%2FUSDT%3AUSDT")[0], 400)

    def test_research_and_progress_are_available_without_source_files(self):
        self.assertEqual(self.request("/api/locals/research")[1]["generated_at"], "隔离测试")
        payload = self.request("/api/locals/research_progress")[1]
        self.assertEqual(payload["progress"]["percent"], 25)
        self.assertEqual(payload["trials"]["total"], 1)

    def test_database_failure_is_explicit_and_does_not_expose_credentials(self):
        with patch.object(self.store, "stats", side_effect=psycopg.OperationalError("private-dsn")):
            code, payload = self.request("/api/locals/database/status")
        self.assertEqual(code, 503)
        self.assertNotIn("private-dsn", json.dumps(payload))

    def test_invalid_limits_and_missing_market_do_not_fallback(self):
        self.assertEqual(self.request("/api/locals/database/ticks?limit=nope")[0], 400)
        self.assertEqual(self.request("/api/locals/ohlcv?pair=ETH%2FUSDT&timeframe=1h")[0], 404)


if __name__ == "__main__":
    unittest.main()
