"""
回归测试：自动迭代巡检的鉴权 token 必须会过期刷新。

背景（2026-10-09 11:22 那一轮）：
    C3 实盘 vs 预期 / C4 因子漂移 / C5 信号完整性 三项同时报
    HTTPError: HTTP Error 401: Unauthorized。
    原因不是服务端故障，而是 auto_iterate.py 里的 _token() 缓存永不失效，
    而服务端 TOKEN_TTL 只有 12 小时、巡检周期 6 小时 —— 第 3 轮开始
    拿的是 12 小时前签发的过期 token。

不启动真实服务：只替换 urllib.request.urlopen。
"""

import base64
import importlib.util
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def _make_token(exp_offset):
    """按服务端格式造一个 token：base64url(json).签名。"""
    payload = json.dumps({"u": "admin", "exp": int(time.time() + exp_offset)}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".fakesig"


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._data


def _http_401():
    return urllib.error.HTTPError(
        "http://127.0.0.1:8890/api/v1/profit", 401, "Unauthorized", {}, None)


class AutoIterateAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        spec = importlib.util.spec_from_file_location(
            "auto_iterate_under_test", ROOT / "bot" / "auto_iterate.py")
        cls.mod = importlib.util.module_from_spec(spec)
        try:
            # 模块在导入时会 chdir 到 bot/，这里还原，避免影响其它测试
            spec.loader.exec_module(cls.mod)
        finally:
            os.chdir(cwd)
        sys.modules.setdefault("auto_iterate_under_test", cls.mod)

    def setUp(self):
        self.mod._token_cache.update({"v": None, "exp": 0.0})

    def test_reads_exp_from_server_token(self):
        tok = _make_token(3600)
        self.assertGreater(self.mod._token_exp(tok), time.time() + 3500)

    def test_unparsable_token_falls_back_to_conservative_ttl(self):
        self.assertEqual(self.mod._token_exp("not-a-real-token"), 0.0)

    def test_expired_cache_is_refreshed(self):
        self.mod._token_cache.update({"v": "stale", "exp": 1.0})  # 早已过期
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse({"access_token": _make_token(12 * 3600)})

        with patch("urllib.request.urlopen", fake_urlopen):
            tok = self.mod._token()
        self.assertNotEqual(tok, "stale")
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.mod._token_cache["exp"] > 0)

    def test_fresh_cache_is_reused(self):
        tok = _make_token(12 * 3600)
        self.mod._token_cache.update({"v": tok, "exp": time.time() + 12 * 3600})
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("不应重新登录")):
            self.assertEqual(self.mod._token(), tok)

    def test_api_get_retries_once_after_401(self):
        # 真实场景：本地缓存的 token 尚未到期（比如服务端换了签名密钥），
        # 服务端回 401 —— 此时必须强制换新 token 再试一次。
        self.mod._token_cache.update({"v": _make_token(12 * 3600), "exp": 1e12})
        seq = [_http_401(),
               FakeResponse({"access_token": _make_token(12 * 3600)}),
               FakeResponse({"profit_all_percent": 1.23, "closed_trade_count": 4})]

        def fake_urlopen(req, timeout=None):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("urllib.request.urlopen", fake_urlopen):
            data = self.mod._api_get("/api/v1/profit", timeout=10)
        self.assertEqual(data["closed_trade_count"], 4)
        self.assertEqual(seq, [])

    def test_api_get_does_not_retry_other_errors(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.mod._api_get("/api/v1/profit", timeout=10)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
