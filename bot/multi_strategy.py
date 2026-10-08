#!/usr/bin/env python3
"""
多策略并行 —— 突破单策略的独立交易数瓶颈（第 20 轮）

问题（第 18/19 轮量化）：
    单策略（趋势跟踪）组合层只有 610~630 笔独立交易
    实测信号 IC ≈ 0.03~0.04，但 n=620 时最小可检测 IC = 2.8/√620 = 0.112
    → 信号比可检测下限低 3 倍，**交易粒度上被噪声淹没**

    扩池（94 币）让 IC 增强到 0.043（t=5.80），但基线策略被小币拖垮。

**唯一剩下、且未被否证的方向：多个低相关策略并行。**
它增加独立交易数，且不需要任一单策略信号更强。

本脚本构造并对照四个策略：
    T  趋势跟踪     Donchian 突破（当前在用的）
    M  横截面动量   按近 N 日收益排名，多强空弱
    R  横截面反转   按近 N 日收益排名，多弱空强（与 M 反号）
    V  波动率突破   突破近期波动区间（ATR 通道）

评估：
    · 每个策略的组合指标（年化/回撤/Calmar/夏普）
    · 两两收益相关性（低相关才有组合价值）
    · 等权合并后的指标 —— **关键看 Calmar 是否超过最好的单策略**
    · 独立交易数（合并后是否真的变多）

用法:
    python multi_strategy.py
    python multi_strategy.py --start 2021-07-01
"""

import argparse
import glob
import json
import os
import warnings
from itertools import combinations

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import event_backtest as E

COST = 0.0005          # taker 单边
COST_M = 6.8 / 10_000  # 实测总成本（第 15 轮）
EXPO = 0.30
TOP_N = 8
MAX_OPEN = 10


# ══════════════════ 信号定义 ══════════════════

def sig_trend(d, entry=20):
    """T 趋势跟踪：Donchian 突破"""
    return E.signals(d["close"], entry, entry)


def sig_momentum(r, n=30, q=0.3):
    """
    M 横截面动量：按近 n 日收益排名，多前 q 空后 q。
    r: 收益率宽表（index=date, columns=coin）
    """
    mom = r.rolling(n).sum()
    rank = mom.rank(axis=1, pct=True)
    s = pd.DataFrame(0.0, index=rank.index, columns=rank.columns)
    s[rank >= 1 - q] = 1.0
    s[rank <= q] = -1.0
    return s


def sig_reversal(r, n=5, q=0.3):
    """R 横截面反转：短期反转，与动量反号"""
    mom = r.rolling(n).sum()
    rank = mom.rank(axis=1, pct=True)
    s = pd.DataFrame(0.0, index=rank.index, columns=rank.columns)
    s[rank <= q] = 1.0          # 短期跌得多的做多
    s[rank >= 1 - q] = -1.0
    return s


