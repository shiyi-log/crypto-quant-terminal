"""Offline operator guards; no encrypted secrets or network are accessed."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_demo_second_level_cli import Data
from tools import run_binance_demo_fee_capture as cli


class FeeCaptureCLITests(unittest.TestCase):
    def run_operator(self, args, data, **dependencies):
        if not getattr(args, "research_id", None):
            args.research_id = "research-1"
        if not getattr(args, "account_scope", None):
            args.account_scope = "offline-test-account"
        import research_ledger
        if isinstance(research_ledger.require_closed_risk_gate, Mock):
            return cli.run_operator(args, data, **dependencies)
        binding = cli._expected_target_binding(args.account_scope, cli.config_from_args(args))
        gate = {"research_id": "research-1", "event_id": "gate-event-1",
                "boundary_hash": "boundary-1", "inputs_hash": "inputs-1",
                "risk_segment_status": "closed", "preflight_status": "pass",
                "target_binding": binding,
                "target_binding_hash": research_ledger.risk_gate_target_binding_hash(binding)}
        with patch("research_ledger.require_closed_risk_gate", return_value=gate):
            return cli.run_operator(args, data, **dependencies)

    def args(self, **changes):
        values = {"session": "fee_capture_offline_test", "execute": True,
                  "max_notional_usdt": "75", "maker_ttl_seconds": 30,
                  "total_timeout_seconds": 600, "reconciliation_seconds": 60,
                  "recover_only": False, "account_scope": "offline-test-account"}
        return argparse.Namespace(**{**values, **changes})

    def dependencies(self, data):
        credentials = SimpleNamespace(
            status=Mock(side_effect=AssertionError("must use loaded key, not a second read")),
            load_credentials=Mock(return_value=SimpleNamespace(api_key="fixture-key", secret="fixture-secret")))
        provider = Mock(return_value=SimpleNamespace(credentials=credentials))
        adapter = Mock()
        factory = Mock(return_value=adapter)
        capture = Mock(return_value={"success": True, "orders": [], "fills": [], "samples": [],
                                     "position_flat": True, "open_orders_zero": True})
        return {"auth_dir": Path("/offline"), "clock_ms": lambda: 100_000,
                "api_factory": provider, "adapter_factory": factory,
                "ledger_factory": lambda _data, **kwargs: SimpleNamespace(**kwargs),
                "capture": capture}

    def test_bad_authorization_namespace_and_bounds_never_construct_provider(self):
        cases = ({"execute": False}, {"session": "demo_default"}, {"session": "fee_capture_../evil"},
                 {"max_notional_usdt": "NaN"}, {"max_notional_usdt": "Infinity"},
                 {"max_notional_usdt": "75.01"}, {"max_notional_usdt": "0"},
                 {"maker_ttl_seconds": 3}, {"maker_ttl_seconds": 61},
                 {"total_timeout_seconds": 601}, {"total_timeout_seconds": 20},
                 {"reconciliation_seconds": 0})
        for changes in cases:
            with self.subTest(changes=changes):
                data = Data()
                deps = self.dependencies(data)
                with self.assertRaises(cli.OperatorPreflightError):
                    self.run_operator(self.args(**changes), data, **deps)
                deps["api_factory"].assert_not_called()
                self.assertEqual(data.documents, {})

    def test_global_foreign_risk_blocks_before_credentials(self):
        for counts in ({"pending": 1}, {"exposure": 1}, {"fill_gaps": 1}):
            with self.subTest(counts=counts):
                data = Data(**counts)
                deps = self.dependencies(data)
                with self.assertRaises(cli.OperatorPreflightError) as error:
                    self.run_operator(self.args(), data, **deps)
                self.assertEqual(error.exception.kind, "other_demo_session_unresolved")
                deps["api_factory"].assert_not_called()

    def test_production_entry_cannot_skip_gate_with_partial_dependency_injection(self):
        data = Data()
        with self.assertRaises(cli.OperatorPreflightError) as error:
            cli.run_operator(self.args(), data, auth_dir=Path("/offline"), capture=Mock())
        self.assertEqual(error.exception.kind, "risk_gate_not_closed")
        self.assertFalse(hasattr(cli, "run_operator_for_test"))
        with self.assertRaises(cli.OperatorPreflightError) as error:
            cli.run_operator(self.args(), data, **self.dependencies(data))
        self.assertEqual(error.exception.kind, "risk_gate_not_closed")

    def test_identity_freezes_gate_and_recovery_does_not_read_current_gate(self):
        data = Data()
        deps = self.dependencies(data)
        gate = {"research_id": "research-1", "event_id": "gate-event-1",
                "boundary_hash": "boundary-1", "inputs_hash": "inputs-1",
                "risk_segment_status": "closed", "preflight_status": "pass",
                "target_binding_hash": "a" * 64}
        with patch("research_ledger.require_closed_risk_gate", return_value=gate):
            self.run_operator(self.args(research_id="research-1"), data, **deps)
        key = "binance_demo_fee_capture/fee_capture_offline_test.identity.json"
        saved = data.get_document(key)
        self.assertEqual(saved["risk_gate_identity"], {
            "research_id": "research-1", "event_id": "gate-event-1",
            "boundary_hash": "boundary-1", "inputs_hash": "inputs-1",
            "target_binding_hash": "a" * 64})
        with patch("research_ledger.require_closed_risk_gate", side_effect=RuntimeError("unavailable")) as gate_reader:
            report = self.run_operator(self.args(research_id="different"), data, **deps)
        gate_reader.assert_not_called()
        self.assertTrue(deps["capture"].call_args.kwargs["recovery_only"])
        self.assertEqual(report["research_id"], "research-1")
        self.assertEqual(report["risk_gate"], gate)

    def test_corrupt_gate_identity_blocks_recovery_without_credentials(self):
        data = Data()
        deps = self.dependencies(data)
        gate = {"research_id": "research-1", "event_id": "gate-event-1",
                "boundary_hash": "boundary-1", "inputs_hash": "inputs-1",
                "target_binding_hash": "a" * 64}
        with patch("research_ledger.require_closed_risk_gate", return_value=gate):
            self.run_operator(self.args(research_id="research-1"), data, **deps)
        key = "binance_demo_fee_capture/fee_capture_offline_test.identity.json"
        data.documents[key]["risk_gate_identity"]["event_id"] = "gate-event-other"
        deps["api_factory"].reset_mock()
        with self.assertRaises(cli.OperatorPreflightError) as error:
            self.run_operator(self.args(recover_only=True), data, **deps)
        self.assertEqual(error.exception.kind, "fee_capture_session_identity_mismatch")
        deps["api_factory"].assert_not_called()

    def test_preflight_excludes_only_this_session(self):
        data = Data()
        self.run_operator(self.args(), data, **self.dependencies(data))
        self.assertIn("READ ONLY", data.queries[0][0])
        self.assertTrue(all(params == (["fee_capture_offline_test"],) for _, params in data.queries[1:]))

    def test_identity_committed_before_adapter_or_capture_and_reports_preserved(self):
        data = Data()
        deps = self.dependencies(data)
        adapter = deps["adapter_factory"].return_value

        def factory(**kwargs):
            saved = data.get_document("binance_demo_fee_capture/fee_capture_offline_test.identity.json")
            self.assertEqual(len(saved["credential_fingerprint"]), 16)
            self.assertNotIn("fixture-key", json.dumps(saved))
            self.assertNotIn("fixture-secret", json.dumps(saved))
            self.assertTrue(kwargs["allow_trading"])
            return adapter

        deps["adapter_factory"] = factory
        report = self.run_operator(self.args(), data, **deps)
        self.assertEqual(data.get_document(report["report_document"]), report)
        self.assertEqual(data.get_document("binance_demo_fee_capture/fee_capture_offline_test.json"), report)
        deps["capture"].assert_called_once()
        deps["api_factory"].return_value.credentials.status.assert_not_called()

    def test_credential_rotation_refuses_recovery_before_adapter(self):
        data = Data()
        deps = self.dependencies(data)
        self.run_operator(self.args(), data, **deps)
        saved = deepcopy(data.documents)
        deps["api_factory"].return_value.credentials.load_credentials.return_value = SimpleNamespace(
            api_key="rotated-fixture-key", secret="rotated-fixture-secret")
        deps["adapter_factory"].reset_mock()
        deps["capture"].reset_mock()
        with self.assertRaises(cli.OperatorPreflightError) as error:
            self.run_operator(self.args(recover_only=True), data, **deps)
        self.assertEqual(error.exception.kind, "fee_capture_credential_identity_mismatch")
        deps["adapter_factory"].assert_not_called()
        deps["capture"].assert_not_called()
        self.assertEqual(data.documents, saved)

    def test_config_drift_and_identity_missing_block_before_provider(self):
        for change in ("ttl", "missing", "recovery_new"):
            with self.subTest(change=change):
                data = Data()
                deps = self.dependencies(data)
                args = self.args()
                if change == "ttl":
                    self.run_operator(args, data, **deps)
                    args = self.args(maker_ttl_seconds=60)
                elif change == "missing":
                    data.counts = (0, 0, 0, 1)
                else:
                    args = self.args(recover_only=True)
                deps["api_factory"].reset_mock()
                with self.assertRaises(cli.OperatorPreflightError):
                    self.run_operator(args, data, **deps)
                deps["api_factory"].assert_not_called()

    def test_recovery_flag_is_forwarded_without_changing_identity(self):
        data = Data()
        deps = self.dependencies(data)
        self.run_operator(self.args(), data, **deps)
        identity = deepcopy(data.documents["binance_demo_fee_capture/fee_capture_offline_test.identity.json"])
        self.run_operator(self.args(max_notional_usdt="75.0"), data, **deps)
        self.assertTrue(deps["capture"].call_args.kwargs["recovery_only"])
        self.assertEqual(data.documents["binance_demo_fee_capture/fee_capture_offline_test.identity.json"], identity)
        self.assertEqual(sum("/reports/" in key for key in data.documents), 2)

    def test_identity_write_failure_prevents_adapter_and_trade(self):
        data = Data()
        deps = self.dependencies(data)
        data.put_document = Mock(side_effect=RuntimeError("private exception detail"))
        with self.assertRaises(RuntimeError):
            self.run_operator(self.args(), data, **deps)
        deps["adapter_factory"].assert_not_called()
        deps["capture"].assert_not_called()

    def test_busy_lock_never_opens_database(self):
        with tempfile.TemporaryDirectory() as path:
            with patch.object(cli, "ROOT", Path(path)), patch.object(cli, "load_environment"), \
                    patch.object(cli.fcntl, "flock", side_effect=BlockingIOError), \
                    patch.object(cli, "DataStore") as store, redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--execute", "--session", "fee_capture_offline_test",
                                   "--account-scope", "offline-test-account"])
                self.assertEqual(result, 2)
                self.assertIn("demo_account_lock_busy", output.getvalue())
                store.assert_not_called()

    def test_unexpected_error_redacts_exception_text_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as path:
            with patch.object(cli, "ROOT", Path(path)), patch.object(cli, "load_environment"), \
                    patch.object(cli, "DataStore") as store, \
                    patch.object(cli, "run_operator", side_effect=RuntimeError("secret=never-print-this")), \
                    patch.object(cli.fcntl, "flock") as flock, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--execute", "--session", "fee_capture_offline_test",
                                           "--account-scope", "offline-test-account"]), 2)
                self.assertNotIn("never-print-this", output.getvalue())
                self.assertIn('"recoveryRequired": true', output.getvalue())
                store.return_value.close.assert_called_once()
                self.assertEqual(flock.call_args.args[1], cli.fcntl.LOCK_UN)

    def test_print_only_actual_fill_fields_and_summary(self):
        report = {"success": True, "samples": [{"pair": "BTC/USDT:USDT", "rate": "0.0002",
                  "is_maker": True, "raw": {"apiKey": "never-print"}}], "orders": [{}], "fills": [{}]}
        with redirect_stdout(io.StringIO()) as output:
            cli.print_report(report)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["kind"] for row in rows], ["fill", "summary"])
        self.assertEqual(rows[0]["rate"], "0.0002")
        self.assertNotIn("never-print", output.getvalue())
        self.assertEqual(rows[1]["orderCount"], 1)


if __name__ == "__main__":
    unittest.main()
