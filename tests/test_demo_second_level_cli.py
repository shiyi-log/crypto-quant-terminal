import argparse
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import run_binance_demo_second_level as cli


class Data:
    def __init__(self, *, pending=0, exposure=0, fill_gaps=0, own_orders=0):
        self.counts = pending, exposure, fill_gaps, own_orders
        self.documents, self.events, self.queries = {}, [], []
        self.fail_checkpoint = False

    @contextmanager
    def _connection(self):
        data = self

        class Connection:
            def execute(self, query, params=None):
                data.queries.append((query, params))
                if query.startswith("SET TRANSACTION"):
                    return None
                index = len([sql for sql, _ in data.queries if not sql.startswith("SET TRANSACTION")]) - 1
                return SimpleNamespace(fetchone=lambda: {"n": data.counts[index % 4]})

        yield Connection()

    def get_document(self, key):
        return deepcopy(self.documents.get(key))

    def put_document(self, key, value):
        if self.fail_checkpoint and key.endswith("source.json"):
            raise RuntimeError("private-message-must-not-be-used")
        self.events.append(("persist", key))
        self.documents[key] = deepcopy(value)


class Source:
    def __init__(self, _data=None, pair="BTC/USDT:USDT", *, state=None, started_ms=None):
        self._state = deepcopy(state or {"started_ms": started_ms, "pair": pair, "count": 0})
        self.last_decision = None

    @property
    def state(self):
        return deepcopy(self._state)

    @property
    def metrics(self):
        return {"count": self._state["count"]}

    def latest_signal(self, now_ms):
        self._state["count"] += 1
        self.last_decision = {"reason": "test_signal", "decision_ms": now_ms}
        return {"signal_id": "offline-signal"}


