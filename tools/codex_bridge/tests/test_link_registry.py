#!/usr/bin/env python3
"""命名 link（多项目并行路由）的离线测试。

全部用例使用隔离的 `CODEX_BRIDGE_HOME`，不连接真实桌面、不发真实消息。
Codex 侧的 MCP 路由在子进程里跑真实 `mcp_server._send_direct`，只 mock 最后一跳
的 `desktop_link.send`，因此校验的是完整链路：MCP 参数 → links.json → 目标会话。

运行：
    cd /Users/shiyi/DeepSeek/量化/tools/codex_bridge
    python3 -m unittest tests.test_link_registry -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parent.parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import desktop_link  # noqa: E402
import link_registry  # noqa: E402

CODEX_A = "11111111-1111-4111-8111-111111111111"
CODEX_B = "22222222-2222-4222-8222-222222222222"
SESSION_A = "session-aaaa1111-2222-3333-4444-555566667777"
SESSION_B = "session-bbbb1111-2222-3333-4444-555566667777"


class IsolatedHomeTestCase(unittest.TestCase):
    """Every case runs against a throwaway bridge home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-link-test-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CODEX_BRIDGE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dsh_url = "http://127.0.0.1:19387"
        self.cookie_db = str(Path(self.tmp.name) / "Cookies")
        # The legacy default binding must keep existing callers working.
        desktop_link.save_config({
            "codex_thread_id": CODEX_A,
            "dsh_session_id": SESSION_A,
            "dsh_url": self.dsh_url,
            "cookie_db": self.cookie_db,
            "codex_binary": "/tmp/codex",
        })

    def write_links(self, links):
        link_registry.save(desktop_link.links_path(), {"version": 1, "links": links})

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = desktop_link.main(argv)
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- 注册表


