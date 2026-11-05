#!/usr/bin/env python3
"""Safe FreqAI training iteration runner.

This module owns the training loop that was intentionally disabled in the
legacy research entrypoints.  It trains only from an isolated FreqAI config,
evaluates the out-of-sample result with fixed gates, and records a challenger.
It never calls ``model_registry.set_champion`` and never edits a live config.

Examples (run from the repository root)::

    .venv/bin/python bot/auto_iteration.py --once --enable-auto-iteration
    .venv/bin/python bot/auto_iteration.py --daemon --interval 21600 --enable-auto-iteration
    .venv/bin/python bot/auto_iteration.py --plan
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable


BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE.parent
for import_root in (PROJECT_ROOT, BASE):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import model_registry as registry  # noqa: E402
from gate_registry import GateRegistry  # noqa: E402


DEFAULT_CONFIG = BASE / "user_data" / "config_freqai_smoke.json"
DEFAULT_REGISTRY = BASE / "user_data" / "model_versions.json"
DEFAULT_STATUS = BASE / "user_data" / "auto_iteration_status.json"
DEFAULT_HISTORY = BASE / "user_data" / "auto_iteration_history.jsonl"
DEFAULT_LOCK = BASE / "user_data" / "auto_iteration.lock"
DEFAULT_STRATEGY = "FreqaiExampleStrategy"
DEFAULT_MODEL = "LightGBMRegressor"
DEFAULT_TIMERANGE = "20240101-20261023"


def auto_iteration_gate_enabled() -> bool:
    return GateRegistry(PROJECT_ROOT).is_enabled("iteration.auto_enabled")

BLOCKED_CONFIG_NAMES = {
    "config_trend_live.json",
    "config_trend_webserver.json",
    "config_dashboard.private.json",
}

# These are deliberately conservative.  A failed gate remains evidence, but
# it is never allowed to change the current champion.
GATE = {
    "min_trades": 100,
    "min_profit_pct": 0.0,
    "max_drawdown_pct": 25.0,
    "min_profit_factor": 1.05,
    "min_sharpe": 0.5,
}


class IterationBusy(RuntimeError):
    """Raised when another training iteration already owns the lock."""


class IterationLock:
    """Non-blocking process lock used to prevent concurrent backtests."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise IterationBusy(f"已有自动迭代任务运行中: {self.path}") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"pid={os.getpid()}\n")
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "IterationLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def _write_status(path: Path | None, payload: dict[str, Any]) -> None:
    if path is not None:
        _json_dump(path, payload)


