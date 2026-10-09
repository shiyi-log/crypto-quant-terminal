#!/usr/bin/env python3
"""mcp_server.py 的端到端测试（纯标准库 unittest）。

所有用例都**在子进程里**通过 stdin/stdout 与真正的服务器对话，
不直接调用服务器内部函数；用 CODEX_BRIDGE_HOME 指向临时目录隔离真实状态。
DeepSeek 使用随机端口的本地 HTTP 夹具，Codex 原生适配器在子进程中 mock，
不会读取真实桌面登录态或发送真实跨助手消息。

运行：
    cd /Users/shiyi/DeepSeek/量化/tools/codex_bridge
    /Users/shiyi/DeepSeek/量化/.venv/bin/python3 -m unittest tests.test_mcp_server -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent
SERVER_PATH = BRIDGE_DIR / "mcp_server.py"
PYTHON = sys.executable

READ_TIMEOUT = 20.0
EXIT_TIMEOUT = 20.0
CODEX_THREAD_ID = "aaaaaaaa-1111-2222-3333-444444444444"
DSH_SESSION_ID = "mcp-test-deepseek-session"


# ---------------------------------------------------------------- 测试夹具


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class LoopbackHarness:
    """Native Harness RPC fixture, isolated from the real desktop service."""

    def __init__(self, home: Path):
        self.requests: list[dict] = []
        self.status = 200
        self.accepted = True
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append({"path": self.path, "body": body})
                response = {
                    "type": "server-response", "rpcId": body["rpcId"],
                    "result": {"ok": True, "value": {"accepted": fixture.accepted}},
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(fixture.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.cookie_db = home / "test-Cookies"
        digest = hashlib.sha256(f"127.0.0.1:{port}".encode()).digest()
        cookie_name = "dsh-auth-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        with sqlite3.connect(self.cookie_db) as connection:
            connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
            connection.execute("INSERT INTO cookies VALUES (?, ?, ?)",
                               ("127.0.0.1", cookie_name, "isolated-test-login"))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class StdioServer:
    """以子进程方式启动 mcp_server.py，并用真管道收发 JSON-RPC。"""

    def __init__(self, home: Path) -> None:
        env = os.environ.copy()
        env["CODEX_BRIDGE_HOME"] = str(home)
        env["PYTHONUNBUFFERED"] = "1"
        # 清掉可能干扰的继承变量
        env.pop("PYTHONPATH", None)
        self.home = Path(home)
        # Mock only the Codex native boundary. DeepSeek still traverses real HTTP,
        # cookie lookup and desktop_link.send. No production test switch is added.
        launcher = f"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(BRIDGE_DIR)!r})
import mcp_server
def native_codex(thread_id, text, binary=None, timeout=20):
    home = Path(os.environ['CODEX_BRIDGE_HOME'])
    if (home / 'native-codex-error').exists():
        from desktop_link import LinkError
        raise LinkError('fixture native owner unavailable')
    path = home / 'native-codex-test.jsonl'
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({{'thread_id': thread_id, 'text': text}}, ensure_ascii=False) + '\\n')
    return {{'accepted': True, 'method': 'mock-native'}}
with patch('desktop_link.queue_codex', new=native_codex):
    raise SystemExit(mcp_server.main([]))
"""
        self.proc = subprocess.Popen(
            [PYTHON, "-c", launcher],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(BRIDGE_DIR),
            env=env,
            bufsize=0,
        )
        self._out_queue: "queue.Queue[bytes]" = queue.Queue()
        self.stdout_lines: list[bytes] = []
        self.stderr_text: list[str] = []
        self._threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    # -- 后台泵 ---------------------------------------------------------
    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            self.stdout_lines.append(raw)
            self._out_queue.put(raw)
        try:
            self.proc.stdout.close()
        except OSError:
            pass

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            self.stderr_text.append(raw.decode("utf-8", errors="replace"))
        try:
            self.proc.stderr.close()
        except OSError:
            pass

    # -- 收发 -----------------------------------------------------------
    def send_raw(self, payload: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def send(self, message: dict) -> None:
        self.send_raw((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def next_line(self, timeout: float = READ_TIMEOUT) -> bytes:
        try:
            return self._out_queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                "等待服务器 stdout 超时。\nstderr:\n" + "".join(self.stderr_text)
            ) from None

    def request(self, method: str, params=None, req_id=1) -> dict:
        message: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.decode(self.next_line())

    @staticmethod
    def decode(raw: bytes) -> dict:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssertionError(f"stdout 出现非协议内容：{raw!r} ({exc})") from None
        if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0":
            raise AssertionError(f"stdout 出现非 JSON-RPC 报文：{raw!r}")
        return parsed

    def call_tool(self, name: str, arguments=None, req_id=100) -> dict:
        response = self.request(
            "tools/call",
            {"name": name, "arguments": arguments if arguments is not None else {}},
            req_id=req_id,
        )
        assert "result" in response, f"tools/call 返回了 JSON-RPC error: {response}"
        result = response["result"]
        assert isinstance(result, dict), result
        assert result.get("content"), result
        return result

    @staticmethod
    def tool_payload(result: dict):
        assert result["content"][0]["type"] == "text"
        return json.loads(result["content"][0]["text"])

    # -- 断言辅助 -------------------------------------------------------
    def assert_stdout_is_protocol_only(self) -> None:
        """stdout 上的每一行都必须是合法 JSON-RPC 2.0 报文（无日志杂音）。"""
        for raw in list(self.stdout_lines):
            self.decode(raw)

    # -- 生命周期 -------------------------------------------------------
    def drain_stderr(self, timeout: float = 3.0) -> None:
        """等 stderr 泵把已有输出读完，避免竞态导致的假阴性。"""
        self._threads[1].join(timeout)

    def close_stdin(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def wait(self, timeout: float = EXIT_TIMEOUT) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("服务器在 EOF 后没有退出（挂死）") from None

    def shutdown(self) -> None:
        try:
            self.close_stdin()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


class MCPTestCase(unittest.TestCase):
    """每个用例：一个临时 home + 一个真子进程服务器。"""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="codex-bridge-test-"))
        self.harness = LoopbackHarness(self.home)
        self.binding = {
            "codex_thread_id": CODEX_THREAD_ID, "dsh_session_id": DSH_SESSION_ID,
            "dsh_url": self.harness.url, "cookie_db": str(self.harness.cookie_db),
        }
        (self.home / "desktop.json").write_text(json.dumps(self.binding), encoding="utf-8")
        self.server = StdioServer(self.home)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.harness.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def seed_legacy(self, direction: str, texts: list[str]) -> list[str]:
        """Populate historical queues independently of the direct send tools."""
        path = self.home / "queue" / f"{direction}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        ids = [f"legacy-{direction}-{index}" for index in range(len(texts))]
        with path.open("w", encoding="utf-8") as handle:
            for message_id, text in zip(ids, texts):
                handle.write(json.dumps({
                    "id": message_id, "ts": "2026-10-09T16:30:00+08:00",
                    "direction": direction, "source": "test", "kind": "message",
                    "text": text, "meta": {},
                }) + "\n")
        return ids

    def assert_no_queued_messages(self):
        self.assertFalse(self.to_dsh_queue.exists())
        self.assertFalse(self.to_chatgpt_queue.exists())
        self.assertEqual(self.cursor_offsets(), {})

    # -- 便捷断言 -------------------------------------------------------
    @property
    def to_dsh_queue(self) -> Path:
        return self.home / "queue" / "to_dsh.jsonl"

    @property
    def to_chatgpt_queue(self) -> Path:
        return self.home / "queue" / "to_chatgpt.jsonl"

    def initialize(self, protocol_version: str | None = "2025-06-18") -> dict:
        params = {} if protocol_version is None else {"protocolVersion": protocol_version}
        return self.server.request("initialize", params, req_id=1)

    def assert_no_traceback(self) -> None:
        self.server.drain_stderr()
        text = "".join(self.server.stderr_text)
        self.assertNotIn("Traceback", text, f"stderr 出现异常：\n{text}")

    def cursor_offsets(self) -> dict:
        path = self.home / "state" / "cursor.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {direction: int(entry.get("offset") or 0) for direction, entry in data.items()}


# ---------------------------------------------------------------- 1. 握手


class TestInitialize(MCPTestCase):
    def test_initialize_returns_frozen_handshake(self) -> None:
        response = self.initialize("2025-06-18")
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(
            result["serverInfo"], {"name": "dsh-codex-bridge", "version": "1.0.0"}
        )
        self.server.assert_stdout_is_protocol_only()

    def test_initialize_defaults_protocol_version(self) -> None:
        result = self.initialize(None)["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")

    def test_initialized_notification_is_silent(self) -> None:
        self.initialize()
        self.server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 通知不该产生响应：下一条读到的必须是 ping 的响应。
        pong = self.server.request("ping", req_id=2)
        self.assertEqual(pong, {"jsonrpc": "2.0", "id": 2, "result": {}})


# ---------------------------------------------------------------- 2. tools/list


class TestToolsList(MCPTestCase):
    EXPECTED = {"send_to_dsh", "send_to_codex", "get_my_messages", "read_dsh_replies", "bridge_status"}

    def test_direct_and_legacy_tools_with_input_schema(self) -> None:
        self.initialize()
        result = self.server.request("tools/list", req_id=2)["result"]
        tools = result["tools"]
        self.assertIsInstance(tools, list)
        self.assertEqual(len(tools), 5)
        names = [tool["name"] for tool in tools]
        self.assertEqual(set(names), self.EXPECTED)
        self.assertEqual(len(names), len(set(names)), "工具名不得重复")
        for tool in tools:
            self.assertTrue(tool.get("description"), tool["name"])
            schema = tool.get("inputSchema")
            self.assertIsInstance(schema, dict, tool["name"])
            self.assertEqual(schema.get("type"), "object", tool["name"])
            self.assertIsInstance(schema.get("properties"), dict, tool["name"])

        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            by_name["send_to_dsh"]["inputSchema"].get("required"), ["text"]
        )
        for name in ("send_to_dsh", "send_to_codex"):
            self.assertIn("direct", by_name[name]["description"].lower())
            self.assertIn("message_id", by_name[name]["inputSchema"]["properties"])
            self.assertIn("reply_to", by_name[name]["inputSchema"]["properties"])
        for name in ("get_my_messages", "read_dsh_replies"):
            self.assertIn("legacy", by_name[name]["description"].lower())
        self.server.assert_stdout_is_protocol_only()


