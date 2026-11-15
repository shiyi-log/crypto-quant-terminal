"""可审计的迭代判据和终态留档；不涉及训练或交易。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import uuid
from typing import Any

GATE = {
    "min_trades": 100,
    "min_profit_pct": 0.0,
    "max_drawdown_pct": 25.0,
    "min_profit_factor": 1.05,
    "min_sharpe": 0.5,
}


def _number(metrics: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = metrics.get(name)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value
    return None


def _nonfinite_paths(value: Any, path: str = "metrics") -> list[str]:
    """Find NaN/inf anywhere in an evaluation payload before persistence."""

    bad: list[str] = []
    if isinstance(value, bool) or value is None:
        return bad
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            bad.append(path)
        return bad
    if isinstance(value, dict):
        for key, item in value.items():
            bad.extend(_nonfinite_paths(item, f"{path}.{key}"))
        return bad
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            bad.extend(_nonfinite_paths(item, f"{path}[{index}]"))
    return bad


def build_gate(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate fixed gates and return auditable reasons.

    ``oos_*`` aliases are accepted for the injectable test/analysis API.
    """

    metrics = metrics or {}
    raw_values = {
        "trades": _number(metrics, "trades", "oos_trades"),
        "profit_pct": _number(metrics, "profit_pct", "oos_profit_pct"),
        "max_drawdown_pct": _number(metrics, "max_drawdown_pct", "oos_max_drawdown_pct"),
        "profit_factor": _number(metrics, "profit_factor"),
        "sharpe": _number(metrics, "sharpe", "oos_sharpe"),
    }
    values: dict[str, float | None] = {}
    reasons: list[str] = []
    finite = True
    for key, value in raw_values.items():
        if value is None or not math.isfinite(value):
            finite = False
            values[key] = None
            reasons.append(f"{key} must be finite / {key} 必须是有限数值")
        else:
            values[key] = value
    extra_nonfinite = _nonfinite_paths(metrics)
    if extra_nonfinite:
        finite = False
        reasons.append("metrics contains non-finite values: " + ", ".join(extra_nonfinite[:5]))
    if finite:
        if values["trades"] < GATE["min_trades"]:
            reasons.append(f"trades={values['trades']:.0f} < {GATE['min_trades']}")
        if values["profit_pct"] <= GATE["min_profit_pct"]:
            reasons.append(f"profit_pct={values['profit_pct']:.3f} <= {GATE['min_profit_pct']}")
        if values["max_drawdown_pct"] > GATE["max_drawdown_pct"]:
            reasons.append(f"max_drawdown_pct={values['max_drawdown_pct']:.3f} > {GATE['max_drawdown_pct']}")
        if values["profit_factor"] < GATE["min_profit_factor"]:
            reasons.append(f"profit_factor={values['profit_factor']:.3f} < {GATE['min_profit_factor']}")
        if values["sharpe"] < GATE["min_sharpe"]:
            reasons.append(f"sharpe={values['sharpe']:.3f} < {GATE['min_sharpe']}")
    return {
        "criteria": deepcopy(GATE),
        "values": values,
        "passed": finite and not reasons,
        "valid": finite,
        "reasons": reasons,
    }


def research_verdict(gate: dict[str, Any]) -> str:
    """样本不足只描述为不可评估，避免把它当成研究拒绝。"""
    if not gate.get("valid"):
        return "not_estimable"
    trades = (gate.get("values") or {}).get("trades")
    if trades is None or trades < GATE["min_trades"]:
        return "not_estimable"
    return "passed" if gate.get("passed") else "rejected"


def safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("invalid run_id")
    return value


def state_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """保留旧 status，同时增加执行与研究两个独立维度。"""
    state = payload.get("status")
    gate = payload.get("gate")
    execution = {"registered": "succeeded", "rejected": "succeeded"}.get(state, state)
    if state == "rejected" and isinstance(gate, dict) and not gate.get("valid"):
        execution = "failed"
    return {**payload, "execution_status": execution,
            "research_verdict": research_verdict(gate) if isinstance(gate, dict)
            and execution == "succeeded" else "not_estimable"}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def write_status(path: Path | None, payload: dict[str, Any]) -> None:
    payload.update(state_fields(payload))
    if payload.get("status") in {"registered", "rejected", "failed"}:
        payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    if path is not None:
        write_json(path, payload)


def run_is_finished(history_path: Path | None, run_id: str) -> bool:
    return read_frozen(history_path, run_id) is not None


def read_frozen(history_path: Path | None, run_id: str) -> dict[str, Any] | None:
    if history_path is None:
        return None
    path = history_path.parent / "iterations" / safe_run_id(run_id) / "result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("run_id") == run_id
                and value.get("status") in {"registered", "rejected", "failed"}):
            return value
    except (OSError, ValueError):
        pass
    return None


def freeze_result(path: Path | None, payload: dict[str, Any]) -> None:
    payload.update(state_fields(payload))
    payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    if path is None:
        raise OSError("iteration evidence history is not configured")
    # 每轮终态只写一次。共享最新状态可以覆盖，冻结结果必须保留。
    if payload.get("status") in {"registered", "rejected", "failed"}:
        run_dir = path.parent / "iterations" / safe_run_id(payload["run_id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "result.json"
        frozen = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        temporary = run_dir / (".result-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(frozen)
                handle.flush()
                os.fsync(handle.fileno())
            # link 原子发布且不会覆盖原有事实；半条文件永远不会占用最终路径。
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def append_history(path: Path | None, payload: dict[str, Any], *, freeze: bool = True) -> None:
    payload.update(state_fields(payload))
    payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if freeze:
        freeze_result(path, payload)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


def register_frozen(payload: dict[str, Any], *, history_path: Path | None,
                    status_path: Path | None, register: Any) -> dict[str, Any]:
    """登记收据独立于冻结测量，失败后可直接重试登记，无需重新训练。"""
    if payload.get("entry") is not None:
        try:
            register(payload["entry"])
        except OSError:
            payload = {**payload, "status": "failed", "error_code": "registry_write_failed"}
            payload.update(state_fields(payload))
        if history_path is not None:
            receipt = history_path.parent / "iterations" / payload["run_id"] / "registration.json"
            write_json(receipt, {"run_id": payload["run_id"],
                                 "registered": payload["status"] != "failed",
                                 "error_code": payload.get("error_code")})
    append_history(history_path, payload, freeze=False)
    write_status(status_path, payload)
    return payload


def resume_registration(history_path: Path | None, run_id: str, *,
                        status_path: Path | None, register: Any) -> dict[str, Any] | None:
    frozen = read_frozen(history_path, run_id)
    if frozen is None:
        return None
    if frozen.get("entry") is not None and history_path is not None:
        receipt = history_path.parent / "iterations" / run_id / "registration.json"
        try:
            complete = json.loads(receipt.read_text()).get("registered") is True
        except (OSError, ValueError, AttributeError):
            complete = False
        if not complete:
            return register_frozen(frozen, history_path=history_path, status_path=status_path, register=register)
    return state_fields({"status": "skipped", "run_id": run_id})


def finish_evaluation(payload: dict[str, Any], *, history_path: Path | None,
                      status_path: Path | None, register: Any) -> dict[str, Any]:
    """冻结证据成功后才登记候选，归档失败不能遗留可用候选。"""
    try:
        freeze_result(history_path, payload)
    except OSError:
        payload = {**payload, "status": "failed", "error_code": "archive_failed"}
        payload.pop("entry", None)
        payload.update(state_fields(payload))
    return register_frozen(payload, history_path=history_path, status_path=status_path, register=register)


