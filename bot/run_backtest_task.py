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
import zipfile
from datetime import datetime, timezone


def ui_progress_path() -> str:
    """定位 FreqUI 静态目录，进度文件写在这里才能被 API 服务器同源提供。"""
    import freqtrade

    base = os.path.dirname(os.path.abspath(freqtrade.__file__))
    d = os.path.join(base, "rpc", "api_server", "ui", "installed")
    if not os.path.isdir(d):
        raise SystemExit(f"找不到 FreqUI 静态目录: {d}（先执行 freqtrade install-ui）")
    return os.path.join(d, "run_progress.json")


def atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def parse_result(zip_path: str) -> dict | None:
    """从回测结果 zip 中提取关键绩效指标。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            inner = [n for n in z.namelist() if n.endswith(".json") and "meta" not in n]
            if not inner:
                return None
            data = json.loads(z.read(inner[0]))
        s = list(data["strategy"].values())[0]
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
            "strategy": list(data["strategy"].keys())[0],
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
    except Exception as exc:  # 结果解析失败不应影响主流程
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--freqaimodel", default="LightGBMRegressor")
    ap.add_argument("--timerange", required=True)
    ap.add_argument("--identifier", default=None, help="默认从 config 的 freqai.identifier 读取")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    identifier = args.identifier or cfg.get("freqai", {}).get("identifier", "unknown")
    n_pairs = len(cfg["exchange"]["pair_whitelist"])
    models_dir = os.path.join("user_data", "models", identifier)

    progress_file = ui_progress_path()
    started = time.time()

    cmd = [
        sys.executable, "-m", "freqtrade", "backtesting",
        "--config", args.config,
        "--strategy", args.strategy,
        "--freqaimodel", args.freqaimodel,
        "--timerange", args.timerange,
    ]

    state = {
        "status": "running",
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
    }
    atomic_write(progress_file, state)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )

    re_timeranges = re.compile(r"Training (\d+) timeranges")
    re_pair = re.compile(r"Training (\S+), (\d+)/(\d+) pairs .*?, (\d+)/(\d+) trains")
    re_done = re.compile(r"Done training (\S+) \(([\d.]+) secs\)")
    re_backtest = re.compile(r"Running backtesting for Strategy")
    re_simulating = re.compile(r"Backtesting|Simulating|Trade simulation")

    log_tail: list[str] = []

    def pump():
        """读取子进程输出（阻塞行读），更新状态。"""
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
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
                state["percent"] = round(min(100.0, state["trained"] / state["total"] * 100), 1)
                el = time.time() - started
                if state["trained"] > 0 and el > 5:
                    rate = state["trained"] / el * 60
                    state["rate_per_min"] = round(rate, 1)
                    remain = max(0, state["total"] - state["trained"])
                    state["eta_seconds"] = round(remain / (rate / 60)) if rate > 0 else None
            state["elapsed_seconds"] = round(time.time() - started)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write(progress_file, state)

    pump()
    rc = proc.wait()

    state["status"] = "done" if rc == 0 else "failed"
    state["phase"] = "已完成" if rc == 0 else f"失败 (退出码 {rc})"
    state["trained"] = state.get("total") or state.get("trained")
    state["percent"] = 100.0 if rc == 0 else state.get("percent", 0)
    state["eta_seconds"] = 0
    state["elapsed_seconds"] = round(time.time() - started)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    if rc == 0:
        state["phase"] = "读取结果"
        atomic_write(progress_file, state)
        results = sorted(
            [os.path.join("user_data/backtest_results", f)
             for f in os.listdir("user_data/backtest_results")
             if f.endswith(".zip")],
            key=os.path.getmtime,
        )
        if results:
            state["result"] = parse_result(results[-1])
            # 归档到 identifier 目录，便于多模型横向对比
            try:
                arch = os.path.join(models_dir, "backtest_result.json")
                os.makedirs(models_dir, exist_ok=True)
                with open(arch, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "identifier": identifier,
                            "strategy": args.strategy,
                            "model": args.freqaimodel,
                            "timerange": args.timerange,
                            "elapsed_seconds": state["elapsed_seconds"],
                            "result": state["result"],
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                print(f"结果已归档: {arch}")
            except Exception as exc:
                print(f"结果归档失败: {exc}", file=sys.stderr)
        state["phase"] = "已完成"
    atomic_write(progress_file, state)

    print(f"\n进度文件: {progress_file}")
    print(f"状态: {state['status']}  耗时 {state['elapsed_seconds']} 秒")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
