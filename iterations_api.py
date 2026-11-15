"""从同步后的迭代事件生成只读视图，不从请求路径读取文件。"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any

from bot.iteration_evidence import safe_run_id, state_fields


_FIELDS = {
    "run_id", "status", "execution_status", "research_verdict", "version_id",
    "strategy", "model", "timerange", "started_at", "finished_at",
    "elapsed_seconds", "returncode", "error_code", "gate", "metrics", "evidence", "progress",
    "result_source",
}
_SECRET = re.compile(r"password|secret|token|authorization|credential|api.?key", re.I)


def _public(value: Any) -> Any:
    """公开视图剔除凭据、非有限数值和本机绝对路径。"""
    if isinstance(value, dict):
        return {str(k): _public(v) for k, v in value.items()
                if not _SECRET.search(str(k)) and str(k) != "config"}
    if isinstance(value, list):
        return [_public(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
        return None
    return value


def _project(row: dict[str, Any]) -> dict[str, Any]:
    result = state_fields(row)
    entry = row.get("entry") or {}
    spec = entry.get("spec") or {}
    for key, source in (("strategy", "strategy"), ("model", "freqaimodel"), ("timerange", "timerange")):
        result.setdefault(key, spec.get(source))
    if "metrics" not in result and isinstance(entry.get("metrics"), dict):
        result["metrics"] = entry["metrics"]
    return _public({key: value for key, value in result.items() if key in _FIELDS})


def iterations_payload(store: Any, *, run_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    """按最后事件保留每轮结果，最新状态可补充尚未结束的任务。"""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if run_id is not None:
        safe_run_id(run_id)
    rows = store.read_events("auto_iteration_history.jsonl")
    current = store.get_document("auto_iteration_status.json")
    ordered: dict[str, dict[str, Any]] = {}
    terminal = {"registered", "rejected", "failed", "cancelled"}
    for row in [*(rows or []), current]:
        if not isinstance(row, dict) or not row.get("run_id"):
            continue
        try:
            identity = safe_run_id(row["run_id"])
        except (ValueError, TypeError):
            continue
        previous = ordered.get(identity) or {}
        if previous.get("status") in terminal and row.get("status") not in terminal:
            continue
        ordered.pop(identity, None)
        ordered[identity] = {**previous, **row}
    for identity, row in ordered.items():
        if row.get("status") != "running":
            continue
        progress = store.get_document(f"iterations/{identity}/progress.json")
        if isinstance(progress, dict) and progress.get("run_id") == identity:
            row["progress"] = {key: value for key, value in progress.items()
                               if key in {"status", "percent", "epoch", "epochs", "stage",
                                          "elapsed_seconds", "message", "phase",
                                          "current_train", "trained", "total"}}
    if run_id is not None:
        if run_id not in ordered:
            raise KeyError(run_id)
        return _project(ordered[run_id])
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "total": len(ordered),
            "items": [_project(row) for row in list(ordered.values())[::-1][:limit]]}
