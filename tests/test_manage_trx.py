"""TRX 侧车与现有 BTC/ETH 采集器的管理边界；不运行真实服务。"""

from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import manage


class TrxProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/tmp/quant-trx-test")
        self.specs = manage.services(self.root)
        self.primary = self.specs["market"]
        self.sidecar = self.specs["market-trx"]
        self.primary_command = f"python3 -u {self.root}/market_stream.py"
        self.sidecar_command = shlex.join(self.sidecar.command)

    def test_sidecar_command_has_isolated_spool_and_status(self):
        command = self.sidecar.command
        self.assertEqual(command[command.index("--pairs") + 1], "TRX/USDT")
        self.assertEqual(command[command.index("--market") + 1], "futures")
        self.assertEqual(command[command.index("--spool-dir") + 1],
                         str(self.root / "database/runtime/trx/market_spool"))
        self.assertEqual(command[command.index("--status-document-key") + 1],
                         "market_stream_trx_status.json")
        self.assertNotEqual(self.primary.stream_spool_dir.parent,
                            self.sidecar.stream_spool_dir.parent)
        self.assertEqual(self.sidecar.stream_kline_intervals, ("1m", "5m", "15m", "1h", "1d"))

    def test_existing_primary_pid_does_not_match_sidecar(self):
        self.assertTrue(manage.matches_process(self.primary, self.primary_command, self.root))
        self.assertFalse(manage.matches_process(self.sidecar, self.primary_command, self.root))

    def test_primary_restart_identity_accepts_legacy_but_validates_new_intervals(self):
        self.assertEqual(self.primary.stream_kline_intervals, ("1m", "5m", "15m", "1h", "1d"))
        self.assertTrue(manage.matches_process(self.primary, self.primary_command, self.root))
        expected = shlex.join(self.primary.command)
        self.assertTrue(manage.matches_process(self.primary, expected, self.root))
        self.assertFalse(manage.matches_process(
            self.primary, expected.replace("1m,5m,15m,1h,1d", "5m"), self.root))

    def test_sidecar_pid_does_not_match_primary(self):
        self.assertTrue(manage.matches_process(self.sidecar, self.sidecar_command, self.root))
        self.assertFalse(manage.matches_process(self.primary, self.sidecar_command, self.root))

    def test_discovery_separates_same_script_pids(self):
        commands = {6368: self.primary_command, 42: self.sidecar_command}
        with patch("manage.process_cwd", return_value=self.root):
            self.assertEqual(manage.find_processes(self.primary, commands), [6368])
            self.assertEqual(manage.find_processes(self.sidecar, commands), [42])

    def test_relative_spool_and_equal_options_are_recognized(self):
        command = ("python3 market_stream.py --market=futures --pairs=TRX/USDT:USDT "
                   "--kline-intervals=1m,5m,15m,1h,1d "
                   "--spool-dir=database/runtime/trx/market_spool "
                   "--status-document-key=market_stream_trx_status.json")
        self.assertTrue(manage.matches_process(self.sidecar, command, self.root))
        self.assertFalse(manage.matches_process(self.primary, command, self.root))

    def test_primary_accepts_its_explicit_spool_and_document(self):
        command = (self.primary_command + f" --spool-dir {self.primary.stream_spool_dir}"
                   " --status-document-key market_stream_status.json")
        self.assertTrue(manage.matches_process(self.primary, command, self.root))
        self.assertFalse(manage.matches_process(self.sidecar, command, self.root))

    def test_custom_unregistered_collector_is_not_managed(self):
        command = self.primary_command + " --spool-dir database/runtime/another/market_spool"
        for spec in (self.primary, self.sidecar):
            self.assertFalse(manage.matches_process(spec, command, self.root))

    def test_sidecar_requires_trx_only_and_isolated_status_document(self):
        for original, replacement in (("TRX/USDT", "BTC/USDT,TRX/USDT"),
                                      ("futures", "spot"),
                                      ("market_stream_trx_status.json", "market_stream_status.json")):
            with self.subTest(replacement=replacement):
                command = self.sidecar_command.replace(original, replacement)
                self.assertFalse(manage.matches_process(self.sidecar, command, self.root))
        no_document = self.sidecar_command.replace(
            " --status-document-key market_stream_trx_status.json", "")
        self.assertFalse(manage.matches_process(self.sidecar, no_document, self.root))

    def test_malformed_or_duplicate_identity_options_are_not_claimed(self):
        for suffix in (" --spool-dir", " --spool-dir=", " --pairs --market futures",
                       " --pairs TRX/USDT", " --status-document-key=market_stream_trx_status.json"):
            with self.subTest(suffix=suffix):
                self.assertFalse(manage.matches_process(
                    self.sidecar, self.sidecar_command + suffix, self.root))

    def test_sidecar_in_other_project_is_not_claimed(self):
        self.assertFalse(manage.matches_process(
            self.sidecar, self.sidecar_command, Path("/tmp/other-project")))