def _append_history(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


def validate_training_config(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate that a config is a dry-run FreqAI research config."""

    path = Path(config_path).expanduser().resolve()
    if path.name in BLOCKED_CONFIG_NAMES:
        raise ValueError(f"禁止使用 live/webserver 配置做自动训练: {path.name}")
    if not path.exists():
        raise ValueError(f"训练配置不存在: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"训练配置不是有效 JSON: {path}") from exc
    if config.get("dry_run") is not True:
        raise ValueError("自动迭代只接受 dry_run=true 的隔离训练配置")
    freqai = config.get("freqai") or {}
    if freqai.get("enabled") is not True:
        raise ValueError("自动迭代配置必须启用 freqai")
    pairs = (config.get("exchange") or {}).get("pair_whitelist") or []
    if not pairs:
        raise ValueError("自动迭代配置的 pair_whitelist 不能为空")
    config["_path"] = str(path)
    return config


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


def discover_progress_path() -> Path:
    from run_backtest_task import ui_progress_path

    return Path(ui_progress_path())


def _command_config_path(config_path: Path) -> str:
    try:
        return os.path.relpath(config_path, BASE)
    except ValueError:
        return str(config_path)


def default_runner(command: list[str], cwd: Path, _progress_path: Path) -> int:
    env = os.environ.copy()
    sklearn_dylibs = Path(sys.prefix) / "lib" / "python3.12" / "site-packages" / "sklearn" / ".dylibs"
    if sklearn_dylibs.is_dir():
        old = env.get("DYLD_LIBRARY_PATH")
        env["DYLD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(sklearn_dylibs), old]))
    completed = subprocess.run(command, cwd=str(cwd), env=env, check=False)
    return completed.returncode


def _read_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _register_challenger(
    *,
    candidate: dict[str, Any],
    metrics: dict[str, Any],
    gate: dict[str, Any],
    registry_path: Path,
    run_id: str,
) -> dict[str, Any]:
    reg = registry.load(str(registry_path))
    entry = deepcopy(candidate)
    entry.setdefault("layer", "ml_model")
    entry["status"] = "challenger"
    entry.setdefault("source", "auto_iteration")
    entry["deployed"] = None
    entry["metrics"] = deepcopy(metrics)
    entry["gate"] = deepcopy(gate)
    entry["evaluation_status"] = "accepted" if gate["passed"] else "rejected"
    entry["run_id"] = run_id
    registry.upsert(reg, entry)
    registry.save(reg, str(registry_path))
    return entry


def _already_done(registry_path: Path, candidate_id: str, run_id: str) -> bool:
    if not registry_path.exists():
        return False
    current = registry.load(str(registry_path))
    old = registry.get(current, candidate_id)
    return bool(old and old.get("run_id") == run_id)


def _generic_iteration(
    *,
    candidate: dict[str, Any],
    train_fn: Callable[[], dict[str, Any]],
    evaluate_fn: Callable[[dict[str, Any]], dict[str, Any]],
    registry_path: Path,
    progress_path: Path,
    lock_path: Path,
    run_id: str,
    enable_auto_iteration: bool,
    history_path: Path | None,
) -> dict[str, Any]:
    if not enable_auto_iteration:
        return {"status": "disabled", "run_id": run_id}
    try:
        lock = IterationLock(lock_path)
        lock.acquire()
    except IterationBusy:
        return {"status": "busy", "run_id": run_id}
    try:
        if _already_done(registry_path, candidate["id"], run_id):
            return {"status": "skipped", "run_id": run_id, "version_id": candidate["id"]}
        try:
            artifact = train_fn()
            metrics = evaluate_fn(artifact)
        except Exception as exc:
            result = {"status": "failed", "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
            _write_status(progress_path, result)
            _append_history(history_path, result)
            return result
        gate = build_gate(metrics)
        result = {"status": "rejected", "run_id": run_id, "version_id": candidate["id"], "gate": gate}
        if gate["valid"]:
            entry = _register_challenger(candidate=candidate, metrics=metrics, gate=gate,
                                          registry_path=registry_path, run_id=run_id)
            result["entry"] = entry
            if gate["passed"]:
                result["status"] = "registered"
        _write_status(progress_path, result)
        _append_history(history_path, result)
        return result
    finally:
        lock.release()


def run_iteration(
    *,
    # Injectable API used by tests and by future non-FreqAI trainers.
    candidate: dict[str, Any] | None = None,
    train_fn: Callable[[], dict[str, Any]] | None = None,
    evaluate_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    enable_auto_iteration: bool = False,
    run_id: str | None = None,
    # FreqAI subprocess API.
    config_path: str | os.PathLike[str] | None = None,
    strategy: str = DEFAULT_STRATEGY,
    freqaimodel: str = DEFAULT_MODEL,
    timerange: str = DEFAULT_TIMERANGE,
    identifier: str | None = None,
    runner: Callable[[list[str], Path, Path], int] | None = None,
    registry_path: str | os.PathLike[str] = DEFAULT_REGISTRY,
    status_path: str | os.PathLike[str] = DEFAULT_STATUS,
    progress_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] = DEFAULT_LOCK,
    history_path: str | os.PathLike[str] | None = DEFAULT_HISTORY,
) -> dict[str, Any]:
    """Run one iteration, using either injected functions or FreqAI."""

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reg_path = Path(registry_path)
    status = Path(status_path) if status_path else None
    if progress_path:
        progress = Path(progress_path).expanduser()
        if not progress.is_absolute():
            progress = progress.resolve()
    elif enable_auto_iteration:
        progress = discover_progress_path()
    else:
        # Disabled calls must not import Freqtrade or touch its UI progress file.
        progress = DEFAULT_STATUS.with_name("auto_iteration_progress.json")
    lock = Path(lock_path)
    history = Path(history_path) if history_path else None

    if not auto_iteration_gate_enabled():
        result = {
            "status": "disabled",
            "run_id": run_id,
            "gate": "iteration.auto_enabled",
            "reason": "iteration.auto_enabled is disabled",
        }
        _write_status(status, result)
        _append_history(history, result)
        return result

    if candidate is not None or train_fn is not None or evaluate_fn is not None:
        if candidate is None or train_fn is None or evaluate_fn is None:
            raise TypeError("candidate、train_fn、evaluate_fn 必须同时提供")
        result = _generic_iteration(
            candidate=candidate,
            train_fn=train_fn,
            evaluate_fn=evaluate_fn,
            registry_path=reg_path,
            progress_path=progress,
            lock_path=lock,
            run_id=run_id,
            enable_auto_iteration=enable_auto_iteration,
            history_path=history,
        )
        if result.get("status") not in {"busy", "disabled", "skipped"}:
            _write_status(status, result)
        return result

    if not enable_auto_iteration:
        result = {"status": "disabled", "run_id": run_id}
        _write_status(status, result)
        return result
    if config_path is None:
        config_path = DEFAULT_CONFIG
    try:
        config = validate_training_config(config_path)
    except Exception as exc:
        result = {"status": "failed", "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
        _write_status(status, result)
        _append_history(history, result)
        return result

    freqai = config["freqai"]
    identifier = identifier or freqai.get("identifier") or f"auto-{run_id}"
    candidate = {
        "id": f"auto-{identifier}-{run_id}",
        "label": f"自动迭代 {strategy} / {identifier}",
        "spec": {
            "strategy": strategy,
            "freqaimodel": freqaimodel,
            "timerange": timerange,
            "identifier": identifier,
            "config": str(Path(config["_path"])),
        },
        "artifacts": [str(progress)],
    }
    command = [
        sys.executable,
        str(BASE / "run_backtest_task.py"),
        "--config", _command_config_path(Path(config["_path"])),
        "--strategy", strategy,
        "--freqaimodel", freqaimodel,
        "--timerange", timerange,
        "--identifier", identifier,
        "--progress", str(progress),
    ]
    runner = runner or default_runner
    start = time.time()
    try:
        lock_obj = IterationLock(lock)
        lock_obj.acquire()
    except IterationBusy:
        result = {"status": "busy", "run_id": run_id, "version_id": candidate["id"]}
        _write_status(status, result)
        return result
    try:
        running = {"status": "running", "run_id": run_id, "version_id": candidate["id"],
                   "config": str(config["_path"]), "strategy": strategy, "model": freqaimodel,
                   "timerange": timerange, "started_at": datetime.now(timezone.utc).isoformat()}
        _write_status(status, running)
        try:
            returncode = int(runner(command, BASE, progress))
        except Exception as exc:
            returncode = -1
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None
        progress_data = _read_progress(progress)
        if returncode != 0:
            result = {"status": "failed", "run_id": run_id, "version_id": candidate["id"],
                      "returncode": returncode, "elapsed_seconds": round(time.time() - start, 1)}
            if error:
                result["error"] = error
            _write_status(status, result)
            _append_history(history, result)
            return result
        metrics = progress_data.get("result") or {}
        if not metrics or metrics.get("error"):
            result = {"status": "failed", "run_id": run_id, "version_id": candidate["id"],
                      "returncode": returncode, "error": metrics.get("error", "训练没有产出可解析结果")}
            _write_status(status, result)
            _append_history(history, result)
            return result
        gate = build_gate(metrics)
        entry = None
        if gate["valid"]:
            entry = _register_challenger(candidate=candidate, metrics=metrics, gate=gate,
                                         registry_path=reg_path, run_id=run_id)
        result = {"status": "registered" if gate["passed"] else "rejected",
                  "run_id": run_id, "version_id": candidate["id"], "gate": gate,
                  "entry": entry, "returncode": returncode,
                  "elapsed_seconds": round(time.time() - start, 1)}
        _write_status(status, result)
        _append_history(history, result)
        return result
    finally:
        lock_obj.release()


def _plan(config_path: Path, strategy: str, model: str, timerange: str) -> dict[str, Any]:
    config = validate_training_config(config_path)
    return {
        "status": "planned",
        "config": config["_path"],
        "strategy": strategy,
        "freqaimodel": model,
        "timerange": timerange,
        "identifier": config["freqai"].get("identifier"),
        "writes_registry": False,
        "changes_live_config": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全 FreqAI 自动迭代")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--daemon", action="store_true", help="按 interval 持续运行")
    parser.add_argument("--interval", type=int, default=6 * 3600)
    parser.add_argument("--plan", action="store_true", help="只校验并打印计划，不训练、不写注册表")
    parser.add_argument("--enable-auto-iteration", action="store_true",
                        help="显式允许训练并登记 challenger；默认只保持禁用")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--freqaimodel", default=DEFAULT_MODEL)
    parser.add_argument("--timerange", default=DEFAULT_TIMERANGE)
    parser.add_argument("--identifier", default=None)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--status", default=str(DEFAULT_STATUS))
    parser.add_argument("--progress", default=None)
    parser.add_argument("--lock", default=str(DEFAULT_LOCK))
    parser.add_argument("--history", default=str(DEFAULT_HISTORY))
    args = parser.parse_args(argv)
    config = Path(args.config)
    if args.plan:
        print(json.dumps(_plan(config, args.strategy, args.freqaimodel, args.timerange), ensure_ascii=False, indent=2))
        return 0

    def one() -> dict[str, Any]:
        result = run_iteration(
            config_path=config, strategy=args.strategy, freqaimodel=args.freqaimodel,
            timerange=args.timerange, identifier=args.identifier, registry_path=args.registry,
            status_path=args.status, progress_path=args.progress, lock_path=args.lock,
            history_path=args.history,
            enable_auto_iteration=args.enable_auto_iteration,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return result

    if not args.daemon or args.once:
        result = one()
        return 0 if result.get("status") in {"registered", "rejected"} else 1
    while True:
        one()
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