def sig_volbreak(d, n=20):
    """V 波动率突破：收盘突破 ATR 通道"""
    c = d["close"]
    tr = pd.concat([(d["high"] - d["low"]),
                    (d["high"] - c.shift()).abs(),
                    (d["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(n).mean()
    ma = c.rolling(n).mean()
    up = ma + 1.5 * atr
    dn = ma - 1.5 * atr
    s = pd.Series(0.0, index=c.index)
    pos = 0.0
    for i in range(len(c)):
        if c.iloc[i] > up.iloc[i]:
            pos = 1.0
        elif c.iloc[i] < dn.iloc[i]:
            pos = -1.0
        elif i > 0 and abs(c.iloc[i] - ma.iloc[i]) < 0.2 * atr.iloc[i]:
            pos = 0.0
        s.iloc[i] = pos
    return s


# ══════════════════ 组合回测 ══════════════════

def run_signal(data, sig_map, start, cost=COST, expo=EXPO, top_n=TOP_N,
               max_open=MAX_OPEN):
    """
    sig_map: {coin: Series(方向)} 或 {coin: DataFrame}（横截面信号的宽表）
    统一成 {coin: Series} 后跑事件回测
    """
    S, P = {}, {}
    for s, d in data.items():
        dd = d[d.index >= start]
        if len(dd) < 150:
            continue
        sg = sig_map.get(s)
        if sg is None:
            continue
        if isinstance(sg, pd.DataFrame):
            sg = sg[s]
        sg = sg.reindex(dd.index).fillna(0.0)
        S[s] = sg
        P[s] = dd[["open", "close"]]
    if len(S) < 3:
        return None

    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()
    Od, Cd = Od.ffill(), Cd.ffill()
    # 只保留有信号的币
    cols = [c for c in Sd.columns if c in Od.columns and c in Cd.columns]
    Sd, Od, Cd = Sd[cols], Od[cols], Cd[cols]
    stg, state = Sd.values, Sd.values
    opx, clx = Od.values, Cd.values
    T, K = state.shape

    # 强度：用 |信号| 或原始强度（这里用信号本身的绝对值）
    strength = np.abs(state)

    cash, pos, eqc = 10_000.0, {}, np.zeros(T)
    n_tr = 0
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0 or np.sign(state[i, k]) != pos[k]["side"]:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                cash += p["stake"] + p["q"] * (opx[fi, k] - p["e"]) - p["stake"] * cost
                n_tr += 1
        if i > 0:
            cand = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: strength[i - 1, k] for k in cand}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:top_n]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= max_open:
                    continue
                eq = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                                for kk, pp in pos.items())
                n_act = max(len(pos) + 1, 1)
                stake = min(eq * expo / top_n, eq * expo / n_act * 2.0)
                e = opx[i, k]
                if not np.isfinite(e) or e <= 0:
                    continue
                q = stake / e * np.sign(state[i, k])
                fee = stake * cost
                if cash < stake + fee:
                    continue
                cash -= stake + fee
                pos[k] = {"stake": stake, "q": q, "e": e, "side": np.sign(state[i, k])}
        eqc[i] = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                            for kk, pp in pos.items())
    eq = pd.Series(eqc, index=Sd.index)
    ret = eq.pct_change().fillna(0.0)
    return {"eq": eq, "ret": ret, "n_trades": n_tr}


