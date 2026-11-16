"""Offline account-lock handoff: no worker or owned risk may be abandoned."""
import fcntl
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock

from demo_runner_controller import DemoHandoffError, DemoRunnerController, STATE_KEY


class Documents:
    def __init__(self):
        self.items = {}

    def put_document(self, key, value):
        self.items[key] = value

    def get_document(self, key, default=None):
        return self.items.get(key, default)


class PausedService:
    def __init__(self):
        self.config = SimpleNamespace(strategy_id="frozen-paper", virtual_account_key="demo_default",
                                      max_notional_usdt="1000", poll_interval_ms=60_000)
        self.policy_hash = "frozen-policy"
        self.entries_enabled = True
        self.cycle = Mock()

    def stop(self, **kwargs):
        self.entries_enabled = False

    def status(self):
        return {"running": True, "entries_enabled": self.entries_enabled,
                "status": "paused_protecting" if not self.entries_enabled else "observing"}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Documents()
        self.ledger = Mock()
        self.ledger.virtual_account_key = "demo_default"
        self.ledger.get_binding.return_value = None
        self.ledger.net_fill_positions.return_value = {}
        self.ledger.pending_orders.return_value = []
        self.ledger.runner_orders.return_value = []
        self.ledger.runner_fills.return_value = []
        self.ledger.account_reconciliation_status.return_value = {"safeToSwitch": True}
        self.credentials = SimpleNamespace(api_key="offline-handoff-key", secret="offline-handoff-secret")
        self.provider = Mock(return_value=self.credentials)
        self.adapter = Mock()
        self.adapter.fetch_positions.return_value = []
        self.adapter.fetch_open_orders.return_value = []
        self.adapter.fetch_open_conditional_orders.return_value = []
        self.adapter.fetch_position_mode.return_value = {"hedged": False}
        self.adapter_factory = Mock(return_value=self.adapter)
        self.controller = DemoRunnerController(self.data, self.ledger, self.provider, self.adapter_factory,
            paper_source=Mock(), lock_path=Path(self.temp.name) / "demo.lock",
            risk_gate_required=False)
        self.controller._lock()
        self.service = PausedService()
        self.controller.service = self.service
        self.controller._selected_account_id = "frozen-paper"

    def assert_lock_owned(self, expected):
        with self.controller.lock_path.open("a+") as contender:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owned = True
            else:
                owned = False
                fcntl.flock(contender, fcntl.LOCK_UN)
        self.assertEqual(owned, expected)

    def tearDown(self):
        self.ledger.net_fill_positions.return_value = {}
        self.ledger.net_fill_positions.side_effect = None
        self.ledger.pending_orders.return_value = []
        self.ledger.runner_orders.return_value = []
        self.ledger.runner_orders.side_effect = None
        self.ledger.runner_fills.return_value = []
        self.ledger.account_reconciliation_status.return_value = {"safeToSwitch": True}
        self.provider.side_effect = None
        self.provider.return_value = self.credentials
        self.ledger.get_binding.return_value = None
        for method, value in (("fetch_positions", []), ("fetch_open_orders", []),
                              ("fetch_open_conditional_orders", []),
                              ("fetch_position_mode", {"hedged": False})):
            if not callable(getattr(self.adapter, method, None)):
                setattr(self.adapter, method, Mock())
            getattr(self.adapter, method).side_effect = None
            getattr(self.adapter, method).return_value = value
        self.controller.close_if_flat()

    def test_owned_exposure_and_pending_submission_keep_paused_protection_and_lock(self):
        for condition, code in (("exposure", "demo_owned_exposure_unresolved"),
                                ("pending", "demo_owned_orders_unresolved")):
            with self.subTest(condition=condition):
                self.ledger.net_fill_positions.return_value = {"BTC/USDT:USDT": "0.0006"} \
                    if condition == "exposure" else {}
                self.ledger.pending_orders.return_value = [{"status": "unknown"}] \
                    if condition == "pending" else []
                with self.assertRaises(DemoHandoffError) as refused:
                    self.controller.close_if_flat(join_timeout=0)
                self.assertEqual(refused.exception.code, code)
                self.assertIs(self.controller.service, self.service)
                self.assertFalse(self.service.entries_enabled)
                self.assertFalse(self.controller._halt_worker.is_set())
                self.assertFalse(self.data.items[STATE_KEY]["entriesEnabled"])
                self.assert_lock_owned(True)

    def test_join_timeout_retains_lock_and_cancels_worker_halt(self):
        worker = Mock()
        worker.is_alive.return_value = True
        self.controller._thread = worker
        with self.assertRaises(DemoHandoffError) as refused:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(refused.exception.code, "demo_worker_busy")
        worker.join.assert_called_once_with(timeout=0)
        self.assertIs(self.controller.service, self.service)
        self.assertFalse(self.controller._halt_worker.is_set())
        self.assertTrue(self.controller.status()["accountLockOwned"])
        self.assert_lock_owned(True)
        worker.is_alive.return_value = False

    def test_terminal_filled_order_without_matching_fills_keeps_protection_and_lock(self):
        self.ledger.runner_orders.return_value = [
            {"client_id": "owned-entry", "status": "filled", "filled_quantity": "0.0006"},
            {"client_id": "owned-exit", "status": "filled", "filled_quantity": "0.0006"}]
        self.ledger.runner_fills.return_value = []
        with self.assertRaises(DemoHandoffError) as refused:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(refused.exception.code, "demo_owned_fills_unresolved")
        self.assertIs(self.controller.service, self.service)
        self.assertFalse(self.service.entries_enabled)
        self.assert_lock_owned(True)

    def test_fresh_exchange_conditional_order_prevents_yield_after_worker_join(self):
        joined = threading.Event()
        worker = Mock()
        worker.is_alive.return_value = False
        worker.join.side_effect = lambda **kwargs: joined.set()
        self.controller._thread = worker
        def conditional_snapshot():
            self.assertTrue(joined.is_set())
            return [{"client_algo_id": "foreign-conditional", "algo_status": "NEW"}]
        self.adapter.fetch_open_conditional_orders.side_effect = conditional_snapshot
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(error.exception.code, "demo_account_conditional_orders_unresolved")
        self.assertIs(self.controller.service, self.service)
        self.assertFalse(self.service.entries_enabled)
        self.assertFalse(self.data.items[STATE_KEY].get("yieldedFlat"))
        self.assertTrue(self.controller.status()["workerRunning"])
        self.assert_lock_owned(True)

    def test_each_fresh_exchange_query_failure_retains_lock_and_paused_protection(self):
        for method in ("fetch_positions", "fetch_open_orders",
                       "fetch_open_conditional_orders", "fetch_position_mode"):
            with self.subTest(method=method):
                getattr(self.adapter, method).side_effect = RuntimeError(self.credentials.secret)
                with self.assertLogs("demo_runner_controller", level="INFO") as captured, self.assertRaises(DemoHandoffError) as error:
                    self.controller.close_if_flat(join_timeout=1)
                self.assertEqual(error.exception.code, "demo_switch_exchange_check_failed")
                self.assertNotIn(self.credentials.secret, str(error.exception))
                self.assertNotIn(self.credentials.secret, str(captured.output))
                self.assertNotIn(self.credentials.secret, str(self.controller.status()))
                self.assertIs(self.controller.service, self.service)
                self.assertFalse(self.service.entries_enabled)
                self.assertFalse(self.controller._halt_worker.is_set())
                self.assert_lock_owned(True)
                getattr(self.adapter, method).side_effect = None

    def test_conditional_triggered_during_snapshot_is_rechecked_as_normal_child_order(self):
        def triggered_snapshot():
            self.adapter.fetch_open_orders.return_value = [{"client_id": "actual-trigger-child"}]
            return []
        self.adapter.fetch_open_conditional_orders.side_effect = triggered_snapshot
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(error.exception.code, "demo_account_open_orders_unresolved")
        self.assert_lock_owned(True)

    def test_full_account_nonflat_unknown_and_hedged_snapshots_prevent_yield(self):
        snapshots = (
            ("fetch_positions", [{"signed_qty": "0.1", "pair": "SOLUSDT"}],
             "demo_account_positions_not_flat"),
            ("fetch_positions", [{"signed_qty": "1", "position_side": "LONG"},
                                 {"signed_qty": "-1", "position_side": "SHORT"}],
             "demo_account_positions_not_flat"),
            ("fetch_positions", [{"signed_qty": "NaN"}], "demo_switch_exchange_check_failed"),
            ("fetch_positions", None, "demo_switch_exchange_check_failed"),
            ("fetch_open_orders", [{"client_id": "foreign-order"}],
             "demo_account_open_orders_unresolved"),
            ("fetch_open_orders", {}, "demo_switch_exchange_check_failed"),
            ("fetch_position_mode", {"hedged": True}, "demo_switch_exchange_check_failed"),
            ("fetch_position_mode", {}, "demo_switch_exchange_check_failed"),
        )
        for method, snapshot, code in snapshots:
            with self.subTest(method=method, code=code):
                getattr(self.adapter, method).return_value = snapshot
                with self.assertRaises(DemoHandoffError) as error:
                    self.controller.close_if_flat(join_timeout=1)
                self.assertEqual(error.exception.code, code)
                self.assertFalse(self.service.entries_enabled)
                self.assert_lock_owned(True)
                getattr(self.adapter, method).return_value = {"hedged": False} if method == "fetch_position_mode" else []

    def test_missing_or_invalid_conditional_snapshot_never_yields(self):
        for snapshot in (None, {}, {"data": []}, "unknown"):
            with self.subTest(snapshot=snapshot):
                self.adapter.fetch_open_conditional_orders.return_value = snapshot
                with self.assertRaises(DemoHandoffError) as error:
                    self.controller.close_if_flat(join_timeout=1)
                self.assertEqual(error.exception.code, "demo_switch_exchange_check_failed")
                self.assert_lock_owned(True)
        self.adapter.fetch_open_conditional_orders = None
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=1)
        self.assertEqual(error.exception.code, "demo_switch_exchange_check_failed")
        self.assert_lock_owned(True)

    def test_credential_identity_change_before_yield_never_queries_another_account(self):
        self.controller._adapter()
        self.adapter.reset_mock()
        self.provider.return_value = SimpleNamespace(api_key="another-key", secret="another-secret")
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(error.exception.code, "demo_credential_identity_mismatch")
        self.adapter.fetch_positions.assert_not_called()
        self.assert_lock_owned(True)

    def test_locked_controller_without_service_still_requires_fresh_account_proof(self):
        self.controller.service = None
        self.data.put_document(STATE_KEY, {"selectedAccountId": "frozen-paper", "entriesEnabled": False})
        self.adapter.fetch_open_conditional_orders.return_value = [{"algo_status": "NEW"}]
        with self.assertRaises(DemoHandoffError):
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "frozen-paper")
        self.assert_lock_owned(True)

    def test_unresolved_other_namespace_blocks_yield_before_exchange_checks(self):
        self.ledger.account_reconciliation_status.return_value = {
            "safeToSwitch": False, "pendingOrderCount": 1}
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(error.exception.code, "demo_account_reconciliation_unresolved")
        self.provider.assert_not_called()
        self.assertFalse(self.service.entries_enabled)
        self.assert_lock_owned(True)

    def test_never_owned_uninitialized_close_does_not_claim_verified_flatness(self):
        uninitialized = DemoRunnerController(Documents(), self.ledger, self.provider, self.adapter_factory,
            paper_source=Mock(), lock_path=Path(self.temp.name) / "uninitialized.lock",
            risk_gate_required=False)
        result = uninitialized.close_if_flat(join_timeout=0)
        self.assertFalse(result["accountLockOwned"])
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(uninitialized._yielded_flat)
        self.assertEqual(uninitialized.data_store.items, {})
        self.provider.assert_not_called()

    def test_fresh_account_queries_and_credentials_are_read_while_account_lock_is_owned(self):
        def credentials_under_lock():
            self.assert_lock_owned(True)
            return self.credentials
        self.provider.side_effect = credentials_under_lock
        for method, value in (("fetch_positions", []), ("fetch_open_orders", []),
                              ("fetch_open_conditional_orders", []),
                              ("fetch_position_mode", {"hedged": False})):
            def snapshot_under_lock(value=value):
                self.assert_lock_owned(True)
                return value
            getattr(self.adapter, method).side_effect = snapshot_under_lock
        result = self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(result["status"], "yielded_flat")
        self.assert_lock_owned(False)

    def test_fully_reconciled_round_trip_can_yield(self):
        self.ledger.runner_orders.return_value = [
            {"client_id": "owned-entry", "status": "filled", "filled_quantity": "0.0006"},
            {"client_id": "owned-exit", "status": "filled", "filled_quantity": "0.0006"}]
        self.ledger.runner_fills.return_value = [
            {"client_id": "owned-entry", "quantity": "0.0006"},
            {"client_id": "owned-exit", "quantity": "0.0006"}]
        self.assertFalse(self.controller.close_if_flat(join_timeout=0)["accountLockOwned"])
        self.assert_lock_owned(False)

    def test_reconciliation_gap_appearing_after_join_keeps_lock_and_restores_protection(self):
        self.ledger.runner_orders.side_effect = [[], [
            {"client_id": "owned-entry", "status": "filled", "filled_quantity": "0.0006"}]]
        with self.assertRaises(DemoHandoffError) as refused:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(refused.exception.code, "demo_owned_fills_unresolved")
        self.assertIs(self.controller.service, self.service)
        self.assertTrue(self.controller.status()["workerRunning"])
        self.assert_lock_owned(True)

    def test_real_finished_thread_join_zero_releases_only_flat_account(self):
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        self.controller._thread = worker
        result = self.controller.close_if_flat(join_timeout=0)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.controller.service)
        self.assertFalse(result["workerRunning"])
        self.assertFalse(result["accountLockOwned"])
        self.assertEqual(result["status"], "yielded_flat")
        self.assertEqual(result["selectedAccountId"], "frozen-paper")
        self.assertFalse(result["entriesEnabled"])
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "frozen-paper")
        self.assertFalse(self.data.items[STATE_KEY]["entriesEnabled"])
        self.assert_lock_owned(False)

    def test_successful_yield_never_resumes_automatically_even_after_controller_restart(self):
        self.controller.close_if_flat(join_timeout=0)
        self.assertTrue(self.data.items[STATE_KEY]["yieldedFlat"])
        self.controller._create = Mock()
        result = self.controller.resume_protection()
        self.controller._create.assert_not_called()
        self.assertEqual(result["selectedAccountId"], "frozen-paper")
        restarted = DemoRunnerController(self.data, self.ledger, Mock(), paper_source=Mock(),
                                        lock_path=self.controller.lock_path, risk_gate_required=False)
        restarted._create = Mock()
        result = restarted.resume_protection()
        restarted._create.assert_not_called()
        self.assertFalse(result["accountLockOwned"])
        self.assertFalse(result["entriesEnabled"])
        self.assertEqual(result["status"], "yielded_flat")
        self.assertEqual(result["selectedAccountId"], "frozen-paper")
        self.assert_lock_owned(False)

    def test_fresh_flat_controller_keeps_persisted_frozen_identity(self):
        self.controller.service = None
        self.controller._selected_account_id = None
        self.data.put_document(STATE_KEY, {"selectedAccountId": "frozen-paper", "entriesEnabled": False})
        result = self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(result["selectedAccountId"], "frozen-paper")
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "frozen-paper")
        self.assert_lock_owned(False)

    def test_store_failure_after_join_restores_protection_even_if_rollback_save_fails(self):
        original_save = self.data.put_document
        failed = False
        def failing_save(key, value):
            nonlocal failed
            failed = failed or value.get("yieldedFlat") is True
            if failed:
                raise RuntimeError("document unavailable")
            original_save(key, value)
        self.data.put_document = failing_save
        try:
            with self.assertRaisesRegex(RuntimeError, "document unavailable"):
                self.controller.close_if_flat(join_timeout=0)
            self.assertIs(self.controller.service, self.service)
            self.assertFalse(self.service.entries_enabled)
            self.assertTrue(self.controller.status()["workerRunning"])
            self.assert_lock_owned(True)
        finally:
            self.data.put_document = original_save

    def test_running_worker_finishes_without_mutex_join_deadlock(self):
        entered = threading.Event()
        self.service.cycle.side_effect = entered.set
        with self.controller._mutex:
            self.controller._start_worker()
        worker = self.controller._thread
        self.assertTrue(entered.wait(timeout=1))
        result = self.controller.close_if_flat(join_timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result["accountLockOwned"])
        self.assert_lock_owned(False)

    def test_fill_arriving_during_last_cycle_blocks_handoff_and_restores_protection(self):
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        self.controller._thread = worker
        self.ledger.net_fill_positions.side_effect = [{}, {"BTC/USDT:USDT": "0.0006"}]
        with self.assertRaises(DemoHandoffError) as refused:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(refused.exception.code, "demo_owned_exposure_unresolved")
        self.assertIs(self.controller.service, self.service)
        self.assertFalse(self.service.entries_enabled)
        self.assertTrue(self.controller.status()["workerRunning"])
        self.assert_lock_owned(True)


if __name__ == "__main__":
    unittest.main()
