#!/usr/bin/env python3
"""
网格交易回测

原理：
    在价格周围布一串网格。跌一格买入，涨一格卖出，赚取震荡的差价。
    本质是【卖出波动率 + 赚震荡】，与趋势跟踪（赚单边）性质互补。

已知的三个死穴（必须正面回答）：
    ① 成本墙：每格利润必须显著大于双边手续费，否则交易越多次亏越多
    ② 趋势市：价格单边突破区间后，网格会持续接飞刀 / 被套
    ③ 区间判定：怎么定上下界？定窄了频繁突破，定宽了很少成交

本实现的设计（用已验证的波动率因子解决 ①③）：
    · 网格间距 = k × ATR（波动率自适应，而不是固定百分比）
    · 区间 = 现价 ± m × ATR
    · 每次成交立即挂反向单（真实网格的做法）
    · 严格计入 maker 双边成本

用法:
    python grid_backtest.py                    # 默认参数
    python grid_backtest.py --sweep            # 参数扫描
    python grid_backtest.py --regime           # 分市场状态统计
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
MAKER_ONE = 0.0002      # 单边 maker
SLIP_ONE = 0.0001       # 单边滑点
COST_RT = 2 * (MAKER_ONE + SLIP_ONE)   # 双边 0.06%


def atr(df, win=20):
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(win).mean()


def grid_backtest(df: pd.DataFrame, grid_k=0.5, range_m=3.0, atr_win=14,
                  capital=1.0, max_levels=10):
    """
    单币网格。
    grid_k   : 网格间距 = grid_k × ATR
    range_m  : 区间半宽 = range_m × ATR
    max_levels: 最多同时持有的网格层数（控制最大仓位）
    返回逐日收益序列
    """
    d = df.copy()
    d["atr"] = atr(d, atr_win)
    d["atr_pct"] = d["atr"] / d["close"]
    d = d.dropna()
    if len(d) < 60:
        return None

    # 每个网格层分配的资金
    per_level = capital / max_levels

    equity = capital
    curve = []
    n_fills = 0
    n_roundtrips = 0
    gross_sum = 0.0
    cost_sum = 0.0

    i = 0
    while i < len(d):
        row = d.iloc[i]
        step = row["atr"] * grid_k
        if not np.isfinite(step) or step <= 0:
            curve.append((row.name, equity))
            i += 1
            continue

        anchor = row["close"]
        lo = anchor - range_m * row["atr"]
        hi = anchor + range_m * row["atr"]

        # 在该区间内运行网格，直到价格突破区间
        if equity <= 0.02:            # 本金基本亏光 → 停止交易（爆仓）
            curve.append((row.name, max(equity, 0.0)))
            i += 1
            continue

        holdings = []          # 每层: (买入价)
        j = i
        while j < len(d):
            px = d.iloc[j]["close"]
            if px > hi or px < lo:
                break                      # 突破区间 → 结束本轮网格
            # 检查是否触发网格层
            lvl = int((anchor - px) / step)     # 低于锚点的层数
            if lvl > 0 and len(holdings) < max_levels:
                buy_px = anchor - lvl * step
                if px <= buy_px:
                    holdings.append(buy_px)
                    equity -= per_level * COST_RT / 2
                    cost_sum += per_level * COST_RT / 2
                    n_fills += 1
            # 检查持仓是否达到卖出条件
            still = []
            for bp in holdings:
                sell_px = bp + step
                if px >= sell_px:
                    pnl = per_level * (sell_px / bp - 1)
                    equity += pnl - per_level * COST_RT / 2
                    gross_sum += pnl
                    cost_sum += per_level * COST_RT / 2
                    n_roundtrips += 1
                else:
                    still.append(bp)
            holdings = still
            curve.append((d.iloc[j].name, equity))
            j += 1

        # 突破区间：按突破价了结所有持仓（可能亏损）
        if holdings and j < len(d):
            px = d.iloc[j]["close"]
            for bp in holdings:
                pnl = per_level * (px / bp - 1)
                equity += pnl - per_level * COST_RT / 2
                gross_sum += pnl
                cost_sum += per_level * COST_RT / 2
            holdings = []
        i = max(j, i + 1)

    s = pd.Series(dict(curve)).sort_index()
    s = s[~s.index.duplicated()]
    ret = s.pct_change().dropna()
    return {"ret": ret, "n_fills": n_fills, "n_roundtrips": n_roundtrips,
            "gross": gross_sum, "cost": cost_sum, "final": equity}


def stats(ret: pd.Series, sym=""):
    if len(ret) < 50:
        return None
    eq = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365
    ann = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = ret.std() * np.sqrt(365)
    sharpe = (ret.mean() * 365) / vol if vol > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"sym": sym, "ann": ann * 100, "vol": vol * 100, "sharpe": sharpe,
            "mdd": mdd * 100, "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "win": (ret > 0).mean() * 100}


def load_all(tf="1d"):
    out = {}
    for f in sorted(glob.glob(f"{PERP}/*-{tf}-futures.feather")):
        s = os.path.basename(f).split("_")[0]
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) > 200:
            out[s] = d[["close", "high", "low", "volume"]]
    return out


def run_all(data, **kw):
    per = {}
    detail = []
    for s, d in data.items():
        r = grid_backtest(d, **kw)
        if r is None:
            continue
        per[s] = r["ret"]
        detail.append({"sym": s, "fills": r["n_fills"], "rt": r["n_roundtrips"],
                       "gross": r["gross"], "cost": r["cost"]})
    if not per:
        return None, None
    R = pd.DataFrame(per).fillna(0.0)
    port = R.mean(axis=1)          # 等权分配到各币
    return port, pd.DataFrame(detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid-k", type=float, default=0.5)
    ap.add_argument("--range-m", type=float, default=3.0)
    ap.add_argument("--max-levels", type=int, default=10)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--regime", action="store_true")
    args = ap.parse_args()

    print("  加载日线数据…")
    data = load_all("1d")
    print(f"  币种 {len(data)}")

    print()
    print("=" * 104)
    print("网格交易回测（日线，波动率自适应间距，含双边成本 0.06%）")
    print("=" * 104)

    if args.sweep:
        print(f"\n  {'间距k':>7}{'区间m':>7}{'层数':>6}{'成交次数':>10}{'年化':>10}"
              f"{'波动':>9}{'Sharpe':>9}{'回撤':>10}{'Calmar':>9}")
        print("  " + "-" * 88)
        best = []
        for k in [0.3, 0.5, 1.0, 1.5]:
            for m in [2.0, 3.0, 5.0]:
                port, det = run_all(data, grid_k=k, range_m=m,
                                    max_levels=args.max_levels)
                if port is None:
                    continue
                st = stats(port)
                if not st:
                    continue
                fills = int(det["fills"].sum())
                print(f"  {k:>7.1f}{m:>7.1f}{args.max_levels:>6}{fills:>10}"
                      f"{st['ann']:>9.2f}%{st['vol']:>8.1f}%{st['sharpe']:>9.2f}"
                      f"{st['mdd']:>9.2f}%{st['calmar']:>9.2f}")
                best.append((st, k, m, det))
        print("  " + "-" * 88)
        if best:
            b = max(best, key=lambda x: x[0]["sharpe"])
            print(f"\n  Sharpe 最高: k={b[1]} m={b[2]} → Sharpe {b[0]['sharpe']:.2f}  "
                  f"年化 {b[0]['ann']:.2f}%  回撤 {b[0]['mdd']:.2f}%")
            d = b[3]
            print(f"  总成交 {int(d['fills'].sum())} 次 · 总毛利 {d['gross'].sum():.3f} · "
                  f"总成本 {d['cost'].sum():.3f} · 成本/毛利 = {d['cost'].sum()/max(d['gross'].sum(),1e-9):.1%}")
    else:
        port, det = run_all(data, grid_k=args.grid_k, range_m=args.range_m,
                            max_levels=args.max_levels)
        st = stats(port)
        print(f"\n  参数: 间距 {args.grid_k}×ATR · 区间 ±{args.range_m}×ATR · 最多 {args.max_levels} 层")
        print(f"  币种 {len(det)} · 总成交 {int(det['fills'].sum())} 次 · "
              f"完整往返 {int(det['rt'].sum())} 次")
        print(f"  总毛利 {det['gross'].sum():.3f}   总成本 {det['cost'].sum():.3f}   "
              f"成本占毛利 {det['cost'].sum()/max(det['gross'].sum(),1e-9):.1%}")
        print()
        print(f"  年化 {st['ann']:+.2f}%   波动 {st['vol']:.1f}%   Sharpe {st['sharpe']:.2f}   "
              f"最大回撤 {st['mdd']:.2f}%   Calmar {st['calmar']:.2f}")
        print(f"  日胜率 {st['win']:.1f}%")
        yearly = {}
        for y, g in port.groupby(port.index.year):
            yearly[int(y)] = ((1 + g).prod() - 1) * 100
        print("  分年: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in sorted(yearly.items())))

        print()
        print("  分币明细（前 12 个）:")
        det["eff"] = det["gross"] / det["cost"].replace(0, np.nan)
        print(f"    {'币种':<10}{'成交':>7}{'往返':>7}{'毛利':>10}{'成本':>10}{'毛利/成本':>11}")
        for _, r in det.sort_values("eff", ascending=False).head(12).iterrows():
            print(f"    {r['sym']:<10}{int(r['fills']):>7}{int(r['rt']):>7}"
                  f"{r['gross']:>10.3f}{r['cost']:>10.3f}{r['eff']:>11.2f}")

    if args.regime:
        print()
        print("=" * 104)
        print("分市场状态：震荡期 vs 趋势期（这是网格能否赚钱的关键）")
        print("=" * 104)
        # 用 BTC 的趋势强度划分市场状态
        btc = data.get("BTC")
        if btc is not None:
            ret = btc["close"].pct_change()
            # 趋势强度 = |20日收益| / 20日波动
            trend = ret.rolling(20).sum() / (ret.rolling(20).std() * np.sqrt(20) + 1e-12)
            trend = trend.shift(1)
            port, _ = run_all(data, grid_k=args.grid_k, range_m=args.range_m,
                              max_levels=args.max_levels)
            df = pd.DataFrame({"ret": port, "trend": trend.reindex(port.index)}).dropna()
            # ⚠️ 注意：pd.qcut 按数值【升序】贴标签。
            #    曾经写成 labels=["趋势最强","中性","震荡最强"]，
            #    导致 trend 最低的一层被贴上「趋势最强」——标签完全反了，
            #    一度得出「网格在震荡市 Sharpe 2.05」的错误结论。
            q = pd.qcut(df["trend"], 3, labels=["震荡最强", "中性", "趋势最强"])
            print(f"  {'市场状态':<12}{'天数':>7}{'年化':>12}{'Sharpe':>10}{'胜率':>9}")
            print("  " + "-" * 54)
            for lab in ["震荡最强", "中性", "趋势最强"]:
                g = df[q == lab]
                if len(g) < 30:
                    continue
                ann = (1 + g["ret"]).prod() ** (365 / len(g)) - 1
                vol = g["ret"].std() * np.sqrt(365)
                sh = (g["ret"].mean() * 365) / vol if vol > 0 else np.nan
                print(f"  {lab:<12}{len(g):>7}{ann*100:>11.1f}%{sh:>10.2f}"
                      f"{(g['ret']>0).mean()*100:>8.1f}%")


if __name__ == "__main__":
    main()
