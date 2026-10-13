"""Demo session lifecycles with one account lock and protected restart recovery."""
from __future__ import annotations

import copy
import fcntl
import hashlib
from decimal import Decimal
from pathlib import Path
import threading
import time
import uuid

from demo_execution_service import DemoExecutionService, DemoRunnerConfig, PaperEventSource

STATE_KEY = "binance_demo_runner_controller.json"
SHARED_STATE_KEY = "binance_demo_shared_sessions.json"
SHARED_POLICY = "one_way_dynamic_pair_exclusive_v1"
MAX_SHARED_SESSIONS = 3


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
        self._parallel_sessions = {}
        self._shared_records = {}
        self._session_errors = {}
        self._shared_enabled = False
        self._shared_persistence_unresolved = False
        self._restore_incomplete = False
        self._account_coordinator = None
        self._account_cycle_lock = threading.RLock()

    def _services(self):
        return ([self.service] if self.service is not None else []) + list(self._parallel_sessions.values())

    def _coordinator(self):
        if self._account_coordinator is None:
            from demo_shared_account import SharedDemoAccount
            self._account_coordinator = SharedDemoAccount(self.demo_store)
            self._account_cycle_lock = self._account_coordinator.lock
        return self._account_coordinator

    def _register(self, service):
        coordinator = self._coordinator()
        service.account_coordinator = coordinator
        coordinator.register(service)

    def _registry_document(self, *, extra=None, primary=None, yielded=False):
        records = copy.deepcopy(self._shared_records)
        for service in self._services() + ([extra] if extra else []):
            namespace = service.config.virtual_account_key
            records[namespace] = {**self._document(service),
                                  "initializationError": self._session_errors.get(namespace)}
        return {"version": 1, "policy": SHARED_POLICY,
                "primaryVirtualAccountKey": primary or self.demo_store.virtual_account_key,
                "credentialFingerprint": self._credential_fingerprint,
                "yieldedFlat": yielded, "sessions": list(records.values()),
                "recoveryPolicy": "register_all_members_pause_entries_continue_owned_risk_protection"}

    def _pause_members(self):
        with self._account_cycle_lock:
            for service in self._services():
                try:
                    service.stop()
                except Exception as exc:
                    self._session_errors[service.config.virtual_account_key] = type(exc).__name__

    def _commit_registry(self, document):
        """A lost acknowledgement never permits overwriting an unknown registry."""
        previous = self.data_store.get_document(SHARED_STATE_KEY, {})
        try:
            self.data_store.put_document(SHARED_STATE_KEY, document)
        except Exception:
            try:
                observed = self.data_store.get_document(SHARED_STATE_KEY, {})
            except Exception:
                observed = None
            if observed == document:
                pass
            elif observed == previous:
                raise DemoHandoffError("demo_shared_persistence_failed",
                                       "shared session registry was not committed") from None
            else:
                self._shared_persistence_unresolved = True
                self._pause_members()
                raise DemoHandoffError("demo_shared_persistence_unresolved",
                                       "shared session registry commit must be reconciled") from None
        self._shared_records = {record["virtualAccountKey"]: copy.deepcopy(record)
                                for record in document["sessions"]}

    def _require_shared_ready(self):
        if self._restore_incomplete or self._session_errors:
            raise DemoHandoffError("demo_shared_recovery_incomplete",
                                   "every registered session must recover before entries can start")

    @staticmethod
    def _assert_initialized(service):
        state = service.status()
        if (state.get("entries_enabled") is not False or state.get("last_error")
                or state.get("ownership_conflicts")
                or state.get("status") in {"reconciliation_error", "blocked_unknown_exposure"}):
            raise DemoHandoffError("demo_shared_session_initialization_failed",
                                   "shared Demo session could not initialize paused reconciliation")

    def _assert_companions_paused_flat(self):
        if any(record.get("virtualAccountKey") not in
               {service.config.virtual_account_key for service in self._services()}
               for record in self._shared_records.values()):
            raise DemoHandoffError("demo_shared_recovery_incomplete",
                                   "unrestored sessions prohibit a primary handoff")
        for service in self._parallel_sessions.values():
            if service.status().get("entries_enabled") is not False:
                raise DemoHandoffError("demo_parallel_entries_enabled",
                                       "pause every companion before switching or yielding the account")
            self._assert_ledger_flat(service.store)

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
            if self._shared_enabled and not self._shared_persistence_unresolved:
                self._commit_registry(self._registry_document())
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
        if self._shared_enabled:
            shared = self.data_store.get_document(SHARED_STATE_KEY, {})
            expected += (shared.get("credentialFingerprint"),)
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
                or account["execution"].get("risk_sizing_mode") != "entry_atr"
                or account["execution"].get("blocked_execution_reason")):
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
        if self._shared_enabled:
            self._register(service)
            self.service = service
            self._commit_registry(self._registry_document())
        with self._account_cycle_lock:
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
        if self._shared_persistence_unresolved:
            raise DemoHandoffError("demo_shared_persistence_unresolved",
                                   "shared session registry must be reconciled before changing entries")

    def start(self, account_id):
        """Invoked by the user's frontend start action; never a live start."""
        with self._mutex:
            self._require_not_closing()
            if not self.service:
                shared = self.data_store.get_document(SHARED_STATE_KEY, {})
                if shared.get("sessions"):
                    self._restore_shared({**shared, "yieldedFlat": False})
                    self._require_shared_ready()
            if self._shared_enabled and self.service:
                if self.service.config.strategy_id != account_id:
                    raise ValueError("Demo primary session is bound to another frozen identity")
                return self.start_session(self.service.config.virtual_account_key)
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
                with self._account_cycle_lock:
                    self.service.stop(flatten=flatten)
                self._save()
                self._wake.set()
            return self.status()

    def attach(self, account_id):
        """Add a new immutable, paused companion; explicit start opens its gate."""
        return self._attach(account_id)

    def adopt_existing_session(self, namespace):
        """Resume an existing immutable namespace without changing its boundary."""
        with self._mutex:
            self._require_not_closing()
            if namespace == self.demo_store.virtual_account_key or namespace in self._parallel_sessions:
                return self.status()
            ledger = self.ledger_factory(str(namespace))
            binding = ledger.get_binding() or {}
            binding = binding.get("payload", binding)
            if not binding.get("strategy_id"):
                raise DemoHandoffError("demo_session_binding_missing", "existing namespace has no strategy binding")
            return self._attach(binding["strategy_id"], existing_ledger=ledger)

    def _attach(self, account_id, *, existing_ledger=None):
        with self._mutex:
            self._require_not_closing()
            self._require_shared_ready()
            if not self.service:
                self.resume_protection()
                self._require_shared_ready()
            if not self.service:
                raise DemoHandoffError("demo_primary_session_required", "start the primary session before attaching")
            self._lock()
            account_id = str(account_id)
            for service in self._services():
                if service.config.strategy_id == account_id:
                    return self.status()
            if len(self._services()) >= MAX_SHARED_SESSIONS:
                raise DemoHandoffError("demo_shared_session_limit", "at most three Demo sessions may share the account")
            self._validate_account(account_id)
            adapter = self._adapter()
            ledger = existing_ledger or self.ledger_factory("demo_strategy_" + uuid.uuid4().hex)
            if existing_ledger is None and (ledger.get_binding() or ledger.runner_orders()):
                raise DemoHandoffError("demo_switch_namespace_collision", "new Demo session is not empty")
            namespace = ledger.virtual_account_key
            candidate = self._service(account_id, ledger, adapter)
            self._register(self.service)
            self._register(candidate)
            committed = False
            try:
                with self._account_cycle_lock:
                    conflicts = self._coordinator().assert_attach_safe(
                        adapter.fetch_positions(), adapter.fetch_open_orders())
                    mode = adapter.fetch_position_mode()
                    if conflicts or not isinstance(mode, dict) or mode.get("hedged") is not False:
                        raise DemoHandoffError("demo_shared_account_unreconciled",
                                               "shared ownership must reconcile in one-way mode before attaching")
                    # This registry is the recovery authority BEFORE any start
                    # cycle, including protection of an adopted old namespace.
                    self._commit_registry(self._registry_document(extra=candidate))
                    committed = True
                    self._shared_enabled = True
                    self._parallel_sessions[namespace] = candidate
                    try:
                        candidate.start(entries_enabled=False)
                        self._assert_initialized(candidate)
                    except Exception as exc:
                        self._session_errors[namespace] = type(exc).__name__
                        self._restore_incomplete = True
                        self._pause_members()
                        raise
                self._save()
                self._start_worker()
                self._wake.set()
                return self.status()
            except Exception:
                if not committed and not self._shared_persistence_unresolved:
                    self._coordinator().unregister(namespace)
                elif not committed:
                    # Keep even an ambiguously committed member available for
                    # recovery; never overwrite the authoritative registry.
                    self._shared_enabled = True
                    self._parallel_sessions[namespace] = candidate
                    self._session_errors[namespace] = "registry_commit_unresolved"
                    self._restore_incomplete = True
                if committed or self._shared_persistence_unresolved:
                    self._pause_members()
                    self._start_worker()
                raise

    def start_session(self, namespace):
        with self._mutex:
            self._require_not_closing()
            self._require_shared_ready()
            service = self._find_session(namespace)
            self._start_worker()
            # Persist membership before enable_entries can synchronously post.
            if self._shared_enabled:
                self._commit_registry(self._registry_document())
            with self._account_cycle_lock:
                service.enable_entries()
            try:
                self._save()
            except Exception:
                self._pause_members()
                raise
            self._wake.set()
            return self.status()

    def stop_session(self, namespace, *, flatten=False):
        with self._mutex:
            self._require_not_closing()
            service = self._find_session(namespace)
            with self._account_cycle_lock:
                service.stop(flatten=flatten)
            self._save()
            self._wake.set()
            return self.status()

    def _find_session(self, namespace):
        if self.service and namespace == self.service.config.virtual_account_key:
            return self.service
        if namespace in self._parallel_sessions:
            return self._parallel_sessions[namespace]
        raise DemoHandoffError("demo_session_not_found", "Demo session is not registered")

    def _restore_shared(self, persisted):
        """Assemble every member before any cycle; restore all entry gates shut."""
        if self._services() and not self._restore_incomplete:
            return self.status()
        records = persisted.get("sessions", [])
        namespaces = [record.get("virtualAccountKey") for record in records if isinstance(record, dict)]
        primary = persisted.get("primaryVirtualAccountKey")
        if (len(namespaces) != len(records) or len(set(namespaces)) != len(records)
                or not 1 <= len(records) <= MAX_SHARED_SESSIONS or primary not in namespaces
                or any(not namespace or not record.get("selectedAccountId")
                       for namespace, record in zip(namespaces, records))):
            self._error = "Demo shared recovery failed: invalid registry"
            self._restore_incomplete = True
            return self.status()
        self._shared_enabled = True
        self._shared_records = {record["virtualAccountKey"]: copy.deepcopy(record) for record in records}
        self._credential_fingerprint = persisted.get("credentialFingerprint")
        self.demo_store = self.ledger_factory(primary)
        self._selected_account_id = self._shared_records[primary]["selectedAccountId"]
        if persisted.get("yieldedFlat") is True and not self._services():
            self._yielded_flat = True
            return self.status()
        self._lock()
        self._recovery_mode = True
        self._session_errors = {}
        self._restore_incomplete = True
        # Existing successfully recovered members stay registered on retries.
        registered_services = []
        for namespace, record in self._shared_records.items():
            try:
                service = next((item for item in self._services()
                                if item.config.virtual_account_key == namespace), None)
                if service is None:
                    ledger = self.demo_store if namespace == primary else self.ledger_factory(namespace)
                    self._validate_account(record["selectedAccountId"])
                    service = self._service(record["selectedAccountId"], ledger, self._adapter())
                    if namespace == primary:
                        self.service = service
                    else:
                        self._parallel_sessions[namespace] = service
                self._register(service)
                registered_services.append(service)
            except Exception as exc:
                self._session_errors[namespace] = type(exc).__name__
        # Registration must finish for every possible owner before ANY start()
        # runs its first reconciliation/protection cycle.
        with self._account_cycle_lock:
            for service in registered_services:
                namespace = service.config.virtual_account_key
                try:
                    service.start(entries_enabled=False)
                    service.stop()
                    self._assert_initialized(service)
                except Exception as exc:
                    self._session_errors[namespace] = type(exc).__name__
        self._restore_incomplete = bool(self._session_errors)
        self._error = "Demo shared recovery incomplete" if self._restore_incomplete else None
        try:
            self._save()
        except Exception as exc:
            self._restore_incomplete = True
            self._error = "Demo shared recovery persistence failed: " + type(exc).__name__
            self._pause_members()
        if self._services():
            self._start_worker()
            self._wake.set()
        return self.status()

    def resume_protection(self):
        """On application restart: only reconcile/protect, never resume entries."""
        with self._mutex:
            self._require_not_closing()
            shared = self.data_store.get_document(SHARED_STATE_KEY, {})
            if shared.get("sessions"):
                return self._restore_shared(shared)
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
            sessions = []
            for service in self._services():
                member_state = service.status()
                sessions.append({**member_state, "selectedAccountId": service.config.strategy_id,
                                 "virtualAccountKey": service.config.virtual_account_key,
                                 "entriesEnabled": member_state.get("entries_enabled", False),
                                 "primary": service is self.service,
                                 "initializationError": self._session_errors.get(service.config.virtual_account_key)})
            present = {member["virtualAccountKey"] for member in sessions}
            for namespace, record in self._shared_records.items():
                if namespace not in present:
                    sessions.append({"selectedAccountId": record["selectedAccountId"],
                                     "virtualAccountKey": namespace, "entriesEnabled": False,
                                     "primary": namespace == self.demo_store.virtual_account_key,
                                     "status": "yielded_flat" if self._yielded_flat else "recovery_error",
                                     "initializationError": self._session_errors.get(namespace)})
            return {**state, "selectedAccountId": self.service.config.strategy_id if self.service else self._selected_account_id,
                    "virtualAccountKey": self.demo_store.virtual_account_key,
                    "entriesEnabled": state["entries_enabled"], "recoveryMode": self._recovery_mode,
                    "controllerError": self._error,
                    "workerRunning": bool((self._thread and self._thread.is_alive())
                                          or any(worker.is_alive() for worker in self._workers)),
                    "accountLockOwned": self._file_lock is not None,
                    "handoffInProgress": self._closing,
                    "switchPersistenceUnresolved": self._switch_persistence_unresolved,
                    "parallelSessions": sessions, "sharedAccountPolicy": SHARED_POLICY,
                    "sharedSessionsEnabled": self._shared_enabled,
                    "allEntriesEnabled": bool(sessions) and all(member["entriesEnabled"] for member in sessions),
                    "sharedRecoveryIncomplete": self._restore_incomplete,
                    "sharedRegistryPersistenceUnresolved": self._shared_persistence_unresolved}

    def _loop(self):
        try:
            while not self._halt_worker.is_set():
                with self._mutex:
                    services = self._services()
                if not services:
                    break
                # Do not hold the controller mutex around HTTP requests.
                for service in services:
                    try:
                        with self._account_cycle_lock:
                            service.cycle()
                    except Exception as exc:
                        self._error = "Demo polling failed: " + type(exc).__name__
                try:
                    self._save()
                except Exception as exc:
                    self._error = "Demo recovery persistence failed: " + type(exc).__name__
                    with self._mutex:
                        self._pause_members()
                self._wake.wait(min(service.config.poll_interval_ms for service in services) / 1000)
                self._wake.clear()
        finally:
            with self._mutex:
                if self._thread is threading.current_thread():
                    self._thread = None
                    # A timed-out handoff cancels the halt request. If this
                    # worker had already decided to exit, restore protection.
                    if self._services() and not self._halt_worker.is_set():
                        self._start_worker()

    def _assert_owned_flat(self):
        self._assert_ledger_flat(self.demo_store)

    @staticmethod
    def _assert_ledger_flat(ledger):
        positions = ledger.net_fill_positions()
        if any(Decimal(str(qty)) != 0 for qty in positions.values()):
            raise DemoHandoffError("demo_owned_exposure_unresolved",
                                   "cannot stop Demo protection with owned exposure")
        if ledger.pending_orders():
            raise DemoHandoffError("demo_owned_orders_unresolved",
                                   "cannot stop Demo reconciliation with unresolved orders")
        quantities = {}
        for fill in ledger.runner_fills():
            client_id = fill["client_id"]
            quantities[client_id] = quantities.get(client_id, Decimal(0)) + Decimal(str(fill["quantity"]))
        for order in ledger.runner_orders():
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
        if self._services():
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
            self._assert_companions_paused_flat()
            self._lock()
            self._validate_account(account_id)
            if self.service is None:
                persisted = self.data_store.get_document(STATE_KEY, {})
                if persisted.get("selectedAccountId"):
                    self._restore_active_store(persisted)
                    self._create(persisted["selectedAccountId"], entries_enabled=False)
            if self.service:
                with self._account_cycle_lock:
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
                if self._shared_enabled:
                    self._register(candidate)
                with self._account_cycle_lock:
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
                if self._shared_enabled:
                    shared_document = self._registry_document(extra=candidate,
                        primary=candidate_store.virtual_account_key)
                    shared_document["sessions"] = [record for record in shared_document["sessions"]
                        if record["virtualAccountKey"] != self.demo_store.virtual_account_key]
                    self._commit_registry(shared_document)
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
                        if self._shared_enabled:
                            readback_error = "Demo primary compatibility pointer update failed; shared registry committed"
                        else:
                            raise DemoHandoffError("demo_switch_persistence_failed",
                                                   "new Demo recovery pointer was not committed") from None
                    else:
                        if self._shared_enabled:
                            readback_error = "Demo primary compatibility pointer unconfirmed; shared registry committed"
                        else:
                            self._switch_persistence_unresolved = True
                            raise DemoHandoffError("demo_switch_persistence_unresolved",
                                                   "Demo switch commit outcome must be reconciled") from None
                if self._shared_enabled:
                    self._coordinator().unregister(self.demo_store.virtual_account_key)
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
                if self._shared_enabled and "candidate" in locals() and candidate is not self.service:
                    try:
                        self._coordinator().unregister(candidate.config.virtual_account_key)
                    except Exception:
                        self._restore_incomplete = True
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
            if self._yielded_flat and not self._services():
                return self.status()
            self._assert_companions_paused_flat()
            if self.service:
                with self._account_cycle_lock:
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
                self._assert_companions_paused_flat()
                persisted = self.data_store.get_document(STATE_KEY, {})
                self._selected_account_id = (self.service.config.strategy_id if self.service else
                                             self._selected_account_id or persisted.get("selectedAccountId"))
                if self._shared_enabled:
                    self._commit_registry(self._registry_document(yielded=True))
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
                self._parallel_sessions.clear()
                self._account_coordinator = None
                self._account_cycle_lock = threading.RLock()
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
