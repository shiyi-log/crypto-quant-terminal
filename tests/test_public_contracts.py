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


if __name__ == "__main__":
    unittest.main()