# ---------------------------------------------------------------- 3. 原生直接投递


class TestSendToDsh(MCPTestCase):
    def test_send_to_dsh_uses_native_rpc_and_never_writes_queue(self) -> None:
        self.initialize()
        result = self.server.call_tool(
            "send_to_dsh",
            {"text": "来自 ChatGPT 的端到端测试消息", "title": "e2e"},
            req_id=3,
        )
        self.assertFalse(result.get("isError", False), result)
        payload = self.server.tool_payload(result)
        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["queued"])
        self.assertEqual(payload["transport"], "native-direct")
        new_id = payload["id"]
        self.assertTrue(new_id.startswith("link-"), new_id)

        # 真正的端到端证据：随机端口 HTTP 夹具收到原生 session/prompt。
        self.assertEqual(len(self.harness.requests), 1)
        request = self.harness.requests[0]
        self.assertEqual(request["path"], "/api/session/prompt")
        prompt = request["body"]["payload"]["args"]["request"]
        self.assertEqual(prompt["sessionId"], DSH_SESSION_ID)
        self.assertEqual(prompt["requestId"], new_id)
        self.assertEqual(prompt["mode"], "steer")
        self.assertEqual(prompt["content"][0]["text"],
                         f"[跨助手消息 · 来自 Codex · id={new_id}]\n\ne2e\n\n来自 ChatGPT 的端到端测试消息")
        self.assert_no_queued_messages()

        # stdout 上不能有任何杂音，且请求/响应仍然对齐。
        self.server.assert_stdout_is_protocol_only()
        pong = self.server.request("ping", req_id=4)
        self.assertEqual(pong["id"], 4)
        self.assertEqual(pong["result"], {})
        self.assert_no_queued_messages()
        self.assert_no_traceback()

    def test_send_to_dsh_preserves_text_and_reply_identity(self) -> None:
        self.initialize()
        text = "no title\n$(literal) `literal` 中文 🦉"
        result = self.server.call_tool("send_to_dsh", {
            "text": text, "message_id": "direct-identity", "reply_to": "prior-message",
        }, req_id=3)
        self.assertFalse(result.get("isError", False), result)
        prompt = self.harness.requests[0]["body"]["payload"]["args"]["request"]
        self.assertEqual(prompt["requestId"], "direct-identity")
        self.assertEqual(prompt["content"][0]["text"],
                         "[跨助手消息 · 来自 Codex · id=direct-identity · 回复 prior-message]\n\n" + text)
        self.assert_no_queued_messages()

    def test_stable_message_id_is_not_delivered_twice(self):
        self.initialize()
        arguments = {"text": "only once", "message_id": "mcp-same-id"}
        first = self.server.tool_payload(self.server.call_tool("send_to_dsh", arguments, req_id=3))
        second = self.server.tool_payload(self.server.call_tool("send_to_dsh", arguments, req_id=4))
        self.assertTrue(first["accepted"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.harness.requests), 1)
        self.assert_no_queued_messages()

    def test_missing_binding_is_error_without_queue_fallback(self):
        self.initialize()
        (self.home / "desktop.json").unlink()
        for tool in ("send_to_dsh", "send_to_codex"):
            result = self.server.call_tool(tool, {"text": "missing binding"})
            self.assertTrue(result.get("isError"), result)
            self.assertIn("绑定", result["content"][0]["text"])
        self.assertEqual(self.harness.requests, [])
        self.assertFalse((self.home / "desktop.sqlite").exists())
        self.assertFalse((self.home / "native-codex-test.jsonl").exists())
        self.assert_no_queued_messages()

    def test_native_rejection_is_error_without_queue_fallback(self):
        self.initialize()
        self.harness.accepted = False
        result = self.server.call_tool("send_to_dsh", {"text": "rejected"})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("未确认", result["content"][0]["text"])
        self.assert_no_queued_messages()

    def test_native_http_failure_is_error_without_queue_fallback(self):
        self.initialize()
        self.harness.status = 503
        result = self.server.call_tool("send_to_dsh", {"text": "offline"})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("503", result["content"][0]["text"])
        self.assert_no_queued_messages()

    def test_incomplete_binding_is_error_before_native_send(self):
        self.initialize()
        del self.binding["dsh_session_id"]
        (self.home / "desktop.json").write_text(json.dumps(self.binding), encoding="utf-8")
        result = self.server.call_tool("send_to_dsh", {"text": "incomplete"})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("绑定不完整", result["content"][0]["text"])
        self.assertEqual(self.harness.requests, [])
        self.assert_no_queued_messages()

    def test_direct_send_leaves_existing_legacy_backlog_and_cursors_untouched(self):
        self.initialize()
        self.seed_legacy("to_dsh", ["old inbound"])
        self.seed_legacy("to_chatgpt", ["old outbound"])
        cursor_file = self.home / "state" / "cursor.json"
        cursor_file.parent.mkdir()
        cursor_file.write_text(json.dumps({"to_dsh": {"offset": 3, "consumed_ids": []}}))
        paths = (self.to_dsh_queue, self.to_chatgpt_queue, cursor_file)
        before = [path.read_bytes() for path in paths]
        result = self.server.call_tool("send_to_dsh", {"text": "new direct message"})
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(len(self.harness.requests), 1)