class CLITests(unittest.TestCase):
    def args(self, **changes):
        values = {"session": "offline_test", "execute": True, "pair": "BTC", "max_signals": 6,
                  "duration_seconds": 600, "max_notional_usdt": "75", "recover_only": False}
        return argparse.Namespace(**{**values, **changes})

    def dependencies(self, data):
        credentials = SimpleNamespace(
            status=Mock(side_effect=AssertionError("status/read race must not be used")),
            load_credentials=Mock(return_value=SimpleNamespace(api_key="fixture-key", secret="fixture-secret")))
        provider = Mock(return_value=SimpleNamespace(credentials=credentials))
        adapter = SimpleNamespace(fetch_position_mode=Mock(return_value={"hedged": False}),
                                  fetch_positions=Mock(return_value=[]), fetch_open_orders=Mock(return_value=[]))
        factory = Mock(return_value=adapter)

        def run(_adapter, ledgers, source, *, config):
            self.assertEqual(set(ledgers), {"maker", "taker"})
            signal = source.latest_signal(100_001)
            data.events.append(("runner_received_signal", signal["signal_id"]))
            return {"success": True, "cycles": [], "recovery_only": False}

        recovery = Mock(return_value={"success": True, "cycles": [], "recovery_only": True})
        return {"auth_dir": Path("/offline"), "clock_ms": lambda: 100_000,
                "api_factory": provider, "adapter_factory": factory,
                "ledger_factory": lambda _data, **kwargs: SimpleNamespace(**kwargs),
                "source_factory": Source, "snapshot_reader": lambda *_: {"quality": "valid"},
                "run_session": run, "recover_session": recovery}

    def test_global_foreign_risk_blocks_before_credentials(self):
        for counts in ({"pending": 1}, {"exposure": 1}, {"fill_gaps": 1}):
            with self.subTest(counts=counts):
                data = Data(**counts)
                deps = self.dependencies(data)
                with self.assertRaises(cli.OperatorPreflightError) as raised:
                    cli.run_operator(self.args(), data, **deps)
                self.assertEqual(raised.exception.kind, "other_demo_session_unresolved")
                deps["api_factory"].assert_not_called()

    def test_preflight_excludes_exactly_two_own_namespaces(self):
        data = Data()
        result = cli.ledger_preflight(data, ["second_level_x_maker", "second_level_x_taker"])
        self.assertEqual(result["sessionOrderCount"], 0)
        for _, params in data.queries[1:]:
            self.assertEqual(params, (["second_level_x_maker", "second_level_x_taker"],))
        self.assertIn("READ ONLY", data.queries[0][0])

    def test_source_unavailable_blocks_before_credentials(self):
        data = Data()
        deps = self.dependencies(data)
        deps["snapshot_reader"] = lambda *_: {"quality": "unknown", "reason": "stale_snapshot"}
        with self.assertRaises(cli.OperatorPreflightError) as raised:
            cli.run_operator(self.args(), data, **deps)
        self.assertEqual(raised.exception.kind, "second_level_source_unavailable")
        deps["api_factory"].assert_not_called()
        self.assertFalse(data.documents)

    def test_checkpoint_committed_before_signal_reaches_runner(self):
        data = Data()
        deps = self.dependencies(data)
        report = cli.run_operator(self.args(), data, **deps)
        checkpoints = [index for index, event in enumerate(data.events) if event[0] == "persist" and event[1].endswith("source.json")]
        received = next(index for index, event in enumerate(data.events) if event[0] == "runner_received_signal")
        self.assertEqual(len(checkpoints), 3)
        self.assertLess(checkpoints[-1], received)
        saved = data.get_document("binance_demo_second_level/offline_test/source.json")
        self.assertEqual(saved["state"]["count"], 1)
        self.assertEqual(saved["started_ms"], 100_000)
        self.assertEqual(report["virtual_account_keys"], {
            "maker": "second_level_offline_test_maker", "taker": "second_level_offline_test_taker"})
        self.assertEqual(data.get_document(report["report_document"]), report)

    def test_rotated_credentials_cannot_reuse_session_even_for_recovery(self):
        data = Data()
        deps = self.dependencies(data)
        cli.run_operator(self.args(), data, **deps)
        saved = data.get_document("binance_demo_second_level/offline_test/source.json")
        credentials = deps["api_factory"].return_value.credentials
        credentials.load_credentials.return_value = SimpleNamespace(api_key="rotated-fixture-key", secret="fixture-secret")
        deps["adapter_factory"].reset_mock()
        with self.assertRaises(cli.OperatorPreflightError) as raised:
            cli.run_operator(self.args(recover_only=True), data, **deps)
        self.assertEqual(raised.exception.kind, "second_level_credential_identity_mismatch")
        deps["adapter_factory"].assert_not_called()
        deps["recover_session"].assert_not_called()
        self.assertEqual(data.get_document("binance_demo_second_level/offline_test/source.json"), saved)
        credentials.status.assert_not_called()

    def test_credential_identity_commits_before_adapter_creation(self):
        data = Data()
        deps = self.dependencies(data)
        adapter = deps["adapter_factory"].return_value

        def adapter_factory(**_kwargs):
            saved = data.get_document("binance_demo_second_level/offline_test/source.json")
            self.assertEqual(len(saved["credential_fingerprint"]), 16)
            self.assertNotIn("api_key", saved)
            return adapter

        deps["adapter_factory"] = adapter_factory
        cli.run_operator(self.args(), data, **deps)

    def test_orders_without_frozen_credential_identity_cannot_recover(self):
        data = Data()
        deps = self.dependencies(data)
        cli.run_operator(self.args(), data, **deps)
        data.documents["binance_demo_second_level/offline_test/source.json"]["credential_fingerprint"] = None
        data.counts = (0, 0, 0, 1)
        deps["adapter_factory"].reset_mock()
        with self.assertRaises(cli.OperatorPreflightError) as raised:
            cli.run_operator(self.args(), data, **deps)
        self.assertEqual(raised.exception.kind, "second_level_credential_identity_missing")
        deps["adapter_factory"].assert_not_called()

    def test_failed_decision_checkpoint_does_not_return_signal(self):
        data = Data()
        source = Source(started_ms=100_000)
        wrapped = cli.CheckpointedSource(source, data, "session/source.json", {"started_ms": 100_000})
        data.fail_checkpoint = True
        with self.assertRaises(RuntimeError):
            wrapped.latest_signal(100_001)
        self.assertEqual(source.state["count"], 1)
        self.assertFalse(data.documents)

    def test_reusing_existing_session_recovers_without_source_poll_or_flat_requirement(self):
        data = Data()
        deps = self.dependencies(data)
        cli.run_operator(self.args(), data, **deps)
        saved = data.get_document("binance_demo_second_level/offline_test/source.json")
        deps["snapshot_reader"] = Mock(side_effect=AssertionError("recovery cannot depend on live source"))
        adapter = deps["adapter_factory"].return_value
        adapter.fetch_positions.side_effect = AssertionError("recovery runner must attribute existing risk")
        recovery_report = cli.run_operator(self.args(), data, **deps)
        self.assertTrue(recovery_report["recovery_only"])
        deps["recover_session"].assert_called_once()
        self.assertEqual(data.get_document("binance_demo_second_level/offline_test/source.json"), saved)
        deps["snapshot_reader"].assert_not_called()

    def test_existing_order_without_checkpoint_blocks_before_credentials(self):
        data = Data(own_orders=1)
        deps = self.dependencies(data)
        with self.assertRaises(cli.OperatorPreflightError) as raised:
            cli.run_operator(self.args(), data, **deps)
        self.assertEqual(raised.exception.kind, "second_level_source_checkpoint_missing")
        deps["api_factory"].assert_not_called()

    def test_changed_config_cannot_reuse_session(self):
        data = Data()
        deps = self.dependencies(data)
        cli.run_operator(self.args(), data, **deps)
        deps["api_factory"].reset_mock()
        with self.assertRaises(cli.OperatorPreflightError) as raised:
            cli.run_operator(self.args(max_signals=8), data, **deps)
        self.assertEqual(raised.exception.kind, "second_level_session_identity_mismatch")
        deps["api_factory"].assert_not_called()

    def test_new_session_rejects_hedge_and_foreign_account_risk(self):
        for positions, orders, hedged in (([{"signed_qty": "0.1"}], [], False),
                ([], [{"client_id": "foreign"}], False), ([], [], True)):
            with self.subTest(positions=positions, orders=orders, hedged=hedged):
                data = Data()
                deps = self.dependencies(data)
                adapter = deps["adapter_factory"].return_value
                adapter.fetch_positions.return_value = positions
                adapter.fetch_open_orders.return_value = orders
                adapter.fetch_position_mode.return_value = {"hedged": hedged}
                with self.assertRaises(cli.OperatorPreflightError):
                    cli.run_operator(self.args(), data, **deps)
                deps["recover_session"].assert_not_called()

    def test_busy_account_lock_does_not_open_database(self):
        with tempfile.TemporaryDirectory() as path:
            with patch.object(cli, "ROOT", Path(path)), patch.object(cli, "load_environment"), \
                    patch.object(cli.fcntl, "flock", side_effect=BlockingIOError), \
                    patch.object(cli, "DataStore") as store, redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--execute", "--session", "offline_test"])
                self.assertEqual(result, 2)
                self.assertIn("demo_account_lock_busy", output.getvalue())
                store.assert_not_called()

    def test_compact_report_preserves_frozen_quantity_and_execution_coverage(self):
        report = {"success": True, "coverage_population": "evaluated_cycles_including_local_prePOST_rejections",
            "attempt_population": "durably_claimed_attempts_including_local_prePOST_guard_rejections",
            "observed_arms": ["maker", "taker"], "accepted_arms": ["taker"], "filled_arms": ["taker"],
            "observed_maker_distances_bp": [0], "cycles": [
                {"arm": "maker", "frozen_entry_quantity": "0.0006", "entry_filled_quantity": "0",
                 "entry_exchange_accepted": False, "entry_terminal_status": "rejected"},
                {"arm": "taker", "frozen_entry_quantity": "0.0006", "entry_filled_quantity": "0.0006",
                 "entry_exchange_accepted": True, "entry_terminal_status": "filled"}]}
        summary = cli._compact_report(report)
        self.assertEqual(summary["cycleCount"], 2)
        self.assertEqual(summary["cycleResults"][0]["frozen_entry_quantity"], "0.0006")
        self.assertNotIn("entry_requested_quantity", summary["cycleResults"][0])
        self.assertFalse(summary["cycleResults"][0]["entry_exchange_accepted"])
        self.assertTrue(summary["cycleResults"][1]["entry_exchange_accepted"])
        self.assertEqual(summary["accepted_arms"], ["taker"])
        self.assertEqual(summary["filled_arms"], ["taker"])
        self.assertEqual(summary["coverage_population"], report["coverage_population"])
        self.assertEqual(summary["attempt_population"], report["attempt_population"])


if __name__ == "__main__":
    unittest.main()
