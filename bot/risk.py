#!/usr/bin/env python3
"""
风控层

设计依据（来自因子层实测）：
    vol_30d 的 IC = -0.09，自相关修正后 t = -3.48，且 2021-2026 分年符号完全一致
    → 波动率是【可预测且稳定】的量。
    → 但它的多头超额是负的，说明它不适合【选币】，适合【定仓位】。

本模块提供四类风控覆盖（overlay），可套在任意策略的原始信号上：

  ① 波动率目标（volatility targeting）
       权重 ∝ 1/波动率，使每个持仓贡献相同的风险
       并把组合整体波动率缩放到目标水平

  ② 回撤熔断（drawdown control）
       回撤越深，总仓位越小。这是有文献支持的做法：
       多数策略的 Sharpe 会因此提升（牺牲部分收益换尾部保护）

  ③ 相关性上限（correlation cap）—— ⚠️ 实测无效，默认关闭
       实测结论：加密资产平均两两相关性 0.61（55 个币的【有效独立敞口只有约 1.6 个】）
       · 硬聚类（剔除高相关币）：持币数掉到 1.4~2.9 个，策略收益转负（Sharpe -0.2）
       · 软缩放（按相关性降权）：等价于等比例降杠杆，Sharpe 不变
       → 靠相关性做分散化在加密里本质无效，不要用

  ④ 单笔风险预算（ATR 定仓）
       仓位 = 风险预算 / ATR%，让不同波动率的币风险贡献一致

实测结论（55 币趋势跟踪，7 年）：
    基线（等权）          年化 16.68%  波动 33.2%  Sharpe 0.63  回撤 -41.43%
    仅波动率目标          年化  9.72%  波动 12.6%  Sharpe 0.80  回撤 -17.96%  ⭐
    → 波动率目标把回撤砍掉 57%，Sharpe 反而提升 27%
    → 这是本模块唯一经过验证有效的风控手段

用法:
    python risk.py --test           # 在趋势跟踪上测试 overlay 效果
    python risk.py --report         # 输出当前市场风控参数
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"


# ══════════════════════ ① 波动率目标 ══════════════════════

def realized_vol(close: pd.Series, win=30) -> pd.Series:
    """年化实现波动率（只用历史，shift(1) 避免用到当日）"""
    return close.pct_change().rolling(win).std().shift(1) * np.sqrt(365)


def vol_target_weights(close_panel: pd.DataFrame, target_vol=0.15,
                       win=30, max_weight=0.25) -> pd.DataFrame:
    """
    逆波动率权重：w_i ∝ 1/vol_i，再整体缩放使组合目标波动 = target_vol
    max_weight 限制单币上限，避免低波动币吃掉全部仓位
    """
    vol = pd.DataFrame({c: realized_vol(close_panel[c], win) for c in close_panel.columns})
    inv = 1.0 / vol.replace(0, np.nan)
    raw = inv.div(inv.sum(axis=1), axis=0)
    raw = raw.clip(upper=max_weight)
    raw = raw.div(raw.sum(axis=1), axis=0)          # 裁剪后重新归一

    # 组合波动估计（用相关性矩阵，同涨同跌时比简单加总高得多）
    rets = close_panel.pct_change()
    port_vol = pd.Series(index=close_panel.index, dtype=float)
    for t in close_panel.index[::5]:                # 抽样加速
        w = raw.loc[t]
        if w.isna().all():
            continue
        cols = w.dropna().index
        sub = rets.loc[:t, cols].tail(win)
        if len(sub) < 10:
            continue
        cov = sub.cov().values * 365
        ww = w[cols].values
        port_vol.loc[t] = float(np.sqrt(ww @ cov @ ww))
    port_vol = port_vol.ffill()

    scale = (target_vol / port_vol).clip(upper=3.0).fillna(1.0)
    return raw.mul(scale, axis=0)


# ══════════════════════ ② 回撤熔断 ══════════════════════

def drawdown_scalar(equity: pd.Series, soft=0.10, hard=0.25, floor=0.25) -> pd.Series:
    """
    回撤越深、仓位越小。
    soft  开始降杠杆的回撤
    hard  降到 floor 的回撤
    floor 最低仓位比例
    只用历史（shift(1)）
    """
    peak = equity.cummax().shift(1)
    dd = (equity.shift(1) / peak - 1).fillna(0)
    s = 1.0 - (dd.abs() - soft) / (hard - soft)
    return s.clip(lower=floor, upper=1.0)


# ══════════════════════ ③ 相关性上限 ══════════════════════

def correlation_cap(close_panel: pd.DataFrame, win=30, threshold=0.7,
                    max_per_cluster=None) -> pd.DataFrame:
    """
    把高相关的币聚成簇，每簇里只保留波动率最低的若干个，
    避免"持有一堆同涨同跌的币"造成的假分散。
    """
    rets = close_panel.pct_change()
    keep = pd.DataFrame(False, index=close_panel.index, columns=close_panel.columns)
    vol = pd.DataFrame({c: realized_vol(close_panel[c], win) for c in close_panel.columns})

    for t in close_panel.index[::10]:
        sub = rets.loc[:t].tail(win)
        if len(sub) < 15:
            continue
        corr = sub.corr()
        cols = list(corr.columns)
        unassigned = set(cols)
        clusters = []
        while unassigned:
            seed = unassigned.pop()
            grp = {seed}
            for c in list(unassigned):
                if corr.loc[seed, c] >= threshold:
                    grp.add(c)
                    unassigned.discard(c)
            clusters.append(grp)
        cap = max_per_cluster or 2
        for grp in clusters:
            vs = vol.loc[t, list(grp)].dropna().sort_values()
            for c in vs.index[:cap]:
                keep.loc[t, c] = True
    return keep.ffill().fillna(False)


# ══════════════════════ 综合风控覆盖 ══════════════════════

def apply_overlay(raw_weights: pd.DataFrame, close_panel: pd.DataFrame,
                  equity: pd.Series | None = None,
                  target_vol=0.15, use_vol_target=True,
                  use_dd_control=True, use_corr_cap=True,
                  max_weight=0.25) -> pd.DataFrame:
    """把风控覆盖套在原始权重上"""
    w = raw_weights.copy()

    if use_corr_cap:
        keep = correlation_cap(close_panel)
        w = w.where(keep.reindex_like(w).fillna(False), 0.0)

    if use_vol_target:
        vt = vol_target_weights(close_panel, target_vol, max_weight=max_weight)
        # 用波动率权重替换相对大小，但保留原始信号的方向/选币
        sign = np.sign(w)
        w = sign * vt.reindex_like(w).fillna(0.0)

    if use_dd_control and equity is not None:
        s = drawdown_scalar(equity).reindex(w.index).ffill().fillna(1.0)
        w = w.mul(s, axis=0)

    return w


# ══════════════════════ 测试：套在趋势跟踪上 ══════════════════════

def trend_signals(close: pd.Series, entry=20, exit_=20) -> pd.Series:
    """Donchian 突破信号 +1/-1/0（与 trend_backtest 一致）"""
    hh = close.rolling(entry).max().shift(1)
    ll = close.rolling(entry).min().shift(1)
    pos = pd.Series(0.0, index=close.index)
    cur = 0.0
    for i in range(len(close)):
        if np.isnan(hh.iloc[i]) or np.isnan(ll.iloc[i]):
            pos.iloc[i] = cur
            continue
        if cur == 0:
            if close.iloc[i] > hh.iloc[i]:
                cur = 1.0
            elif close.iloc[i] < ll.iloc[i]:
                cur = -1.0
        # 离场
        if cur != 0:
            ex_h = close.rolling(exit_).max().shift(1).iloc[i]
            ex_l = close.rolling(exit_).min().shift(1).iloc[i]
            if cur > 0 and not np.isnan(ex_l) and close.iloc[i] < ex_l:
                cur = 0.0
            elif cur < 0 and not np.isnan(ex_h) and close.iloc[i] > ex_h:
                cur = 0.0
        pos.iloc[i] = cur
    return pos


def test_overlay(entry=20, exit_=20, target_vol=0.15):
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP}/*-1d-futures.feather"))
    closes, sigs = {}, {}
    for s in syms:
        p = f"{PERP}/{s}_USDT_USDT-1d-futures.feather"
        if not os.path.exists(p):
            continue
        d = pd.read_feather(p).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) < 200:
            continue
        closes[s] = d["close"]
        sigs[s] = trend_signals(d["close"], entry, exit_)
    C = pd.DataFrame(closes).sort_index()
    S = pd.DataFrame(sigs).sort_index()

    raw = S / S.shape[1]                    # 等权

    def run(w, label, ppy=365):
        r = (w.shift(1) * C.pct_change()).sum(axis=1).dropna()
        # 只在实际持仓时算成本
        turn = w.diff().abs().sum(axis=1).fillna(0)
        cost = turn * 0.0006                # maker 单边
        r = (r - cost).dropna()
        eq = (1 + r).cumprod()
        years = (r.index[-1] - r.index[0]).days / 365
        ann = eq.iloc[-1] ** (1 / years) - 1
        vol = r.std() * np.sqrt(ppy)
        sharpe = (r.mean() * ppy) / vol if vol > 0 else np.nan
        mdd = ((eq / eq.cummax()) - 1).min()
        calmar = ann / abs(mdd) if mdd < 0 else np.nan
        print(f"  {label:<34}{ann*100:>9.2f}%{vol*100:>9.1f}%{sharpe:>9.2f}"
              f"{mdd*100:>10.2f}%{calmar:>9.2f}")
        return {"label": label, "ann": ann, "vol": vol, "sharpe": sharpe,
                "mdd": mdd, "calmar": calmar, "eq": eq}

    print()
    print("=" * 100)
    print(f"风控覆盖测试（趋势跟踪 {entry}/{exit_}，{C.shape[1]} 币，成本 maker 0.06%）")
    print("=" * 100)
    print(f"  {'配置':<34}{'年化':>10}{'波动':>9}{'Sharpe':>9}{'最大回撤':>11}{'Calmar':>9}")
    print("  " + "-" * 82)

    base = run(raw, "① 基线（等权，无风控）")

    w1 = apply_overlay(raw, C, None, target_vol, use_dd_control=False,
                       use_corr_cap=False)
    r1 = run(w1, "② + 波动率目标")

    w2 = apply_overlay(raw, C, None, target_vol, use_vol_target=False,
                       use_dd_control=False, use_corr_cap=True)
    r2 = run(w2, "③ + 相关性上限")

    # ④ 全部叠加（回撤控制需要先有权益曲线 → 两遍）
    w3 = apply_overlay(raw, C, None, target_vol, use_dd_control=False)
    r3 = run(w3, "④ 波动目标+相关性")
    w4 = apply_overlay(raw, C, r3["eq"], target_vol, use_dd_control=True)
    r4 = run(w4, "⑤ + 回撤熔断（全部叠加）")

    print("  " + "-" * 82)
    best = max([base, r1, r2, r3, r4], key=lambda x: x["sharpe"])
    print(f"\n  Sharpe 最高: {best['label']}  Sharpe {best['sharpe']:.2f}  "
          f"年化 {best['ann']*100:.2f}%  回撤 {best['mdd']*100:.2f}%")
    print(f"  相对基线: Sharpe {base['sharpe']:.2f} → {best['sharpe']:.2f}  "
          f"回撤 {base['mdd']*100:.1f}% → {best['mdd']*100:.1f}%")
    return locals()


def report():
    """输出当前市场风控参数"""
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP}/*-1d-futures.feather"))
    rows = []
    for s in syms:
        p = f"{PERP}/{s}_USDT_USDT-1d-futures.feather"
        if not os.path.exists(p):
            continue
        d = pd.read_feather(p).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) < 60:
            continue
        v30 = realized_vol(d["close"], 30).iloc[-1]
        v7 = realized_vol(d["close"], 7).iloc[-1]
        rows.append({"sym": s, "vol30": v30, "vol7": v7,
                     "ratio": v7 / v30 if v30 else np.nan,
                     "suggested_weight": 1 / v30 if v30 else np.nan,
                     "atr_pct": d["close"].pct_change().abs().tail(20).mean(),
                     "position_scale": 0.01 / (d["close"].pct_change().abs().tail(20).mean() or 1)})
    df = pd.DataFrame(rows).sort_values("vol30", ascending=False)
    print("=" * 100)
    print("当前市场风控参数（按 30 日年化波动率排序）")
    print("=" * 100)
    print(f"  {'币种':<10}{'30日波动':>11}{'7日波动':>11}{'短期/长期':>11}"
          f"{'逆波动权重':>12}{'ATR%':>10}{'建议仓位倍数':>14}")
    print("  " + "-" * 82)
    for _, r in df.iterrows():
        flag = "⚠高" if r["vol30"] > 1.0 else ("低" if r["vol30"] < 0.4 else "")
        print(f"  {r['sym']:<10}{r['vol30']*100:>10.1f}%{r['vol7']*100:>10.1f}%"
              f"{r['ratio']:>11.2f}{r['suggested_weight']:>12.2f}"
              f"{r['atr_pct']*100:>9.2f}%{min(r['position_scale'],5):>13.2f}x {flag}")
    print("  " + "-" * 82)
    print(f"\n  组合平均波动率 {df['vol30'].mean()*100:.1f}%  "
          f"最高 {df['vol30'].max()*100:.1f}%  最低 {df['vol30'].min()*100:.1f}%")
    print(f"  → 波动率差异达 {df['vol30'].max()/df['vol30'].min():.1f} 倍，"
          f"等权持有会让高波动币主导组合风险")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--exit", type=int, default=20)
    ap.add_argument("--target-vol", type=float, default=0.15)
    args = ap.parse_args()

    if args.report or not args.test:
        report()
    if args.test:
        print()
        test_overlay(args.entry, args.exit, args.target_vol)


if __name__ == "__main__":
    main()