class TestSendToCodex(MCPTestCase):
    def test_send_to_codex_uses_native_adapter_without_queue(self):
        self.initialize()
        result = self.server.call_tool("send_to_codex", {
            "text": "Codex direct reply", "message_id": "codex-direct", "reply_to": "dsh-1",
        })
        self.assertFalse(result.get("isError", False), result)
        payload = self.server.tool_payload(result)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["destination"], "codex")
        self.assertFalse(payload["queued"])
        rows = read_jsonl(self.home / "native-codex-test.jsonl")
        self.assertEqual(rows, [{
            "thread_id": CODEX_THREAD_ID,
            "text": "[跨助手消息 · 来自 DeepSeek · id=codex-direct · 回复 dsh-1]\n\nCodex direct reply",
        }])
        self.assertEqual(self.harness.requests, [])
        self.assert_no_queued_messages()

    def test_native_codex_failure_is_error_without_queue_fallback(self):
        self.initialize()
        (self.home / "native-codex-error").touch()
        result = self.server.call_tool("send_to_codex", {"text": "unavailable native owner"})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("native owner unavailable", result["content"][0]["text"])
        self.assertFalse((self.home / "native-codex-test.jsonl").exists())
        self.assertEqual(self.harness.requests, [])
        self.assert_no_queued_messages()