class TrxLifecycleTests(unittest.TestCase):
    def test_start_sidecar_does_not_reuse_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "market_stream.py").write_text("# test entry\n")
            spec = manage.services(root)["market-trx"]
            commands = {6368: f"python3 -u {root}/market_stream.py"}
            child = MagicMock(pid=42)
            child.poll.return_value = None
            with patch("manage.process_commands", return_value=commands), \
                 patch("manage.process_cwd", return_value=root), \
                 patch("manage.subprocess.Popen", return_value=child) as spawn, \
                 patch("manage.time.sleep"), patch.dict("os.environ", {"QUANT_STREAM_ENABLED": "true"}):
                self.assertTrue(manage.start_service(spec, root))
            self.assertEqual(spawn.call_args.args[0], spec.command)
            self.assertEqual(manage.pid_file(root, spec).read_text().strip(), "42")

    def test_existing_sidecar_is_reused(self):
        spec = manage.services()["market-trx"]
        with patch("manage.find_processes", return_value=[42]), \
             patch("manage.subprocess.Popen") as spawn:
            self.assertTrue(manage.start_service(spec))
        spawn.assert_not_called()

    def test_disabled_streams_skip_both_collectors(self):
        specs = manage.services()
        with patch("manage.find_processes", return_value=[]), \
             patch("manage.subprocess.Popen") as spawn, \
             patch.dict("os.environ", {"QUANT_STREAM_ENABLED": "false"}):
            self.assertTrue(manage.start_service(specs["market"]))
            self.assertTrue(manage.start_service(specs["market-trx"]))
        spawn.assert_not_called()

    def test_stop_sidecar_signals_only_sidecar_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = manage.services(root)["market-trx"]
            primary = f"python3 -u {root}/market_stream.py"
            command = shlex.join(spec.command)
            with patch("manage.process_commands", side_effect=[
                    {6368: primary, 42: command}, {6368: primary, 42: command}, {6368: primary}]), \
                 patch("manage.process_cwd", return_value=root), \
                 patch("manage.os.kill") as kill:
                self.assertTrue(manage.stop_service(spec, root))
            kill.assert_called_once_with(42, manage.signal.SIGTERM)

    def test_stop_recheck_rejects_pid_reused_by_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = manage.services(root)["market-trx"]
            with patch("manage.find_processes", return_value=[42]), \
                 patch("manage.process_commands", return_value={42: f"python3 {root}/market_stream.py"}), \
                 patch("manage.process_cwd", return_value=root), \
                 patch("manage.os.kill") as kill:
                self.assertTrue(manage.stop_service(spec, root))
            kill.assert_not_called()

    def test_lifecycle_parser_accepts_sidecar(self):
        for action in ("start", "stop", "restart", "status"):
            with self.subTest(action=action):
                args = manage.parser().parse_args([action, "market-trx"])
                self.assertEqual(args.services, ["market-trx"])

    def test_automatic_lifecycle_selection_includes_sidecar(self):
        for action in ("start", "stop", "restart", "status"):
            with self.subTest(action=action), \
                 patch("manage.load_environment"), patch("manage.ensure_local_postgres"), \
                 patch("manage.start_service", return_value=True) as start, \
                 patch("manage.stop_service", return_value=True) as stop, \
                 patch("manage.status", return_value=0) as status:
                self.assertEqual(manage.main([action]), 0)
                if action in {"start", "restart"}:
                    self.assertIn("market-trx", [call.args[0].name for call in start.call_args_list])
                if action in {"stop", "restart"}:
                    self.assertIn("market-trx", [call.args[0].name for call in stop.call_args_list])
                if action == "status":
                    self.assertIn("market-trx", [spec.name for spec in status.call_args.args[0]])


if __name__ == "__main__":
    unittest.main()
