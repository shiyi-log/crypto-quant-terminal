"""Demo session lifecycles with one account lock and protected restart recovery."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import logging
import os
from decimal import Decimal
from pathlib import Path
import threading
import time
import uuid

from demo_execution_service import DemoExecutionService, DemoRunnerConfig, PaperEventSource

STATE_KEY = "binance_demo_runner_controller.json"
SHARED_STATE_KEY = "binance_demo_shared_sessions.json"
SHARED_POLICY = "one_way_dynamic_pair_exclusive_v1"
MAX_SHARED_SESSIONS = 6
LOGGER = logging.getLogger(__name__)


class DemoHandoffError(RuntimeError):
    """A fixed-code refusal which leaves the account lock owned."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class _ExternalCapability:
    """进程内前台能力；字符串 owner 不能伪造持锁上下文。"""

    __slots__ = ('controller', 'handle', 'pid', 'device', 'inode', 'owner_id')

    def __init__(self, controller, handle, owner_id):
        self.controller = controller
        self.handle = handle
        self.pid = os.getpid()
        stat = os.fstat(handle.fileno())
        self.device, self.inode = stat.st_dev, stat.st_ino
        self.owner_id = owner_id

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError('frozen external writer capability')
        object.__setattr__(self, name, value)


class DemoRunnerController:
    def __init__(self, data_store, demo_store, credential_provider, adapter_factory=None,
                 *, paper_source=None, lock_path=None, clock_ms=None, ledger_factory=None,
                 risk_gate_required=None, risk_account_scope=None, gate_registry=None,
                 acceptance_round_provider=None, acceptance_round_required=False,
                 safety_runtime_factory=None, driver_mode='legacy_thread'):
        if driver_mode not in {'legacy_thread', 'external_paused'}:
            raise ValueError('invalid Demo controller driver mode')
        self.driver_mode = driver_mode
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
        self._external_capability = None
        self._external_owner_id = None
        self._external_pid = None
        self._external_lock_identity = None
        self._external_command_binding_hash = None
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
        self._external_operation_lock = threading.Lock()
        self.gate_registry = gate_registry
        if risk_gate_required is None:
            risk_gate_required = self._gate_enabled("risk.required")
        self.risk_gate_required = bool(risk_gate_required)
        self.risk_account_scope = risk_account_scope
        self._risk_gate = None
        self._research_id = None
        self._risk_gate_binding_missing = False
        # The provider is a server-side durable approval authority, never an
        # HTTP body flag. Legacy research sessions can remain outside formal
        # acceptance; a persisted round binding always keeps this gate active.
        self.acceptance_round_provider = acceptance_round_provider
        self.acceptance_round_required = bool(acceptance_round_required)
        self._acceptance_round = None
        # 仅接收组合根的内部工厂，HTTP 字段不能构造权威风险/保护事实。
        self.safety_runtime_factory = safety_runtime_factory

    def _acceptance_mode(self):
        # Lifecycle calls can precede resume_protection after a process restart.
        # Never let an unloaded persisted binding silently become legacy mode.
        if self._acceptance_round is None:
            for key in (STATE_KEY, SHARED_STATE_KEY):
                saved = self.data_store.get_document(key, {})
                if isinstance(saved, dict) and saved.get("acceptanceRound") is not None:
                    self._acceptance_round = copy.deepcopy(saved["acceptanceRound"])
                    break
        return (self.acceptance_round_required or self._acceptance_round is not None
                or callable(self.acceptance_round_provider))

    def _require_acceptance_exclusive(self):
        if self._acceptance_mode():
            raise DemoHandoffError("demo_acceptance_round_exclusive",
                                   "an acceptance round cannot add or switch strategies")

    def _require_acceptance_round(self, account_id):
        try:
            if not self._acceptance_mode():
                return
            if self._parallel_sessions or self._shared_records:
                raise DemoHandoffError("demo_acceptance_round_exclusive",
                                       "acceptance requires a single frozen strategy writer")
            candidate = self.acceptance_round_provider()
            required = ("round_id", "environment", "account_scope", "strategy_id",
                        "rule_hash", "execution_hash", "status",
                        "approval_id", "approved_by")
            if not isinstance(candidate, dict) or any(
                    not isinstance(candidate.get(key), str) or not candidate[key].strip()
                    or candidate[key] != candidate[key].strip() for key in required):
                raise ValueError("missing approval identity")
            frozen = self.paper.account(str(account_id))
            if (candidate["environment"] != "binance_demo"
                    or candidate["status"] != "active"
                    or candidate["account_scope"] != self.risk_account_scope
                    or candidate["strategy_id"] != str(account_id)
                    or any(candidate[key] != frozen.get(key)
                           for key in ("strategy_id", "rule_hash", "execution_hash"))):
                raise ValueError("approval identity mismatch")
            binding = {key: candidate[key] for key in required}
            if self._acceptance_round is not None and self._acceptance_round != binding:
                raise ValueError("round identity is already frozen")
        except DemoHandoffError:
            self._pause_members()
            raise
        except Exception:
            self._pause_members()
            raise DemoHandoffError("demo_acceptance_approval_required",
                                   "matching active acceptance approval is required") from None
        self._acceptance_round = binding

    def _gate_enabled(self, key):
        """Use the injected process registry when the public API has one."""
        if self.gate_registry is None:
            return True
        return bool(self.gate_registry.is_enabled(key))

    def _require_gate(self, key):
        if not self._gate_enabled(key):
            raise DemoHandoffError("execution_gate_closed", f"{key} is disabled")

    def _require_entry_gates(self):
        for key in ("execution.demo_enabled", "execution.trading_enabled",
                    "execution.entries_enabled"):
            self._require_gate(key)

    @staticmethod
    def _gate_identity(gate):
        if not isinstance(gate, dict):
            return None
        keys = ("research_id", "event_id", "boundary_hash", "inputs_hash",
                "target_binding_hash")
        values = tuple(gate.get(key) for key in keys)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        return values

    def _ledger_has_session_history(self):
        get_binding = getattr(self.demo_store, "get_binding", None)
        runner_orders = getattr(self.demo_store, "runner_orders", None)
        return bool((get_binding() if callable(get_binding) else None)
                    or (runner_orders() if callable(runner_orders) else None))

    def _expected_risk_binding(self, account_id):
        if not isinstance(self.risk_account_scope, str) or not self.risk_account_scope.strip():
            raise DemoHandoffError("risk_gate_identity_missing",
                                   "the verified Demo risk account scope is not configured")
        try:
            account = self.paper.account(str(account_id))
            if account.get("strategy_id") != str(account_id):
                raise ValueError("frozen strategy identity mismatch")
            target = {"strategy_id": str(account_id),
                      "rule_hash": account["rule_hash"],
                      "execution_hash": account["execution_hash"]}
            import research_ledger
            return research_ledger.build_risk_gate_target_binding(
                self.risk_account_scope, [target])
        except Exception:
            raise DemoHandoffError("risk_gate_identity_missing",
                                   "the frozen Demo strategy identity is unavailable") from None

    def _require_risk_gate(self, research_id=None, *, account_id=None):
        """Validate the immutable research gate before any Demo entry path."""
        if not self.risk_gate_required:
            return None
        if self._risk_gate_binding_missing:
            raise DemoHandoffError("risk_gate_identity_missing",
                                   "an existing Demo session has no frozen risk-gate identity")
        if not isinstance(research_id, str) or not research_id.strip() or research_id != research_id.strip():
            raise DemoHandoffError("risk_gate_not_closed", "a research id is required before Demo trading")
        if self._research_id and research_id != self._research_id:
            raise DemoHandoffError("risk_gate_identity_mismatch",
                                   "the Demo session is bound to another research id")
        import research_ledger
        try:
            if account_id is None:
                raise ValueError("Demo target strategy is required")
            expected_binding = self._expected_risk_binding(account_id)
            gate = research_ledger.require_closed_risk_gate(
                self.data_store, research_id, expected_binding=expected_binding)
        except DemoHandoffError:
            raise
        except Exception:
            raise DemoHandoffError("risk_gate_not_closed", "the research risk gate is not closed") from None
        if self.risk_gate_required and self._services() and self._risk_gate is None:
            raise DemoHandoffError("risk_gate_identity_missing",
                                   "an existing Demo session is not bound to a risk-gate event")
        if self._risk_gate is not None:
            identity = ("research_id", "event_id", "boundary_hash", "inputs_hash",
                        "target_binding_hash")
            if any(self._risk_gate.get(key) != gate.get(key) for key in identity):
                raise DemoHandoffError("risk_gate_identity_mismatch",
                                       "the Demo session is bound to another risk-gate event")
        self._risk_gate = dict(gate)
        self._research_id = research_id
        return self._risk_gate

    def _services(self):
        return ([self.service] if self.service is not None else []) + list(self._parallel_sessions.values())

    def _coordinator(self):
        # This lock exists before the first legacy cycle and NEVER changes.
        # Installing a different lock while a worker owns the old one can
        # invert service/coordinator locking during the first attachment.
        with self._account_cycle_lock:
            if self._account_coordinator is None:
                from demo_shared_account import SharedDemoAccount
                coordinator = SharedDemoAccount(self.demo_store)
                coordinator.lock = self._account_cycle_lock
                self._account_coordinator = coordinator
            return self._account_coordinator

    def _register(self, service):
        with self._account_cycle_lock:
            coordinator = self._coordinator()
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
                "researchId": self._research_id,
                "riskGate": copy.deepcopy(self._risk_gate),
                "acceptanceRound": copy.deepcopy(self._acceptance_round),
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

    def _external_refuse_legacy(self):
        if self.driver_mode == 'external_paused':
            raise DemoHandoffError('demo_external_driver_required',
                                   'external writer owns the paused controller')

    def _assert_external(self, capability):
        if (self.driver_mode != 'external_paused' or capability is not self._external_capability
                or self._external_capability is None or os.getpid() != self._external_pid
                or self._file_lock is None or self._file_lock.closed
                or capability.handle is not self._file_lock or capability.pid != os.getpid()):
            raise DemoHandoffError('demo_external_capability_invalid',
                                   'external writer capability is invalid')
        try:
            stat = os.fstat(self._file_lock.fileno())
            current = self.lock_path.stat()
            if ((stat.st_dev, stat.st_ino) != self._external_lock_identity
                    or (current.st_dev, current.st_ino) != self._external_lock_identity):
                raise OSError('lock identity changed')
        except (OSError, ValueError):
            raise DemoHandoffError('demo_external_capability_invalid',
                                   'external writer lock identity is invalid') from None

    def assert_external(self, capability):
        self._assert_external(capability)

    def activate_external(self, target, *, command_store):
        if not self._external_operation_lock.acquire(blocking=False):
            raise DemoHandoffError('demo_external_operation_active',
                                   'external writer operation is already active')
        try:
            return self._activate_external_locked(target, command_store=command_store)
        finally:
            self._external_operation_lock.release()

    def _activate_external_locked(self, target, *, command_store):
        from demo_writer import FrozenWriterTarget
        if self.driver_mode != 'external_paused' or type(target) is not FrozenWriterTarget:
            raise DemoHandoffError('demo_external_driver_required', 'external writer mode is required')
        target = FrozenWriterTarget(target.binding, strategy_id=target.strategy_id,
            rule_hash=target.rule_hash, execution_hash=target.execution_hash,
            demo_policy_hash=target.demo_policy_hash)
        if self._external_capability is not None or self.service or self._thread or self._workers:
            raise DemoHandoffError('demo_external_already_active', 'external writer is already active')
        try:
            self._lock()
        except Exception:
            raise
        try:
            self.data_store.check_schema()
            self.demo_store.check_schema()
            if (not hasattr(command_store, 'check_schema') or command_store.check_schema() is False
                    or getattr(command_store, 'account_id', None) != target.binding.to_dict()['scope']['account_id']
                    or getattr(command_store, 'environment', None) != 'binance_demo'):
                raise DemoHandoffError('demo_external_binding_unavailable',
                                       'external writer binding is unavailable')
            stored = command_store.get_binding(target.binding.binding_id)
            expected = target.binding.to_dict()
            if stored != expected:
                raise DemoHandoffError('demo_external_binding_mismatch',
                                       'external writer binding does not match')
            scope = expected['scope']
            if self.risk_account_scope != scope['account_id']:
                raise DemoHandoffError('demo_external_account_mismatch',
                                       'external writer account scope does not match')
            if self.data_store.get_document(SHARED_STATE_KEY, {}).get('sessions'):
                raise DemoHandoffError('demo_external_shared_recovery_unsupported',
                                       'shared external recovery is not enabled')
            persisted = self.data_store.get_document(STATE_KEY, {}) or {}
            if (not isinstance(persisted, dict) or persisted.get('yieldedFlat') is True
                    or persisted.get('selectedAccountId') != target.strategy_id
                    or persisted.get('demoPolicyHash') != target.demo_policy_hash
                    or persisted.get('virtualAccountKey')
                       != expected['namespace']):
                raise DemoHandoffError('demo_external_session_mismatch',
                                       'external writer session identity does not match')
            if self.demo_store.virtual_account_key != expected['namespace']:
                raise DemoHandoffError('demo_external_session_mismatch',
                                       'external writer ledger namespace does not match')
            ledger_binding = self.demo_store.get_binding()
            if not isinstance(ledger_binding, dict):
                raise DemoHandoffError('demo_external_legacy_binding_missing',
                                       'external writer legacy binding is unavailable')
            for key, value in (('strategy_id', target.strategy_id), ('rule_hash', target.rule_hash),
                               ('execution_hash', target.execution_hash), ('demo_policy_hash', target.demo_policy_hash)):
                if ledger_binding.get(key) != value:
                    raise DemoHandoffError('demo_external_legacy_binding_mismatch',
                                           'external writer legacy binding does not match')
            paper = self.paper.account(target.strategy_id)
            if (not isinstance(paper, dict) or any(paper.get(key) != value for key, value in
                    (('strategy_id', target.strategy_id), ('rule_hash', target.rule_hash),
                     ('execution_hash', target.execution_hash)))):
                raise DemoHandoffError('demo_external_paper_binding_mismatch',
                                       'external writer paper identity does not match')
            self._restore_active_store(persisted)
            self._external_owner_id = 'writer-' + uuid.uuid4().hex
            self._external_pid = os.getpid()
            stat = os.fstat(self._file_lock.fileno())
            self._external_lock_identity = (stat.st_dev, stat.st_ino)
            self._external_command_binding_hash = target.binding.sha256
            self._external_capability = _ExternalCapability(self, self._file_lock, self._external_owner_id)
            try:
                self._create(target.strategy_id, entries_enabled=False)
            except Exception:
                if self.service is None:
                    raise
                self.service.pause_entries_without_cycle()
                self._error = 'demo_external_initial_cycle_incomplete'
            return self._external_capability
        except Exception:
            if self._file_lock is not None and self.service is None:
                fcntl.flock(self._file_lock, fcntl.LOCK_UN)
                self._file_lock.close()
                self._file_lock = None
                self._external_capability = None
                self._external_pid = None
                self._external_lock_identity = None
            raise

    def _external_state(self, capability):
        self._assert_external(capability)
        if self.service is None:
            raise DemoHandoffError('demo_external_recovery_incomplete',
                                   'external writer service is not active')
        state = self.service.status()
        if state.get('entries_enabled') is not False:
            raise DemoHandoffError('demo_external_entries_open',
                                   'external writer entries must remain paused')
        return state

    def pause_external(self, capability):
        self._assert_external(capability)
        if not self._external_operation_lock.acquire(blocking=False):
            raise DemoHandoffError('demo_external_operation_active',
                                   'external writer operation is already active')
        try:
            return self._pause_external_locked(capability)
        finally:
            self._external_operation_lock.release()

    def _pause_external_locked(self, capability):
        self._assert_external(capability)
        if self.service is None:
            raise DemoHandoffError('demo_external_recovery_incomplete', 'external writer service is unavailable')
        with self._account_cycle_lock:
            result = self.service.pause_entries_without_cycle()
        self._save()
        return {'entries_paused': result.get('entries_enabled') is False, 'pause_persisted': True}

    def cycle_external(self, capability):
        self._assert_external(capability)
        if not self._external_operation_lock.acquire(blocking=False):
            raise DemoHandoffError('demo_external_operation_active',
                                   'external writer operation is already active')
        try:
            return self._cycle_external_locked(capability)
        finally:
            self._external_operation_lock.release()

    def _cycle_external_locked(self, capability):
        self._assert_external(capability)
        if self.service is None:
            raise DemoHandoffError('demo_external_recovery_incomplete', 'external writer service is unavailable')
        with self._account_cycle_lock:
            started_now = False
            if not getattr(self.service, '_started', True):
                try:
                    self.service.start(entries_enabled=False)
                    started_now = True
                    self._error = None
                except Exception:
                    return {'entries_paused': True, 'pause_persisted': False,
                            'cycle_ok': False, 'reason_code': 'cycle_start_incomplete'}
            self.service.pause_entries_without_cycle()
            if not started_now:
                self.service.cycle()
            state = self.service.status()
            self.service.pause_entries_without_cycle()
            entries_paused = self.service.status().get('entries_enabled') is False
        persisted = True
        try:
            self._save()
        except Exception:
            persisted = False
        return {'entries_paused': entries_paused,
                'pause_persisted': persisted,
                'cycle_ok': state.get('status') not in {'reconciliation_error', 'blocked_unknown_exposure'}
                and not any(state.get(key) for key in ('last_error', 'risk_alert', 'ownership_conflicts',
                    'submission_unknown', 'safety_runtime_error', 'safety_runtime_reason',
                    'safety_runtime_cancel_error', 'safety_runtime_protection_error')),
                'reason_code': 'cycle_observed'}

    def _external_exit_readiness(self):
        service = self.service
        if service is None:
            raise DemoHandoffError('demo_external_exit_unavailable', 'external writer service is unavailable')
        if (getattr(service, 'safety_runtime_required', True)
                or getattr(service, 'safety_runtime', None) is not None):
            # 首版没有原生子单与成交最终性裁定，不把运行时存在当成退出证明。
            raise DemoHandoffError('demo_external_exit_readiness_unavailable',
                                   'durable native protection finality is unavailable')
        # 仅从真实持久存储重放风险；条件历史缺少账户键，首版对任意历史保守拒绝。
        try:
            from demo_risk_reservation_store import DemoRiskReservationStore
            risk = DemoRiskReservationStore(self.data_store, self.risk_account_scope,
                                            self.demo_store.virtual_account_key)
            state = risk.exposure()
            if not isinstance(state, dict) or state.get('reservations') != []:
                raise ValueError('unresolved durable risk')
            with self.data_store._connection() as conn:
                history = conn.execute('SELECT 1 FROM demo_conditional_intents WHERE environment=%s LIMIT 1',
                                       (self.demo_store.environment,)).fetchone()
            if history is not None:
                raise ValueError('conditional finality unavailable')
        except Exception:
            raise DemoHandoffError('demo_external_exit_readiness_unavailable',
                                   'durable risk and conditional readiness is unavailable') from None

    def yield_external_if_flat(self, capability):
        self._external_state(capability)
        if not self._external_operation_lock.acquire(blocking=False):
            raise DemoHandoffError('demo_external_operation_active',
                                   'external writer operation is already active')
        try:
            return self._yield_external_if_flat_locked(capability)
        finally:
            self._external_operation_lock.release()

    def _yield_external_if_flat_locked(self, capability):
        self._external_state(capability)
        self._external_exit_readiness()
        with self._account_cycle_lock:
            self.service.pause_entries_without_cycle()
        self._assert_companions_paused_flat()
        self._assert_owned_flat()
        self._assert_account_reconciled()
        adapter = self._adapter()
        self._assert_exchange_flat(adapter)
        persisted = self.data_store.get_document(STATE_KEY, {}) or {}
        self.data_store.put_document(STATE_KEY, {**persisted, 'entriesEnabled': False,
            'yieldedFlat': True, 'status': {'running': False, 'entries_enabled': False,
            'status': 'yielded_flat'}, 'recoveryPolicy': 'explicit_start_after_flat_account_handoff'})
        if self._file_lock is not None:
            fcntl.flock(self._file_lock, fcntl.LOCK_UN)
            self._file_lock.close()
        self._file_lock = None
        self.service = None
        self._external_capability = None
        self._external_pid = None
        self._external_lock_identity = None
        self._yielded_flat = True
        return self.status()

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
                "researchId": self._research_id,
                "riskGate": copy.deepcopy(self._risk_gate),
                "acceptanceRound": copy.deepcopy(self._acceptance_round),
                "entriesEnabled": state["entries_enabled"], "yieldedFlat": False,
                "status": state, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"}

    def _restore_active_store(self, persisted):
        namespace = persisted.get("virtualAccountKey", "demo_default")
        if namespace != self.demo_store.virtual_account_key:
            self.demo_store = self.ledger_factory(namespace)
        self._credential_fingerprint = persisted.get("credentialFingerprint")
        research_id = persisted.get("researchId")
        saved_gate = persisted.get("riskGate")
        identity = self._gate_identity(saved_gate)
        self._risk_gate_binding_missing = bool(persisted.get("selectedAccountId")) and (
            identity is None or identity[0] != research_id)
        self._research_id = research_id if identity is not None and identity[0] == research_id else None
        self._risk_gate = copy.deepcopy(saved_gate) if self._research_id else None
        self._acceptance_round = copy.deepcopy(persisted.get("acceptanceRound"))

    def _shared_research_id(self, shared, *, account_id=None, namespace=None):
        """Resolve a shared gate only when every persisted member agrees."""
        if not isinstance(shared, dict):
            return None
        records = shared.get("sessions")
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_SHARED_SESSIONS:
            return None
        namespaces = [record.get("virtualAccountKey") for record in records if isinstance(record, dict)]
        if (len(namespaces) != len(records)
                or any(not isinstance(value, str) or not value for value in namespaces)
                or len(set(namespaces)) != len(namespaces)):
            return None
        primary = shared.get("primaryVirtualAccountKey")
        if not isinstance(primary, str):
            return None
        by_namespace = {record["virtualAccountKey"]: record for record in records}
        primary_record = by_namespace.get(primary)
        identity = self._gate_identity(shared.get("riskGate"))
        if (primary_record is None or identity is None
                or identity != self._gate_identity(primary_record.get("riskGate"))
                or shared.get("researchId") != identity[0]
                or primary_record.get("researchId") != identity[0]):
            return None
        for record in records:
            if (record.get("researchId") != identity[0]
                    or self._gate_identity(record.get("riskGate")) != identity
                    or not isinstance(record.get("selectedAccountId"), str)
                    or not record["selectedAccountId"]):
                return None
        if account_id is not None and primary_record.get("selectedAccountId") != str(account_id):
            return None
        if namespace is not None and namespace not in by_namespace:
            return None
        return identity[0]

    def recoverable_research_id(self, account_id):
        """Resolve only a complete frozen identity for an existing /start request."""
        with self._mutex:
            account_id = str(account_id)
            if self.service:
                if self._shared_enabled:
                    shared = self.data_store.get_document(SHARED_STATE_KEY, {}) or {}
                    return self._shared_research_id(shared, account_id=account_id)
                if (self.service.config.strategy_id != account_id or self._risk_gate_binding_missing
                        or self._gate_identity(self._risk_gate) is None
                        or self._risk_gate.get("research_id") != self._research_id):
                    return None
                return self._research_id
            shared = self.data_store.get_document(SHARED_STATE_KEY, {}) or {}
            if shared.get("sessions"):
                return self._shared_research_id(shared, account_id=account_id)
            persisted = self.data_store.get_document(STATE_KEY, {}) or {}
            if not isinstance(persisted, dict):
                return None
            if persisted.get("selectedAccountId"):
                identity = self._gate_identity(persisted.get("riskGate"))
                if (persisted.get("selectedAccountId") != account_id or identity is None
                        or persisted.get("researchId") != identity[0]):
                    return None
                return identity[0]
            if self._ledger_has_session_history():
                return None
            return None

    def recoverable_session_research_id(self, namespace):
        """Resolve the exact frozen identity for an existing session namespace."""
        with self._mutex:
            namespace = str(namespace)
            shared = self.data_store.get_document(SHARED_STATE_KEY, {}) or {}
            if isinstance(shared, dict) and shared.get("sessions"):
                return self._shared_research_id(shared, namespace=namespace)
            if (self.service and self.service.config.virtual_account_key == namespace
                    and not self._risk_gate_binding_missing
                    and self._gate_identity(self._risk_gate) is not None
                    and self._risk_gate.get("research_id") == self._research_id):
                return self._research_id
            persisted = self.data_store.get_document(STATE_KEY, {}) or {}
            if (not isinstance(persisted, dict)
                    or persisted.get("virtualAccountKey") != namespace):
                return None
            identity = self._gate_identity(persisted.get("riskGate"))
            if identity is None or persisted.get("researchId") != identity[0]:
                return None
            return identity[0]

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
        # 独立于策略 namespace 的账户风险账本。没有经核验账户范围时
        # 沿用同一凭据身份；不把策略切换产生的新订单账本当作风险重置。
        scope = self.risk_account_scope or self._credential_fingerprint
        risk_store = (self.ledger_factory("account_risk_" + hashlib.sha256(
            str(scope).encode()).hexdigest()[:48]) if scope else ledger)
        required = bool(self.risk_account_scope or self.acceptance_round_required
                        or self._acceptance_round or callable(self.acceptance_round_provider)
                        or self.safety_runtime_factory is not None)
        service = DemoExecutionService(adapter, ledger, self.paper, config,
                                    clock_ms=self.clock_ms,
                                    risk_store=risk_store,
                                    safety_runtime_required=required,
                                    account_writer_lock=self._account_cycle_lock,
                                    native_protection_required=bool(
                                        self.acceptance_round_required or self._acceptance_round),
                                    gate_registry=self.gate_registry)
        if callable(self.safety_runtime_factory):
            try:
                runtime = self.safety_runtime_factory(account_scope=scope, ledger=ledger,
                    adapter=adapter, account_lock=self._account_cycle_lock,
                    strategy_id=str(account_id), acceptance_round=copy.deepcopy(self._acceptance_round))
                from demo_safety_runtime import SafetyRuntime
                if not isinstance(runtime, SafetyRuntime):
                    raise ValueError("内部安全工厂未返回 SafetyRuntime")
                runtime.bind(service)
                service.safety_runtime = runtime
            except Exception as error:
                # 保护恢复仍可启动；入口由最终执行边界关闭。
                service._status["safety_runtime_error"] = type(error).__name__
        return service

    def _create(self, account_id, *, entries_enabled, research_id=None):
        if self.driver_mode == 'external_paused' and entries_enabled:
            raise DemoHandoffError('demo_external_entries_forbidden', 'external writer entries remain closed')
        if entries_enabled:
            self._require_acceptance_round(account_id)
        if self.risk_gate_required and entries_enabled:
            self._require_risk_gate(
                self._research_id if research_id is None else research_id,
                account_id=account_id)
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
            "researchId": self._research_id,
            "riskGate": copy.deepcopy(self._risk_gate),
            "acceptanceRound": copy.deepcopy(self._acceptance_round),
            "entriesEnabled": False, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"})
        service = self._service(account_id, self.demo_store, adapter)
        if self.driver_mode == 'external_paused':
            # 同步首次周期即可能执行既有保护；后续失败仍保留服务引用与 flock。
            self.service = service
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
        if self.driver_mode == 'external_paused':
            return
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

    def start(self, account_id, *, research_id=None):
        self._external_refuse_legacy()
        """Invoked by the user's frontend start action; never a live start."""
        with self._mutex:
            self._require_not_closing()
            self._require_entry_gates()
            if not self.service:
                shared = self.data_store.get_document(SHARED_STATE_KEY, {})
                if not isinstance(shared, dict):
                    raise DemoHandoffError("demo_shared_recovery_incomplete",
                                           "shared Demo recovery registry is invalid")
                if shared.get("sessions"):
                    self._restore_shared({**shared, "yieldedFlat": False})
                    self._require_shared_ready()
                else:
                    persisted = self.data_store.get_document(STATE_KEY, {}) or {}
                    if not isinstance(persisted, dict):
                        raise DemoHandoffError("risk_gate_identity_missing",
                                               "the persisted Demo session identity is invalid")
                    if persisted.get("selectedAccountId"):
                        if str(persisted["selectedAccountId"]) != str(account_id):
                            raise DemoHandoffError("risk_gate_identity_mismatch",
                                                   "the Demo session is bound to another paper identity")
                        self._restore_active_store(persisted)
                    elif self._ledger_has_session_history():
                        self._risk_gate_binding_missing = True
            if self.risk_gate_required and self._research_id and research_id is not None \
                    and research_id != self._research_id:
                raise DemoHandoffError("risk_gate_identity_mismatch",
                                       "the Demo session is bound to another research id")
            effective_research_id = self._research_id if research_id is None else research_id
            self._require_acceptance_round(account_id)
            if self._shared_enabled and self.service:
                if self.service.config.strategy_id != account_id:
                    raise ValueError("Demo primary session is bound to another frozen identity")
                return self.start_session(self.service.config.virtual_account_key,
                                          research_id=effective_research_id)
            self._error = None
            self._recovery_mode = False
            if self.service:
                if self.service.config.strategy_id != account_id:
                    raise ValueError("Demo account is permanently bound to the selected frozen identity")
                self._require_risk_gate(effective_research_id, account_id=account_id)
                # A prior worker-start failure can leave a legally persisted,
                # paused service. Establish continuous protection before an
                # explicit start can synchronously submit its next entry.
                self._start_worker()
                if self._acceptance_round is not None:
                    self._save()
                self.service.enable_entries()
                self._save()
            else:
                self._create(account_id, entries_enabled=True, research_id=effective_research_id)
            self._wake.set()
            return self.status()

    def stop(self, *, flatten=False):
        self._external_refuse_legacy()
        """Pauses new entries; polling remains active for fills and owned exits."""
        with self._mutex:
            self._require_not_closing()
            if self.service:
                with self._account_cycle_lock:
                    self.service.stop(flatten=flatten)
                self._save()
                self._wake.set()
            return self.status()

    def attach(self, account_id, *, research_id=None):
        self._external_refuse_legacy()
        """Add a new immutable, paused companion; explicit start opens its gate."""
        self._require_acceptance_exclusive()
        self._require_risk_gate(research_id, account_id=account_id)
        return self._attach(account_id, research_id=research_id)

    def adopt_existing_session(self, namespace):
        self._external_refuse_legacy()
        """Resume an existing immutable namespace without changing its boundary."""
        with self._mutex:
            self._require_not_closing()
            self._require_acceptance_exclusive()
            if namespace == self.demo_store.virtual_account_key or namespace in self._parallel_sessions:
                return self.status()
            ledger = self.ledger_factory(str(namespace))
            binding = ledger.get_binding() or {}
            binding = binding.get("payload", binding)
            if not binding.get("strategy_id"):
                raise DemoHandoffError("demo_session_binding_missing", "existing namespace has no strategy binding")
            return self._attach(binding["strategy_id"], existing_ledger=ledger,
                                research_id=self._research_id)

    def _attach(self, account_id, *, existing_ledger=None, research_id=None):
        self._external_refuse_legacy()
        with self._mutex:
            self._require_not_closing()
            self._require_acceptance_exclusive()
            if existing_ledger is None:
                self._require_risk_gate(research_id or self._research_id,
                                        account_id=account_id)
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
                raise DemoHandoffError("demo_shared_session_limit", "at most six Demo sessions may share the account")
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
                    try:
                        self._save()
                    except Exception as exc:
                        self._error = "Demo shared recovery persistence failed: " + type(exc).__name__
                    self._start_worker()
                raise

    def start_session(self, namespace, *, research_id=None):
        self._external_refuse_legacy()
        with self._mutex:
            self._require_not_closing()
            effective_research_id = self._research_id if research_id is None else research_id
            self._require_shared_ready()
            service = self._find_session(namespace)
            self._require_acceptance_round(service.config.strategy_id)
            self._require_risk_gate(effective_research_id,
                                    account_id=service.config.strategy_id)
            self._start_worker()
            if self._acceptance_round is not None:
                self._save()
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
        self._external_refuse_legacy()
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
        self._external_refuse_legacy()
        """Assemble every member before any cycle; restore all entry gates shut."""
        if self._services() and not self._restore_incomplete and not self._session_errors:
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
        # Preserve an existing primary binding if the shared registry predates
        # acceptance support. Paused recovery must not erase stronger policy.
        self._acceptance_mode()
        self._shared_enabled = True
        if self._acceptance_round is None:
            self._acceptance_round = copy.deepcopy(persisted.get("acceptanceRound"))
        self._shared_records = {record["virtualAccountKey"]: copy.deepcopy(record) for record in records}
        self._credential_fingerprint = persisted.get("credentialFingerprint")
        research_id = persisted.get("researchId")
        saved_gate = persisted.get("riskGate")
        top_identity = self._gate_identity(saved_gate)
        primary_record = self._shared_records.get(primary, {})
        primary_identity = self._gate_identity(primary_record.get("riskGate"))
        member_identities = [self._gate_identity(record.get("riskGate"))
                             for record in self._shared_records.values()]
        self._risk_gate_binding_missing = (
            top_identity is None or top_identity != primary_identity
            or research_id != top_identity[0]
            or any(identity != top_identity for identity in member_identities))
        self._research_id = research_id if not self._risk_gate_binding_missing else None
        self._risk_gate = copy.deepcopy(saved_gate) if not self._risk_gate_binding_missing else None
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
        self._external_refuse_legacy()
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
                    "maxSharedSessions": MAX_SHARED_SESSIONS,
                    "sharedSessionsEnabled": self._shared_enabled,
                    "researchId": self._research_id,
                    "riskGate": copy.deepcopy(self._risk_gate),
                    "acceptanceRound": copy.deepcopy(self._acceptance_round),
                    "allEntriesEnabled": bool(sessions) and all(member["entriesEnabled"] for member in sessions),
                    "sharedRecoveryIncomplete": self._restore_incomplete,
                    "sharedRegistryPersistenceUnresolved": self._shared_persistence_unresolved,
                    'driverMode': self.driver_mode,
                    'externalDriverActive': self._external_capability is not None}

    def _loop(self):
        try:
            while not self._halt_worker.is_set():
                with self._mutex:
                    services = self._services()
                if not services:
                    break
                # Do not hold the controller mutex around HTTP requests.
                for service in services:
                    # Approval/round closure can change while a worker runs.
                    # Revalidate before every cycle, but always keep protection
                    # available when the authority is closed or unavailable.
                    with self._mutex:
                        try:
                            self._require_acceptance_round(service.config.strategy_id)
                        except DemoHandoffError as exc:
                            self._error = exc.code
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
            # 先看算法单，再看触发后的普通子单与仓位，避免先读空普通列表
            # 后算法单恰好触发而两类列表同时表现为空；这仍不是交易所原子快照。
            conditional_orders = adapter.fetch_open_conditional_orders()
            open_orders = adapter.fetch_open_orders()
            positions = adapter.fetch_positions()
            mode = adapter.fetch_position_mode()
            if (not isinstance(positions, list) or not isinstance(open_orders, list)
                    or not isinstance(conditional_orders, list)):
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
            if conditional_orders:
                raise DemoHandoffError("demo_account_conditional_orders_unresolved",
                                       "Demo account still has actual conditional orders")
            LOGGER.info("Demo账户交接只读检查通过：仓位行数=%s，普通挂单数=0，条件挂单数=0；不代表交易所原子快照",
                        len(positions))
        except DemoHandoffError as error:
            LOGGER.info("Demo账户交接只读检查拒绝：原因码=%s", error.code)
            raise
        except Exception:
            LOGGER.info("Demo账户交接只读检查拒绝：原因码=demo_switch_exchange_check_failed")
            raise DemoHandoffError("demo_switch_exchange_check_failed",
                                   "fresh full-account Demo checks failed") from None

    def _resume_paused_worker(self):
        self._closing = False
        self._halt_worker.clear()
        if self._services():
            self._start_worker()
            self._wake.set()

    def switch(self, account_id, *, join_timeout=12, research_id=None):
        self._external_refuse_legacy()
        """Select a new paused session only after a fully flat account boundary.

        Old namespace bindings never change. This operation retains the same
        account flock, so another local writer cannot enter between sessions.
        A prepared new session has no orders and entries disabled; the old
        recovery pointer remains authoritative until one atomic document save.
        """
        with self._mutex:
            self._require_not_closing()
            self._require_acceptance_exclusive()
            self._require_risk_gate(research_id, account_id=account_id)
            self._assert_companions_paused_flat()
            self._lock()
            self._validate_account(account_id)
            if self.service is None:
                persisted = self.data_store.get_document(STATE_KEY, {})
                if persisted.get("selectedAccountId"):
                    self._restore_active_store(persisted)
                    self._create(persisted["selectedAccountId"], entries_enabled=False,
                                 research_id=research_id)
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
        self._external_refuse_legacy()
        """Yield the account flock only after paused protection is fully joined.

        Failed handoffs keep the service, lock and paused protection. The
        selected frozen identity remains persisted; a later start is explicit.
        """
        with self._mutex:
            self._require_not_closing()
            if self._yielded_flat and not self._services():
                return self.status()
            # 从未拥有账户的空控制器无需加载凭据，也不能写成已核验平仓。
            if (self._file_lock is None and not self._services()
                    and self._thread is None and not self._workers):
                return self.status()
            self._lock()
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
                self._assert_account_reconciled()
                # 工作线程最后一轮可能留下条件单或触发子单；持锁重新读取全账户，
                # 凭据身份沿用内部工厂与持久指纹，不接受外部快照或旧循环结果。
                try:
                    adapter = self._adapter()
                except DemoHandoffError:
                    raise
                except Exception:
                    raise DemoHandoffError("demo_switch_exchange_check_failed",
                                           "fresh full-account Demo checks failed") from None
                self._assert_exchange_flat(adapter)
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
                self._thread = None
                self._workers.clear()
                self._yielded_flat = True
                self._closing = False
                return self.status()
        except Exception:
            with self._mutex:
                self._resume_paused_worker()
                if self._services():
                    try:
                        self._save()
                    except Exception as exc:
                        # Persistence failure cannot prevent paused protection
                        # from resuming while this controller retains the lock.
                        self._error = "Demo paused recovery persistence failed: " + type(exc).__name__
            raise