# ---------------------------------------------------------------- 4. 空白正文


class TestSendToDshValidation(MCPTestCase):
    def test_blank_text_is_tool_error_and_not_queued(self) -> None:
        self.initialize()
        for index, blank in enumerate(["", "   ", "\n\t  \r\n"]):
            result = self.server.call_tool(
                "send_to_dsh", {"text": blank}, req_id=10 + index
            )
            self.assertTrue(result.get("isError"), f"{blank!r} 应当报错：{result}")
            self.assertIn("text", result["content"][0]["text"])

        # 缺参数同样失败。
        missing = self.server.call_tool("send_to_dsh", {}, req_id=20)
        self.assertTrue(missing.get("isError"), missing)

        self.assertEqual(read_jsonl(self.to_dsh_queue), [], "空白消息不得入队")
        self.assertEqual(self.harness.requests, [])
        self.server.assert_stdout_is_protocol_only()

    def test_non_string_optional_arguments_fail_before_native_send(self):
        self.initialize()
        for tool in ("send_to_dsh", "send_to_codex"):
            for key in ("title", "message_id", "reply_to"):
                for invalid in (42, True, {}, " \t"):
                    with self.subTest(tool=tool, key=key, invalid=invalid):
                        result = self.server.call_tool(tool, {"text": "test", key: invalid})
                        self.assertTrue(result.get("isError"), result)
                        self.assertIn(key, result["content"][0]["text"])
        self.assertEqual(self.harness.requests, [])
        self.assertFalse((self.home / "native-codex-test.jsonl").exists())
        self.assert_no_queued_messages()


