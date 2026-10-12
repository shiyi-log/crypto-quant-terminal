"""Offline strategy switches retain one account lock and immutable ledgers."""
import copy
import fcntl
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from demo_runner_controller import DemoHandoffError, DemoRunnerController, STATE_KEY


class Documents:
    def __init__(self):
        self.items = {}
        self.writes = []

    def get_document(self, key, default=None):
        return copy.deepcopy(self.items.get(key, default))

    def put_document(self, key, value):
        self.items[key] = copy.deepcopy(value)
        self.writes.append((key, copy.deepcopy(value)))


class Ledger:
    def __init__(self, namespace, *, binding=None):
        self.virtual_account_key = namespace
        self.binding = copy.deepcopy(binding)
        self.net = {}
        self.pending = []
        self.orders = []
        self.fills = []
        self.safe = {"safeToSwitch": True, "pendingOrderCount": 0,
                     "nonflatLedgerPairCount": 0, "fillQuantityGapCount": 0}

    def get_binding(self):
        return copy.deepcopy(self.binding)

    def net_fill_positions(self):
        return self.net

    def pending_orders(self):
        return self.pending

    def runner_orders(self):
        return self.orders

    def runner_fills(self):
        return self.fills

    def account_reconciliation_status(self):
        return self.safe


class Service:
    def __init__(self, adapter, ledger, paper, config, **kwargs):
        self.adapter, self.store, self.config = adapter, ledger, config
        self.policy_hash = "policy:" + config.virtual_account_key
        self.entries_enabled = True
        self.cycle = Mock()
        self.start_calls = []
        self.extra_status = {}
        self.start_hook = None

    def start(self, *, entries_enabled):
        self.start_calls.append(entries_enabled)
        self.entries_enabled = entries_enabled
        if self.start_hook:
            self.start_hook()
        if self.store.binding is None:
            self.store.binding = {"strategy_id": self.config.strategy_id, "rule_hash": "frozen-rule",
                                  "execution_hash": "frozen-execution"}

    def stop(self, **kwargs):
        self.entries_enabled = False

    def enable_entries(self):
        self.entries_enabled = True

    def status(self):
        return {"running": True, "entries_enabled": self.entries_enabled,
                "status": "observing" if self.entries_enabled else "paused_flat",
                "forward_boundary_ms": 123, **self.extra_status}


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Documents()
        self.old = Ledger("demo_default", binding={"strategy_id": "old", "rule_hash": "old-frozen"})
        self.ledgers = {"demo_default": self.old}
        self.credentials = SimpleNamespace(api_key="synthetic-demo-key", secret="synthetic-demo-secret")
        self.fingerprint = hashlib.sha256(self.credentials.api_key.encode()).hexdigest()[:16]
        self.provider = Mock(return_value=self.credentials)
        self.adapter = Mock()
        self.adapter.fetch_positions.return_value = [{"signed_qty": "0", "pair": "BTC/USDT:USDT"}]
        self.adapter.fetch_open_orders.return_value = []
        self.adapter.fetch_position_mode.return_value = {"hedged": False}
        self.adapter_factory = Mock(return_value=self.adapter)
        self.paper = Mock()
        self.paper.account.return_value = {"execution": {"risk_sizing_mode": "entry_atr"}}
        self.created = []
        self.prepare_hook = None
        self.prepare_status = {}

        def ledger_factory(namespace):
            return self.ledgers.setdefault(namespace, Ledger(namespace))

        self.ledger_factory = Mock(side_effect=ledger_factory)

        def service_factory(adapter, ledger, paper, config, **kwargs):
            service = Service(adapter, ledger, paper, config, **kwargs)
            service.start_hook = self.prepare_hook
            service.extra_status = copy.deepcopy(self.prepare_status)
            self.created.append(service)
            return service

        patcher = patch("demo_runner_controller.DemoExecutionService", side_effect=service_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = self.make_controller()
        self.controller._credential_fingerprint = self.fingerprint
        config = SimpleNamespace(strategy_id="old", virtual_account_key="demo_default",
                                 max_notional_usdt="1000", poll_interval_ms=60_000)
        self.service = Service(self.adapter, self.old, self.paper, config)
        self.controller.service = self.service
        self.controller._selected_account_id = "old"
        self.controller._lock()
        self.controller._save()
        self.original_binding = copy.deepcopy(self.old.binding)
        self.addCleanup(self.release_controller)

    def make_controller(self):
        controller = DemoRunnerController(self.data, self.old, self.provider, self.adapter_factory,
            paper_source=self.paper, lock_path=Path(self.temp.name) / "demo.lock",
            ledger_factory=self.ledger_factory)
        # The worker lifecycle itself is exercised by handoff tests. These
        # tests explicitly control joins and candidate preparation boundaries.
        controller._start_worker = Mock()
        return controller

    def release_controller(self):
        self.controller._switch_persistence_unresolved = False
        self.old.net, self.old.pending, self.old.orders, self.old.fills = {}, [], [], []
        self.controller.close_if_flat(join_timeout=0)

    def assert_flock_retained(self):
        with self.controller.lock_path.open("a+") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(self.controller.status()["accountLockOwned"])

    def refused(self, code):
        with self.assertRaises(DemoHandoffError) as result:
            self.controller.switch("new", join_timeout=0)
        self.assertEqual(result.exception.code, code)
        self.assertIs(self.controller.service, self.service)
        self.assertIs(self.controller.demo_store, self.old)
        self.assertFalse(self.service.entries_enabled)
        self.assertFalse(self.controller._closing)
        self.assert_flock_retained()

    def test_flat_switch_creates_paused_namespace_and_never_changes_old_binding(self):
        self.old.orders = [{"client_id": "buy", "filled_quantity": "0.0006"},
                           {"client_id": "sell", "filled_quantity": "0.0006"}]
        self.old.fills = [{"client_id": "buy", "quantity": "0.0006"},
                          {"client_id": "sell", "quantity": "0.0006"}]
        before = copy.deepcopy((self.old.orders, self.old.fills))
        result = self.controller.switch("new", join_timeout=0)
        namespace = result["virtualAccountKey"]
        self.assertRegex(namespace, r"^demo_strategy_[0-9a-f]{32}$")
        self.assertEqual(result["selectedAccountId"], "new")
        self.assertFalse(result["entriesEnabled"])
        self.assertEqual(self.controller.service.start_calls, [False])
        self.assertEqual(self.data.items[STATE_KEY]["virtualAccountKey"], namespace)
        self.assertEqual(self.data.items[STATE_KEY]["credentialFingerprint"], self.fingerprint)
        self.assertEqual(self.old.binding, self.original_binding)
        self.assertEqual((self.old.orders, self.old.fills), before)
        self.controller._start_worker.assert_called_once()
        self.assert_flock_retained()

    def test_same_strategy_pause_and_resume_reuses_current_namespace(self):
        result = self.controller.switch("old", join_timeout=0)
        self.assertEqual(result["virtualAccountKey"], "demo_default")
        self.assertFalse(result["entriesEnabled"])
        self.controller.start("old")
        self.assertTrue(self.service.entries_enabled)
        self.ledger_factory.assert_not_called()
        self.adapter_factory.assert_not_called()

    def test_new_session_is_prepared_before_recovery_pointer_changes(self):
        def prepared():
            self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "old")
            self.assertEqual(self.data.items[STATE_KEY]["virtualAccountKey"], "demo_default")
            self.assertFalse(self.data.items[STATE_KEY]["entriesEnabled"])
            self.assert_flock_retained()
        self.prepare_hook = prepared
        self.controller.switch("new", join_timeout=0)

    def test_actual_nonzero_position_outside_supported_symbols_blocks_switch(self):
        self.adapter.fetch_positions.return_value = [{"signed_qty": "1", "pair": "SOLUSDT"}]
        self.refused("demo_account_positions_not_flat")
        self.ledger_factory.assert_not_called()

    def test_opposite_position_rows_cannot_cancel_into_apparent_flatness(self):
        self.adapter.fetch_positions.return_value = [
            {"signed_qty": "1", "pair": "BTC/USDT:USDT", "position_side": "LONG"},
            {"signed_qty": "-1", "pair": "BTC/USDT:USDT", "position_side": "SHORT"}]
        self.refused("demo_account_positions_not_flat")

    def test_hedge_mode_is_refused_even_with_zero_positions(self):
        self.adapter.fetch_position_mode.return_value = {"hedged": True}
        self.refused("demo_switch_exchange_check_failed")

    def test_unowned_actual_order_blocks_switch(self):
        self.adapter.fetch_open_orders.return_value = [{"client_id": "external-order"}]
        self.refused("demo_account_open_orders_unresolved")

    def test_each_fresh_exchange_query_failure_is_closed_and_redacted(self):
        for method in ("fetch_positions", "fetch_open_orders", "fetch_position_mode"):
            with self.subTest(method=method):
                getattr(self.adapter, method).side_effect = RuntimeError(self.credentials.secret)
                self.refused("demo_switch_exchange_check_failed")
                self.assertNotIn(self.credentials.secret, str(self.controller.status()))
                getattr(self.adapter, method).side_effect = None

    def test_missing_nonfinite_or_malformed_position_snapshot_blocks_switch(self):
        for positions in (None, {}, [{}], [{"signed_qty": "NaN"}], [{"signed_qty": "Infinity"}],
                          [{"signed_qty": None}], [{"signed_qty": True}]):
            with self.subTest(positions=positions):
                self.adapter.fetch_positions.return_value = positions
                self.refused("demo_switch_exchange_check_failed")

    def test_any_other_namespace_quantity_problem_blocks_switch(self):
        for field in ("pendingOrderCount", "nonflatLedgerPairCount", "fillQuantityGapCount"):
            with self.subTest(field=field):
                self.old.safe = {"safeToSwitch": False, field: 1}
                self.refused("demo_account_reconciliation_unresolved")
        self.adapter_factory.assert_not_called()

    def test_owned_unknown_submission_or_exposure_blocks_before_exchange(self):
        self.old.pending = [{"status": "unknown"}]
        self.refused("demo_owned_orders_unresolved")
        self.old.pending = []
        self.old.net = {"BTC/USDT:USDT": "0.0006"}
        self.refused("demo_owned_exposure_unresolved")
        self.adapter_factory.assert_not_called()

    def test_filled_order_without_complete_quantities_blocks_switch(self):
        self.old.orders = [{"client_id": "entry", "filled_quantity": "0.0006"}]
        self.old.fills = [{"client_id": "entry", "quantity": "0.0002"}]
        self.refused("demo_owned_fills_unresolved")
        self.adapter_factory.assert_not_called()

    def test_late_owned_fill_after_join_blocks_switch_and_retains_old_protection(self):
        self.old.net_fill_positions = Mock(side_effect=[{}, {"BTC/USDT:USDT": "0.0006"}])
        self.refused("demo_owned_exposure_unresolved")
        self.old.net_fill_positions = Mock(return_value={})
        self.controller._start_worker.assert_called_once()

    def test_join_timeout_retains_lock_and_paused_old_worker(self):
        worker = Mock()
        worker.is_alive.return_value = True
        self.controller._thread = worker
        self.refused("demo_worker_busy")
        worker.join.assert_called_once_with(timeout=0)
        worker.is_alive.return_value = False
        self.adapter_factory.assert_not_called()

    def test_join_does_not_hold_mutex_and_concurrent_start_is_refused(self):
        joined = threading.Event()
        worker = Mock()
        worker.is_alive.return_value = False
        def joining(**kwargs):
            acquired = threading.Event()
            def competing():
                with self.controller._mutex:
                    acquired.set()
                with self.assertRaises(DemoHandoffError) as refused:
                    self.controller.start("old")
                self.assertEqual(refused.exception.code, "demo_handoff_in_progress")
            contender = threading.Thread(target=competing)
            contender.start()
            contender.join(timeout=1)
            self.assertFalse(contender.is_alive())
            self.assertTrue(acquired.is_set())
            self.assert_flock_retained()
            joined.set()
        worker.join.side_effect = joining
        self.controller._thread = worker
        self.controller.switch("new", join_timeout=1)
        self.assertTrue(joined.is_set())

    def test_invalid_frozen_account_does_not_pause_current_strategy(self):
        self.paper.account.return_value = {"execution": {"risk_sizing_mode": "entry_pair_atr"}}
        with self.assertRaises(ValueError):
            self.controller.switch("new", join_timeout=0)
        self.assertTrue(self.service.entries_enabled)
        self.assert_flock_retained()

    def test_changed_loaded_key_is_rejected_before_any_exchange_request(self):
        self.provider.return_value = SimpleNamespace(api_key="different-key", secret="different-secret")
        self.refused("demo_credential_identity_mismatch")
        self.adapter_factory.assert_not_called()

    def test_binding_credential_identity_is_also_enforced(self):
        self.old.binding["credential_fingerprint"] = "another-account"
        self.refused("demo_credential_identity_mismatch")
        self.adapter_factory.assert_not_called()

    def test_prepared_service_error_keeps_old_pointer_and_worker(self):
        self.prepare_status = {"status": "reconciliation_error", "last_error": "DemoExchangeError"}
        self.refused("demo_switch_exchange_check_failed")
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "old")
        self.assertEqual(len(self.created), 1)

    def test_namespace_collision_never_borrows_an_old_ledger(self):
        self.ledger_factory.side_effect = lambda namespace: self.old
        self.refused("demo_switch_namespace_collision")

    def test_pointer_commit_failure_restores_paused_old_pointer_and_worker(self):
        save = self.data.put_document
        def failing_save(key, value):
            if value.get("selectedAccountId") == "new":
                raise RuntimeError("candidate pointer commit failed")
            save(key, value)
        self.data.put_document = failing_save
        self.refused("demo_switch_persistence_failed")
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "old")
        self.assertFalse(self.data.items[STATE_KEY]["entriesEnabled"])
        self.controller._start_worker.assert_called_once()

    def test_committed_pointer_with_lost_ack_uses_readback_and_new_paused_worker(self):
        save = self.data.put_document
        def acknowledged_late(key, value):
            save(key, value)
            if value.get("selectedAccountId") == "new":
                raise RuntimeError("commit acknowledgement failed")
        self.data.put_document = acknowledged_late
        result = self.controller.switch("new", join_timeout=0)
        self.assertEqual(result["selectedAccountId"], "new")
        self.assertFalse(result["entriesEnabled"])
        self.assertIn("readback", result["controllerError"])
        self.assertEqual(self.data.items[STATE_KEY]["virtualAccountKey"], result["virtualAccountKey"])
        self.assert_flock_retained()
        self.data.put_document = save

    def test_worker_start_failure_after_commit_never_enables_entries_without_protection(self):
        self.controller._start_worker.side_effect = RuntimeError("worker unavailable")
        with self.assertRaises(RuntimeError):
            self.controller.switch("new", join_timeout=0)
        candidate = self.controller.service
        self.assertEqual(candidate.config.strategy_id, "new")
        self.assertFalse(candidate.entries_enabled)
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "new")
        self.assertFalse(self.data.items[STATE_KEY]["entriesEnabled"])
        self.assert_flock_retained()
        with self.assertRaises(RuntimeError):
            self.controller.start("new")
        self.assertFalse(candidate.entries_enabled)
        ready = False
        def restored_worker():
            nonlocal ready
            ready = True
        def enable_entries():
            self.assertTrue(ready, "the worker must be available before entry submission")
            candidate.entries_enabled = True
        candidate.enable_entries = Mock(side_effect=enable_entries)
        self.controller._start_worker.side_effect = restored_worker
        self.controller.start("new")
        candidate.enable_entries.assert_called_once()
        self.assertTrue(candidate.entries_enabled)

    def test_global_quantity_gap_appearing_while_new_session_prepares_blocks_commit(self):
        self.old.account_reconciliation_status = Mock(side_effect=[
            {"safeToSwitch": True}, {"safeToSwitch": False, "fillQuantityGapCount": 1}])
        self.refused("demo_account_reconciliation_unresolved")
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "old")

    def test_unknown_commit_outcome_preserves_old_protection_without_overwriting_pointer(self):
        save, read = self.data.put_document, self.data.get_document
        committed = False
        def ambiguous_save(key, value):
            nonlocal committed
            save(key, value)
            if value.get("selectedAccountId") == "new":
                committed = True
                raise RuntimeError("commit acknowledgement failed")
        def unavailable_read(key, default=None):
            if committed:
                raise RuntimeError("readback unavailable")
            return read(key, default)
        self.data.put_document, self.data.get_document = ambiguous_save, unavailable_read
        self.refused("demo_switch_persistence_unresolved")
        self.assertTrue(self.controller.status()["switchPersistenceUnresolved"])
        self.assertEqual(self.data.items[STATE_KEY]["selectedAccountId"], "new")
        writes = len(self.data.writes)
        self.controller._save()
        self.assertEqual(len(self.data.writes), writes)
        with self.assertRaises(DemoHandoffError) as refused:
            self.controller.start("old")
        self.assertEqual(refused.exception.code, "demo_switch_persistence_unresolved")
        self.data.put_document, self.data.get_document = save, read

    def test_restart_restores_new_namespace_only_for_paused_protection(self):
        result = self.controller.switch("new", join_timeout=0)
        self.controller.close_if_flat(join_timeout=0)
        # Simulate an ordinary process stop rather than a yielded-flat handoff.
        self.data.items[STATE_KEY]["yieldedFlat"] = False
        restarted = self.make_controller()
        try:
            resumed = restarted.resume_protection()
            self.assertEqual(resumed["virtualAccountKey"], result["virtualAccountKey"])
            self.assertEqual(resumed["selectedAccountId"], "new")
            self.assertFalse(resumed["entriesEnabled"])
            self.assertEqual(restarted.service.start_calls, [False])
            self.assertIs(restarted.demo_store, self.ledgers[result["virtualAccountKey"]])
        finally:
            restarted.close_if_flat(join_timeout=0)

    def test_legacy_restart_without_namespace_uses_demo_default(self):
        self.controller.service = None
        self.data.put_document(STATE_KEY, {"selectedAccountId": "old", "entriesEnabled": True})
        result = self.controller.resume_protection()
        self.assertEqual(result["virtualAccountKey"], "demo_default")
        self.assertFalse(result["entriesEnabled"])
        self.assertEqual(self.controller.service.start_calls, [False])

    def test_yielded_new_session_does_not_restart_or_lose_selection(self):
        result = self.controller.switch("new", join_timeout=0)
        self.controller.close_if_flat(join_timeout=0)
        restarted = self.make_controller()
        resumed = restarted.resume_protection()
        self.assertEqual(resumed["virtualAccountKey"], result["virtualAccountKey"])
        self.assertEqual(resumed["selectedAccountId"], "new")
        self.assertFalse(resumed["accountLockOwned"])
        self.assertIsNone(restarted.service)


if __name__ == "__main__":
    unittest.main()
