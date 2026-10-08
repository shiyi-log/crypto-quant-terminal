#!/usr/bin/env python3
"""
止损机制实测 —— 趋势跟踪该不该加真止损

背景：
    实盘 `stoploss = -0.60`，490 笔回测里只触发 3 次（0.6%）。
    实际效果 = 没有止损。37 笔亏损超过 20%，8 笔超过 30%。

    但「趋势跟踪 + 真止损」是经典权衡：
      · 趋势策略的盈利来自少数大盈利单
      · 真止损会在趋势的正常回调中被打掉 → 砍掉大盈利
      · 不止损则单笔风险不可控

    必须实测，不能拍脑袋。

本脚本：
    ① 固定百分比止损：-10% / -15% / -20% / -25% / -30% / -40% / -60%
    ② 跟踪止损（trailing stop）
    ③ ATR 波动率止损（自适应）
    ④ 用【扩展窗口走查】评估，避免单次切分的抽样运气（第 6 轮教训）

用法:
    python stop_test.py
    python stop_test.py --periods 4      # 分几段走查
"""

import argparse
import json
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import event_backtest as E

COST = 0.0005          # 单边 taker
STAKE_FRAC = 0.30 / 8  # 每笔占权益比例（与实盘一致）
TOP_N = 8
MAX_OPEN = 10


def run_stops(data, start, end=None,
              fixed=None, trailing=None, atr_mult=None):
    """
    事件驱动回测，支持三类止损。
    fixed     固定百分比（如 -0.20）
    trailing  跟踪止损回撤比例（如 0.15）
    atr_mult  ATR 倍数止损
    """
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        dd = d[d.index >= start]
        if end is not None:
            dd = dd[dd.index < end]
        if len(dd) < 150:
            continue
        S[s] = E.signals(dd["close"])
        ST[s] = E.strength(dd["close"])
        P[s] = dd[["open", "high", "low", "close"]]
    if not S:
        return None
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Hd = pd.DataFrame({k: v["high"] for k, v in P.items()}).sort_index()
    Ld = pd.DataFrame({k: v["low"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()

    # ATR
    ATR = {}
    for k, v in P.items():
        tr = pd.concat([v["high"] - v["low"],
                        (v["high"] - v["close"].shift(1)).abs(),
                        (v["low"] - v["close"].shift(1)).abs()], axis=1).max(axis=1)
        ATR[k] = tr.rolling(14).mean()
    Ad = pd.DataFrame(ATR).sort_index()

    stg, state = STd.values, Sd.values
    opx, hix, lox, clx = Od.values, Hd.values, Ld.values, Cd.values
    atr = Ad.values
    T, K = state.shape
    cash, pos = 10_000.0, {}
    eqc = np.zeros(T)
    stops_hit = 0
    trades = []

    for i in range(T):
        # ── 止损检查（用当根最高/最低价判断，触发即在该价位成交）──
        for k in list(pos):
            p = pos[k]
            if p["qty"] > 0:      # 多头
                if fixed is not None and lox[i, k] <= p["entry"] * (1 + fixed):
                    ex = min(p["entry"] * (1 + fixed), hix[i, k])
                    _close(pos, k, ex, i, cash_holder := [cash], "fixed")
                    cash = cash_holder[0]; stops_hit += 1; continue
                if atr_mult is not None and np.isfinite(atr[i, k]):
                    lvl = p["entry"] - atr_mult * p["atr0"]
                    if lox[i, k] <= lvl:
                        _close(pos, k, min(lvl, hix[i, k]), i, cash_holder := [cash], "atr")
                        cash = cash_holder[0]; stops_hit += 1; continue
                if trailing is not None:
                    p["peak"] = max(p.get("peak", p["entry"]), hix[i, k])
                    lvl = p["peak"] * (1 - trailing)
                    if lox[i, k] <= lvl:
                        _close(pos, k, min(lvl, hix[i, k]), i, cash_holder := [cash], "trail")
                        cash = cash_holder[0]; stops_hit += 1; continue
            else:                 # 空头
                if fixed is not None and hix[i, k] >= p["entry"] * (1 - fixed):
                    ex = max(p["entry"] * (1 - fixed), lox[i, k])
                    _close(pos, k, ex, i, cash_holder := [cash], "fixed")
                    cash = cash_holder[0]; stops_hit += 1; continue
                if trailing is not None:
                    p["peak"] = min(p.get("peak", p["entry"]), lox[i, k])
                    lvl = p["peak"] * (1 + trailing)
                    if hix[i, k] >= lvl:
                        _close(pos, k, max(lvl, lox[i, k]), i, cash_holder := [cash], "trail")
                        cash = cash_holder[0]; stops_hit += 1; continue

        # ── 趋势结束离场 ──
        for k in list(pos):
            if state[i, k] == 0.0:
                fi = min(i + 1, T - 1)
                _close(pos, k, opx[fi, k], i, cash_holder := [cash], "trend")
                cash = cash_holder[0]

        # ── 开仓 ──
        if i > 0:
            allsig = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: stg[i - 1, k] for k in allsig if np.isfinite(stg[i - 1, k])}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:TOP_N]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= MAX_OPEN:
                    continue
                eq = cash + sum(pp["stake"] + pp["qty"] * (clx[i, kk] - pp["entry"])
                                for kk, pp in pos.items())
                stake = eq * STAKE_FRAC
                e = opx[i, k]
                if not np.isfinite(e) or e <= 0:
                    continue
                qty = stake / e * np.sign(state[i, k])
                fee = stake * COST
                if cash < stake + fee:
                    continue
                cash -= stake + fee
                pos[k] = {"stake": stake, "qty": qty, "entry": e, "fee": fee,
                          "peak": e, "atr0": atr[i, k] if np.isfinite(atr[i, k]) else e * 0.04}

        eqc[i] = cash + sum(pp["stake"] + pp["qty"] * (clx[i, kk] - pp["entry"])
                            for kk, pp in pos.items())

    eq = pd.Series(eqc, index=Sd.index)
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"ann": ann * 100, "mdd": mdd * 100,
            "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "n_trades": len(trades), "n_stops": stops_hit,
            "eq": eq}