# ---------------------------------------------------------------- 5. peek 不推进游标


class TestPeekDoesNotAdvanceCursor(MCPTestCase):
    def _snapshot(self) -> tuple:
        return (self.cursor_offsets(), self.to_dsh_queue.read_bytes() if self.to_dsh_queue.exists() else b"")

    def test_get_my_messages_is_read_only(self) -> None:
        self.initialize()
        ids = self.seed_legacy("to_dsh", ["msg-0", "msg-1"])

        before = self._snapshot()
        first = self.server.tool_payload(
            self.server.call_tool("get_my_messages", {"limit": 20}, req_id=33)
        )
        second = self.server.tool_payload(
            self.server.call_tool("get_my_messages", {"limit": 20}, req_id=34)
        )
        self.assertEqual(first["count"], 2)
        self.assertTrue(first["legacy"])
        self.assertEqual([m["id"] for m in first["messages"]], ids)
        self.assertEqual([m["text"] for m in first["messages"]], ["msg-0", "msg-1"])
        self.assertEqual(first["messages"], second["messages"], "peek 必须可重复")
        self.assertEqual(second["count"], 2)
        self.assertFalse(first["cursor_advanced"])
        self.assertFalse(second["cursor_advanced"])

        # limit 只是截断展示，pending 仍反映真实积压。
        limited = self.server.tool_payload(
            self.server.call_tool("get_my_messages", {"limit": 1}, req_id=35)
        )
        self.assertEqual(limited["count"], 1)
        self.assertEqual(limited["pending"], 2)

        # 默认 limit=20：不传参数也要能看到全部。
        defaulted = self.server.tool_payload(
            self.server.call_tool("get_my_messages", req_id=36)
        )
        self.assertEqual(defaulted["count"], 2)

        self.assertEqual(self._snapshot(), before, "peek 不得改动队列或游标")
        self.assertEqual(self.cursor_offsets().get("to_dsh", 0), 0)
        self.assertEqual(self.harness.requests, [])

    def test_read_dsh_replies_is_read_only(self) -> None:
        self.initialize()
        # 由测试直接铺设出站队列内容（出站作者是 DSH/daemon，不是本服务器）。
        self.to_chatgpt_queue.parent.mkdir(parents=True, exist_ok=True)
        fixtures = [
            {
                "id": "m-out1",
                "ts": "2026-10-09T16:30:00+08:00",
                "direction": "to_chatgpt",
                "source": "dsh",
                "text": "DSH 的回复一",
                "kind": "reply",
                "meta": {},
            },
            {
                "id": "m-out2",
                "ts": "2026-10-09T16:31:00+08:00",
                "direction": "to_chatgpt",
                "source": "dsh",
                "text": "DSH 的回复二",
                "kind": "reply",
                "meta": {},
            },
        ]
        with self.to_chatgpt_queue.open("a", encoding="utf-8") as handle:
            for row in fixtures:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        before = self.cursor_offsets()
        first = self.server.tool_payload(
            self.server.call_tool("read_dsh_replies", {"limit": 20}, req_id=40)
        )
        second = self.server.tool_payload(
            self.server.call_tool("read_dsh_replies", {"limit": 20}, req_id=41)
        )
        self.assertEqual(first["direction"], "to_chatgpt")
        self.assertTrue(first["legacy"])
        self.assertEqual([m["id"] for m in first["messages"]], ["m-out1", "m-out2"])
        self.assertEqual([m["text"] for m in first["messages"]], ["DSH 的回复一", "DSH 的回复二"])
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(self.cursor_offsets(), before)
        self.assertEqual(self.cursor_offsets().get("to_chatgpt", 0), 0)
        self.server.assert_stdout_is_protocol_only()