def metrics(ret, label=""):
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 365
    if yrs <= 0 or eq.iloc[-1] <= 0:
        return None
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    dd = ((eq / eq.cummax()) - 1).min()
    sd = ret.std()
    return {"ann": ann * 100, "dd": dd * 100,
            "calmar": ann / abs(dd) if dd < 0 else np.nan,
            "sharpe": ret.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
            "label": label}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--universe", default="binance",
                    choices=["binance", "merged"])
    args = ap.parse_args()

    print("=" * 104)
    print("多策略并行 —— 突破独立交易数瓶颈")
    print("=" * 104)
    if args.universe == "merged":
        data = {}
        for f in sorted(glob.glob("user_data/data/merged/*-1d-merged.feather")):
            s_ = os.path.basename(f).split("-")[0]
            d = pd.read_feather(f).sort_values("date")
            d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
            d = d.set_index("date")
            if len(d) < 400:
                continue
            data[s_] = d[["open", "high", "low", "close", "volume"]]
    else:
        import ml_lab
        data = ml_lab.load_ohlcv()
    print(f"  标的池 [{args.universe}] · {len(data)} 币 · 起点 {args.start}")
    print(f"  成本 {COST*1e4:.1f}bp/边 · 敞口 {EXPO:.0%} · top{TOP_N} · 最多 {MAX_OPEN} 仓")
    print()

    # 宽表收益率（横截面信号用）
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index()
    rets = closes.pct_change()

    # ── 构造四个策略的信号 ──
    S = {}
    print("  构造信号…")
    for s, d in data.items():
        dd = d[d.index >= args.start]
        if len(dd) < 150:
            continue
        S[s] = {"T": sig_trend(dd, 20), "V": sig_volbreak(dd, 20)}
    print(f"    单币信号 T/V: {len(S)} 个币")
    M = sig_momentum(rets, 30, 0.3)
    R = sig_reversal(rets, 5, 0.3)
    print(f"    横截面信号 M/R: {M.shape}")

    strategies = {}
    strategies["T 趋势跟踪"] = {s: v["T"] for s, v in S.items()}
    strategies["V 波动突破"] = {s: v["V"] for s, v in S.items()}
    strategies["M 横截面动量"] = {s: M[s] for s in S if s in M.columns}
    strategies["R 横截面反转"] = {s: R[s] for s in S if s in R.columns}

    # ── 单策略 ──
    print()
    print("=" * 104)
    print("单策略")
    print("=" * 104)
    print(f"  {'策略':<16}{'交易':>8}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}")
    print("  " + "-" * 60)
    res, rets_by = {}, {}
    for lab, sm in strategies.items():
        r = run_signal(data, sm, args.start)
        if not r:
            print(f"  {lab:<16} 无结果"); continue
        m = metrics(r["ret"], lab)
        if not m:
            continue
        res[lab] = m
        rets_by[lab] = r["ret"]
        print(f"  {lab:<16}{r['n_trades']:>8}{m['ann']:>8.1f}%{m['dd']:>9.1f}%"
              f"{m['calmar']:>9.2f}{m['sharpe']:>8.2f}")
    print("  " + "-" * 60)

    # ── 相关性 ──
    print()
    print("=" * 104)
    print("策略间收益相关性（越低越有组合价值）")
    print("=" * 104)
    labs = list(rets_by)
    print(f"  {'':<16}" + "".join(f"{l.split()[0]:>8}" for l in labs))
    print("  " + "-" * (16 + 8 * len(labs)))
    for a in labs:
        row = f"  {a:<16}"
        for b in labs:
            n = min(len(rets_by[a]), len(rets_by[b]))
            c = np.corrcoef(rets_by[a].values[:n], rets_by[b].values[:n])[0, 1]
            row += f"{c:>8.2f}"
        print(row)
    print("  " + "-" * (16 + 8 * len(labs)))

    # ── 合并 ──
    print()
    print("=" * 104)
    print("合并组合（等权）")
    print("=" * 104)
    print(f"  {'组合':<34}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}")
    print("  " + "-" * 72)
    best_single = max(res.items(), key=lambda kv: kv[1]["calmar"] if np.isfinite(kv[1]["calmar"]) else -9)
    print(f"  {'【最好单策略】'+best_single[0]:<34}{best_single[1]['ann']:>8.1f}%"
          f"{best_single[1]['dd']:>9.1f}%{best_single[1]['calmar']:>9.2f}"
          f"{best_single[1]['sharpe']:>8.2f}")
    print("  " + "-" * 72)
    combos = []
    for k in range(2, len(labs) + 1):
        for combo in combinations(labs, k):
            idx = None
            rs = []
            for l in combo:
                r = rets_by[l]
                idx = r.index if idx is None else idx.intersection(r.index)
            for l in combo:
                rs.append(rets_by[l].reindex(idx).fillna(0.0))
            blended = pd.concat(rs, axis=1).mean(axis=1)
            m = metrics(blended, "+".join(x.split()[0] for x in combo))
            if m:
                combos.append((combo, m))
                if k == len(labs) or m["calmar"] > best_single[1]["calmar"]:
                    nm = "+".join(x.split()[0] for x in combo)
                    flag = " ⭐" if m["calmar"] > best_single[1]["calmar"] else ""
                    print(f"  {nm:<34}{m['ann']:>8.1f}%{m['dd']:>9.1f}%"
                          f"{m['calmar']:>9.2f}{m['sharpe']:>8.2f}{flag}")
    print("  " + "-" * 72)
    print()
    best_combo = max(combos, key=lambda kv: kv[1]["calmar"]) if combos else None
    if best_combo and best_combo[1]["calmar"] > best_single[1]["calmar"]:
        nm = "+".join(x.split()[0] for x in best_combo[0])
        imp = best_combo[1]["calmar"] / best_single[1]["calmar"] - 1
        print(f"  ⭐ 最佳组合 {nm}: Calmar {best_combo[1]['calmar']:.2f} "
              f"vs 最好单策略 {best_single[1]['calmar']:.2f} ({imp:+.0%})")
        print(f"     年化 {best_combo[1]['ann']:.1f}% · 回撤 {best_combo[1]['dd']:.1f}%")
    else:
        print("  ❌ 没有任何组合优于最好的单策略 —— 多策略并行不成立")

    with open("user_data/multi_strategy.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "universe": args.universe, "start": args.start,
                   "single": {k: {kk: round(float(vv), 3) if np.isfinite(vv) else None
                                  for kk, vv in v.items() if kk != "label"}
                              for k, v in res.items()},
                   "combos": [{"combo": "+".join(x.split()[0] for x in c),
                               "calmar": round(float(m["calmar"]), 3),
                               "ann": round(float(m["ann"]), 2),
                               "dd": round(float(m["dd"]), 2)} for c, m in combos]},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/multi_strategy.json")


if __name__ == "__main__":
    main()
