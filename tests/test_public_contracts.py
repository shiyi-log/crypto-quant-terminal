"""验证本机免密登录的公开接口；测试账户与密钥均隔离于真实环境。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class AutoLoginContractTests(unittest.TestCase):
    """保留免密登录行为，同时保证外部请求不能获取本机账户令牌。"""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="quant_auth_test_")
        cls.addClassCleanup(cls.temp_dir.cleanup)
        spec = importlib.util.spec_from_file_location(
            "quant_auth_contract_test", ROOT / "auth_service.py"
        )
        assert spec is not None and spec.loader is not None
        cls.auth = importlib.util.module_from_spec(spec)
        # 导入会准备签名密钥，必须使用临时目录，不能触碰真实 auth/。
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temp_dir.name}):
            spec.loader.exec_module(cls.auth)

    def setUp(self):
        # 不启动 HTTP 服务；直接调用实际 Handler 方法并替换存储边界。
        self.handler = object.__new__(self.auth.Handler)
        self.handler.path = "/auth/auto-login"
        self.handler.client_address = ("127.0.0.1", 12345)
        self.handler._read_body = Mock(return_value=b"{}")
        self.handler._send = Mock(side_effect=lambda code, payload: (code, payload))
        self.users = {"users": {self.auth.DEFAULT_USER: {}}}
        self.load_users = self.enterContext(
            patch.object(self.auth, "load_users", return_value=self.users)
        )
        self.make_token = self.enterContext(
            patch.object(self.auth, "make_token", return_value="test-only-token")
        )

    def test_post_route_returns_compatible_token_response(self):
        # 经真实 POST 分发链验证入口仍然可达，而不仅验证内部辅助方法。
        self.handler.do_POST()
        self.handler._send.assert_called_once_with(
            200,
            {
                "access_token": "test-only-token",
                "auto": True,
                "username": self.auth.DEFAULT_USER,
            },
        )
        self.make_token.assert_called_once_with(self.auth.DEFAULT_USER)

    def test_ipv6_loopback_can_use_auto_login(self):
        self.handler.client_address = ("::1", 12345)
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 200)

    def test_query_string_does_not_break_existing_route(self):
        self.handler.path = "/auth/auto-login?source=login-page"
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 200)

    def test_remote_client_is_denied_before_reading_users(self):
        self.handler.client_address = ("198.51.100.23", 12345)
        code, payload = self.handler._auth_post()
        self.assertEqual(code, 403)
        self.assertNotIn("access_token", payload)
        self.load_users.assert_not_called()
        self.make_token.assert_not_called()

    def test_forwarded_loopback_header_does_not_allow_remote_client(self):
        self.handler.client_address = ("198.51.100.23", 12345)
        self.handler.headers = {"X-Forwarded-For": "127.0.0.1"}
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 403)
        self.make_token.assert_not_called()

    def test_missing_default_user_has_no_token(self):
        self.users["users"].clear()
        code, payload = self.handler._auth_post()
        self.assertEqual(code, 404)
        self.assertNotIn("access_token", payload)
        self.make_token.assert_not_called()

    def test_market_stream_discovery_requires_login_and_honors_external_url(self):
        self.handler.path = "/api/locals/market-stream-info"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value=None):
            self.assertEqual(self.handler.do_GET()[0], 401)
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.dict(os.environ, {"QUANT_LIVE_WS_URL": "wss://example.test/market"}):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"url": "wss://example.test/market"})
        self.load_users.assert_not_called()

    def test_research_ledger_requires_login(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value=None)
        with patch.object(self.auth, "verify_token", return_value=None):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 401)
        self.assertEqual(payload["detail"], "未登录或登录已过期")

    def test_research_ledger_reads_fixed_source_projection(self):
        self.handler.path = "/api/locals/research-ledger?version_id=demo"
        self.handler._bearer = Mock(return_value="test-only-token")
        expected = {"summary": {"version_count": 1}, "versions": []}
        store = Mock()
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", return_value=store), \
             patch("research_ledger.ledger_payload", return_value=expected) as payload_fn:
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload, expected)
        payload_fn.assert_called_once_with(
            store, version_id="demo", source_id="bot/tradesv3.dryrun.sqlite",
            registry_path=self.auth.os.path.join(self.auth.ROOT, "bot", "user_data", "model_versions.json"),
        )
        store.get_document.assert_not_called()
        store.read_events.assert_not_called()

    def test_research_ledger_returns_service_unavailable_on_database_error(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", side_effect=self.auth.psycopg.OperationalError("offline")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload["detail"], "研究证据账暂时不可用，请检查同步源")

    def test_research_ledger_returns_service_unavailable_on_missing_database_config(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", side_effect=RuntimeError("private database config missing")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload, {"detail": "研究证据账暂时不可用，请检查同步源"})

    def test_research_ledger_returns_service_unavailable_on_projection_read_failure(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", return_value=Mock()), \
             patch("research_ledger.ledger_payload", side_effect=self.auth.psycopg.OperationalError("mirror offline")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload, {"detail": "研究证据账暂时不可用，请检查同步源"})

    def test_kline_response_does_not_wait_for_database_archive(self):
        self.handler.path = "/api/locals/klines?pair=BTC%2FUSDT%3AUSDT&timeframe=1h&limit=1&fresh=1"
        self.handler._bearer = Mock(return_value="test-only-token")
        import json
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps([[1700000000000, "100", "103", "99", "102", "5"]]).encode()
        cached = {("futures", "BTCUSDT", "1h", 1): (self.auth.time.time(), {"data": [["old"]]})}
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "_kline_cache", cached), \
             patch.object(self.auth.urllib.request, "urlopen", return_value=response), \
             patch.object(self.auth, "_kline_archive") as archive, \
             patch.object(self.auth, "get_store", side_effect=AssertionError("HTTP 不能同步访问数据库")):
            archive.offer.return_value = True
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload["data"][0][4], 102)
        self.assertEqual(payload["storage"], "queued")
        archive.offer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
