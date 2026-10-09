"""Compatibility sender shares main transport and journal; never contacts apps."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import desktop_link  # noqa: E402
import direct_codex  # noqa: E402
import dsh_send_fixed  # noqa: E402
from test_direct_codex import DesktopFixture  # noqa: E402

THREAD = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER_THREAD = "bbbbbbbb-1111-2222-3333-444444444444"
RECEIPT = {"accepted": True, "thread_id": THREAD, "turn_id": "fixture-turn",
           "method": "turn/steer", "transport": "desktop-native-ipc"}


class CompatibilitySenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fixed-sender-test-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CODEX_BRIDGE_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"codex_thread_id": THREAD, "dsh_session_id": "fixture-session",
                       "dsh_url": "http://127.0.0.1:19387",
                       "cookie_db": str(Path(self.tmp.name) / "Cookies")}
        desktop_link.save_config(self.config)

    def main(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = dsh_send_fixed.main(list(args))
        return rc, json.loads(output.getvalue())

    def test_python_adapter_delegates_without_changing_target_text_or_timeout(self):
        text = "中文：$(literal) → 内容"
        with mock.patch.object(direct_codex, "deliver_codex", return_value=RECEIPT) as send:
            self.assertEqual(dsh_send_fixed.deliver(text, timeout=7), RECEIPT)
        send.assert_called_once_with(THREAD, text, timeout=7)

    def test_python_adapter_does_not_turn_native_failure_into_acceptance_or_retry(self):
        with mock.patch.object(direct_codex, "deliver_codex", side_effect=
                               direct_codex.DirectCodexError("unconfirmed")) as send:
            with self.assertRaises(direct_codex.DirectCodexError):
                dsh_send_fixed.deliver("hello", THREAD)
        send.assert_called_once()

    def test_python_adapter_requires_this_threads_affirmative_idle_receipt(self):
        for message in (
            f"Cannot steer conversation {OTHER_THREAD} because its active turn already ended",
            f"Cannot steer conversation {THREAD} because its active turn has not ended",
        ):
            fixture = DesktopFixture(lambda request, message=message: {
                "resultType": "error", "error": message,
            })
            self.addCleanup(fixture.close)
            with self.subTest(message=message), \
                    mock.patch.object(direct_codex, "default_socket_path", return_value=fixture.path), \
                    self.assertRaises(direct_codex.CodexRPCError):
                dsh_send_fixed.deliver("hello", THREAD, timeout=3)
            self.assertEqual([request["method"] for request in fixture.requests], [
                "initialize", "thread-owner-discovery", "thread-follower-steer-turn",
            ])

    def test_python_adapter_rejects_a_failed_started_turn(self):
        def responder(request):
            if request["method"] == "thread-follower-steer-turn":
                return {"resultType": "error", "error":
                        f"Cannot steer conversation {THREAD} because its active turn already ended"}
            return {"result": {"result": {"turn": {"id": "fixture-turn", "status": "failed"}}}}
        fixture = DesktopFixture(responder)
        self.addCleanup(fixture.close)
        with mock.patch.object(direct_codex, "default_socket_path", return_value=fixture.path), \
                self.assertRaises(direct_codex.DirectCodexError):
            dsh_send_fixed.deliver("hello", THREAD, timeout=3)
        self.assertEqual(fixture.requests[-1]["method"], "thread-follower-start-turn")

    def test_cli_and_official_entry_share_journal_and_duplicate_guard(self):
        with mock.patch.object(desktop_link, "queue_codex", return_value=RECEIPT) as send:
            rc, first = self.main("--id", "same-id", "--text", "hello")
            rc2, second = self.main("--id", "same-id", "--text", "hello")
            official = desktop_link.send(self.config, "codex", "hello", message_id="same-id")
        self.assertEqual((rc, rc2), (0, 0))
        self.assertTrue(first["accepted"])
        self.assertEqual(first["transport"], "desktop-native-ipc")
        self.assertTrue(second["duplicate"])
        self.assertTrue(official["duplicate"])
        send.assert_called_once()
        with sqlite3.connect(Path(self.tmp.name) / "desktop.sqlite") as db:
            self.assertEqual(db.execute("SELECT id,status FROM messages").fetchall(),
                             [("same-id", "accepted")])
            self.assertNotIn("dsh_fixed_messages", [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")])

    def test_unconfirmed_cli_message_cannot_be_blindly_resent(self):
        with mock.patch.object(desktop_link, "queue_codex", side_effect=
                               desktop_link.LinkError("receipt lost")) as send:
            rc, first = self.main("--id", "lost-id", "--text", "hello")
            rc2, second = self.main("--id", "lost-id", "--text", "hello")
        self.assertEqual((rc, rc2), (1, 1))
        self.assertFalse(first["ok"])
        self.assertIn("已有投递记录", second["error"])
        send.assert_called_once()

    def test_reply_identity_and_thread_override_preserve_official_envelope(self):
        original_binding = desktop_link.config_path().read_bytes()
        with mock.patch.object(desktop_link, "queue_codex", return_value=RECEIPT) as send:
            rc, result = self.main("--id", "reply-id", "--reply-to", "origin-id",
                                   "--thread", OTHER_THREAD, "--text", "答复")
        self.assertEqual(rc, 0)
        self.assertTrue(result["accepted"])
        self.assertEqual(send.call_args.args[0], OTHER_THREAD)
        self.assertIn("id=reply-id", send.call_args.args[1])
        self.assertIn("回复 origin-id", send.call_args.args[1])
        self.assertIn("答复", send.call_args.args[1])
        self.assertEqual(desktop_link.config_path().read_bytes(), original_binding)

    def test_same_id_cannot_be_reused_for_a_different_thread(self):
        with mock.patch.object(desktop_link, "queue_codex", return_value=RECEIPT) as send:
            self.main("--id", "bound-id", "--text", "hello")
            rc, result = self.main("--id", "bound-id", "--thread", OTHER_THREAD,
                                   "--text", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("目标或正文已改变", result["error"])
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
