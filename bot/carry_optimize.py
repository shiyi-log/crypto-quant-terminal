#!/usr/bin/env python3
"""
Carry 选币逻辑优化（在统一账户方案下，即不存在强平）

基线：取过去 30 天累计资金费最高的 5 个币，等权持有。
可改进的方向（都有经济含义，不是为了凑参数）：

  ① 阈值过滤：只持有费率为【正】的币 —— 避免倒贴资金费
  ② 加权方式：按费率高低加权，而不是等权
  ③ 持有币数：分散度 vs 费率集中度的权衡
  ④ 调仓周期：换手成本 vs 费率变化的灵敏度

⚠️ 只有 33 个调仓期，参数扫描极易过拟合。
   所以每一项都要检查【分年一致性】：正负号在 2024/2025/2026 是否一致。

用法: python carry_optimize.py
"""

import glob
import os

import numpy as np
import pandas as pd

PERP_DIR = "user_data/data/binance/futures"
SPOT_DIR = "user_data/data/binance"
TAKER = 0.0005
SLIPPAGE = 0.0002
COST_PER_NOTIONAL = 2 * (TAKER + SLIPPAGE) * 2   # 两腿 × 开平


def load_all():
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP_DIR}/*-1h-futures.feather"))
    S, P, F = {}, {}, {}
    for s in syms:
        sp = f"{SPOT_DIR}/{s}_USDT-1h.feather"
        pp = f"{PERP_DIR}/{s}_USDT_USDT-1h-futures.feather"
        fr = f"{PERP_DIR}/{s}_USDT_USDT-1h-funding_rate.feather"
        if not all(os.path.exists(x) for x in (sp, pp, fr)):
            continue
        a = pd.read_feather(sp); a["date"] = pd.to_datetime(a["date"], utc=True)
        b = pd.read_feather(pp); b["date"] = pd.to_datetime(b["date"], utc=True)
        c = pd.read_feather(fr); c["date"] = pd.to_datetime(c["date"], utc=True)
        S[s] = a.set_index("date")["close"].astype(float)
        P[s] = b.set_index("date")["close"].astype(float)
        F[s] = c.set_index("date")["funding_rate"].astype(float)
    u = sorted(S.keys())
    Sd = pd.DataFrame(S)[u].sort_index()
    Pd = pd.DataFrame(P)[u].sort_index()
    Fd = pd.DataFrame(F)[u].sort_index()
    idx = Sd.index.intersection(Pd.index)
    return Sd.loc[idx], Pd.loc[idx], Fd


def backtest(Sd, Pd, Fd, topn=5, rebalance_days=30, lookback=720,
             min_funding=None, weight="equal", leverage=1.0):
    """
    返回每期收益（占【总投入资金】的比例）
    资金占用：现货全额 N + 永续保证金 N/leverage
    """
    idx = Sd.index
    rebal = [t for t in idx[::rebalance_days * 24]]
    cap_mult = 1.0 + 1.0 / leverage
    recs, prev_sel = [], None

    for i, t in enumerate(rebal[:-1]):
        nxt = rebal[i + 1]
        hist = Fd.loc[:t].tail(lookback).sum().dropna()
        if min_funding is not None:
            hist = hist[hist > min_funding]
        if len(hist) < topn:
            continue
        sel = list(hist.nlargest(topn).index)

        seg_s = Sd.loc[t:nxt, sel]
        seg_p = Pd.loc[t:nxt, sel]
        if len(seg_s) < 2:
            continue

        # 每币毛收益 = 现货涨幅 − 永续涨幅 + 资金费
        spot_ret = (seg_s.iloc[-1] / seg_s.iloc[0] - 1)
        perp_ret = (seg_p.iloc[-1] / seg_p.iloc[0] - 1)
        fnd = Fd.loc[(Fd.index > t) & (Fd.index <= nxt), sel].sum()
        gross = spot_ret - perp_ret + fnd

        if weight == "funding":
            w = hist[sel].clip(lower=0)
            w = w / w.sum() if w.sum() > 0 else pd.Series(1 / len(sel), index=sel)
        else:
            w = pd.Series(1 / len(sel), index=sel)
        gross_w = float((gross * w).sum())

        # 换手成本（按名义本金比例）
        churn = 1.0 if prev_sel is None else len(set(sel) - set(prev_sel)) / topn
        cost = churn * COST_PER_NOTIONAL

        net = gross_w - cost
        recs.append({
            "t": t, "t_end": nxt,
            "gross": gross_w, "funding": float((fnd * w).sum()),
            "basis": float(((spot_ret - perp_ret) * w).sum()),
            "cost": cost, "net": net,
            "net_on_capital": net / cap_mult,
            "sel": ",".join(sel),
        })
        prev_sel = sel
    return pd.DataFrame(recs)


def summarize(df, label):
    if df.empty:
        return None
    days = (df["t_end"].iloc[-1] - df["t"].iloc[0]).days
    years = days / 365
    eq = (1 + df["net_on_capital"]).cumprod()
    total = eq.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1
    yearly = {}
    for y, g in df.groupby(df["t"].dt.year):
        e = (1 + g["net_on_capital"]).prod() - 1
        yearly[int(y)] = e * 100
    return {
        "label": label, "periods": len(df),
        "total": total * 100, "annual": ann * 100,
        "mdd": ((eq / eq.cummax()) - 1).min() * 100,
        "win": (df["net"] > 0).mean() * 100,
        "yearly": yearly,
        "funding": df["funding"].mean() * 100,
        "basis": df["basis"].mean() * 100,
        "cost": df["cost"].mean() * 100,
    }


def main():
    Sd, Pd, Fd = load_all()
    print(f"  币种 {Sd.shape[1]} · 区间 {Sd.index[0].date()} → {Sd.index[-1].date()}")

    print()
    print("=" * 108)
    print("Carry 选币逻辑优化（统一账户假设：无强平风险）")
    print("=" * 108)
    print(f"  {'策略':<36}{'期数':>6}{'年化':>9}{'回撤':>9}{'胜率':>8}"
          f"{'2024':>9}{'2025':>9}{'2026':>9}")
    print("  " + "-" * 96)

    cases = [
        ("基线：Top5 等权 30d",        dict(topn=5, rebalance_days=30)),
        ("① 仅持正费率（>0）",          dict(topn=5, rebalance_days=30, min_funding=0.0)),
        ("① 仅持正费率（>2%/年）",      dict(topn=5, rebalance_days=30, min_funding=0.0000023)),
        ("② 按费率加权",               dict(topn=5, rebalance_days=30, weight="funding")),
        ("③ Top3",                    dict(topn=3, rebalance_days=30)),
        ("③ Top8",                    dict(topn=8, rebalance_days=30)),
        ("③ Top10",                   dict(topn=10, rebalance_days=30)),
        ("④ 7天调仓",                  dict(topn=5, rebalance_days=7)),
        ("④ 14天调仓",                 dict(topn=5, rebalance_days=14)),
        ("④ 60天调仓",                 dict(topn=5, rebalance_days=60)),
        ("组合：正费率+Top8+14d",       dict(topn=8, rebalance_days=14, min_funding=0.0)),
        ("组合：正费率+Top5+14d",       dict(topn=5, rebalance_days=14, min_funding=0.0)),
    ]
    results = []
    for label, kw in cases:
        try:
            df = backtest(Sd, Pd, Fd, **kw)
        except Exception as exc:
            print(f"  {label:<36} 失败: {exc}")
            continue
        r = summarize(df, label)
        if not r:
            continue
        results.append(r)
        y = r["yearly"]
        print(f"  {label:<36}{r['periods']:>6}{r['annual']:>8.2f}%{r['mdd']:>8.2f}%"
              f"{r['win']:>7.1f}%"
              f"{y.get(2024, float('nan')):>8.2f}%{y.get(2025, float('nan')):>8.2f}%"
              f"{y.get(2026, float('nan')):>8.2f}%")
    print("  " + "-" * 96)

    print()
    print("=" * 108)
    print("分年一致性检查（正负号必须一致，否则是过拟合）")
    print("=" * 108)
    print(f"  {'策略':<36}{'符号一致':>10}{'最差年份':>12}{'最好年份':>12}")
    print("  " + "-" * 72)
    for r in results:
        vs = [v for v in r["yearly"].values() if not np.isnan(v)]
        if len(vs) < 2:
            continue
        same = all(v > 0 for v in vs) or all(v < 0 for v in vs)
        print(f"  {r['label']:<36}{'✅ 是' if same else '❌ 否':>10}"
              f"{min(vs):>11.2f}%{max(vs):>11.2f}%")

    print()
    print("=" * 108)
    print("收益拆解对比")
    print("=" * 108)
    print(f"  {'策略':<36}{'资金费/期':>12}{'基差/期':>12}{'成本/期':>12}{'净/期':>12}")
    print("  " + "-" * 84)
    for r in results:
        print(f"  {r['label']:<36}{r['funding']:>11.4f}%{r['basis']:>11.4f}%"
              f"{r['cost']:>11.4f}%{r['funding']+r['basis']-r['cost']:>11.4f}%")

    print()
    best = max(results, key=lambda x: x["annual"])
    print(f"  年化最高: {best['label']} → {best['annual']:.2f}%  回撤 {best['mdd']:.2f}%")
    print()
    print("  ⚠️ 提醒：只有 33 个调仓期，参数越多越容易过拟合。")
    print("     建议只采纳【分年符号一致】且【经济含义清晰】的改进，不要挑年化最高的那个。")


if __name__ == "__main__":
    main()