# ---------------------------------------------------------------- 5b. bridge_status


class TestBridgeStatus(MCPTestCase):
    def test_status_reports_native_binding_without_connection(self) -> None:
        self.initialize()
        payload = self.server.tool_payload(
            self.server.call_tool("bridge_status", req_id=51)
        )
        self.assertEqual(payload["home"], str(self.home))
        self.assertEqual(payload["transport"], "native-direct")
        self.assertFalse(payload["queued"])
        self.assertEqual(payload["binding"], {
            "bound": True, "codex_thread_id": CODEX_THREAD_ID,
            "dsh_session_id": DSH_SESSION_ID, "dsh_url": self.harness.url,
        })
        self.assertNotIn("legacy", payload)
        self.assertNotIn("cookie_db", payload["binding"])
        self.assertEqual(self.harness.requests, [])
        self.assertFalse((self.home / "desktop.sqlite").exists())
        self.assert_no_queued_messages()

    def test_status_optionally_reports_legacy_home_thread_and_pending(self) -> None:
        self.initialize()
        self.seed_legacy("to_dsh", ["pending one"])
        payload = self.server.tool_payload(
            self.server.call_tool("bridge_status", {"include_legacy": True}, req_id=51)
        )
        self.assertEqual(payload["home"], str(self.home))
        self.assertIsInstance(payload["legacy"]["thread"], dict)
        self.assertEqual(payload["legacy"]["pending_to_dsh"], 1)
        self.assertEqual(payload["legacy"]["pending_to_chatgpt"], 0)

        # 线程文件存在时应被读取出来。
        thread_file = self.home / "thread.json"
        thread_file.write_text(
            json.dumps({"thread_id": "uuid-abc", "turn_count": 3}), encoding="utf-8"
        )
        payload = self.server.tool_payload(
            self.server.call_tool("bridge_status", {"include_legacy": True}, req_id=52)
        )
        self.assertEqual(payload["legacy"]["thread"]["thread_id"], "uuid-abc")
        self.assertEqual(self.cursor_offsets(), {})

    def test_status_reports_missing_binding(self):
        self.initialize()
        (self.home / "desktop.json").unlink()
        payload = self.server.tool_payload(self.server.call_tool("bridge_status"))
        self.assertFalse(payload["binding"]["bound"])
        self.assertIn("绑定", payload["binding"]["error"])
        self.assertEqual(self.harness.requests, [])
        self.assert_no_queued_messages()

    def test_status_requires_boolean_legacy_flag(self):
        self.initialize()
        result = self.server.call_tool("bridge_status", {"include_legacy": "false"})
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.harness.requests, [])


