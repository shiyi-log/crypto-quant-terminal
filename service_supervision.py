"""Shared service reconciliation and bounded supervisor diagnostics."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import json
from pathlib import Path

from service_state import atomic_json, desired_state, lifecycle_lock, set_desired, timestamp


DATABASE_SERVICES = frozenset({
    "auth", "sync", "market", "market-trx", "market-spot", "market-breadth",
    "strategy-lab", "spot-basis", "integrity", "market-live",
})
# These entry points use their own legacy store or serve without PostgreSQL.
DATABASE_INDEPENDENT_SERVICES = frozenset({"api", "webserver", "web"})


def check_service_schema(root: Path) -> None:
    """Check installed global migrations without opening a business pool or migrating."""
    try:
        from data_store import DataStore

        store = DataStore(root=root)
        try:
            result = store.check_schema()
        finally:
            store.close()
        if not isinstance(result, dict) or result.get("status") != "compatible":
            raise RuntimeError("incompatible schema result")
    except Exception:
        # Only a stable public code may reach CLI output, snapshots, or supervisor logs.
        raise RuntimeError("service_schema_preflight_failed") from None


def _requires_database(name: str) -> bool:
    if name in DATABASE_SERVICES:
        return True
    if name in DATABASE_INDEPENDENT_SERVICES:
        return False
    # New lifecycle services must be classified, never implicitly exempted.
    raise RuntimeError("service_dependency_unclassified")


def _database_preflight(manager, root: Path) -> None:
    try:
        manager.ensure_local_postgres(root)
        check_service_schema(root)
    except Exception:
        raise RuntimeError("service_schema_preflight_failed") from None


def run_lifecycle(manager, action: str, selected: list, root: Path) -> int:
    with lifecycle_lock(root):
        names = [spec.name for spec in selected]
        if action in {"start", "restart"}:
            dependencies = [_requires_database(name) for name in names]
            if any(dependencies):
                # Check the whole selected batch before stopping or launching a child,
                # and before changing desired state that a supervisor may act upon.
                _database_preflight(manager, root)
        if action in {"stop", "restart"}:
            # Persist first: a failed stop must never trigger a replacement.
            set_desired(root, names, "stopped")
            if not all([manager.stop_service(spec, root) for spec in reversed(selected)]):
                return 1
        if action in {"start", "restart"}:
            set_desired(root, names, "running")
            for spec in selected:
                manager.start_service(spec, root)
    return 0


def service_snapshot(manager, root: Path, selected: list | None = None) -> list[dict]:
    try:
        previous = json.loads((root / "database/runtime/service_supervisor.json").read_text())
        last_records = {item["key"]: item for item in previous.get("services", [])}
    except (OSError, ValueError, TypeError, KeyError):
        last_records = {}
    result = []
    for spec in (selected if selected is not None else manager.services(root).values()):
        try:
            desired, updated = desired_state(root, spec.name)
            error = None
        except RuntimeError:
            desired, updated, error = "unknown", None, "service_control_invalid"
        try:
            pids = manager.find_processes(spec, allow_legacy=False)
            reason = manager.health(spec) if pids else "not_running"
        except (OSError, RuntimeError):
            pids, reason, error = [], "process_probe_failed", "process_probe_failed"
        if error == "service_control_invalid":
            reason = error
        elif desired == "stopped":
            reason = "operator_stopped"
        if not pids and desired == "running" and error is None:
            error = last_records.get(spec.name, {}).get("last_error")
        result.append({"key": spec.name, "name": spec.label, "port": spec.port,
                       "desired_state": desired, "running": bool(pids),
                       "pid": str(pids[0]) if pids else None, "pids": pids,
                       "health_reason": reason, "last_error": error, "updated_at": updated})
    return result


def write_supervisor_log(root: Path, message: str, *, max_bytes: int = 10_485_760,
                         backups: int = 5) -> None:
    if max_bytes <= 0 or backups < 1:
        raise ValueError("invalid log rotation limits")
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "supervisor.log", maxBytes=max_bytes,
                                  backupCount=backups, encoding="utf-8")
    try:
        # Stable public codes only; exception text can contain credentials.
        handler.emit(logging.LogRecord("quant.supervisor", logging.INFO, "", 0,
                                       f"{timestamp()} {message}", (), None))
    finally:
        handler.close()


def supervise_once(manager, selected: list, root: Path) -> list[dict]:
    with lifecycle_lock(root):
        records = service_snapshot(manager, root, selected)
        database_checked = False
        database_error = False
        for spec, record in zip(selected, records):
            if record["desired_state"] != "running" or record["running"]:
                continue
            try:
                requires_database = _requires_database(spec.name)
            except RuntimeError:
                record["last_error"] = "service_dependency_unclassified"
                record["health_reason"] = "service_dependency_unclassified"
                write_supervisor_log(root, f"{spec.name} service_dependency_unclassified")
                continue
            if requires_database and not database_checked:
                database_checked = True
                try:
                    _database_preflight(manager, root)
                except RuntimeError:
                    database_error = True
            if requires_database and database_error:
                record["last_error"] = "service_schema_preflight_failed"
                record["health_reason"] = "service_schema_preflight_failed"
                write_supervisor_log(root, f"{spec.name} service_schema_preflight_failed")
                continue
            try:
                manager.start_service(spec, root)
                updated = service_snapshot(manager, root, [spec])[0]
                record.update(updated)
            except (OSError, RuntimeError):
                record["last_error"] = "service_start_failed"
                record["health_reason"] = "service_start_failed"
                write_supervisor_log(root, f"{spec.name} service_start_failed")
        atomic_json(root / "database/runtime/service_supervisor.json",
                    {"schema": "quant-service-supervisor-v1",
                     "generated_at": timestamp(), "services": records})
    return records
