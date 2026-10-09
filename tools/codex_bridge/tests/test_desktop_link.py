"""Offline checks for the authenticated DeepSeek/Codex desktop link.

The HTTP fixture speaks the desktop harness wire protocol on loopback.  No real
desktop account, DeepSeek message, or Codex process is used by these tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import desktop_link  # noqa: E402

CODEX_THREAD_ID = "aaaaaaaa-1111-2222-3333-444444444444"


def auth_cookie_name(port: int) -> str:
    digest = hashlib.sha256(f"127.0.0.1:{port}".encode("utf-8")).digest()
    return "dsh-auth-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class HarnessFixture:
    """A real local HTTP server with injectable responses and recorded requests."""

    def __init__(self):
        self.requests: list[dict] = []
        self.responder = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw.decode("utf-8"))
                request = {"path": self.path, "headers": dict(self.headers), "body": body}
                fixture.requests.append(request)
                if fixture.responder is not None:
                    status, response, headers = fixture.responder(request)
                else:
                    status, response, headers = 200, {
                        "type": "server-response",
                        "rpcId": body.get("rpcId"),
                        "result": {"ok": True, "value": {"accepted": True}},
                    }, {}
                encoded = response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class DesktopHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="desktop-link-test-")
        self.addCleanup(self.tmp.cleanup)
        self.cookie_db = Path(self.tmp.name) / "Cookies"
        with sqlite3.connect(self.cookie_db) as connection:
            connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
        self.server = HarnessFixture()
        self.addCleanup(self.server.close)
        self.cookie_name = auth_cookie_name(self.server.port)
        self.put_cookie("127.0.0.1", self.cookie_name, "first-login-token")
        self.client = desktop_link.HarnessClient(self.server.url, self.cookie_db, timeout=2)

    def put_cookie(self, host, name, value):
        with sqlite3.connect(self.cookie_db) as connection:
            connection.execute(
                "DELETE FROM cookies WHERE host_key = ? AND name = ?", (host, name)
            )
            connection.execute("INSERT INTO cookies VALUES (?, ?, ?)", (host, name, value))

    def test_sessions_uses_native_envelope_and_only_matching_login(self):
        self.put_cookie("127.0.0.1", "dsh-auth-other-port", "wrong-port-token")
        self.put_cookie("example.com", self.cookie_name, "remote-account-token")
        self.put_cookie("127.0.0.1", "unrelated-cookie", "other-cookie-value")
        sessions = [{"sessionId": "deepseek-session-1", "title": "bridge"}]
        self.server.responder = lambda request: (
            200,
            {"type": "server-response", "rpcId": request["body"]["rpcId"],
             "result": {"ok": True, "value": {"items": sessions}}},
            {},
        )

        self.assertEqual(self.client.sessions(), sessions)
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/api/session/list")
        self.assertEqual(request["headers"]["Cookie"], f"{self.cookie_name}=first-login-token")
        self.assertEqual(request["body"]["type"], "client-request")
        self.assertTrue(request["body"]["rpcId"])
        self.assertEqual(request["body"]["method"], "session/list")
        self.assertEqual(request["body"]["payload"], {"args": {"_request": {}}})

    def test_cookie_rotation_is_read_on_each_request(self):
        self.client.call("session/list", {"_request": {}})
        self.put_cookie("127.0.0.1", self.cookie_name, "rotated-login-token")
        self.client.call("session/list", {"_request": {}})
        self.assertEqual(
            [request["headers"]["Cookie"] for request in self.server.requests],
            [f"{self.cookie_name}=first-login-token", f"{self.cookie_name}=rotated-login-token"],
        )

    def test_wrong_port_cookie_is_not_reused(self):
        with sqlite3.connect(self.cookie_db) as connection:
            connection.execute("DELETE FROM cookies")
        self.put_cookie("127.0.0.1", auth_cookie_name(self.server.port + 1), "wrong-port-token")
        with self.assertRaises(desktop_link.LinkError):
            self.client.sessions()
        self.assertEqual(self.server.requests, [])

    def test_prompt_preserves_session_identity_request_identity_and_text(self):
        text = "联动测试\n保留 `字面值` 和 $(shell)；emoji 🦉"
        self.assertEqual(
            self.client.prompt("deepseek-session-1", text, "request-1"),
            {"accepted": True},
        )
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/api/session/prompt")
        self.assertEqual(request["body"]["method"], "session/prompt")
        self.assertEqual(request["body"]["payload"], {
            "args": {"request": {
                "requestId": "request-1",
                "sessionId": "deepseek-session-1",
                "mode": "steer",
                "content": [{"type": "text", "text": text}],
                "clientTimeZone": "Asia/Shanghai",
            }},
        })

    def test_empty_prompt_is_rejected_before_network(self):
        for text in ("", " \t\n"):
            with self.subTest(text=repr(text)), self.assertRaises(desktop_link.LinkError):
                self.client.prompt("deepseek-session-1", text, "request-1")
        self.assertEqual(self.server.requests, [])

    def test_queue_mode_is_rejected_before_network(self):
        with self.assertRaises(desktop_link.LinkError):
            self.client.prompt("deepseek-session-1", "hello", "request-1", mode="queue")
        self.assertEqual(self.server.requests, [])

    def test_wire_failure_is_visible(self):
        self.server.responder = lambda request: (
            200,
            {"type": "server-response", "rpcId": request["body"]["rpcId"],
             "result": {"ok": False, "error": {"message": "session was deleted"}}},
            {},
        )
        with self.assertRaisesRegex(desktop_link.LinkError, "Harness RPC"):
            self.client.sessions()

    def test_http_failure_is_visible(self):
        self.server.responder = lambda request: (503, {"error": "offline"}, {})
        with self.assertRaises(desktop_link.LinkError):
            self.client.sessions()

    def test_invalid_json_is_reported_as_link_error(self):
        self.server.responder = lambda request: (200, b"{invalid JSON", {})
        with self.assertRaises(desktop_link.LinkError):
            self.client.sessions()

    def test_unrelated_rpc_response_is_not_treated_as_confirmation(self):
        self.server.responder = lambda request: (
            200,
            {"type": "server-response", "rpcId": "another-request",
             "result": {"ok": True, "value": {"accepted": True}}},
            {},
        )
        with self.assertRaises(desktop_link.LinkError):
            self.client.prompt("deepseek-session-1", "hello", "request-1")

    def test_rpc_success_without_prompt_acceptance_is_not_delivery(self):
        self.server.responder = lambda request: (
            200,
            {"type": "server-response", "rpcId": request["body"]["rpcId"],
             "result": {"ok": True, "value": {"accepted": False}}},
            {},
        )
        with self.assertRaises(desktop_link.LinkError):
            self.client.prompt("deepseek-session-1", "hello", "request-1")

    def test_non_loopback_urls_are_rejected_before_io(self):
        for url in (
            "http://example.com:1234",
            "http://127.0.0.1.example.com:1234",
            "https://127.0.0.1:1234",
            "http://user:password@127.0.0.1:1234",
            "http://0.0.0.0:1234",
            "file:///tmp/harness",
        ):
            with self.subTest(url=url), self.assertRaises(desktop_link.LinkError):
                desktop_link.HarnessClient(url, self.cookie_db, timeout=2).sessions()
        self.assertEqual(self.server.requests, [])

    def test_redirect_does_not_forward_login_cookie(self):
        receiver = HarnessFixture()
        self.addCleanup(receiver.close)
        self.server.responder = lambda request: (302, {}, {"Location": receiver.url + "/api/session/list"})
        with self.assertRaises(desktop_link.LinkError):
            self.client.sessions()
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(receiver.requests, [])

    def test_environment_proxy_does_not_receive_login_cookie(self):
        proxy = HarnessFixture()
        self.addCleanup(proxy.close)
        proxy_env = {
            "http_proxy": proxy.url,
            "HTTP_PROXY": proxy.url,
            "all_proxy": proxy.url,
            "ALL_PROXY": proxy.url,
            "no_proxy": "",
            "NO_PROXY": "",
        }
        with mock.patch.dict(os.environ, proxy_env):
            self.assertEqual(self.client.call("session/list", {"_request": {}}), {"accepted": True})
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(proxy.requests, [])


class CodexDirectWrapperTests(unittest.TestCase):
    def test_compatibility_entry_uses_native_transport_with_literal_text(self):
        text = "继续这次对话\n$(touch /tmp/never) `literal` 'quoted' --model=x 🦉"
        receipt = {"accepted": True, "method": "turn/steer", "turn_id": "turn-fixture"}
        with mock.patch.object(desktop_link, "deliver_codex", return_value=receipt) as deliver:
            result = desktop_link.queue_codex(CODEX_THREAD_ID, text, binary="/tmp/codex", timeout=7)
        deliver.assert_called_once_with(CODEX_THREAD_ID, text, timeout=7)
        self.assertEqual(result, receipt)
        self.assertNotIn("queued", result)

    def test_blank_message_never_contacts_desktop(self):
        with mock.patch.object(desktop_link, "deliver_codex") as deliver:
            for text in ("", " \t\n"):
                with self.subTest(text=repr(text)), self.assertRaises(desktop_link.LinkError):
                    desktop_link.queue_codex(CODEX_THREAD_ID, text)
        deliver.assert_not_called()

    def test_native_failure_is_link_error_and_never_falls_back(self):
        with mock.patch.object(desktop_link, "deliver_codex",
                               side_effect=desktop_link.DirectCodexError("desktop offline")) as deliver:
            with self.assertRaisesRegex(desktop_link.LinkError, "desktop offline"):
                desktop_link.queue_codex(CODEX_THREAD_ID, "hello", binary="/tmp/codex")
        deliver.assert_called_once()

    def test_invalid_thread_identity_never_contacts_desktop(self):
        with mock.patch.object(desktop_link, "deliver_codex") as deliver:
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.queue_codex("not-a-uuid", "hello")
        deliver.assert_not_called()


class DeliveryJournalTests(unittest.TestCase):
    """Protect against blind retries after a timeout or lost confirmation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="desktop-link-journal-test-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CODEX_BRIDGE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {
            "dsh_session_id": "deepseek-session-1",
            "codex_thread_id": CODEX_THREAD_ID,
            "dsh_url": "http://127.0.0.1:19387",
            "cookie_db": str(Path(self.tmp.name) / "Cookies"),
            "codex_binary": "/tmp/codex",
        }

    def message_row(self, message_id):
        with closing(desktop_link.journal()) as connection:
            return dict(connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def test_confirmed_message_is_not_sent_twice(self):
        harness = mock.Mock()
        harness.prompt.return_value = {"accepted": True}
        with mock.patch.object(desktop_link, "client", return_value=harness):
            first = desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
            second = desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
        self.assertTrue(first["accepted"])
        self.assertTrue(second["duplicate"])
        harness.prompt.assert_called_once()
        self.assertEqual(self.message_row("delivery-1")["status"], "accepted")

    def test_queue_mode_does_not_create_a_delivery_or_contact_either_app(self):
        with mock.patch.object(desktop_link, "client") as harness, \
                mock.patch.object(desktop_link, "deliver_codex") as codex:
            for destination in ("codex", "deepseek"):
                with self.subTest(destination=destination), self.assertRaises(desktop_link.LinkError):
                    desktop_link.send(self.config, destination, "hello", "delivery-queue", mode="queue")
        harness.assert_not_called()
        codex.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / "desktop.sqlite").exists())

    def test_same_id_cannot_change_destination_or_text(self):
        harness = mock.Mock()
        harness.prompt.return_value = {"accepted": True}
        with mock.patch.object(desktop_link, "client", return_value=harness), \
                mock.patch.object(desktop_link, "queue_codex") as queue:
            desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
            for destination, text in (("deepseek", "changed"), ("codex", "hello")):
                with self.subTest(destination=destination), self.assertRaises(desktop_link.LinkError):
                    desktop_link.send(self.config, destination, text, "delivery-1")
        harness.prompt.assert_called_once()
        queue.assert_not_called()

    def test_failed_harness_delivery_is_not_automatically_retried(self):
        harness = mock.Mock()
        harness.prompt.side_effect = desktop_link.LinkError("confirmation was lost")
        with mock.patch.object(desktop_link, "client", return_value=harness):
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
        harness.prompt.assert_called_once()
        row = self.message_row("delivery-1")
        self.assertEqual(row["status"], "unconfirmed")
        self.assertIn("confirmation was lost", row["error"])

    def test_explicit_harness_retry_reuses_request_id_and_payload(self):
        harness = mock.Mock()
        harness.prompt.side_effect = [desktop_link.LinkError("timeout"), {"accepted": True}]
        with mock.patch.object(desktop_link, "client", return_value=harness):
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
            result = desktop_link.send(self.config, "deepseek", "hello", "delivery-1", retry=True)
        self.assertTrue(result["accepted"])
        self.assertEqual(harness.prompt.call_count, 2)
        self.assertEqual(harness.prompt.call_args_list[0], harness.prompt.call_args_list[1])
        self.assertEqual(harness.prompt.call_args.args[2], "delivery-1")
        self.assertEqual(self.message_row("delivery-1")["status"], "accepted")

    def test_codex_lost_confirmation_cannot_be_blindly_retried(self):
        with mock.patch.object(desktop_link, "queue_codex", side_effect=desktop_link.LinkError("timeout")) as queue:
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "codex", "hello", "delivery-1")
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "codex", "hello", "delivery-1", retry=True)
        queue.assert_called_once()
        self.assertEqual(self.message_row("delivery-1")["status"], "unconfirmed")

    def test_late_retry_failure_does_not_erase_concurrent_acceptance(self):
        harness = mock.Mock()
        harness.prompt.side_effect = desktop_link.LinkError("initial confirmation timeout")
        late_request_started = threading.Event()
        acceptance_committed = threading.Event()
        outcomes = {}

        def prompt(*args):
            if threading.current_thread().name == "desktop-late-retry":
                late_request_started.set()
                if not acceptance_committed.wait(timeout=3):
                    raise AssertionError("the successful retry did not finish")
                raise desktop_link.LinkError("late confirmation timeout")
            return {"accepted": True}

        def retry(name):
            try:
                outcomes[name] = desktop_link.send(
                    self.config, "deepseek", "hello", "delivery-1", retry=True
                )
            except Exception as exc:
                outcomes[name] = exc
            finally:
                if name == "accepted":
                    # send has returned only after its accepted journal commit.
                    acceptance_committed.set()

        with mock.patch.object(desktop_link, "client", return_value=harness):
            with self.assertRaises(desktop_link.LinkError):
                desktop_link.send(self.config, "deepseek", "hello", "delivery-1")
            self.assertEqual(self.message_row("delivery-1")["status"], "unconfirmed")
            harness.prompt.side_effect = prompt
            late = threading.Thread(target=retry, args=("late",), name="desktop-late-retry", daemon=True)
            success = threading.Thread(target=retry, args=("accepted",), name="desktop-accepted-retry", daemon=True)
            late.start()
            self.assertTrue(late_request_started.wait(timeout=3), "late retry did not enter the RPC")
            success.start()
            success.join(timeout=3)
            late.join(timeout=3)
            self.assertFalse(success.is_alive(), "successful retry did not finish")
            self.assertFalse(late.is_alive(), "late retry did not finish")

        self.assertIsInstance(outcomes["late"], desktop_link.LinkError)
        self.assertIn("late confirmation timeout", str(outcomes["late"]))
        self.assertIsInstance(outcomes["accepted"], dict)
        self.assertTrue(outcomes["accepted"]["accepted"])
        self.assertEqual(harness.prompt.call_count, 3)
        row = self.message_row("delivery-1")
        self.assertEqual(row["status"], "accepted")
        self.assertIsNone(row["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
