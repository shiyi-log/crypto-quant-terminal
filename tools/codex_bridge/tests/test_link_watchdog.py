"""Watchdog receipt and retry checks; no real processes or desktop messages."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import link_watchdog  # noqa: E402


class WatchdogProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="watchdog-test-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = str(Path(self.tmp.name) / "desktop.sqlite")
        with sqlite3.connect(self.ledger) as db:
            db.execute("CREATE TABLE messages (id TEXT, destination TEXT, reply_to TEXT, "
                       "status TEXT, created_at REAL)")
        self.status = json.dumps({"ready": True, "deepseek": {
            "session_id": "fixture-session", "running": True,
        }, "codex_thread_id": "aaaaaaaa-1111-2222-3333-444444444444"})

    def check(self, probe, uptime=None):
        with mock.patch.object(link_watchdog, "LEDGER", self.ledger), \
                mock.patch.object(link_watchdog, "proc_running", return_value=(True, "123")), \
                mock.patch.object(link_watchdog, "port_open", return_value=True), \
                mock.patch.object(link_watchdog, "run_link", side_effect=[
                    (0, self.status, ""), probe,
                ]) as link, \
                mock.patch.object(link_watchdog, "codex_uptime_s", return_value=uptime), \
                mock.patch.object(link_watchdog.subprocess, "run") as subprocess_run:
            result = link_watchdog.check()
        # The old implementation retries through subprocess.run on every
        # failed/missing receipt. That must fail these assertions even if its
        # emergency sender claims success after a lost original confirmation.
        subprocess_run.assert_not_called()
        self.assertEqual(link.call_count, 2)
        self.assertEqual(link.call_args_list[0].args, ("status",))
        self.assertEqual(link.call_args_list[1].args[:3], ("send", "--to", "codex"))
        self.assertEqual(result["send_probe_via"], "desktop_link")
        return result

    def test_lost_receipt_never_retries_through_emergency_sender(self):
        result = self.check((-1, "", "timeout; acceptance unknown"))
        self.assertFalse(result["send_probe_ok"])
        self.assertFalse(result["healthy"])
        self.assertIn("acceptance unknown", result["send_probe_error"])
        self.assertTrue(any("未重试" in text for text in result["problems"]))

    def test_explicit_rejection_is_visible_and_not_retried(self):
        result = self.check((1, '{"accepted": false}', ""))
        self.assertFalse(result["healthy"])
        self.assertTrue(result["send_probe_error"])

    def test_zero_exit_without_acceptance_is_not_healthy(self):
        for payload in ('{"accepted": false}', '{"accepted": 1}',
                        '{"note": "accepted=true"}', '[{"accepted": true}]',
                        'not json', ''):
            with self.subTest(payload=payload):
                result = self.check((0, payload, ""))
                self.assertFalse(result["send_probe_ok"])
                self.assertFalse(result["healthy"])
                self.assertTrue(result["send_probe_error"])

    def test_structured_acceptance_is_healthy_regardless_of_spacing(self):
        result = self.check((0, '{"accepted":true,"id":"fixture-probe"}', ""))
        self.assertTrue(result["send_probe_ok"])
        self.assertTrue(result["healthy"])
        self.assertEqual(result["problems"], [])

    def test_nonzero_exit_cannot_claim_success_from_stdout(self):
        result = self.check((1, '{"accepted":true}', "native failure"))
        self.assertFalse(result["send_probe_ok"])
        self.assertFalse(result["healthy"])

    def test_failed_probe_during_restart_window_stays_unhealthy(self):
        result = self.check((0, '{"accepted":false}', ""), uptime=42)
        self.assertFalse(result["send_probe_ok"])
        self.assertFalse(result["healthy"])
        self.assertTrue(any("重启后的 IPC 注册窗口" in text for text in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
