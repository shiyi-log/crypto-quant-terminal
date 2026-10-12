"""Frontend lifecycle for one Demo session, with protected restart recovery."""
from __future__ import annotations

import fcntl
import hashlib
from decimal import Decimal
from pathlib import Path
import threading
import time
import uuid

from demo_execution_service import DemoExecutionService, DemoRunnerConfig, PaperEventSource

STATE_KEY = "binance_demo_runner_controller.json"


class DemoHandoffError(RuntimeError):
    """A fixed-code refusal which leaves the account lock owned."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class DemoRunnerController:
    def __init__(self, data_store, demo_store, credential_provider, adapter_factory=None,
                 *, paper_source=None, lock_path=None, clock_ms=None, ledger_factory=None):
        self.data_store, self.demo_store = data_store, demo_store
        self.credential_provider = credential_provider
        if adapter_factory is None:
            from binance_demo_adapter import BinanceDemoAdapter
            adapter_factory = BinanceDemoAdapter
        self.adapter_factory = adapter_factory
        self.paper = paper_source or PaperEventSource(data_store, clock_ms=clock_ms)
        self.lock_path = Path(lock_path or Path(__file__).parent / "database/runtime/binance_demo_runner.lock")
        self.clock_ms = clock_ms
        if ledger_factory is None:
            from demo_store import DemoStore
            def ledger_factory(namespace):
                return DemoStore(data_store, virtual_account_key=namespace)
        self.ledger_factory = ledger_factory
        self.service = None
        self._thread = None
        self._workers = set()
        self._wake = threading.Event()
        self._mutex = threading.RLock()
        self._file_lock = None
        self._error = None
        self._recovery_mode = False
        self._selected_account_id = None
        self._halt_worker = threading.Event()
        self._closing = False
        self._yielded_flat = False
        self._credential_fingerprint = None
        self._switch_persistence_unresolved = False

    def _lock(self):
        if self._file_lock:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("another Demo controller owns this account") from None
        self._file_lock = handle

    def _save(self):
        # Serialize snapshot plus commit with stop/start so a delayed worker
        # save cannot overwrite a newer paused-entry state.
        with self._mutex:
            if self.service and not self._switch_persistence_unresolved:
                self.data_store.put_document(STATE_KEY, self._document(self.service))

    def _document(self, service):
        state = service.status()
        return {"selectedAccountId": service.config.strategy_id,
                "virtualAccountKey": service.config.virtual_account_key,
                "maxNotionalUsdt": service.config.max_notional_usdt,
                "demoPolicyHash": service.policy_hash,
                "forwardBoundaryMs": state.get("forward_boundary_ms"),
                "credentialFingerprint": self._credential_fingerprint,
                "entriesEnabled": state["entries_enabled"], "yieldedFlat": False,
                "status": state, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"}

    def _restore_active_store(self, persisted):
        namespace = persisted.get("virtualAccountKey", "demo_default")
        if namespace != self.demo_store.virtual_account_key:
            self.demo_store = self.ledger_factory(namespace)
        self._credential_fingerprint = persisted.get("credentialFingerprint")

    def _adapter(self):
        credentials = self.credential_provider()
        if credentials is None:
            raise RuntimeError("Demo credentials are not configured")
        fingerprint = hashlib.sha256(credentials.api_key.encode()).hexdigest()[:16]
        persisted = self.data_store.get_document(STATE_KEY, {})
        binding = self.demo_store.get_binding() or {}
        expected = (self._credential_fingerprint, persisted.get("credentialFingerprint"),
                    binding.get("credential_fingerprint"))
        if any(value is not None and value != fingerprint for value in expected):
            del credentials
            raise DemoHandoffError("demo_credential_identity_mismatch",
                                   "Demo session belongs to another API identity")
        self._credential_fingerprint = fingerprint
        adapter = self.adapter_factory(api_key=credentials.api_key, api_secret=credentials.secret,
                                       allow_trading=True)
        del credentials
        return adapter

    def _validate_account(self, account_id):
        account = self.paper.account(str(account_id))
        if (account["execution"].get("execution_enabled") is False
                or account["execution"].get("risk_sizing_mode") != "entry_atr"):
            raise ValueError("Demo requires an enabled frozen single-leg ATR account")

    def _service(self, account_id, ledger, adapter):
        config = DemoRunnerConfig(str(account_id),
            virtual_account_key=ledger.virtual_account_key, trading_enabled=True)
        return DemoExecutionService(adapter, ledger, self.paper, config, clock_ms=self.clock_ms)

    def _create(self, account_id, *, entries_enabled):
        self._lock()
        self._yielded_flat = False
        self._validate_account(account_id)
        binding = self.demo_store.get_binding()
        if binding and binding.get("strategy_id") != account_id:
            raise ValueError("Demo account is permanently bound to another paper identity")
        self._selected_account_id = str(account_id)
        adapter = self._adapter()
        # Save recovery identity before any network call can create exposure.
        self.data_store.put_document(STATE_KEY, {"selectedAccountId": str(account_id),
            "virtualAccountKey": self.demo_store.virtual_account_key, "maxNotionalUsdt": "1000",
            "credentialFingerprint": self._credential_fingerprint,
            "entriesEnabled": False, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"})
        service = self._service(account_id, self.demo_store, adapter)
        service.start(entries_enabled=entries_enabled)
        self.service = service
        self._save()
        self._start_worker()

    def _start_worker(self):
        """Called under the controller mutex; keep one protection worker."""
        if self._thread is None or not self._thread.is_alive():
            self._halt_worker.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="binance-demo-execution", daemon=True)
            self._workers.add(self._thread)
            try:
                self._thread.start()
            except Exception:
                self._workers.discard(self._thread)
                self._thread = None
                raise

    def _require_not_closing(self):
        if self._closing:
            raise DemoHandoffError("demo_handoff_in_progress", "Demo flat handoff is in progress")
        if self._switch_persistence_unresolved:
            raise DemoHandoffError("demo_switch_persistence_unresolved",
                                   "Demo recovery pointer must be reconciled before changing entries")

    def start(self, account_id):
        """Invoked by the user's frontend start action; never a live start."""
        with self._mutex:
            self._require_not_closing()
            self._error = None
            self._recovery_mode = False
            if self.service:
                if self.service.config.strategy_id != account_id:
                    raise ValueError("Demo account is permanently bound to the selected frozen identity")
                # A prior worker-start failure can leave a legally persisted,
                # paused service. Establish continuous protection before an
                # explicit start can synchronously submit its next entry.
                self._start_worker()
                self.service.enable_entries()
                self._save()
            else:
                persisted = self.data_store.get_document(STATE_KEY, {})
                if persisted.get("selectedAccountId"):
                    self._restore_active_store(persisted)
                self._create(account_id, entries_enabled=True)
            self._wake.set()
            return self.status()

    def stop(self, *, flatten=False):
        """Pauses new entries; polling remains active for fills and owned exits."""
        with self._mutex:
            self._require_not_closing()
            if self.service:
                self.service.stop(flatten=flatten)
                self._save()
                self._wake.set()
            return self.status()

    def resume_protection(self):
        """On application restart: only reconcile/protect, never resume entries."""
        with self._mutex:
            self._require_not_closing()
            persisted = self.data_store.get_document(STATE_KEY, {})
            account_id = persisted.get("selectedAccountId")
            if account_id and not self.service:
                self._restore_active_store(persisted)
            if account_id and persisted.get("yieldedFlat") is True and not self.service:
                self._selected_account_id = str(account_id)
                self._yielded_flat = True
                return self.status()
            if not account_id or self.service:
                return self.status()
            self._recovery_mode = True
            self._selected_account_id = str(account_id)
            try:
                self._create(account_id, entries_enabled=False)
            except Exception as exc:
                self._error = "Demo recovery failed: " + type(exc).__name__
                self.data_store.put_document(STATE_KEY, {**persisted, "entriesEnabled": False,
                    "recoveryMode": True, "recoveryError": self._error})
            return self.status()

    def status(self):
        with self._mutex:
            state = self.service.status() if self.service else {"running": False, "entries_enabled": False,
                "status": "yielded_flat" if self._yielded_flat else "not_started",
                "environment": "binance_demo", "live_enabled": False}
            return {**state, "selectedAccountId": self.service.config.strategy_id if self.service else self._selected_account_id,
                    "virtualAccountKey": self.demo_store.virtual_account_key,
                    "entriesEnabled": state["entries_enabled"], "recoveryMode": self._recovery_mode,
                    "controllerError": self._error,
                    "workerRunning": bool((self._thread and self._thread.is_alive())
                                          or any(worker.is_alive() for worker in self._workers)),
                    "accountLockOwned": self._file_lock is not None,
                    "handoffInProgress": self._closing,
                    "switchPersistenceUnresolved": self._switch_persistence_unresolved}

    def _loop(self):
        try:
            while self.service is not None and not self._halt_worker.is_set():
                service = self.service
                # Do not hold the controller mutex around HTTP requests.
                try:
                    service.cycle()
                    self._save()
                except Exception as exc:
                    self._error = "Demo polling failed: " + type(exc).__name__
                self._wake.wait(service.config.poll_interval_ms / 1000)
                self._wake.clear()
        finally:
            with self._mutex:
                if self._thread is threading.current_thread():
                    self._thread = None
                    # A timed-out handoff cancels the halt request. If this
                    # worker had already decided to exit, restore protection.
                    if self.service is not None and not self._halt_worker.is_set():
                        self._start_worker()

    def _assert_owned_flat(self):
        positions = self.demo_store.net_fill_positions()
        if any(Decimal(str(qty)) != 0 for qty in positions.values()):
            raise DemoHandoffError("demo_owned_exposure_unresolved",
                                   "cannot stop Demo protection with owned exposure")
        if self.demo_store.pending_orders():
            raise DemoHandoffError("demo_owned_orders_unresolved",
                                   "cannot stop Demo reconciliation with unresolved orders")
        quantities = {}
        for fill in self.demo_store.runner_fills():
            client_id = fill["client_id"]
            quantities[client_id] = quantities.get(client_id, Decimal(0)) + Decimal(str(fill["quantity"]))
        for order in self.demo_store.runner_orders():
            if Decimal(str(order["filled_quantity"])) != quantities.get(order["client_id"], Decimal(0)):
                raise DemoHandoffError("demo_owned_fills_unresolved",
                                       "cannot stop Demo reconciliation with incomplete owned fills")

    def _assert_account_reconciled(self):
        result = self.demo_store.account_reconciliation_status()
        if not isinstance(result, dict) or result.get("safeToSwitch") is not True:
            raise DemoHandoffError("demo_account_reconciliation_unresolved",
                                   "all Demo ledgers must be flat and quantity reconciled")

    @staticmethod
    def _assert_exchange_flat(adapter):
        try:
            positions = adapter.fetch_positions()
            open_orders = adapter.fetch_open_orders()
            mode = adapter.fetch_position_mode()
            if not isinstance(positions, list) or not isinstance(open_orders, list):
                raise ValueError("invalid Demo account snapshot")
            if not isinstance(mode, dict) or mode.get("hedged") is not False:
                raise ValueError("one-way Demo position mode required")
            for position in positions:
                if not isinstance(position, dict) or "signed_qty" not in position:
                    raise ValueError("missing Demo position quantity")
                quantity = Decimal(str(position["signed_qty"]))
                if not quantity.is_finite():
                    raise ValueError("invalid Demo position quantity")
                # Check each row, including unsupported symbols. Opposite
                # hedge-mode rows must never be treated as a flat net account.
                if quantity != 0:
                    raise DemoHandoffError("demo_account_positions_not_flat",
                                           "Demo account still has actual positions")
            if open_orders:
                raise DemoHandoffError("demo_account_open_orders_unresolved",
                                       "Demo account still has actual open orders")
        except DemoHandoffError:
            raise
        except Exception:
            raise DemoHandoffError("demo_switch_exchange_check_failed",
                                   "fresh full-account Demo checks failed") from None

    def _resume_paused_worker(self):
        self._closing = False
        self._halt_worker.clear()
        if self.service is not None:
            self._start_worker()
            self._wake.set()

    def switch(self, account_id, *, join_timeout=12):
        """Select a new paused session only after a fully flat account boundary.

        Old namespace bindings never change. This operation retains the same
        account flock, so another local writer cannot enter between sessions.
        A prepared new session has no orders and entries disabled; the old
        recovery pointer remains authoritative until one atomic document save.
        """
        with self._mutex:
            self._require_not_closing()
            self._lock()
            self._validate_account(account_id)
            if self.service is None:
                persisted = self.data_store.get_document(STATE_KEY, {})
                if persisted.get("selectedAccountId"):
                    self._restore_active_store(persisted)
                    self._create(persisted["selectedAccountId"], entries_enabled=False)
            if self.service:
                self.service.stop()
                self._save()
                if self.service.config.strategy_id == account_id:
                    return self.status()
            self._assert_owned_flat()
            workers = self._workers | ({self._thread} if self._thread is not None else set())
            if threading.current_thread() in workers:
                raise DemoHandoffError("demo_worker_busy", "Demo protection worker cannot join itself")
            self._closing = True
            self._halt_worker.set()
            self._wake.set()

        try:
            deadline = time.monotonic() + max(join_timeout, 0)
            for worker in workers:
                worker.join(timeout=max(0, deadline - time.monotonic()))
            with self._mutex:
                if any(worker.is_alive() for worker in workers):
                    raise DemoHandoffError("demo_worker_busy", "Demo protection worker did not finish")
                self._thread = None
                self._workers.clear()
                self._assert_owned_flat()
                self._assert_account_reconciled()
                adapter = self._adapter()
                self._assert_exchange_flat(adapter)
                candidate_store = self.ledger_factory("demo_strategy_" + uuid.uuid4().hex)
                if candidate_store.get_binding() or candidate_store.runner_orders():
                    raise DemoHandoffError("demo_switch_namespace_collision",
                                           "new Demo session is not empty")
                candidate = self._service(account_id, candidate_store, adapter)
                candidate.start(entries_enabled=False)
                candidate_state = candidate.status()
                if (candidate_state.get("entries_enabled") is not False
                        or candidate_state.get("last_error")
                        or candidate_state.get("ownership_conflicts")
                        or candidate_state.get("status") in {"reconciliation_error", "blocked_unknown_exposure"}):
                    raise DemoHandoffError("demo_switch_exchange_check_failed",
                                           "new paused Demo session could not be prepared")
                self._assert_account_reconciled()
                previous_document = self.data_store.get_document(STATE_KEY, {})
                document = {**self._document(candidate),
                            "previousVirtualAccountKey": self.demo_store.virtual_account_key,
                            "switchPolicy": "flat_account_new_immutable_session_explicit_start"}
                readback_error = None
                try:
                    self.data_store.put_document(STATE_KEY, document)
                except Exception as exc:
                    # The commit may have succeeded before its acknowledgement
                    # failed. Read the pointer before deciding which worker can
                    # legally save it; never blindly overwrite it with old state.
                    try:
                        observed = self.data_store.get_document(STATE_KEY, {})
                    except Exception:
                        observed = None
                    if observed == document:
                        readback_error = "Demo switch commit verified by readback: " + type(exc).__name__
                    elif observed == previous_document:
                        raise DemoHandoffError("demo_switch_persistence_failed",
                                               "new Demo recovery pointer was not committed") from None
                    else:
                        self._switch_persistence_unresolved = True
                        raise DemoHandoffError("demo_switch_persistence_unresolved",
                                               "Demo switch commit outcome must be reconciled") from None
                self.demo_store = candidate_store
                self.service = candidate
                self._selected_account_id = str(account_id)
                self._yielded_flat = False
                self._recovery_mode = False
                self._error = readback_error
                self._resume_paused_worker()
                return self.status()
        except Exception as exc:
            with self._mutex:
                self._error = "Demo switch refused: " + type(exc).__name__
                self._resume_paused_worker()
                try:
                    self._save()
                except Exception as save_exc:
                    self._error = "Demo paused recovery persistence failed: " + type(save_exc).__name__
            raise

    def close_if_flat(self, *, join_timeout=12):
        """Yield the account flock only after paused protection is fully joined.

        Failed handoffs keep the service, lock and paused protection. The
        selected frozen identity remains persisted; a later start is explicit.
        """
        with self._mutex:
            self._require_not_closing()
            if self.service:
                self.service.stop()
                self._save()
            self._assert_owned_flat()
            workers = self._workers | ({self._thread} if self._thread is not None else set())
            if threading.current_thread() in workers:
                raise DemoHandoffError("demo_worker_busy", "Demo protection worker cannot join itself")
            self._closing = True
            self._halt_worker.set()
            self._wake.set()
        # The worker's completion path takes _mutex. Joining while holding it
        # would deadlock and could release the flock over an active HTTP call.
        try:
            deadline = time.monotonic() + max(join_timeout, 0)
            for worker in workers:
                worker.join(timeout=max(0, deadline - time.monotonic()))
            with self._mutex:
                if any(worker.is_alive() for worker in workers):
                    raise DemoHandoffError("demo_worker_busy", "Demo protection worker did not finish")
                self._assert_owned_flat()
                persisted = self.data_store.get_document(STATE_KEY, {})
                self._selected_account_id = (self.service.config.strategy_id if self.service else
                                             self._selected_account_id or persisted.get("selectedAccountId"))
                self.data_store.put_document(STATE_KEY, {**persisted,
                    "selectedAccountId": self._selected_account_id, "entriesEnabled": False,
                    "yieldedFlat": True,
                    "status": {"running": False, "entries_enabled": False, "status": "yielded_flat"},
                    "recoveryPolicy": "explicit_start_after_flat_account_handoff"})
                if self._file_lock:
                    fcntl.flock(self._file_lock, fcntl.LOCK_UN)
                    self._file_lock.close()
                    self._file_lock = None
                self.service = None
                self._thread = None
                self._workers.clear()
                self._yielded_flat = True
                self._closing = False
                return self.status()
        except Exception:
            with self._mutex:
                self._closing = False
                self._halt_worker.clear()
                if self.service is not None:
                    self._start_worker()
                    self._wake.set()
                    try:
                        self._save()
                    except Exception as exc:
                        # Persistence failure cannot prevent paused protection
                        # from resuming while this controller retains the lock.
                        self._error = "Demo paused recovery persistence failed: " + type(exc).__name__
            raise