class RegistryTests(IsolatedHomeTestCase):

    def test_round_trip_and_permissions(self):
        link_registry.save(desktop_link.links_path(), {"links": {"wdhash": {"dsh_session_id": SESSION_B}}})
        data = link_registry.load(desktop_link.links_path())
        self.assertEqual(data["links"]["wdhash"]["dsh_session_id"], SESSION_B)
        self.assertEqual(oct(desktop_link.links_path().stat().st_mode & 0o777), "0o600")

    def test_missing_file_is_an_empty_registry(self):
        data = link_registry.load(Path(self.tmp.name) / "nope.json")
        self.assertEqual(data["links"], {})

    def test_partial_entry_without_codex_thread_is_allowed(self):
        entry = link_registry.validate_entry("wdhash", {"dsh_session_id": SESSION_B})
        self.assertEqual(entry, {"dsh_session_id": SESSION_B})

    def test_bad_names_unknown_fields_and_bad_ids_are_rejected(self):
        for name in ("WDHASH", "with space", "", "x" * 33, "-lead"):
            with self.subTest(name=name), self.assertRaises(link_registry.RegistryError):
                link_registry.validate_name(name)
        with self.assertRaises(link_registry.RegistryError):
            link_registry.validate_entry("wdhash", {"dsh_session_id": SESSION_B, "surprise": "x"})
        with self.assertRaises(link_registry.RegistryError):
            link_registry.validate_entry("wdhash", {"codex_thread_id": "not-a-uuid"})
        with self.assertRaises(link_registry.RegistryError):
            link_registry.validate_entry("wdhash", {"dsh_session_id": "nope"})

    def test_lookup_by_session_and_thread(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        links = link_registry.load(desktop_link.links_path())
        self.assertEqual(link_registry.find_by_dsh_session(links, SESSION_B)[0], "wdhash")
        self.assertEqual(link_registry.find_by_codex_thread(links, CODEX_B)[0], "wdhash")
        self.assertIsNone(link_registry.find_by_dsh_session(links, "session-unknown"))
        self.assertIsNone(link_registry.find_by_dsh_session(links, None))

    def test_describe_marks_the_callers_own_link(self):
        self.write_links({"quant": {"dsh_session_id": SESSION_A}, "wdhash": {"dsh_session_id": SESSION_B}})
        rows = link_registry.describe(link_registry.load(desktop_link.links_path()), SESSION_B)
        by_name = {row["name"]: row for row in rows}
        self.assertTrue(by_name["wdhash"]["current_session"])
        self.assertFalse(by_name["quant"]["current_session"])
        self.assertTrue(by_name["quant"]["dsh_bound"])
        self.assertFalse(by_name["quant"]["codex_bound"])


# ---------------------------------------------------------------- 解析


class ResolveConfigTests(IsolatedHomeTestCase):

    def test_explicit_link_wins_over_the_callers_own_session(self):
        self.write_links({
            "quant": {"dsh_session_id": SESSION_A, "codex_thread_id": CODEX_A},
            "wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B},
        })
        config = desktop_link.resolve_config(link="wdhash", destination="codex", dsh_session=SESSION_A)
        self.assertEqual(config["codex_thread_id"], CODEX_B)
        self.assertEqual(config["dsh_session_id"], SESSION_B)

    def test_sending_to_codex_matches_the_callers_own_session(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        config = desktop_link.resolve_config(destination="codex", dsh_session=SESSION_B)
        self.assertEqual(config["codex_thread_id"], CODEX_B)

    def test_unknown_session_falls_back_to_the_legacy_default(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        config = desktop_link.resolve_config(destination="codex", dsh_session="session-not-listed")
        self.assertEqual(config["codex_thread_id"], CODEX_A)
        self.assertEqual(config["dsh_session_id"], SESSION_A)

    def test_sending_to_deepseek_never_auto_matches_a_session(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        config = desktop_link.resolve_config(destination="deepseek", dsh_session=SESSION_B)
        self.assertEqual(config["dsh_session_id"], SESSION_A)

    def test_unknown_link_is_an_error_listing_known_links(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        with self.assertRaises(desktop_link.LinkError) as caught:
            desktop_link.resolve_config(link="quant", destination="codex")
        self.assertIn("wdhash", str(caught.exception))

    def test_partial_link_fails_loudly_only_for_the_unbound_direction(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        self.assertEqual(
            desktop_link.resolve_config(link="wdhash", destination="deepseek")["dsh_session_id"], SESSION_B)
        with self.assertRaises(desktop_link.LinkError) as caught:
            desktop_link.resolve_config(link="wdhash", destination="codex")
        self.assertIn("bind --link wdhash --codex-thread", str(caught.exception))

    def test_missing_links_file_keeps_the_legacy_behaviour(self):
        config = desktop_link.resolve_config(destination="codex", dsh_session=SESSION_B)
        self.assertEqual(config["codex_thread_id"], CODEX_A)


# ---------------------------------------------------------------- CLI


class LinkCliTests(IsolatedHomeTestCase):

    def fake_client(self, session_id):
        harness = mock.Mock()
        harness.sessions.return_value = [{"sessionId": session_id, "cwd": "/tmp", "running": True,
                                          "agentAvailable": True, "projections": {"values": {"title": "t"}}}]
        return harness

    def test_bind_link_writes_links_json_and_leaves_the_default_untouched(self):
        before = desktop_link.load_config()
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_B)):
            code, out, _ = self.run_cli([
                "bind", "--link", "wdhash", "--dsh-session", SESSION_B, "--note", "wdhash 项目",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["link"], "wdhash")
        self.assertIsNone(payload["codex_thread_id"])
        self.assertEqual(json.loads(desktop_link.links_path().read_text())["links"]["wdhash"]["note"], "wdhash 项目")
        self.assertEqual(desktop_link.load_config(), before)

    def test_bind_link_merges_a_later_codex_thread(self):
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_B)):
            self.run_cli(["bind", "--link", "wdhash", "--dsh-session", SESSION_B])
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_B)), \
                mock.patch.object(desktop_link, "inspect_codex", return_value={"ready": True}):
            code, out, _ = self.run_cli(["bind", "--link", "wdhash", "--codex-thread", CODEX_B])
        self.assertEqual(code, 0)
        entry = json.loads(desktop_link.links_path().read_text())["links"]["wdhash"]
        self.assertEqual(entry["codex_thread_id"], CODEX_B)
        self.assertEqual(entry["dsh_session_id"], SESSION_B)
        self.assertIn("deepseek", json.loads(out))

    def test_bind_without_link_still_rewrites_the_legacy_default(self):
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_B)), \
                mock.patch.object(desktop_link, "inspect_codex", return_value={"ready": True}):
            code, _, _ = self.run_cli([
                "bind", "--codex-thread", CODEX_B, "--dsh-session", SESSION_B,
            ])
        self.assertEqual(code, 0)
        self.assertEqual(desktop_link.load_config()["codex_thread_id"], CODEX_B)
        self.assertFalse(desktop_link.links_path().exists())

    def test_default_bind_still_requires_both_ids(self):
        code, _, err = self.run_cli(["bind", "--dsh-session", SESSION_B])
        self.assertEqual(code, 1)
        self.assertIn("--codex-thread", json.loads(err)["error"])

    def test_send_to_codex_uses_the_named_link(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        with mock.patch.object(desktop_link, "queue_codex", return_value={"accepted": True}) as queue:
            code, out, _ = self.run_cli(["send", "--to", "codex", "--link", "wdhash", "--text", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(queue.call_args[0][0], CODEX_B)
        self.assertTrue(json.loads(out)["accepted"])

    def test_send_to_codex_auto_matches_the_callers_session(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        with mock.patch.dict(os.environ, {"DSH_SESSION_ID": SESSION_B}), \
                mock.patch.object(desktop_link, "queue_codex", return_value={"accepted": True}) as queue:
            code, _, _ = self.run_cli(["send", "--to", "codex", "--text", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(queue.call_args[0][0], CODEX_B)

    def test_send_falls_back_to_the_default_without_any_match(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B, "codex_thread_id": CODEX_B}})
        with mock.patch.dict(os.environ, {"DSH_SESSION_ID": "session-unrelated"}), \
                mock.patch.object(desktop_link, "queue_codex", return_value={"accepted": True}) as queue:
            code, _, _ = self.run_cli(["send", "--to", "codex", "--text", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(queue.call_args[0][0], CODEX_A)

    def test_links_command_flags_the_callers_own_link(self):
        self.write_links({"quant": {"dsh_session_id": SESSION_A}, "wdhash": {"dsh_session_id": SESSION_B}})
        with mock.patch.dict(os.environ, {"DSH_SESSION_ID": SESSION_B}):
            code, out, _ = self.run_cli(["links"])
        rows = {row["name"]: row for row in json.loads(out)}
        self.assertEqual(code, 0)
        self.assertTrue(rows["wdhash"]["current_session"])
        self.assertFalse(rows["quant"]["current_session"])

    def test_status_on_a_partial_link_reports_the_missing_side(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_B)):
            code, out, _ = self.run_cli(["status", "--link", "wdhash"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(payload["ready"])
        self.assertIn("codex_thread_id", payload["codex"]["error"])
        self.assertEqual(payload["link"], "wdhash")

    def test_status_without_link_keeps_the_legacy_output_shape(self):
        with mock.patch.object(desktop_link, "client", return_value=self.fake_client(SESSION_A)), \
                mock.patch.object(desktop_link, "inspect_codex", return_value={"thread_id": CODEX_A, "ready": True}):
            code, out, _ = self.run_cli(["status"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIsNone(payload["link"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["delivery_mode"], "direct")


# ---------------------------------------------------------------- MCP 路由


class McpRoutingTests(IsolatedHomeTestCase):
    """Run the real MCP entry point in a child process; only the last hop is mocked."""

    CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
import desktop_link, mcp_server

captured = {}

def fake_send(config, destination, text, **kwargs):
    captured["config"] = config
    captured["destination"] = destination
    captured["text"] = text
    return {"id": "x", "accepted": True, "destination": destination}

desktop_link.send = fake_send
result = mcp_server._send_direct(json.loads(sys.argv[2]), sys.argv[3])
with open(sys.argv[4], "w") as handle:
    json.dump({"captured": captured, "result": result}, handle, ensure_ascii=False)
"""

    def route(self, arguments, destination):
        script = Path(self.tmp.name) / "child.py"
        script.write_text(self.CHILD)
        out_path = Path(self.tmp.name) / "out.json"
        completed = subprocess.run(
            [sys.executable, str(script), str(BRIDGE_DIR), json.dumps(arguments), destination, str(out_path)],
            env={**os.environ, "CODEX_BRIDGE_HOME": self.tmp.name},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(out_path.read_text())

    def test_send_to_dsh_with_link_targets_that_links_session(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        data = self.route({"text": "hello", "link": "wdhash"}, "deepseek")
        self.assertEqual(data["captured"]["config"]["dsh_session_id"], SESSION_B)
        self.assertEqual(data["captured"]["destination"], "deepseek")
        self.assertIsNot(data["result"].get("isError"), True)

    def test_send_to_dsh_without_link_keeps_the_default_binding(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        data = self.route({"text": "hello"}, "deepseek")
        self.assertEqual(data["captured"]["config"]["dsh_session_id"], SESSION_A)

    def test_send_to_dsh_without_link_rejects_ambiguous_named_links(self):
        self.write_links({
            "quant": {"dsh_session_id": SESSION_A},
            "wdhash": {"dsh_session_id": SESSION_B},
        })
        data = self.route({"text": "hello"}, "deepseek")
        self.assertTrue(data["result"]["isError"])
        self.assertEqual(data["captured"], {})
        self.assertIn("必须显式传", data["result"]["content"][0]["text"])

    def test_send_with_an_unknown_link_is_a_tool_error_not_a_fallback(self):
        self.write_links({"wdhash": {"dsh_session_id": SESSION_B}})
        data = self.route({"text": "hello", "link": "quant"}, "deepseek")
        self.assertTrue(data["result"]["isError"])
        self.assertEqual(data["captured"], {})


if __name__ == "__main__":
    unittest.main()
