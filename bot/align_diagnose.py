#!/usr/bin/env python3
"""
执行口径对齐诊断

目标：
    研究框架 Calmar 0.82  vs  Freqtrade 1.40（偏差 70%）。
    逐项量化三个嫌疑来源，找出主要贡献者。

三个嫌疑：
    ① 执行价  研究假设「信号当根收盘成交」；Freqtrade 实际「下一根开盘成交」
    ② 成本    研究 0.03%/边；实盘 taker 0.05%（配置已显式写入）
    ③ 复利    研究用固定分数权重；Freqtrade 的 stake 随钱包增长

方法：
    构造一个「可切换口径」的研究回测，逐个打开开关，看 Calmar 怎么变。

用法: python align_diagnose.py
"""

import glob
import json
import os

import numpy as np
import pandas as pd

import walkforward as wf

PERP = "user_data/data/binance/futures"
LIVE = "user_data/config_trend_live.json"


# ══════════════════ 数据（带 open） ══════════════════

def load_ohlc(syms):
    out = {}
    for s in syms:
        f = f"{PERP}/{s}_USDT_USDT-1d-futures.feather"
        if not os.path.exists(f):
            continue
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) < 200:
            continue
        out[s] = d[["open", "high", "low", "close", "volume"]]
    return out


# ══════════════════ 信号（与策略一致：收盘价通道 + 状态机） ══════════════════

def signals(close: pd.Series, entry=20, exit_=20):
    c = close.values
    hhe = close.rolling(entry).max().shift(1).values
    lle = close.rolling(entry).min().shift(1).values
    hxe = close.rolling(exit_).max().shift(1).values
    lxe = close.rolling(exit_).min().shift(1).values
    st = np.zeros(len(c))
    cur = 0.0
    for i in range(len(c)):
        if cur == 0.0:
            if np.isfinite(hhe[i]) and c[i] > hhe[i]:
                cur = 1.0
            elif np.isfinite(lle[i]) and c[i] < lle[i]:
                cur = -1.0
        else:
            if cur > 0 and np.isfinite(lxe[i]) and c[i] < lxe[i]:
                cur = 0.0
            elif cur < 0 and np.isfinite(hxe[i]) and c[i] > hxe[i]:
                cur = 0.0
        st[i] = cur
    return pd.Series(st, index=close.index)


def strength(close: pd.Series, entry=20):
    span = (close.rolling(entry).max().shift(1) - close.rolling(entry).min().shift(1)).replace(0, np.nan)
    up = (close - close.rolling(entry).max().shift(1)) / span
    dn = (close.rolling(entry).min().shift(1) - close) / span
    return pd.concat([up, dn], axis=1).max(axis=1)


# ══════════════════ 可切换口径的回测 ══════════════════

def backtest(data, top_n=8, start=None,
             entry_at="close",      # 'close' = 信号当根收盘 | 'next_open' = 下一根开盘
             cost_one=0.0003,       # 单边成本
             compound=True,         # 是否复利（False = 固定名义本金）
             exposure=None):        # 目标敞口；None = 等权 1/N
    S, ST, C, O = {}, {}, {}, {}
    for s, d in data.items():
        if start is not None:
            d = d[d.index >= start]
            if len(d) < 120:
                continue
        S[s] = signals(d["close"])
        ST[s] = strength(d["close"])
        C[s] = d["close"]
        O[s] = d["open"]
    if not S:
        return None

    Sd = pd.DataFrame(S).sort_index()
    STd = pd.DataFrame(ST).sort_index()
    Cd = pd.DataFrame(C).sort_index()
    Od = pd.DataFrame(O).sort_index()
    N = Sd.shape[1]

    # 选币：只留强度前 top_n
    act = (Sd != 0)
    rank = STd.where(act).rank(axis=1, ascending=False, method="first")
    keep = rank <= top_n

    # ⚠️ 必须保留方向符号！Sd 里 +1=多 / -1=空。
    #    写成 (Sd != 0).astype(float) 会把空头也当成 +1 做多 ——
    #    实测与 walkforward 的相关系数变成 -0.12（几乎完全相反），
    #    差异最大的那几天幅度相同、符号相反，正是符号错误的特征。
    w = Sd.where(act & keep, 0.0).astype(float)
    if exposure is None:
        w = w / N                     # 等权 1/N（每币固定分数）
    else:
        w = w * (exposure / top_n)    # 目标敞口 / top_n

    # ── 执行价 ──
    if entry_at == "next_open":
        # 用「下一根开盘价」计算当日收益：
        #   持仓从 t+1 开盘开始，当日收益 = close(t+1)/open(t+1) - 1
        ret = (Cd / Od - 1).shift(-1)      # 对齐到 t
        # 换仓也在开盘发生
        W = w.shift(1)
        gross = (W * ret.shift(0)).sum(axis=1)
        # 之后的完整日收益用 close-to-close
        ret_cc = Cd.pct_change()
        # 首日按 open→close，其余按 close→close
        gross = (w.shift(1) * Cd.pct_change()).sum(axis=1)
        # 修正建仓首日：weight 从 0 变正的那天，收益应从 open 起算
        newly = (w.shift(1).fillna(0) > 0) & (w.shift(2).fillna(0) == 0)
        adj = ((Cd / Od - 1) - Cd.pct_change())
        gross = gross + (newly * w.shift(1).fillna(0) * adj).sum(axis=1)
    else:
        # 信号当根收盘成交：权重滞后一期，收益用 close-to-close
        gross = (w.shift(1).fillna(0) * Cd.pct_change()).sum(axis=1)

    # ── 成本 ──
    W = w.shift(1).fillna(0)
    turn = W.diff().abs().sum(axis=1).fillna(0)
    net = (gross - turn * cost_one).dropna()

    if not compound:
        # 固定名义本金：每期收益直接相加（不复利）
        eq = 1 + net.cumsum()
    else:
        eq = (1 + net).cumprod()
    return net, eq


