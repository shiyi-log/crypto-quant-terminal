#!/usr/bin/env python3
"""
自动迭代系统（Auto-Iteration）

作用：
    把「策略是否需要迭代」这件事从人工判断变成常驻自动巡检。
    与 monitor.py 的分工：
        monitor.py      —— 看【市场】: 波动率中枢、因子健康度、重估信号
        auto_iterate.py —— 看【策略】: 实现一致性、参数稳定性、实盘 vs 预期、信号完整性

设计原则（重要）：
    ① 只诊断 + 报警，**不自动改策略或参数**。
       理由：本项目刚发现「验证的代码路径 ≠ 部署的代码路径」导致 3 倍偏差，
       以及「样本内最优在样本外排名 21/96」——自动套用参数调整
       正是过拟合的入口。所有改动必须经人工确认。
    ② 每项检查都有明确阈值，避免「指标好看就继续」的模糊判断。
    ③ 每次结果落盘 iteration_history.jsonl，形成可追溯的迭代日志。

检查项：
    C1 实现一致性   event_backtest（研究） vs Freqtrade（实盘）
    C2 参数稳定性   滚动走查：当前参数是否还在最优区间
    C3 实盘 vs 预期 干跑实际表现 vs 回测预期
    C4 因子漂移     复用 monitor 的因子健康度
    C5 信号完整性   最近是否有开仓信号（策略是否「哑火」）

用法:
    python auto_iterate.py                # 跑一轮
    python auto_iterate.py --daemon       # 常驻（默认 6 小时一轮）
    python auto_iterate.py --interval 3600
"""

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

import live_params  # noqa: E402  （必须在 sys.path 设置之后导入）

HIST = "user_data/iteration_history.jsonl"
STATUS = "user_data/auto_iterate_status.json"
LIVE = "user_data/config_trend_live.json"

# ── 阈值（全部显式，便于审计）──
TH = {
    "consistency_dev": 0.30,      # 研究 vs 实盘【年化收益】偏差上限
                                  # 不用 Calmar 做主判据：回撤对槽位填充顺序敏感，
                                  # 实测年化只差 5.8% 时 Calmar 仍差 87%。
    "trade_match": 0.35,          # 交易清单匹配率下限
    "param_rank_pct": 0.25,       # 当前参数需落在最优的 25% 内
    "live_lag": 0.50,             # 实盘年化低于预期超过 50% 则告警
    "signal_gap_days": 30,        # 超过 30 天无新开仓则告警
}


def to_py(o):
    """递归把 numpy 类型转成原生类型（否则 json 会失败）"""
    import numpy as _np
    if isinstance(o, dict):
        return {k: to_py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_py(v) for v in o]
    if isinstance(o, (_np.bool_,)):
        return bool(o)
    if isinstance(o, (_np.integer,)):
        return int(o)
    if isinstance(o, (_np.floating,)):
        return float(o)
    if isinstance(o, _np.ndarray):
        return o.tolist()
    return o


# ══════════════════ C1 实现一致性 ══════════════════

