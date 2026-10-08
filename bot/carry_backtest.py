#!/usr/bin/env python3
"""
资金费率 Delta 中性 Carry 回测器

策略：
    对每个选中的币，做多现货 N + 做空永续 N（方向中性）
    收益 = 资金费（空头收取） + 现货/永续价差变化（基差） − 手续费 − 滑点

为什么必须算基差：
    只算资金费会严重高估收益。现货与永续价格不总是相等，
    基差变化会直接产生盈亏，极端行情下可能吃掉全部资金费。

资金占用：
    现货腿需全额资金 N，永续腿需保证金 N/leverage
    → 单币占用 = N + N/leverage

用法:
    python carry_backtest.py --topn 5 --rebalance-days 30 --leverage 1
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

SPOT_DIR = "user_data/data/binance"
PERP_DIR = "user_data/data/binance/futures"

TAKER = 0.0005      # 单边 taker 0.05%
SLIPPAGE = 0.0002   # 单边滑点 0.02%（现货+永续各一次）
FUNDING_MIN_HISTORY = 720   # 选币至少需要 30 天费率历史
MAINT_MARGIN = 0.005        # 维持保证金率（用于估算强平）


def load_series(sym, tf, kind):
    if kind == "spot":
        p = f"{SPOT_DIR}/{sym}_USDT-{tf}.feather"
    else:
        p = f"{PERP_DIR}/{sym}_USDT_USDT-{tf}-futures.feather"
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")["close"].astype(float)


def load_funding(sym):
    p = f"{PERP_DIR}/{sym}_USDT_USDT-1h-funding_rate.feather"
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")["funding_rate"].astype(float)


def discover_symbols():
    out = []
    for f in sorted(glob.glob(f"{PERP_DIR}/*-1h-futures.feather")):
        out.append(os.path.basename(f).split("_")[0])
    return out


def run(topn, rebalance_days, leverage, tf="1h", start=None, verbose=True):
    symbols = discover_symbols()
    spot, perp, fund = {}, {}, {}
    for s in symbols:
        sp = load_series(s, tf, "spot")
        pp = load_series(s, tf, "perp")
        fr = load_funding(s)
        if sp is None or pp is None or fr is None or sp.empty or pp.empty or fr.empty:
            continue
        spot[s], perp[s], fund[s] = sp, pp, fr

    usable = sorted(spot.keys())
    if not usable:
        raise SystemExit("没有同时具备现货/永续/资金费数据的币种")

    S = pd.DataFrame(spot)[usable].sort_index()
    P = pd.DataFrame(perp)[usable].sort_index()
    F = pd.DataFrame(fund)[usable].sort_index()

    idx = S.index.intersection(P.index)
    if start:
        idx = idx[idx >= pd.Timestamp(start, tz="UTC")]
    S, P = S.loc[idx], P.loc[idx]
    if verbose:
        print(f"  币种 {len(usable)} 个: {', '.join(usable)}")
        print(f"  区间 {idx[0]} → {idx[-1]}（{(idx[-1]-idx[0]).days} 天，{len(idx)} 根 {tf}）")

    # ── 基差统计（风险量化）──
    basis = (P / S - 1)
    if verbose:
        print()
        print("  ── 基差（永续/现货 − 1）统计，衡量对冲不完全的风险 ──")
        b = basis.stack()
        print(f"     均值 {b.mean()*100:+.4f}%   标准差 {b.std()*100:.4f}%   "
              f"最小 {b.min()*100:+.3f}%   最大 {b.max()*100:+.3f}%")
        print(f"     95% 分位区间: [{b.quantile(0.025)*100:+.3f}%, {b.quantile(0.975)*100:+.3f}%]")
        print(f"     基差变化的标准差（每小时）: {(basis.diff().stack().std())*100:.4f}%")

    # ── 逐期模拟 ──
    rebal_ts = [t for t in idx[::rebalance_days * 24]]
    records, prev_sel = [], None
    for i, t in enumerate(rebal_ts[:-1]):
        nxt = rebal_ts[i + 1]
        # 用 t 之前的费率历史选币
        hist = F.loc[:t].tail(FUNDING_MIN_HISTORY).sum().dropna()
        if len(hist) < topn:
            continue
        sel = list(hist.nlargest(topn).index)

        seg_idx = idx[(idx >= t) & (idx <= nxt)]
        if len(seg_idx) < 2:
            continue

        # ① 基差带来的价格盈亏：多现货 + 空永续
        s0, s1 = S.loc[t, sel], S.loc[nxt, sel]
        p0, p1 = P.loc[t, sel], P.loc[nxt, sel]
        price_pnl = ((s1 / s0) - (p1 / p0)).mean()   # 每单位名义本金的净盈亏

        # ② 资金费：空头收取正费率
        fsum = F.loc[(F.index > t) & (F.index <= nxt), sel].sum().mean()

        # ③ 手续费 + 滑点（调仓时计算换手）
        if prev_sel is None:
            churn = 1.0
        else:
            churn = len(set(sel) - set(prev_sel)) / topn
        # 建仓+平仓两条腿，各 taker+滑点；换手比例 churn 承担
        cost = churn * 2 * (TAKER + SLIPPAGE) * 2  # ×2 两腿，×2 开+平

        net = price_pnl + fsum - cost
        cap = 1.0 + 1.0 / leverage    # 单币占用资金（现货全额 + 永续保证金）
        records.append({
            "t": t, "t_end": nxt,
            "selected": ",".join(sel),
            "price_pnl_pct": price_pnl * 100,
            "funding_pct": fsum * 100,
            "cost_pct": cost * 100,
            "net_pct": net * 100,
            "net_on_capital_pct": net / cap * 100,
        })
        prev_sel = sel

    df = pd.DataFrame(records)
    if df.empty:
        raise SystemExit("无有效调仓记录")

    # ── 汇总 ──
    df["equity"] = (1 + df["net_on_capital_pct"] / 100).cumprod()
    days = (df["t_end"].iloc[-1] - df["t"].iloc[0]).days
    years = days / 365 if days > 0 else np.nan
    total = df["equity"].iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1 if years and years > 0 else np.nan
    peak = df["equity"].cummax()
    dd = ((df["equity"] / peak) - 1).min()

    if verbose:
        print()
        print("═" * 92)
        print(f"结果：每月调仓 · 持有费率最高 {topn} 币 · 永续杠杆 {leverage}x")
        print("═" * 92)
        print(f"  调仓次数      : {len(df)}")
        print(f"  累计收益      : {total*100:+.2f}%   年化 {ann*100:+.2f}%")
        print(f"  最大回撤      : {dd*100:.2f}%")
        print(f"  胜率(按期)    : {(df['net_pct'] > 0).mean()*100:.1f}%")
        print()
        print(f"  收益拆解（每期平均）:")
        print(f"     资金费收入  : {df['funding_pct'].mean():+.4f}%")
        print(f"     基差盈亏    : {df['price_pnl_pct'].mean():+.4f}%   ← 常被忽略，可能为负")
        print(f"     手续费滑点  : {df['cost_pct'].mean():.4f}%")
        print(f"     净收益      : {df['net_pct'].mean():+.4f}%  (占资金 {df['net_on_capital_pct'].mean():+.4f}%)")
        print()
        print(f"  基差贡献占资金费的 {abs(df['price_pnl_pct'].mean())/abs(df['funding_pct'].mean())*100:.1f}%"
              f"  {'⚠ 基差侵蚀明显' if df['price_pnl_pct'].mean() < 0 else ''}")
        print(f"  年化收益中，资金费贡献 {df['funding_pct'].mean()*len(df)/years:.2f}%"
              f"，基差贡献 {df['price_pnl_pct'].mean()*len(df)/years:.2f}%")
        print()
        print("  分阶段（按年）:")
        for y, g in df.groupby(df["t"].dt.year):
            eq = (1 + g["net_on_capital_pct"] / 100).prod() - 1
            print(f"     {y}: {len(g):>2} 期  净 {eq*100:+7.2f}%  "
                  f"资金费 {g['funding_pct'].sum():+7.2f}%  基差 {g['price_pnl_pct'].sum():+7.2f}%")
        print()
        print("  最近 6 期明细:")
        for _, r in df.tail(6).iterrows():
            print(f"     {r['t'].strftime('%Y-%m-%d')} → {r['t_end'].strftime('%Y-%m-%d')}  "
                  f"净 {r['net_pct']:+6.3f}%  (资金费 {r['funding_pct']:+6.3f}% 基差 {r['price_pnl_pct']:+6.3f}%)  "
                  f"[{r['selected']}]")

    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--rebalance-days", type=int, default=30)
    ap.add_argument("--leverage", type=float, default=1.0, help="永续腿杠杆，越高占用资金越少但强平风险越大")
    ap.add_argument("--start", default=None)
    args = ap.parse_args()

    print("=" * 92)
    print("资金费率 Delta 中性 Carry 回测（现货多 + 永续空）")
    print("=" * 92)
    run(args.topn, args.rebalance_days, args.leverage, start=args.start)


if __name__ == "__main__":
    main()
