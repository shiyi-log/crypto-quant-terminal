"""Frontend lifecycle for one Demo session, with protected restart recovery."""
from __future__ import annotations

import fcntl
from decimal import Decimal
from pathlib import Path
import threading

from demo_execution_service import DemoExecutionService, DemoRunnerConfig, PaperEventSource

STATE_KEY = "binance_demo_runner_controller.json"


class DemoRunnerController:
    def __init__(self, data_store, demo_store, credential_provider, adapter_factory=None,
                 *, paper_source=None, lock_path=None, clock_ms=None):
        self.data_store, self.demo_store = data_store, demo_store
        self.credential_provider = credential_provider
        if adapter_factory is None:
            from binance_demo_adapter import BinanceDemoAdapter
            adapter_factory = BinanceDemoAdapter
        self.adapter_factory = adapter_factory
        self.paper = paper_source or PaperEventSource(data_store, clock_ms=clock_ms)
        self.lock_path = Path(lock_path or Path(__file__).parent / "database/runtime/binance_demo_runner.lock")
        self.clock_ms = clock_ms
        self.service = None
        self._thread = None
        self._wake = threading.Event()
        self._mutex = threading.RLock()
        self._file_lock = None
        self._error = None
        self._recovery_mode = False
        self._selected_account_id = None

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
        if self.service:
            state = self.service.status()
            self.data_store.put_document(STATE_KEY, {
                "selectedAccountId": self.service.config.strategy_id,
                "virtualAccountKey": self.service.config.virtual_account_key,
                "maxNotionalUsdt": self.service.config.max_notional_usdt,
                "demoPolicyHash": self.service.policy_hash,
                "forwardBoundaryMs": state.get("forward_boundary_ms"),
                "entriesEnabled": state["entries_enabled"],
                "status": state, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"})

    def _create(self, account_id, *, entries_enabled):
        self._lock()
        account = self.paper.account(str(account_id))
        binding = self.demo_store.get_binding()
        if binding and binding.get("strategy_id") != account_id:
            raise ValueError("Demo account is permanently bound to another paper identity")
        if (account["execution"].get("execution_enabled") is False
                or account["execution"].get("risk_sizing_mode") != "entry_atr"):
            raise ValueError("Demo requires an enabled frozen single-leg ATR account")
        self._selected_account_id = str(account_id)
        # Save recovery identity before any network call can create exposure.
        self.data_store.put_document(STATE_KEY, {"selectedAccountId": str(account_id),
            "virtualAccountKey": self.demo_store.virtual_account_key, "maxNotionalUsdt": "1000",
            "entriesEnabled": False, "recoveryPolicy": "pause_entries_continue_owned_risk_protection"})
        credentials = self.credential_provider()
        if credentials is None:
            raise RuntimeError("Demo credentials are not configured")
        adapter = self.adapter_factory(api_key=credentials.api_key, api_secret=credentials.secret,
                                       allow_trading=True)
        # The local variables are not stored on the controller or in documents.
        del credentials
        config = DemoRunnerConfig(str(account_id),
            virtual_account_key=self.demo_store.virtual_account_key, trading_enabled=True)
        service = DemoExecutionService(adapter, self.demo_store, self.paper, config,
                                       clock_ms=self.clock_ms)
        service.start(entries_enabled=entries_enabled)
        self.service = service
        self._save()
        if self._thread is None or not self._thread.is_alive():
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="binance-demo-execution", daemon=True)
            self._thread.start()

    def start(self, account_id):
        """Invoked by the user's frontend start action; never a live start."""
        with self._mutex:
            self._error = None
            self._recovery_mode = False
            if self.service:
                if self.service.config.strategy_id != account_id:
                    raise ValueError("Demo account is permanently bound to the selected frozen identity")
                self.service.enable_entries()
                self._save()
            else:
                self._create(account_id, entries_enabled=True)
            self._wake.set()
            return self.status()

    def stop(self, *, flatten=False):
        """Pauses new entries; polling remains active for fills and owned exits."""
        with self._mutex:
            if self.service:
                self.service.stop(flatten=flatten)
                self._save()
                self._wake.set()
            return self.status()

    def resume_protection(self):
        """On application restart: only reconcile/protect, never resume entries."""
        with self._mutex:
            persisted = self.data_store.get_document(STATE_KEY, {})
            account_id = persisted.get("selectedAccountId")
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
                "status": "not_started", "environment": "binance_demo", "live_enabled": False}
            return {**state, "selectedAccountId": self.service.config.strategy_id if self.service else self._selected_account_id,
                    "entriesEnabled": state["entries_enabled"], "recoveryMode": self._recovery_mode,
                    "controllerError": self._error}

    def _loop(self):
        while self.service is not None:
            service = self.service
            # Do not hold the controller mutex around HTTP requests.
            try:
                service.cycle()
                self._save()
            except Exception as exc:
                self._error = "Demo polling failed: " + type(exc).__name__
            self._wake.wait(service.config.poll_interval_ms / 1000)
            self._wake.clear()

    def close_if_flat(self):
        """Test/operator cleanup only after all durable owned exposure resolves."""
        with self._mutex:
            if self.service:
                self.service.stop()
                if (self.demo_store.net_fill_positions()
                        and any(Decimal(str(qty)) != 0 for qty in self.demo_store.net_fill_positions().values())):
                    raise RuntimeError("cannot stop Demo protection with owned exposure")
                if self.demo_store.pending_orders():
                    raise RuntimeError("cannot stop Demo reconciliation with unresolved orders")
            self.service = None
            self._wake.set()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=12)
            if self._file_lock:
                fcntl.flock(self._file_lock, fcntl.LOCK_UN)
                self._file_lock.close()
                self._file_lock = None
