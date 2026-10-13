"""Shared Demo membership preserves identities and resumes risk with gates shut."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from demo_runner_controller import (
    DemoHandoffError, DemoRunnerController, SHARED_STATE_KEY, STATE_KEY,
)


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
    def __init__(self, namespace, ledgers):
        self.virtual_account_key = namespace
        self.ledgers = ledgers
        self.binding = None
        self.net, self.orders, self.fills, self.pending = {}, [], [], []

    def get_binding(self):
        return copy.deepcopy(self.binding)

    def net_fill_positions(self):
        return self.net

    def runner_orders(self):
        return self.orders

    def runner_fills(self):
        return self.fills

    def pending_orders(self):
        return self.pending

    def account_reconciliation_status(self):
        return {"safeToSwitch": all(not ledger.net and not ledger.pending for ledger in self.ledgers.values())}

    def shared_account_ownership(self):
        return {"orders": [dict(order, virtual_account_key=namespace)
                           for namespace, ledger in self.ledgers.items() for order in ledger.orders],
                "net_positions": {namespace: ledger.net for namespace, ledger in self.ledgers.items()}}


class Service:
    def __init__(self, adapter, ledger, paper, config, **kwargs):
        self.adapter, self.store, self.config = adapter, ledger, config
        self.policy_hash = "policy:" + config.virtual_account_key
        self.entries_enabled = False
        self.start_calls = []
        self.cycle = Mock()
        self.start_hook = None

    def start(self, *, entries_enabled):
        self.start_calls.append(entries_enabled)
        if self.start_hook:
            self.start_hook()
        if self.store.binding is None:
            self.store.binding = {"strategy_id": self.config.strategy_id,
                                  "forward_boundary_ms": 456, "rule_hash": "frozen-rule",
                                  "execution_hash": "frozen-execution"}
        self.entries_enabled = entries_enabled

    def enable_entries(self):
        self.entries_enabled = True
        self.cycle()

    def stop(self, **kwargs):
        self.entries_enabled = False

    def status(self):
        binding = self.store.binding or {}
        return {"running": True, "entries_enabled": self.entries_enabled,
                "status": "observing" if self.entries_enabled else "paused_flat",
                "forward_boundary_ms": binding.get("forward_boundary_ms")}


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Documents()
        self.ledgers = {}
        self.ledger_factory = Mock(side_effect=self.make_ledger)
        self.primary_ledger = self.make_ledger("demo_strategy_existing_s35")
        self.primary_ledger.binding = {"strategy_id": "s35", "rule_hash": "s35-rule",
                                       "execution_hash": "s35-execution", "forward_boundary_ms": 123}
        self.paper = Mock()
        self.paper.account.return_value = {"execution": {"execution_enabled": True,
                                                         "risk_sizing_mode": "entry_atr"}}
        self.credentials = SimpleNamespace(api_key="synthetic-shared-key", secret="synthetic-shared-secret")
        self.provider = Mock(return_value=self.credentials)
        self.adapter = Mock()
        self.adapter.fetch_positions.return_value = []
        self.adapter.fetch_open_orders.return_value = []
        self.adapter.fetch_position_mode.return_value = {"hedged": False}
        self.created = []
        self.start_hook = None
        self.patcher = patch("demo_runner_controller.DemoExecutionService", side_effect=self.make_service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.controller = self.make_controller()
        self.primary = Service(self.adapter, self.primary_ledger, self.paper,
            SimpleNamespace(strategy_id="s35", virtual_account_key=self.primary_ledger.virtual_account_key,
                            max_notional_usdt="1000", poll_interval_ms=60_000))
        self.primary.entries_enabled = True
        self.controller.service = self.primary
        self.controller._credential_fingerprint = hashlib.sha256(self.credentials.api_key.encode()).hexdigest()[:16]
        self.controller._lock()
        self.controller._save()
        self.controllers = [self.controller]
        self.addCleanup(self.release)

    def make_ledger(self, namespace):
        if namespace not in self.ledgers:
            self.ledgers[namespace] = Ledger(namespace, self.ledgers)
        return self.ledgers[namespace]

    def make_service(self, adapter, ledger, paper, config, **kwargs):
        service = Service(adapter, ledger, paper, config, **kwargs)
        service.start_hook = self.start_hook
        self.created.append(service)
        return service

    def make_controller(self):
        controller = DemoRunnerController(self.data, self.primary_ledger, self.provider,
            Mock(return_value=self.adapter), paper_source=self.paper,
            lock_path=Path(self.temp.name) / "demo.lock", ledger_factory=self.ledger_factory)
        controller._start_worker = Mock()
        return controller

    def release(self):
        for controller in self.controllers:
            controller._shared_persistence_unresolved = False
            controller._switch_persistence_unresolved = False
            controller._restore_incomplete = False
            controller._session_errors.clear()
            for ledger in self.ledgers.values():
                ledger.net, ledger.pending, ledger.orders, ledger.fills = {}, [], [], []
            controller._shared_records = {service.config.virtual_account_key: controller._document(service)
                                          for service in controller._services()}
            for service in controller._services():
                service.entries_enabled = False
            if controller._file_lock:
                controller.close_if_flat(join_timeout=1)

    def attach(self):
        result = self.controller.attach("s07")
        namespace = next(member["virtualAccountKey"] for member in result["parallelSessions"]
                         if member["selectedAccountId"] == "s07")
        return namespace, self.controller._parallel_sessions[namespace]

    def restart(self):
        # Ordinary process termination keeps the registry's non-yielded state.
        self.controller._file_lock.close()
        self.controller._file_lock = None
        restarted = self.make_controller()
        self.controllers.append(restarted)
        return restarted

    def test_attach_preserves_primary_history_and_persists_before_candidate_start(self):
        old = copy.deepcopy(self.primary_ledger.binding)
        def verify_start():
            records = self.data.items[SHARED_STATE_KEY]["sessions"]
            self.assertEqual({record["selectedAccountId"] for record in records}, {"s35", "s07"})
            self.assertEqual(len(self.controller._coordinator()._members), 2)
        self.start_hook = verify_start
        namespace, companion = self.attach()
        self.assertRegex(namespace, r"^demo_strategy_[0-9a-f]{32}$")
        self.assertIs(self.controller.service, self.primary)
        self.assertEqual(self.primary_ledger.binding, old)
        self.assertEqual(self.data.items[STATE_KEY]["forwardBoundaryMs"], 123)
        self.assertTrue(self.primary.entries_enabled)
        self.assertFalse(companion.entries_enabled)
        self.assertEqual(companion.start_calls, [False])
        self.controller.start_session(namespace)
        self.assertTrue(companion.entries_enabled)

    def test_repeat_attach_is_idempotent_and_stop_only_closes_own_gate(self):
        namespace, companion = self.attach()
        self.controller.start_session(namespace)
        count = len(self.created)
        self.controller.attach("s07")
        self.assertEqual(len(self.created), count)
        self.controller.stop_session(namespace)
        self.assertTrue(self.primary.entries_enabled)
        self.assertFalse(companion.entries_enabled)
        self.controller.stop()
        self.assertFalse(self.primary.entries_enabled)

    def test_healthy_owned_position_can_attach_but_foreign_exposure_cannot(self):
        pair = "BTC/USDT:USDT"
        self.primary_ledger.net = {pair: "0.1"}
        self.adapter.fetch_positions.return_value = [{"pair": pair, "signed_qty": "0.1"}]
        self.attach()
        self.adapter.fetch_positions.return_value = [{"pair": pair, "signed_qty": "0.2"}]
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.attach("s02")
        self.assertEqual(error.exception.code, "demo_shared_account_unreconciled")
        self.assertEqual(len(self.controller._services()), 2)

    def test_restart_registers_all_members_before_first_cycle_and_never_resumes_entries(self):
        namespace, companion = self.attach()
        self.controller.start_session(namespace)
        restarted = self.restart()
        def verify_all_registered():
            self.assertEqual(len(restarted._coordinator()._members), 2)
        self.start_hook = verify_all_registered
        result = restarted.resume_protection()
        self.assertFalse(result["allEntriesEnabled"])
        self.assertEqual(len(result["parallelSessions"]), 2)
        self.assertTrue(all(not service.entries_enabled for service in restarted._services()))
        self.assertEqual(restarted.service.store.binding["forward_boundary_ms"], 123)
        self.assertEqual(restarted._parallel_sessions[namespace].start_calls, [False])

    def test_existing_namespace_is_adopted_without_rebinding_or_boundary_change(self):
        old = self.make_ledger("demo_strategy_old_s07")
        old.binding = {"strategy_id": "s07", "forward_boundary_ms": 17,
                       "rule_hash": "old-rule", "execution_hash": "old-execution"}
        before = copy.deepcopy(old.binding)
        result = self.controller.adopt_existing_session(old.virtual_account_key)
        companion = self.controller._parallel_sessions[old.virtual_account_key]
        self.assertEqual(companion.status()["forward_boundary_ms"], 17)
        self.assertEqual(old.binding, before)
        self.assertFalse(companion.entries_enabled)
        self.assertEqual(len(result["parallelSessions"]), 2)

    def test_known_registry_failure_never_overwrites_pointer_or_starts_candidate(self):
        namespace, companion = self.attach()
        before = copy.deepcopy(self.data.items[SHARED_STATE_KEY])
        save = self.data.put_document
        def failed_registry(key, value):
            if key == SHARED_STATE_KEY:
                raise RuntimeError("synthetic database outage")
            save(key, value)
        self.data.put_document = failed_registry
        try:
            with self.assertRaises(DemoHandoffError) as error:
                self.controller.attach("s02")
            self.assertEqual(error.exception.code, "demo_shared_persistence_failed")
            self.assertEqual(self.data.items[SHARED_STATE_KEY], before)
            self.assertEqual(self.created[-1].start_calls, [])
            self.assertEqual(len(self.controller._services()), 2)
        finally:
            self.data.put_document = save

    def test_partial_restore_failure_remains_in_registry_and_blocks_all_entries_until_retry(self):
        namespace, companion = self.attach()
        restarted = self.restart()
        original = restarted._service
        def fail_companion(account_id, ledger, adapter):
            if account_id == "s07":
                raise RuntimeError("synthetic initialization failure")
            return original(account_id, ledger, adapter)
        restarted._service = fail_companion
        result = restarted.resume_protection()
        self.assertTrue(result["sharedRecoveryIncomplete"])
        self.assertIn(namespace, {record["virtualAccountKey"] for record in self.data.items[SHARED_STATE_KEY]["sessions"]})
        with self.assertRaises(DemoHandoffError) as error:
            restarted.start("s35")
        self.assertEqual(error.exception.code, "demo_shared_recovery_incomplete")
        self.assertFalse(restarted.service.entries_enabled)
        restarted._service = original
        self.assertFalse(restarted.resume_protection()["sharedRecoveryIncomplete"])
        restarted.start_session(namespace)
        self.assertTrue(restarted._parallel_sessions[namespace].entries_enabled)

    def test_failed_new_member_initialization_stays_registered_and_pauses_every_gate(self):
        def failed_start():
            raise RuntimeError("synthetic start failure")
        self.start_hook = failed_start
        with self.assertRaises(RuntimeError):
            self.controller.attach("s07")
        records = self.data.items[SHARED_STATE_KEY]["sessions"]
        namespace = next(record["virtualAccountKey"] for record in records
                         if record["selectedAccountId"] == "s07")
        self.assertIn(namespace, self.controller._parallel_sessions)
        self.assertFalse(self.primary.entries_enabled)
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.start("s35")
        self.assertEqual(error.exception.code, "demo_shared_recovery_incomplete")
        self.controller._parallel_sessions[namespace].start_hook = None
        result = self.controller.resume_protection()
        self.assertFalse(result["sharedRecoveryIncomplete"])
        self.assertFalse(result["allEntriesEnabled"])

    def test_lost_registry_acknowledgement_uses_exact_readback_without_duplicate_member(self):
        save = self.data.put_document
        def lost_acknowledgement(key, value):
            save(key, value)
            if key == SHARED_STATE_KEY:
                raise RuntimeError("synthetic lost acknowledgement")
        self.data.put_document = lost_acknowledgement
        try:
            namespace, companion = self.attach()
            self.assertEqual(companion.start_calls, [False])
            self.assertEqual(len(self.controller._services()), 2)
            self.assertEqual(len(self.data.items[SHARED_STATE_KEY]["sessions"]), 2)
            self.assertIn(namespace, self.controller._coordinator()._members)
        finally:
            self.data.put_document = save

    def test_unknown_registry_commit_keeps_paused_members_without_overwriting_authority(self):
        save, read = self.data.put_document, self.data.get_document
        committed = False
        def lost_acknowledgement(key, value):
            nonlocal committed
            save(key, value)
            if key == SHARED_STATE_KEY:
                committed = True
                raise RuntimeError("synthetic lost acknowledgement")
        def read_unavailable(key, default=None):
            if key == SHARED_STATE_KEY and committed:
                raise RuntimeError("synthetic read outage")
            return read(key, default)
        self.data.put_document, self.data.get_document = lost_acknowledgement, read_unavailable
        try:
            with self.assertRaises(DemoHandoffError) as error:
                self.controller.attach("s07")
            self.assertEqual(error.exception.code, "demo_shared_persistence_unresolved")
            self.assertEqual(len(self.controller._services()), 2)
            self.assertTrue(all(not service.entries_enabled for service in self.controller._services()))
            before = copy.deepcopy(self.data.items[SHARED_STATE_KEY])
            writes = len([key for key, value in self.data.writes if key == SHARED_STATE_KEY])
            self.controller._save()
            self.assertEqual(self.data.items[SHARED_STATE_KEY], before)
            self.assertEqual(len([key for key, value in self.data.writes if key == SHARED_STATE_KEY]), writes)
            with self.assertRaises(DemoHandoffError) as error:
                self.controller.start("s35")
            self.assertEqual(error.exception.code, "demo_shared_persistence_unresolved")
        finally:
            self.data.put_document, self.data.get_document = save, read

    def test_yield_paused_flat_members_and_explicit_start_restores_all_same_namespaces(self):
        namespace, companion = self.attach()
        self.controller.stop()
        result = self.controller.close_if_flat(join_timeout=0)
        self.assertFalse(result["accountLockOwned"])
        self.assertTrue(self.data.items[SHARED_STATE_KEY]["yieldedFlat"])
        restarted = self.make_controller()
        self.controllers.append(restarted)
        result = restarted.resume_protection()
        self.assertFalse(result["accountLockOwned"])
        self.assertIsNone(restarted.service)
        restarted.start("s35")
        self.assertEqual(restarted.service.config.virtual_account_key, self.primary_ledger.virtual_account_key)
        self.assertEqual(restarted.service.status()["forward_boundary_ms"], 123)
        self.assertIn(namespace, restarted._parallel_sessions)
        self.assertTrue(restarted.service.entries_enabled)
        self.assertFalse(restarted._parallel_sessions[namespace].entries_enabled)

    def test_running_or_nonflat_companion_prevents_primary_switch_and_yield(self):
        namespace, companion = self.attach()
        self.controller.start_session(namespace)
        for action in (lambda: self.controller.switch("s02", join_timeout=0),
                       lambda: self.controller.close_if_flat(join_timeout=0)):
            with self.assertRaises(DemoHandoffError) as error:
                action()
            self.assertEqual(error.exception.code, "demo_parallel_entries_enabled")
            self.assertTrue(self.primary.entries_enabled)
        self.controller.stop_session(namespace)
        companion.store.net = {"ETH/USDT:USDT": "1"}
        with self.assertRaises(DemoHandoffError) as error:
            self.controller.close_if_flat(join_timeout=0)
        self.assertEqual(error.exception.code, "demo_owned_exposure_unresolved")
        self.assertTrue(self.controller.status()["accountLockOwned"])

    def test_flat_primary_switch_retains_paused_companion_and_atomic_shared_primary(self):
        namespace, companion = self.attach()
        result = self.controller.switch("s02", join_timeout=0)
        self.assertIs(self.controller._parallel_sessions[namespace], companion)
        self.assertEqual(self.data.items[SHARED_STATE_KEY]["primaryVirtualAccountKey"], result["virtualAccountKey"])
        self.assertEqual({record["selectedAccountId"] for record in self.data.items[SHARED_STATE_KEY]["sessions"]},
                         {"s02", "s07"})
        self.assertEqual(self.primary_ledger.binding["strategy_id"], "s35")

    def test_one_worker_cycles_all_members_under_shared_lock_even_when_one_raises(self):
        namespace, companion = self.attach()
        observed = []
        entered = threading.Event()
        def primary_cycle():
            self.assertTrue(self.controller._account_cycle_lock._is_owned())
            observed.append("s35")
            raise RuntimeError("synthetic primary failure")
        def companion_cycle():
            self.assertTrue(self.controller._account_cycle_lock._is_owned())
            observed.append("s07")
            entered.set()
        self.primary.cycle.side_effect = primary_cycle
        companion.cycle.side_effect = companion_cycle
        self.controller._start_worker = DemoRunnerController._start_worker.__get__(self.controller)
        with self.controller._mutex:
            self.controller._start_worker()
            first = self.controller._thread
            self.controller._start_worker()
            self.assertIs(self.controller._thread, first)
        self.assertTrue(entered.wait(timeout=1))
        self.controller._halt_worker.set()
        self.controller._wake.set()
        first.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertEqual(observed[:2], ["s35", "s07"])


if __name__ == "__main__":
    unittest.main()
