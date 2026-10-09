"""Direct desktop delivery over a real isolated Unix socket, without a model."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from direct_codex import CodexRPCError, DirectCodexError, deliver_codex  # noqa: E402

THREAD = "aaaaaaaa-1111-2222-3333-444444444444"
OWNER = "fixture-desktop-owner"


class DesktopFixture:
    def __init__(self, responder=None, fragment=False):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-direct-test-")
        self.path = Path(self.tmp.name) / "ipc.sock"
        self.requests = []
        self.responder = responder
        self.fragment = fragment
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.conn = None
        self.failure = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _receive(conn, count):
        result = bytearray()
        while len(result) < count:
            part = conn.recv(count - len(result))
            if not part:
                return None
            result.extend(part)
        return bytes(result)

    def _serve(self):
        try:
            self.conn, _ = self.listener.accept()
            self.conn.settimeout(3)
            while True:
                head = self._receive(self.conn, 4)
                if head is None:
                    return
                body = self._receive(self.conn, struct.unpack("<I", head)[0])
                if body is None:
                    return
                request = json.loads(body)
                self.requests.append(request)
                method = request["method"]
                response = {"type": "response", "requestId": request["requestId"],
                            "method": method, "resultType": "success",
                            "handledByClientId": OWNER}
                if method == "initialize":
                    response["result"] = {"clientId": "fixture-bridge-client"}
                elif method == "thread-owner-discovery":
                    response["result"] = {"supportsUntrustedAppInput": True}
                elif self.responder:
                    patch = self.responder(request)
                    if patch is None:
                        return
                    response.update(patch)
                elif method == "thread-follower-steer-turn":
                    response["result"] = {"result": {"turnId": "current-turn"}}
                else:
                    raise AssertionError(f"unexpected method {method}")
                if "__drop__" in response:
                    for key in response.pop("__drop__"):
                        response.pop(key, None)
                raw = json.dumps(response, ensure_ascii=False).encode()
                frame = struct.pack("<I", len(raw)) + raw
                if self.fragment:
                    # Real streams can split both the frame header and UTF-8.
                    for byte in frame:
                        self.conn.sendall(bytes([byte]))
                else:
                    self.conn.sendall(frame)
        except (ConnectionError, OSError) as exc:
            if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                self.failure = exc
        except Exception as exc:
            self.failure = exc
        finally:
            if self.conn:
                self.conn.close()

    def close(self):
        if self.conn:
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.thread.join(timeout=4)
        self.tmp.cleanup()
        if self.failure:
            raise self.failure


class DirectDeliveryTests(unittest.TestCase):
    def fixture(self, *args, **kwargs):
        fixture = DesktopFixture(*args, **kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def methods(self, fixture):
        return [request["method"] for request in fixture.requests]

    def test_busy_thread_is_steered_with_literal_text_and_no_model_override(self):
        fixture = self.fixture(fragment=True)
        text = "跨助手消息 🦉\n$(touch /tmp/never) `literal` --model=other"
        result = deliver_codex(THREAD, text, socket_path=fixture.path, timeout=3)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["method"], "turn/steer")
        self.assertEqual(result["turn_id"], "current-turn")
        self.assertEqual(self.methods(fixture), [
            "initialize", "thread-owner-discovery", "thread-follower-steer-turn",
        ])
        message = fixture.requests[-1]
        self.assertEqual(message["targetClientId"], OWNER)
        self.assertEqual(message["sourceClientId"], "fixture-bridge-client")
        self.assertEqual(message["version"], 1)
        self.assertEqual(message["params"]["input"][0]["text"], text)
        self.assertNotIn("model", message["params"])
        self.assertNotIn("hostId", message)
        self.assertNotIn("queued", result)

    def test_explicit_idle_reply_starts_a_turn_inheriting_thread_settings(self):
        def responder(request):
            if request["method"] == "thread-follower-steer-turn":
                return {"resultType": "error", "error":
                        f"Cannot steer conversation {THREAD} because its active turn already ended"}
            return {"result": {"result": {"turn": {"id": "new-turn", "status": "inProgress"}}}}
        fixture = self.fixture(responder)
        result = deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
        self.assertEqual(result["method"], "turn/start")
        self.assertEqual(result["turn_id"], "new-turn")
        self.assertEqual(self.methods(fixture)[-2:], [
            "thread-follower-steer-turn", "thread-follower-start-turn",
        ])
        started = fixture.requests[-1]
        self.assertEqual(started["version"], 2)
        self.assertEqual(started["params"]["turnStart"]["context"], {"inheritThreadSettings": True})
        self.assertNotIn("model", started["params"]["turnStart"]["request"])
        self.assertEqual(started["params"]["turnStart"]["request"]["clientUserMessageId"],
                         fixture.requests[-2]["params"]["clientUserMessageId"])

    def test_idle_error_without_method_or_owner_still_starts_a_turn(self):
        def responder(request):
            if request["method"] == "thread-follower-steer-turn":
                return {"resultType": "error", "error":
                        f"Cannot steer conversation {THREAD} because its active turn already ended",
                        "__drop__": ["method", "handledByClientId"]}
            return {"result": {"result": {"turn": {"id": "new-turn", "status": "inProgress"}}}}

        fixture = self.fixture(responder)
        result = deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
        self.assertEqual(result["method"], "turn/start")
        self.assertEqual(result["turn_id"], "new-turn")
        self.assertEqual(self.methods(fixture)[-1], "thread-follower-start-turn")

    def test_idle_error_wording_variant_starts_a_turn(self):
        def responder(request):
            if request["method"] == "thread-follower-steer-turn":
                return {"resultType": "error", "error":
                        f"Steer rejected: the active turn for conversation {THREAD} has ended."}
            return {"result": {"result": {"turn": {"id": "new-turn", "status": "inProgress"}}}}

        fixture = self.fixture(responder)
        result = deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
        self.assertEqual(result["method"], "turn/start")
        self.assertEqual(result["turn_id"], "new-turn")

    def test_fallback_requires_explicit_end_semantics_for_the_target_thread(self):
        errors = (
            f"Conversation {THREAD} is not being streamed.",
            f"Conversation state for {THREAD} not found",
            f"Cannot steer conversation {THREAD} because its active turn is still running",
            f"Cannot steer conversation {THREAD} because thread bbbbbbbb-1111-2222-3333-444444444444's active turn already ended",
        )
        for message in errors:
            fixture = self.fixture(lambda request, message=message: {"resultType": "error", "error": message})
            with self.subTest(message=message), self.assertRaises(CodexRPCError):
                deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
            self.assertEqual(self.methods(fixture)[-1], "thread-follower-steer-turn")
            self.assertEqual(len(fixture.requests), 3)

    def test_unknown_or_not_streaming_thread_never_starts_or_queues(self):
        for message in ("no-client-found", f"Conversation {THREAD} is not being streamed.",
                        f"Conversation state for {THREAD} not found", "unsupported-version"):
            fixture = self.fixture(lambda request, message=message: {"resultType": "error", "error": message})
            with self.subTest(message=message), self.assertRaises(CodexRPCError):
                deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
            self.assertEqual(len(fixture.requests), 3)

    def test_negation_and_previous_turn_completion_never_authorize_idle_fallback(self):
        messages = (
            f"Cannot steer conversation {THREAD} because its active turn has not ended",
            f"Cannot steer conversation {THREAD} because its active turn is not completed",
            f"Cannot steer conversation {THREAD} because its active turn is still running, but a previous turn ended",
            f"Cannot steer conversation {THREAD} because its active turn will be completed",
            f"Conversation {THREAD} is not being streamed; its active turn already ended",
            f"The active turn for conversation {THREAD} has not ended",
        )
        for message in messages:
            fixture = self.fixture(lambda request, message=message: {
                "resultType": "error", "error": message,
            })
            with self.subTest(message=message), self.assertRaises(CodexRPCError):
                deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
            self.assertEqual(self.methods(fixture), [
                "initialize", "thread-owner-discovery", "thread-follower-steer-turn",
            ])

    def test_lost_confirmation_never_starts_or_retries(self):
        fixture = self.fixture(lambda request: None)
        with self.assertRaises(DirectCodexError):
            deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
        self.assertEqual(self.methods(fixture)[-1], "thread-follower-steer-turn")
        self.assertEqual(len(fixture.requests), 3)

    def test_wrong_owner_or_method_cannot_confirm_delivery(self):
        for patch in ({"handledByClientId": "another-owner"}, {"method": "another-method"}):
            fixture = self.fixture(lambda request, patch=patch: {"result": {"result": {"turnId": "x"}}, **patch})
            with self.subTest(patch=patch), self.assertRaises(DirectCodexError):
                deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)
            self.assertEqual(len(fixture.requests), 3)

    def test_arbitrary_success_without_turn_id_is_unconfirmed(self):
        fixture = self.fixture(lambda request: {"result": {"result": {"accepted": True}}})
        with self.assertRaises(DirectCodexError):
            deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)

    def test_failed_start_is_not_reported_as_accepted(self):
        def responder(request):
            if request["method"] == "thread-follower-steer-turn":
                return {"resultType": "error", "error":
                        f"Cannot steer conversation {THREAD} because its active turn already ended"}
            return {"result": {"result": {"turn": {"id": "new-turn", "status": "failed"}}}}
        fixture = self.fixture(responder)
        with self.assertRaises(DirectCodexError):
            deliver_codex(THREAD, "hello", socket_path=fixture.path, timeout=3)

    def test_invalid_thread_or_body_is_rejected_before_socket_io(self):
        for thread, text in (("bad-id", "hi"), (THREAD, " "), (THREAD, "x" * (48 * 1024 + 1))):
            with self.subTest(thread=thread, text_length=len(text)), self.assertRaises(DirectCodexError):
                deliver_codex(thread, text, socket_path="/tmp/nonexistent-test-socket")

    def test_regular_file_is_not_used_as_a_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-socket"
            path.write_text("not a socket")
            with self.assertRaises(DirectCodexError):
                deliver_codex(THREAD, "hello", socket_path=path)
            self.assertEqual(path.read_text(), "not a socket")


if __name__ == "__main__":
    unittest.main(verbosity=2)