def ev(net, eq, label):
    years = (net.index[-1] - net.index[0]).days / 365
    total = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + total) ** (1 / years) - 1
    vol = net.std() * np.sqrt(365)
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"label": label, "ann": ann * 100, "vol": vol * 100,
            "mdd": mdd * 100, "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "years": years}


def show(rows):
    print(f"  {'口径':<46}{'年化':>9}{'波动':>8}{'回撤':>9}{'Calmar':>9}")
    print("  " + "-" * 82)
    for r in rows:
        print(f"  {r['label']:<46}{r['ann']:>8.2f}%{r['vol']:>7.1f}%"
              f"{r['mdd']:>8.2f}%{r['calmar']:>9.2f}")
    print("  " + "-" * 82)


def main():
    cfg = json.load(open(LIVE))
    coins = [p.split("/")[0] for p in cfg["exchange"]["pair_whitelist"]]
    data = load_ohlc(coins)
    print(f"  币种 {len(data)} · 区间起点 2023-01-01")

    print()
    print("=" * 90)
    print("逐个打开口径开关（其他保持不变）")
    print("=" * 90)

    rows = []

    def run(label, **kw):
        r = backtest(data, start="2023-01-01", **kw)
        if r is None:
            return
        net, eq = r
        rows.append(ev(net, eq, label))

    # 基线：研究框架原来的口径
    run("① 基线（收盘成交 / 成本0.03% / 复利）",
        entry_at="close", cost_one=0.0003, compound=True)
    # 只改成本
    run("② + 成本改成 0.05%（对齐实盘 taker）",
        entry_at="close", cost_one=0.0005, compound=True)
    # 只改执行价
    run("③ + 开盘成交（对齐 Freqtrade）",
        entry_at="next_open", cost_one=0.0003, compound=True)
    # 都改
    run("④ + 开盘成交 + 成本0.05%",
        entry_at="next_open", cost_one=0.0005, compound=True)
    # 都不复利
    run("⑤ + 开盘成交 + 成本0.05% + 不复利",
        entry_at="next_open", cost_one=0.0005, compound=False)
    # 加上目标敞口（实盘 30%）
    run("⑥ 全部 + 目标敞口 30%（最贴近实盘）",
        entry_at="next_open", cost_one=0.0005, compound=True, exposure=0.30)

    show(rows)

    print()
    print("=" * 90)
    print("对照")
    print("=" * 90)
    print(f"  {'Freqtrade 实测':<46}{15.60:>8.2f}%{'—':>7}{11.14:>8.2f}%{1.40:>9.2f}")

    if rows:
        base = rows[0]
        print()
        print("  各口径对 Calmar 的影响:")
        for r in rows[1:]:
            print(f"    {r['label'][:44]:<46}{base['calmar']:>7.2f} → {r['calmar']:>7.2f}"
                  f"   ({(r['calmar']/base['calmar']-1)*100:+.0f}%)")


if __name__ == "__main__":
    main()
