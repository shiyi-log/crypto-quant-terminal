"""Independent read-only runtime health; never equate import completion with data health."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

from runtime_config import storage_layout
from tools.storage_migration import probe_volume


def _timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and math.isfinite(value):
            return datetime.fromtimestamp(value / (1000 if abs(value) > 10**11 else 1), timezone.utc)
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _component(key: str, label: str) -> dict[str, Any]:
    return {"key": key, "label": label, "state": "unknown", "updated_at": None,
            "last_success_at": None, "age_seconds": None, "error_codes": [], "metrics": {}}


def _flag(item: dict, code: str, severity: str = "failed") -> None:
    if code not in item["error_codes"]:
        item["error_codes"].append(code)
    if item["state"] != "failed" or severity == "failed":
        item["state"] = severity


def _metrics(payload: dict, fields: tuple[str, ...]) -> dict:
    # Status files may contain exception text and credentials. Only numeric facts cross this boundary.
    return {key: value for key in fields if isinstance(value := payload.get(key), (int, float, bool))
            and (not isinstance(value, float) or math.isfinite(value))}


def _heartbeat(item: dict, value: Any, now: datetime, max_age: int) -> None:
    stamp = _timestamp(value)
    if stamp is None:
        _flag(item, "status_timestamp_missing", "unknown")
        return
    item["updated_at"] = _iso(stamp)
    age = (now - stamp).total_seconds()
    item["age_seconds"] = round(age, 1)
    if age < -30:
        _flag(item, "status_clock_skew")
    elif age > max_age:
        _flag(item, "status_stale")


def _read(path: Path) -> tuple[dict | None, str | None]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return None, "status_invalid"
        result = json.loads(path.read_text(encoding="utf-8"))
        return (result, None) if isinstance(result, dict) else (None, "status_invalid")
    except FileNotFoundError:
        return None, "status_missing"
    except (OSError, ValueError):
        return None, "status_invalid"


def _collection(key: str, label: str, path: Path, now: datetime) -> dict:
    item = _component(key, label)
    payload, error = _read(path)
    if error:
        _flag(item, error, "unknown")
        return item
    item["state"] = "ok"
    item["metrics"] = _metrics(payload, ("received", "persisted", "spool_batches", "queue_length",
        "gap_alert_count", "data_gap_possible", "database_failures", "write_failure_batches",
        "dead_letter_batches", "dropped", "corrupt_batches"))
    _heartbeat(item, payload.get("updated_at"), now, 30)
    if payload.get("state") not in {"running", "degraded"}:
        _flag(item, "collection_not_running")
    elif payload.get("state") == "degraded":
        _flag(item, "collection_degraded", "warning")
    message = _timestamp(payload.get("last_message"))
    if message is None:
        _flag(item, "collection_no_messages", "warning")
    elif (now - message).total_seconds() > 60:
        _flag(item, "collection_messages_stale")
    elif (now - message).total_seconds() < -30:
        _flag(item, "status_clock_skew")
    last_flush = _timestamp(payload.get("last_flush"))
    item["last_success_at"] = _iso(last_flush)
    if last_flush is None:
        _flag(item, "collection_no_flush", "warning")
    elif (now - last_flush).total_seconds() > 120:
        _flag(item, "collection_flush_stale")
    for field, code in (("last_error", "collection_error"), ("database_error", "collection_database_error"),
                         ("write_failure_batches", "collection_write_pending"),
                         ("dead_letter_batches", "collection_dead_letter"), ("corrupt_batches", "collection_corrupt")):
        if payload.get(field):
            _flag(item, code)
    if payload.get("data_gap_possible"):
        _flag(item, "collection_gap_possible", "warning")
    if payload.get("storage_warning"):
        _flag(item, "collection_storage_warning", "warning")
    return item


def _sync(payload: dict | None, now: datetime) -> dict:
    item = _component("sync", "来源文件同步")
    if not isinstance(payload, dict) or not payload:
        _flag(item, "status_missing", "unknown")
        return item
    item["state"] = "ok"
    item["metrics"] = _metrics(payload, ("running", "files", "rows", "errors", "storage_errors", "rejected_rows"))
    _heartbeat(item, payload.get("last_at") or payload.get("finished_at") or payload.get("started_at"), now, 180)
    item["last_success_at"] = _iso(_timestamp(payload.get("last_success_at")))
    if payload.get("error_code") or payload.get("status") == "error" or payload.get("errors") or payload.get("storage_errors"):
        _flag(item, "sync_error")
    elif payload.get("status") not in {"running", "completed"}:
        _flag(item, "status_invalid", "unknown")
    return item


def _archive(payload: dict | None) -> dict:
    # This is an in-process live snapshot. Its writer exposes no success timestamp yet.
    item = _component("archive", "REST K 线归档")
    if not isinstance(payload, dict) or not payload:
        _flag(item, "status_missing", "unknown")
        return item
    item["metrics"] = _metrics(payload, ("running", "closed", "pending_rows", "pending_datasets",
                                         "failures", "rejected_offers"))
    if not isinstance(payload.get("pending_rows"), int) or not isinstance(payload.get("closed"), bool):
        _flag(item, "status_invalid", "unknown")
        return item
    item["state"] = "ok"
    if payload.get("last_error") or payload.get("error"):
        _flag(item, "archive_error")
    if payload.get("closed"):
        _flag(item, "archive_closed")
    if payload.get("pending_rows", 0) > 0:
        _flag(item, "archive_pending", "warning")
    return item


def _integrity(path: Path, now: datetime) -> dict:
    item = _component("integrity", "数据完整性检查")
    payload, error = _read(path)
    if error:
        _flag(item, error, "unknown")
        return item
    item["state"] = "ok"
    item["metrics"] = _metrics(payload, ("ok", "cycles", "error_count", "dataset_count",
                                         "failed_dataset_count", "successful_dataset_count"))
    if (not isinstance(payload.get("ok"), bool) or type(payload.get("error_count")) is not int
            or type(payload.get("dataset_count")) is not int):
        _flag(item, "status_invalid", "unknown")
    _heartbeat(item, payload.get("generated_at_utc"), now, max(600, payload.get("interval_seconds", 300) * 2)
               if isinstance(payload.get("interval_seconds", 300), (int, float)) else 600)
    item["last_success_at"] = _iso(_timestamp(payload.get("last_success_at") or payload.get("last_success_at_utc")))
    if payload.get("ok") is False or payload.get("last_error") or payload.get("error_count", 0):
        _flag(item, "integrity_failed")
    return item


def _storage(root: Path, environ: Mapping[str, str], volume_probe: Callable) -> dict:
    item = _component("storage", "运行数据存储")
    try:
        layout = storage_layout(root, environ=environ)
        item["metrics"] = {"external": layout.external}
        if not layout.external:
            _flag(item, "storage_external_not_configured", "warning")
            return item
        if layout.mount is None:
            _flag(item, "storage_unavailable")
            return item
        observed = volume_probe(layout.mount)
        if not observed.get("mounted"):
            _flag(item, "storage_unavailable")
            return item
        if not layout.volume_uuid or observed.get("uuid") != layout.volume_uuid:
            _flag(item, "storage_identity_mismatch")
            return item
        if observed.get("writable") is not True or not os.access(layout.mount, os.W_OK | os.X_OK):
            _flag(item, "storage_not_writable")
            return item
        free = shutil.disk_usage(layout.mount).free
        item["metrics"]["free_bytes"] = free
        item["state"] = "ok"
        if free < 64 * 1024 * 1024:
            _flag(item, "storage_space_low")
    except (OSError, ValueError, RuntimeError):
        _flag(item, "storage_unavailable")
    return item


def build_runtime_health(root: str | Path, *, now: datetime | None = None,
                         environ: Mapping[str, str] | None = None, sync: dict | None = None,
                         archive: dict | None = None, volume_probe: Callable | None = None) -> dict:
    """Read local snapshots plus caller-supplied DB/live facts; no initialization or mutation."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("health evaluation time requires timezone")
    now = now.astimezone(timezone.utc)
    runtime = root / "database/runtime"
    components = [
        _collection("collection", "合约行情采集", runtime / "market_stream_status.json", now),
        _collection("collection_trx", "TRX 行情采集", runtime / "trx/market_stream_status.json", now),
        _collection("collection_spot", "现货行情采集", runtime / "spot/market_stream_status.json", now),
        _sync(sync, now), _archive(archive),
        _integrity(runtime / "candle_integrity_status.json", now),
        _storage(root, os.environ if environ is None else environ, volume_probe or probe_volume),
    ]
    return {"generated_at": _iso(now), "ok": all(item["state"] == "ok" for item in components),
            "components": components}