def _close(pos, k, ex_px, i, cash_holder, reason):
    p = pos.pop(k)
    val = p["stake"] + p["qty"] * (ex_px - p["entry"])
    fee = p["stake"] * COST
    cash_holder[0] += val - fee


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", type=int, default=3)
    args = ap.parse_args()

    import glob, os
    syms = [os.path.basename(f).split("_")[0]
            for f in glob.glob(f"{E.PERP}/*-1d-futures.feather")]
    data = E.load_ohlc(syms)
    # 走查分段
    all_d = max(d.index[-1] for d in data.values())
    segs = []
    t = pd.Timestamp("2021-01-01")
    step = (all_d - t) / args.periods
    for i in range(args.periods):
        a = t + step * i
        b = t + step * (i + 1) if i < args.periods - 1 else None
        segs.append((a, b))

    configs = [
        ("无真止损（当前 -60%）", dict(fixed=-0.60)),
        ("固定 -10%", dict(fixed=-0.10)),
        ("固定 -15%", dict(fixed=-0.15)),
        ("固定 -20%", dict(fixed=-0.20)),
        ("固定 -25%", dict(fixed=-0.25)),
        ("固定 -30%", dict(fixed=-0.30)),
        ("跟踪止损 15%", dict(trailing=0.15)),
        ("跟踪止损 25%", dict(trailing=0.25)),
        ("ATR×2.0", dict(atr_mult=2.0)),
        ("ATR×3.0", dict(atr_mult=3.0)),
        ("固定-20% + 跟踪25%", dict(fixed=-0.20, trailing=0.25)),
    ]

    print("=" * 104)
    print(f"止损实测（{args.periods} 段走查，各段独立跑）")
    print("=" * 104)
    print(f"  区间 {segs[0][0].date()} ~ {all_d.date()}")
    print()
    header = f"  {'止损方案':<22}"
    for i in range(args.periods):
        header += f"{'段'+str(i+1)+' Calmar':>13}"
    header += f"{'平均':>9}{'最少止损次数':>13}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    results = {}
    for label, kw in configs:
        row = []
        nstops = []
        for a, b in segs:
            r = run_stops(data, str(a.date()), str(b.date()) if b is not None else None, **kw)
            row.append(r["calmar"] if r else np.nan)
            if r:
                nstops.append(r["n_stops"])
        avg = np.nanmean(row)
        results[label] = {"seg_calmar": [round(float(x), 3) if np.isfinite(x) else None
                                         for x in row],
                          "avg": round(float(avg), 3) if np.isfinite(avg) else None,
                          "stops": nstops}
        line = f"  {label:<22}"
        for x in row:
            line += f"{x:>13.2f}"
        line += f"{avg:>9.2f}{min(nstops) if nstops else 0:>13}"
        print(line)
    print("  " + "-" * (len(header) - 2))

    base = results.get("无真止损（当前 -60%）", {}).get("avg")
    print()
    print("  相对「无真止损」的变化（按平均 Calmar）:")
    for label, r in results.items():
        if label.startswith("无真") or r["avg"] is None or base is None:
            continue
        d = (r["avg"] / base - 1) * 100 if base else np.nan
        mark = "✅" if d > 10 else ("🟡" if d > 0 else "❌")
        print(f"    {label:<22} {base:.2f} → {r['avg']:.2f}  ({d:+.0f}%) {mark}")

    # 一致性：各段是否同向
    print()
    print("  跨段一致性（各段 Calmar 是否都优于基线）:")
    for label, r in results.items():
        if label.startswith("无真"):
            continue
        b = results["无真止损（当前 -60%）"]["seg_calmar"]
        s = r["seg_calmar"]
        better = sum(1 for x, y in zip(s, b)
                     if x is not None and y is not None and x > y)
        total = sum(1 for x, y in zip(s, b) if x is not None and y is not None)
        print(f"    {label:<22} {better}/{total} 段更好")

    with open("user_data/stop_test_results.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "periods": args.periods, "results": results}, f, ensure_ascii=False)
    print(f"\n  ✅ user_data/stop_test_results.json")


if __name__ == "__main__":
    main()
