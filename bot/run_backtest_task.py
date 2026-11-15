#!/usr/bin/env python3
"""
FreqAI 回测任务包装器 —— 把运行进度写成 JSON，供中文面板轮询展示。

用法：
    python run_backtest_task.py --config user_data/config_xxx.json \
        --strategy FreqaiFundingStrategy --freqaimodel LightGBMRegressor \
        --timerange 20250101-20261008

进度写入：<freqtrade>/rpc/api_server/ui/installed/run_progress.json
面板通过同源静态路径 /run_progress.json 读取，无需鉴权、无 CORS 问题。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from bot import backtest_artifacts as artifacts
except ModuleNotFoundError:
    import backtest_artifacts as artifacts


def ui_progress_path() -> str:
    """定位 FreqUI 静态目录，进度文件写在这里才能被 API 服务器同源提供。"""
    import freqtrade

    base = os.path.dirname(os.path.abspath(freqtrade.__file__))
    d = os.path.join(base, "rpc", "api_server", "ui", "installed")
    if not os.path.isdir(d):
        raise SystemExit(f"找不到 FreqUI 静态目录: {d}（先执行 freqtrade install-ui）")
    return os.path.join(d, "run_progress.json")


def atomic_write(path: str, payload: dict) -> None:
    import tempfile
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".run-progress-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def resolve_user_data_dir(config: dict) -> str:
    value = os.path.expanduser(str(config.get("user_data_dir") or "user_data"))
    if not os.path.isabs(value):
        value = os.path.join(os.getcwd(), value)
    return os.path.abspath(value)


def backtest_results_dir(config: dict) -> Path:
    return Path(resolve_user_data_dir(config)) / "backtest_results"


def build_backtest_command(
    *,
    config: dict,
    config_path: str,
    strategy: str,
    freqaimodel: str,
    timerange: str,
    strategy_path: str | None = None,
    output_dir: str | None = None,
    data_dir: str | None = None,
) -> list[str]:
    base_dir = Path(__file__).resolve().parent
    userdir = resolve_user_data_dir(config)
    strategy_path = strategy_path or os.path.join(base_dir, "user_data", "strategies")
    strategy_path = os.path.abspath(os.path.expanduser(strategy_path))
    command = [
        sys.executable,
        "-m",
        "freqtrade",
        "backtesting",
        "--config",
        config_path,
        "--userdir",
        userdir,
        "--strategy-path",
        strategy_path,
        "--strategy",
        strategy,
        "--freqaimodel",
        freqaimodel,
        "--timerange",
        timerange,
    ]
    if output_dir:
        command.extend(["--backtest-directory", output_dir, "--cache", "none", "--export", "trades"])
    if data_dir:
        command.extend(["--datadir", data_dir])
    fee = (config.get("exchange") or {}).get("taker_fee")
    if fee is not None:
        command.extend(["--fee", str(float(fee))])
    return command


def parse_result(zip_path: str) -> dict | None:
    """从回测结果 zip 中提取关键绩效指标。"""
    try:
        strategy_name, s = artifacts.read_stats(Path(zip_path))
        pairs = [
            {
                "pair": p["key"],
                "trades": p["trades"],
                "profit_abs": round(p["profit_total_abs"], 2),
                "profit_pct": round(p["profit_total"] * 100, 2),
            }
            for p in s.get("results_per_pair", [])
            if p["key"] != "TOTAL"
        ]
        exits = [
            {
                "reason": e["key"],
                "trades": e["trades"],
                "profit_abs": round(e["profit_total_abs"], 2),
            }
            for e in s.get("exit_reason_summary", [])
        ]

        # 从成交记录重建资金曲线（按天采样，供面板画净值图）
        equity = []
        try:
            tr = sorted(s.get("trades", []), key=lambda x: x["close_timestamp"])
            bal = s["starting_balance"]
            by_day: dict[str, float] = {}
            for t in tr:
                bal += t.get("profit_abs", 0.0)
                day = datetime.fromtimestamp(t["close_timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                by_day[day] = bal
            equity = [[d, round(v, 2)] for d, v in sorted(by_day.items())]
        except Exception:
            equity = []

        return {
            "strategy": strategy_name,
            "equity": equity,
            "start": s["backtest_start"],
            "end": s["backtest_end"],
            "days": round(s.get("backtest_days", 0)),
            "trades": s["total_trades"],
            "trades_long": s.get("trade_count_long", 0),
            "trades_short": s.get("trade_count_short", 0),
            "wins": s["wins"],
            "losses": s["losses"],
            "winrate": round(s["winrate"] * 100, 2),
            "profit_abs": round(s["profit_total_abs"], 2),
            "profit_pct": round(s["profit_total"] * 100, 2),
            "final_balance": round(s["final_balance"], 2),
            "starting_balance": round(s["starting_balance"], 2),
            "max_drawdown_pct": round(s["max_drawdown_account"] * 100, 2),
            "sharpe": round(s["sharpe"], 3),
            "sortino": round(s["sortino"], 3),
            "calmar": round(s["calmar"], 3),
            "profit_factor": round(s["profit_factor"], 3),
            "expectancy": round(s["expectancy"], 4),
            "cagr": round(s["cagr"] * 100, 2),
            "trades_per_day": round(s.get("trades_per_day", 0), 2),
            "pairs": pairs,
            "exits": exits,
        }
    except Exception as exc:  # 主流程将解析错误作为执行失败处理
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--freqaimodel", default="LightGBMRegressor")
    ap.add_argument("--timerange", required=True)
    ap.add_argument("--identifier", default=None, help="默认从 config 的 freqai.identifier 读取")
    ap.add_argument("--progress", default=None, help="覆盖共享 FreqUI 进度文件")
    ap.add_argument("--strategy-path", default=None)
    ap.add_argument("--run-id", default=None, help="不可复用的运行身份")
    ap.add_argument("--reuse-run", default=None, help="仅复用输入指纹完整匹配的已归档运行")
    args = ap.parse_args()

    progress_file = args.progress or ui_progress_path()
    started = time.time()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    try:
        artifacts.safe_component(run_id)
        cfg = artifacts.merge(artifacts.load_config(Path(args.config).resolve()),
                              artifacts.environment_config())
        if cfg.get("freqai_backtest_live_models"):
            raise ValueError("live_model_backtest_not_supported")
        identifier = artifacts.safe_component(str(args.identifier or cfg.get("freqai", {}).get("identifier", "unknown")))
        cfg.setdefault("freqai", {})["identifier"] = identifier
        user_data_dir = resolve_user_data_dir(cfg)
        cfg["user_data_dir"] = user_data_dir
        n_pairs = len(cfg["exchange"]["pair_whitelist"])
        strategy_path = Path(args.strategy_path or Path(__file__).parent / "user_data/strategies").expanduser().resolve()
        datadir = Path(cfg.get("datadir") or Path(user_data_dir) / "data" / cfg["exchange"]["name"]).expanduser().resolve()
        cfg.update({"strategy": args.strategy, "freqaimodel": args.freqaimodel,
                    "timerange": args.timerange, "strategy_path": str(strategy_path),
                    "datadir": str(datadir)})
        run_dir = backtest_results_dir(cfg) / "runs" / run_id
        run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(run_dir, 0o700)
        identity, inventory = artifacts.input_identity(
            cfg, args.strategy, args.freqaimodel, args.timerange, identifier, strategy_path, datadir)
        effective_identifier = identifier[:64] + "-" + run_id
        effective_config = artifacts.merge(cfg, {"freqai": {"identifier": effective_identifier}})
        config_snapshot = run_dir / "config.json"
        atomic_write(str(config_snapshot), effective_config)
        atomic_write(str(run_dir / "inputs.json"), inventory)
        cmd = build_backtest_command(
            config=effective_config, config_path=str(config_snapshot), strategy=args.strategy,
            freqaimodel=args.freqaimodel, timerange=args.timerange,
            strategy_path=str(strategy_path), output_dir=str(run_dir), data_dir=str(datadir),
        )
    except Exception as exc:
        atomic_write(progress_file, {"status": "failed", "execution_status": "failed",
                     "run_id": run_id, "result": None, "percent": 0,
                     "error_code": "input_validation_failed", "error": str(exc)})
        return 2

    state = {
        "status": "running",
        "execution_status": "running",
        "run_id": run_id,
        "effective_identifier": effective_identifier,
        "artifact_dir": str(run_dir),
        "manifest_path": str(run_dir / "manifest.json"),
        "evidence": {"manifest": str((run_dir / "manifest.json").relative_to(user_data_dir)),
                     "inputs": str((run_dir / "inputs.json").relative_to(user_data_dir))},
        "phase": "启动中",
        "strategy": args.strategy,
        "model": args.freqaimodel,
        "timerange": args.timerange,
        "identifier": identifier,
        "pairs_total": n_pairs,
        "trains_total_per_pair": None,
        "trained": 0,
        "total": None,
        "current_pair": None,
        "current_train": 0,
        "percent": 0.0,
        "rate_per_min": None,
        "eta_seconds": None,
        "elapsed_seconds": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recent_log": [],
        "result": None,
        "training_failures": [],
    }
    atomic_write(progress_file, state)

    cached_path, cache_rejection = (artifacts.reuse_result(Path(args.reuse_run).resolve(), identity, run_dir)
                                   if args.reuse_run else (None, None))
    state["result_source"] = "cache" if cached_path else "fresh"
    if cache_rejection:
        state["cache_rejection"] = cache_rejection
    proc = None
    if cached_path is None:
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("FREQTRADE__")}
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace", env=child_env,
            )
        except Exception as exc:
            state.update(status="failed", execution_status="failed", error_code="child_start_failed",
                         error=str(exc))
            atomic_write(progress_file, state)
            return 2

    re_timeranges = re.compile(r"Training (\d+) timeranges")
    re_pair = re.compile(r"Training (\S+), (\d+)/(\d+) pairs .*?, (\d+)/(\d+) trains")
    re_done = re.compile(r"Done training (\S+) \(([\d.]+) secs\)")
    re_backtest = re.compile(r"Running backtesting for Strategy")
    re_training_failure = re.compile(r"Training (\S+) raised exception (\w+)")

    log_tail: list[str] = []

    def pump():
        """读取子进程输出（阻塞行读），更新状态。"""
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            failure = re_training_failure.search(line)
            if failure:
                state["training_failures"].append({"pair": failure.group(1),
                                                   "exception_type": failure.group(2)})
            log_tail.append(line)
            del log_tail[:-40]
            state["recent_log"] = log_tail[-25:]

            m = re_timeranges.search(line)
            if m:
                state["trains_total_per_pair"] = int(m.group(1))
                state["total"] = int(m.group(1)) * n_pairs
                state["phase"] = "训练模型"

            m = re_pair.search(line)
            if m:
                state["current_pair"] = m.group(1)
                state["current_train"] = int(m.group(4))
                per_pair = int(m.group(5))
                pair_idx = int(m.group(2))
                state["trains_total_per_pair"] = per_pair
                state["total"] = per_pair * n_pairs
                state["trained"] = (pair_idx - 1) * per_pair + int(m.group(4)) - 1
                state["phase"] = "训练模型"

            if re_done.search(line):
                state["trained"] = min(state.get("trained", 0) + 1, state.get("total") or 10**9)

            if re_backtest.search(line):
                state["phase"] = "回测撮合"

            if state["total"]:
                state["percent"] = round(min(99.0, state["trained"] / state["total"] * 100), 1)
                el = time.time() - started
                if state["trained"] > 0 and el > 5:
                    rate = state["trained"] / el * 60
                    state["rate_per_min"] = round(rate, 1)
                    remain = max(0, state["total"] - state["trained"])
                    state["eta_seconds"] = round(remain / (rate / 60)) if rate > 0 else None
            state["elapsed_seconds"] = round(time.time() - started)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write(progress_file, state)

    rc = 0
    if proc is not None:
        pump()
        rc = proc.wait()
    state["elapsed_seconds"] = round(time.time() - started)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["eta_seconds"] = None
    try:
        if rc != 0:
            raise ValueError("child_exit_nonzero")
        if state["training_failures"]:
            raise ValueError("model_training_failed")
        state["phase"] = "验证并归档结果"
        atomic_write(progress_file, state)
        current_identity, _ = artifacts.input_identity(
            cfg, args.strategy, args.freqaimodel, args.timerange, identifier, strategy_path, datadir)
        if current_identity != identity:
            raise ValueError("inputs_changed_during_backtest")
        paths = list(run_dir.glob("*.zip"))
        if len(paths) != 1:
            raise ValueError("missing_or_ambiguous_result")
        result_path = paths[0]
        _, stats = artifacts.read_stats(result_path, args.strategy, args.timerange)
        if stats.get("freqaimodel") != args.freqaimodel:
            raise ValueError("result_model_mismatch")
        expected_identifier = effective_identifier
        if cached_path:
            prior_manifest = json.loads((Path(args.reuse_run) / "manifest.json").read_text())
            expected_identifier = prior_manifest["effective_identifier"]
        if stats.get("freqai_identifier") != expected_identifier:
            raise ValueError("result_identifier_mismatch")
        state["effective_identifier"] = expected_identifier
        result = parse_result(str(result_path))
        if not result or result.get("error"):
            raise ValueError("invalid_result_statistics")
        summary = {"run_id": run_id, "identifier": identifier, "strategy": args.strategy,
                   "model": args.freqaimodel, "timerange": args.timerange,
                   "elapsed_seconds": state["elapsed_seconds"], "result": result,
                   "evidence": state["evidence"]}
        atomic_write(str(run_dir / "result.json"), summary)
        manifest = {"schema": "quant-backtest-run-v1", "run_id": run_id,
                    "execution_status": "succeeded", "identity": identity,
                    "effective_identifier": expected_identifier,
                    "result_source": state["result_source"],
                    "cache_source": str(Path(args.reuse_run).resolve()) if cached_path else None,
                    "result_file": result_path.name, "result_sha256": artifacts.digest(result_path),
                    "config_sha256": artifacts.digest(config_snapshot),
                    "inputs_sha256": artifacts.digest(run_dir / "inputs.json")}
        execution_config = run_dir / "execution_config.json" if cached_path else config_snapshot
        manifest.update(execution_config_file=execution_config.name,
                        execution_config_sha256=artifacts.digest(execution_config),
                        config_role="request" if cached_path else "execution")
        # 旧接口读取最新摘要；不可变事实保存在各运行目录。
        atomic_write(str(Path(user_data_dir) / "models" / identifier / "backtest_result.json"), summary)
        atomic_write(str(run_dir / "manifest.json"), manifest)
        state.update(status="done", execution_status="succeeded", phase="已完成",
                     result=result, percent=100.0, eta_seconds=0)
        state["trained"] = state.get("total") or state.get("trained")
    except Exception as exc:
        state.update(status="failed", execution_status="failed", phase="结果验证失败",
                     result=None, error_code=str(exc), error=str(exc),
                     percent=min(99.0, state.get("percent", 0)))
        rc = rc or 2
    atomic_write(str(run_dir / "progress.json"), state)
    atomic_write(progress_file, state)

    print(f"\n进度文件: {progress_file}")
    print(f"状态: {state['status']}  耗时 {state['elapsed_seconds']} 秒")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