# ---------------------------------------------------------------- 6. 未知方法


class TestUnknownMethod(MCPTestCase):
    def test_unknown_request_gets_32601(self) -> None:
        self.initialize()
        response = self.server.request("does/not/exist", {"x": 1}, req_id=60)
        self.assertNotIn("result", response)
        self.assertEqual(response["error"]["code"], -32601)
        self.assertIn("does/not/exist", response["error"]["message"])

    def test_unknown_notification_is_silent(self) -> None:
        self.initialize()
        self.server.send({"jsonrpc": "2.0", "method": "does/not/exist"})
        pong = self.server.request("ping", req_id=61)
        self.assertEqual(pong["id"], 61, "未知通知不得产生响应")
        self.assertEqual(pong["result"], {})


# ---------------------------------------------------------------- 7. 坏输入


class TestMalformedInput(MCPTestCase):
    def test_invalid_json_lines_do_not_kill_server(self) -> None:
        self.initialize()
        self.server.send_raw(b"this is not json\n")
        self.server.send_raw(b'{"jsonrpc": "2.0", "id": 70, "method":\n')  # 截断
        self.server.send_raw(b"[1, 2, 3]\n")  # 合法 JSON 但不是对象
        self.server.send_raw(b"42\n")
        self.server.send_raw(b"\xff\xfe\x00bad utf8\n")
        self.server.send_raw(b"\n")  # 空行

        pong = self.server.request("ping", req_id=71)
        self.assertEqual(pong, {"jsonrpc": "2.0", "id": 71, "result": {}})

        # 坏输入之后仍然能正常干活。
        result = self.server.call_tool("send_to_dsh", {"text": "still alive"}, req_id=72)
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual(len(self.harness.requests), 1)
        prompt = self.harness.requests[0]["body"]["payload"]["args"]["request"]
        self.assertTrue(prompt["content"][0]["text"].endswith("\n\nstill alive"))
        self.assert_no_queued_messages()

        self.assertIsNone(self.server.proc.poll(), "服务器不应退出")
        self.server.assert_stdout_is_protocol_only()
        self.assert_no_traceback()


# ---------------------------------------------------------------- 8. EOF


class TestEofExit(MCPTestCase):
    def test_eof_exits_zero(self) -> None:
        self.initialize()
        self.server.assert_stdout_is_protocol_only()
        self.server.close_stdin()
        code = self.server.wait()
        self.assertEqual(code, 0, f"EOF 后退出码应为 0，实际 {code}")
        self.assert_no_traceback()

    def test_immediate_eof_exits_zero(self) -> None:
        self.server.close_stdin()
        code = self.server.wait()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
