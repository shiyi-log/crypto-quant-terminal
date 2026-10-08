#!/usr/bin/env python3
"""
清算缓冲优化：用波动率预测动态定仓位

问题：
    静态缓冲要按历史最坏情况预留（DOGE 单月 +248.6%），
    导致资金占用 3.49N、年化只剩 1%。

思路：
    如果能在建仓时【预测】每个币下一期的最大不利波动，
    就可以对高风险的币降杠杆、低风险的币加杠杆，
    从而在同等安全性下提高资金效率。

检验：
    1. 历史波动率（trailing）能否预测下一期的最大涨幅？
    2. 按预测值反比定仓位，能否降低所需缓冲、提升年化？

用法: python vol_sizing.py
"""

import glob
import os

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

import carry_backtest as cb

PERP_DIR = "user_data/data/binance/futures"


def load_all():
    syms = cb.discover_symbols()
    P, V, F = {}, {}, {}
    for s in syms:
        pp = cb.load_series(s, "1h", "perp")
        fr = cb.load_funding(s)
        if pp is None or fr is None:
            continue
        # Freqtrade 的 feather 只有 close，用 close 差分算收益
        P[s], V[s], F[s] = pp, pp.pct_change(), fr
    u = sorted(P.keys())
    return u, pd.DataFrame(P)[u].sort_index(), pd.DataFrame(V)[u].sort_index(), pd.DataFrame(F)[u].sort_index()


def main():
    u, P, R, F = load_all()
    idx = P.index
    print(f"  币种 {len(u)} · 区间 {idx[0].date()} → {idx[-1].date()}")

    H = 720  # 30 天小时数
    periods = [t for t in idx[::H]]

    rows = []
    for i, t in enumerate(periods[:-1]):
        nxt = periods[i + 1]
        hist = F.loc[:t].tail(H).sum().dropna()
        if len(hist) < 5:
            continue
        sel = list(hist.nlargest(5).index)
        for s in sel:
            px = P[s].loc[:t]
            if len(px) < H + 10:
                continue
            # 特征：过去 30 天的波动率与最大涨幅（建仓时可得）
            trail_ret = px.pct_change().tail(H)
            trail_vol = trail_ret.std() * np.sqrt(24 * 365)      # 年化
            trail_max = px.tail(H).max() / px.tail(H).min() - 1   # 过去区间振幅
            # 目标：下一期的最大涨幅（决定空头是否被强平）
            fut = P[s].loc[t:nxt]
            if len(fut) < 2:
                continue
            fut_max = fut.max() / fut.iloc[0] - 1
            rows.append({
                "t": t, "sym": s,
                "trail_vol": trail_vol,
                "trail_max": trail_max,
                "fut_max": fut_max,
                "funding": hist[s],
            })

    d = pd.DataFrame(rows)
    print(f"  样本 {len(d)} 条（币×期）")

    print()
    print("=" * 92)
    print("检验：建仓时可得的信息，能否预测下一期的最大涨幅（= 空头风险）？")
    print("=" * 92)
    for feat, name in [("trail_vol", "历史波动率(年化)"),
                       ("trail_max", "历史区间振幅"),
                       ("funding", "历史资金费")]:
        ic = spearmanr(d[feat], d["fut_max"]).statistic
        print(f"  {name:<18} vs 未来最大涨幅: IC {ic:+.4f}   "
              f"{'✅ 有预测力' if abs(ic) > 0.2 else '⚠ 预测力弱' if abs(ic) > 0.1 else '❌ 基本无预测力'}")

    print()
    print("=" * 92)
    print("如果按「预测波动率反比」定仓位，所需缓冲能降多少？")
    print("=" * 92)
    # 分位数分层：按 trail_vol 分 3 层，看各层 fut_max 的分布
    d["vol_bucket"] = pd.qcut(d["trail_vol"], 3, labels=["低波动", "中波动", "高波动"])
    print(f"  {'波动率分层':<12}{'样本':>6}{'历史波动率中位':>16}{'未来最大涨幅 中位':>20}{'90%分位':>12}")
    print("  " + "-" * 68)
    for b, g in d.groupby("vol_bucket", observed=True):
        print(f"  {str(b):<12}{len(g):>6}{g['trail_vol'].median()*100:>15.0f}%"
              f"{g['fut_max'].median()*100:>19.1f}%{g['fut_max'].quantile(0.9)*100:>11.1f}%")
    print("  " + "-" * 68)
    print(f"  全局: 未来最大涨幅 中位 {d['fut_max'].median()*100:.1f}%  "
          f"90%分位 {d['fut_max'].quantile(0.9)*100:.1f}%  最坏 {d['fut_max'].max()*100:.1f}%")

    print()
    print("=" * 92)
    print("静态 vs 动态缓冲对比")
    print("=" * 92)
    per_period_net = 0.002952  # 每期净收益（占名义），取自 carry_backtest
    for label, buf in [
        ("静态：全局 90% 分位", d["fut_max"].quantile(0.9)),
        ("静态：全局最坏", d["fut_max"].max()),
        ("动态：按波动率分层 90% 分位", None),
    ]:
        if buf is None:
            # 动态：每层用自己的 90% 分位
            caps = []
            for b, g in d.groupby("vol_bucket", observed=True):
                caps.append(1 + g["fut_max"].quantile(0.9))
            cap = float(np.mean(caps))
            buf = cap - 1
        else:
            cap = 1 + buf
        ann = (1 + per_period_net / cap) ** 12 - 1
        print(f"  {label:<28} 缓冲 {buf*100:>6.1f}%  资金占用 {cap:.2f}N  年化 {ann*100:>5.2f}%")

    print()
    print("  说明：年化基于每月 1 次调仓、每期净收益 0.2952%（占名义），")
    print("        未计入强平后的重新建仓成本与滑点放大。")


if __name__ == "__main__":
    main()