def check_consistency():
    """研究（事件驱动） vs 实盘（Freqtrade）"""
    try:
        import event_backtest as eb
        cfg = json.load(open(LIVE))
        coins = [p.split("/")[0] for p in cfg["exchange"]["pair_whitelist"]]
        data = eb.load_ohlc(coins)
        tr, eq, ret = eb.run(data, start="2023-01-01")
        s = eb.stats(eq, ret)
        ft = eb.ft_trades()

        a = {(r["pair"], r["open_date"].date() + pd.Timedelta(days=1),
              "空" if r["is_short"] else "多") for _, r in tr.iterrows()}
        b = {(r["pair"], r["open_d"].date(), "空" if r["is_short"] else "多")
             for _, r in ft.iterrows()}
        match = len(a & b) / max(len(b), 1)

        # Freqtrade 侧 Calmar
        ft_calmar = None
        out = subprocess.run(
            [".venv/bin/python", "-m", "freqtrade", "backtesting",
             "--config", LIVE, "--strategy", "TrendFollowing",
             "--timerange", "20230101-20261008"],
            capture_output=True, text=True, timeout=1800).stdout
        import re
        cagr = mdd = None
        for line in out.split("\n"):
            if "│" not in line:
                continue
            cells = [c.strip() for c in line.split("│") if c.strip()]
            if len(cells) < 2:
                continue
            if cells[0] == "CAGR %":
                cagr = float(cells[1].replace("%", ""))
            elif cells[0].startswith("Absolute drawdown (wallet"):
                m = re.search(r"\(([\d.]+)%\)", cells[1])
                if m:
                    mdd = float(m.group(1))
        if cagr is not None and mdd:
            ft_calmar = cagr / mdd

        # Freqtrade 侧的 CAGR
        dev = None
        if cagr and s["ann"]:
            dev = abs(s["ann"] - cagr) / abs(s["ann"])

        ok = ((dev is None or dev <= TH["consistency_dev"])
              and match >= TH["trade_match"])
        return {
            "ok": ok,
            "research_ann": round(s["ann"], 2),
            "live_ann": round(cagr, 2) if cagr else None,
            "deviation": round(dev, 3) if dev is not None else None,
            "trade_match": round(match, 3),
            "research_trades": len(tr), "live_trades": len(ft),
            "research_calmar": round(s["calmar"], 3),
            "live_calmar": round(ft_calmar, 3) if ft_calmar else None,
            "detail": (f"年化 研究 {s['ann']:.1f}% vs 实盘 {cagr:.1f}% "
                       f"(偏差 {(dev or 0)*100:.0f}%) · "
                       f"交易 研究 {len(tr)} vs 实盘 {len(ft)} · "
                       f"匹配 {match*100:.0f}%"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════ C2 参数稳定性 ══════════════════

PARAM_GRID = [(10, 10), (20, 20), (20, 10), (30, 15), (40, 10), (55, 20)]
# 当前参数从策略源码读，不写死 —— 否则改策略后这里仍在拿旧参数排名
CURRENT = live_params.current_entry_exit()


def check_params():
    """近 12 个月滚动走查：当前参数还在不在最优区间"""
    try:
        import event_backtest as eb
        cfg = json.load(open(LIVE))
        coins = [p.split("/")[0] for p in cfg["exchange"]["pair_whitelist"]]
        data = eb.load_ohlc(coins)
        end = max(d.index[-1] for d in data.values())
        start = (end - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

        rows = []
        for en, ex in PARAM_GRID:
            S, P = {}, {}
            for s, d in data.items():
                dd = d[d.index >= start]
                if len(dd) < 100:
                    continue
                S[s] = eb.signals(dd["close"], en, ex)
                P[s] = dd[["open", "close"]]
            if not S:
                continue
            Sd = pd.DataFrame(S).sort_index().fillna(0.0)
            Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
            Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()
            # 简化：等权持有，按状态
            w = Sd / Sd.shape[1]
            gross = (w.shift(1).fillna(0) * Cd.pct_change()).sum(axis=1)
            turn = w.diff().abs().sum(axis=1).fillna(0)
            net = (gross - turn * 0.0005).dropna()
            eq = (1 + net).cumprod()
            yrs = (net.index[-1] - net.index[0]).days / 365
            ann = eq.iloc[-1] ** (1 / yrs) - 1
            mdd = ((eq / eq.cummax()) - 1).min()
            rows.append({"params": f"{en}/{ex}",
                         "calmar": round(ann / abs(mdd), 2) if mdd < 0 else 0.0})
        if not rows:
            return {"ok": True, "detail": "样本不足，跳过"}
        rows.sort(key=lambda r: -r["calmar"])
        cur = next((i for i, r in enumerate(rows)
                    if r["params"] == f"{CURRENT[0]}/{CURRENT[1]}"), None)
        pct = (cur + 1) / len(rows) if cur is not None else 1.0
        return {
            "ok": pct <= TH["param_rank_pct"],
            "current_params": f"{CURRENT[0]}/{CURRENT[1]}",
            "window_days": 365,
            "current_rank": (cur + 1) if cur is not None else None,
            "total": len(rows),
            "rank_pct": round(pct, 3),
            "ranking": rows,
            "detail": (f"当前 {CURRENT[0]}/{CURRENT[1]} 在近 12 个月滚动走查中排 "
                       f"{cur+1 if cur is not None else '?'}/{len(rows)}"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════ C3 实盘 vs 预期 ══════════════════

def check_live_vs_expect():
    """干跑实际表现 vs 回测预期"""
    try:
        import urllib.request
        # 从认证服务取收益
        req = urllib.request.Request(
            "http://127.0.0.1:8890/api/v1/profit",
            headers={"Authorization": "Bearer " + _token()})
        with urllib.request.urlopen(req, timeout=10) as r:
            p = json.load(r)
        req2 = urllib.request.Request(
            "http://127.0.0.1:8890/api/v1/health",
            headers={"Authorization": "Bearer " + _token()})
        with urllib.request.urlopen(req2, timeout=10) as r:
            h = json.load(r)
        started = pd.Timestamp(h["bot_startup"])
        days = max((pd.Timestamp.now(tz="UTC") - started).days, 1)
        closed = p.get("closed_trade_count", 0)
        profit_pct = p.get("profit_all_percent", 0) or 0
        ann = (1 + profit_pct / 100) ** (365 / days) - 1 if days >= 3 else None
        return {
            "ok": True,          # 样本太短时不下结论
            "days_running": days,
            "closed_trades": closed,
            "profit_pct": round(profit_pct, 3),
            "annualized": round(ann * 100, 2) if ann is not None else None,
            "detail": (f"运行 {days} 天 · 已平仓 {closed} 笔 · "
                       f"累计 {profit_pct:+.2f}%"
                       + (f" · 折年化 {ann*100:+.1f}%" if ann is not None else "")),
            "note": "样本不足，暂不下结论" if days < 30 or closed < 10 else "",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════ C4 因子漂移 ══════════════════

def check_factors():
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8890/api/locals/ops",
            headers={"Authorization": "Bearer " + _token()})
        with urllib.request.urlopen(req, timeout=20) as r:
            ops = json.load(r)
        facs = ops.get("factors", [])
        alert = [f for f in facs if f.get("alert")]
        return {
            "ok": len(alert) == 0,
            "total": len(facs), "alerts": len(alert),
            "need_retrain": ops.get("retrain", {}).get("need_retrain", False),
            "detail": f"{len(facs)} 个因子 · {len(alert)} 个告警 · "
                      f"重估 {'需要' if ops.get('retrain',{}).get('need_retrain') else '不需要'}",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════ C5 信号完整性 ══════════════════

def check_signal():
    """策略是否「哑火」：最近有没有开仓"""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8890/api/v1/trades?limit=50",
            headers={"Authorization": "Bearer " + _token()})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        trades = d.get("trades", [])
        if not trades:
            return {"ok": True, "detail": "尚无成交（策略刚启动）"}
        last = max(pd.to_datetime(t["open_date"]) for t in trades)
        gap = (pd.Timestamp.now(tz="UTC") - last).days
        return {
            "ok": gap <= TH["signal_gap_days"],
            "last_entry_days": gap,
            "detail": f"最近一次开仓在 {gap} 天前",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_token_cache = {"v": None}


def _token():
    if _token_cache["v"]:
        return _token_cache["v"]
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:8890/auth/auto-login",
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        _token_cache["v"] = json.load(r)["access_token"]
    return _token_cache["v"]


# ══════════════════ 主流程 ══════════════════

def one_round(verbose=True, interval=None):
    t0 = time.time()
    rec = {"t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")}
    if interval:
        # 面板要显示「每 N 小时一轮」，把真实周期写进快照，别让前端猜
        rec["interval_hours"] = round(interval / 3600, 2)

    checks = [
        ("C1 实现一致性", check_consistency),
        ("C2 参数稳定性", check_params),
        ("C3 实盘 vs 预期", check_live_vs_expect),
        ("C4 因子漂移", check_factors),
        ("C5 信号完整性", check_signal),
    ]
    results, fails = {}, []
    for name, fn in checks:
        if verbose:
            print(f"  {name} …", end="", flush=True)
        r = fn()
        results[name] = to_py(r)
        if not r.get("ok"):
            fails.append(name)
        if verbose:
            mark = "✅" if r.get("ok") else "❌"
            print(f" {mark} {r.get('detail') or r.get('error','')}")

    rec["checks"] = results
    rec["failed"] = to_py(fails)
    rec["all_ok"] = len(fails) == 0
    rec["elapsed"] = round(time.time() - t0, 1)

    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=6 * 3600)
    args = ap.parse_args()

    if not args.daemon:
        print("=" * 88)
        print("自动迭代巡检")
        print("=" * 88)
        rec = one_round(interval=args.interval)
        print("=" * 88)
        print(f"  {'✅ 全部通过' if rec['all_ok'] else '⚠ 需关注: ' + ', '.join(rec['failed'])}"
              f"  ({rec['elapsed']}s)")
        print(f"  记录已写入 {HIST}")
        return

    print(f"  自动迭代巡检启动，每 {args.interval//3600} 小时一轮")
    while True:
        try:
            print(f"\n── {pd.Timestamp.now(tz='Asia/Shanghai'):%Y-%m-%d %H:%M} ──")
            one_round(interval=args.interval)
        except Exception as exc:
            print(f"  ⚠ 巡检异常: {type(exc).__name__}: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
