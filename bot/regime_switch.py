#!/usr/bin/env python3
"""
状态切换组合策略：震荡做网格 + 趋势做跟踪

依据（本项目实测）：
    网格交易    震荡最强时  年化 +20.6%  Sharpe  2.05
                趋势最强时  年化 -29.7%  Sharpe -2.76
    趋势跟踪    震荡时表现平平，趋势时 Sharpe 1.14（7年全样本 t=3.03）

    → 两者【完全互补】，可以用市场状态切换。

状态识别（只用历史，无前视）：
    市场趋势强度 = |过去 N 日累计收益| / (过去 N 日波动率 × √N)
    这是一个标准化的趋势强度指标：
        ≈0   → 来回震荡（有波动但没方向）
        大   → 单边趋势
    用 BTC（或全市场等权指数）计算，shift(1) 保证只用已知信息。

切换规则：
    趋势强度 > 阈值  → 趋势跟踪（Donchian 突破）
    趋势强度 ≤ 阈值  → 网格（波动率自适应间距）

用法:
    python regime_switch.py                 # 默认
    python regime_switch.py --sweep         # 阈值扫描
    python regime_switch.py --compare       # 单独策略 vs 切换
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

import grid_backtest as gb
import risk as rk

PERP = "user_data/data/binance/futures"


# ══════════════════ 状态识别 ══════════════════

def trend_strength(close: pd.Series, win=20) -> pd.Series:
    """标准化趋势强度：|累计收益| / (波动率 × √win)，shift(1) 无前视"""
    ret = close.pct_change()
    cum = ret.rolling(win).sum()
    vol = ret.rolling(win).std() * np.sqrt(win)
    s = (cum.abs() / (vol + 1e-12)).shift(1)
    return s


def market_index(data, mode="equal"):
    """市场指数：等权收益（比单用 BTC 更能代表整体状态）"""
    rets = pd.DataFrame({s: d["close"].pct_change() for s, d in data.items()})
    idx_ret = rets.mean(axis=1)
    return (1 + idx_ret.fillna(0)).cumprod()


# ══════════════════ 两个子策略的日收益 ══════════════════

def trend_returns(data, entry=20, exit_=20):
    """趋势跟踪：每个币一个方向信号，等权"""
    sig = {}
    for s, d in data.items():
        sig[s] = rk.trend_signals(d["close"], entry, exit_)
    S = pd.DataFrame(sig).sort_index()
    C = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index()
    w = S / S.shape[1]
    r = (w.shift(1) * C.pct_change()).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0)
    return (r - turn * 0.0006).dropna()


def grid_returns(data, grid_k=1.0, range_m=3.0, max_levels=10):
    port, det = gb.run_all(data, grid_k=grid_k, range_m=range_m, max_levels=max_levels)
    return port


# ══════════════════ 状态切换 ══════════════════

def switch(trend_r, grid_r, strength, threshold):
    """
    按状态拼接两条收益流（同一时间轴，互斥）
    """
    idx = trend_r.index.intersection(grid_r.index).intersection(strength.dropna().index)
    st = strength.reindex(idx).ffill()
    tr = trend_r.reindex(idx).fillna(0)
    gr = grid_r.reindex(idx).fillna(0)
    regime = st > threshold
    out = np.where(regime, tr, gr)
    return pd.Series(out, index=idx), regime.reindex(idx).fillna(False)


def stats(r, label):
    if len(r) < 100:
        return None
    eq = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365
    ann = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(365)
    sharpe = (r.mean() * 365) / vol if vol > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    t = (r.mean() / r.std() * np.sqrt(len(r))) if r.std() > 0 else np.nan
    yearly = {}
    for y, g in r.groupby(r.index.year):
        yearly[int(y)] = ((1 + g).prod() - 1) * 100
    return {"label": label, "ann": ann * 100, "vol": vol * 100, "sharpe": sharpe,
            "mdd": mdd * 100, "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "t": t, "yearly": yearly, "eq": eq}


def show(s):
    print(f"  {s['label']:<30}{s['ann']:>9.2f}%{s['vol']:>8.1f}%{s['sharpe']:>9.2f}"
          f"{s['mdd']:>10.2f}%{s['calmar']:>9.2f}{s['t']:>8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid-k", type=float, default=1.0)
    ap.add_argument("--range-m", type=float, default=3.0)
    ap.add_argument("--win", type=int, default=20)
    ap.add_argument("--threshold", type=float, default=1.0)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()

    print("  加载数据…")
    data = gb.load_all("1d")
    print(f"  币种 {len(data)}")

    print("  计算两个子策略收益流…")
    tr = trend_returns(data)
    gr = grid_returns(data, args.grid_k, args.range_m)
    mkt = market_index(data)
    strength = trend_strength(mkt, args.win)

    print()
    print("=" * 100)
    print("状态切换组合：震荡→网格，趋势→趋势跟踪")
    print("=" * 100)
    print(f"  {'策略':<30}{'年化':>10}{'波动':>9}{'Sharpe':>9}{'最大回撤':>11}{'Calmar':>9}{'t值':>8}")
    print("  " + "-" * 88)

    base = stats(tr, "① 纯趋势跟踪")
    show(base)
    gbase = stats(gr, "② 纯网格")
    show(gbase)

    results = [base, gbase]
    if args.sweep:
        for th in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
            r, reg = switch(tr, gr, strength, th)
            s = stats(r, f"③ 切换 阈值{th} (趋势占比{reg.mean():.0%})")
            if s:
                show(s)
                results.append(s)
    else:
        r, reg = switch(tr, gr, strength, args.threshold)
        s = stats(r, f"③ 切换 阈值{args.threshold} (趋势占比{reg.mean():.0%})")
        show(s)
        results.append(s)

    print("  " + "-" * 88)

    best = max(results, key=lambda x: x["sharpe"])
    print(f"\n  Sharpe 最高: {best['label']}  Sharpe {best['sharpe']:.2f}  "
          f"年化 {best['ann']:.2f}%  回撤 {best['mdd']:.2f}%")
    print(f"  对比 纯趋势 {base['sharpe']:.2f} / 纯网格 {gbase['sharpe']:.2f}")

    print()
    print("  分年对比:")
    print(f"    {'年份':<8}{'纯趋势':>12}{'纯网格':>12}{'切换':>12}")
    for y in sorted(base["yearly"]):
        a = base["yearly"].get(y, np.nan)
        b = gbase["yearly"].get(y, np.nan)
        c = best["yearly"].get(y, np.nan)
        print(f"    {y:<8}{a:>11.1f}%{b:>11.1f}%{c:>11.1f}%")

    return results


if __name__ == "__main__":
    main()
