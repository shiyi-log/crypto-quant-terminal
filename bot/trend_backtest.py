#!/usr/bin/env python3
"""
趋势跟踪（海龟法则 / Donchian 突破）回测

来源：社交媒体与研究院讨论里被反复提到的方向，Gate 研究院声称年化 62.71%。
      也是我们自己数据里唯一出现过「扣费后为正」的方向
      （横截面检验：168h 价格动量 +0.063%/周）。

原理：
      加密资产在中周期（数周~数月）存在趋势延续性（time-series momentum）。
      突破 N 日高点做多、跌破 N 日低点做空，用 ATR 定仓位，反向突破离场。

严格性要求（吸取之前教训）：
      1. 只用历史数据，无前视
      2. 计入 0.10% 双边手续费 + 滑点
      3. 分年检查符号一致性
      4. ATR 仓位管理，风险预算固定
      5. 与「买入持有」基准对比

用法:
    python trend_backtest.py --entry 20 --exit 10 --risk 0.01
    python trend_backtest.py --sweep        # 参数扫描
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP_DIR = "user_data/data/binance/futures"
TAKER = 0.0005
SLIPPAGE = 0.0002
COST = TAKER + SLIPPAGE          # 单边
ATR_WIN = 20


def load_daily(sym):
    """优先用 1d 数据（真实 OHLC + 历史更长）；没有则从 1h 重采样"""
    p1d = f"{PERP_DIR}/{sym}_USDT_USDT-1d-futures.feather"
    if os.path.exists(p1d):
        d = pd.read_feather(p1d).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True)
        d = d.set_index("date")
        df = d[["close", "high", "low"]].dropna()
        return df if len(df) > 100 else None
    p = f"{PERP_DIR}/{sym}_USDT_USDT-1h-futures.feather"
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    d = d.set_index("date")
    o = d["close"].resample("1D").last().dropna()
    h = d["close"].resample("1D").max().dropna()
    l = d["close"].resample("1D").min().dropna()
    df = pd.DataFrame({"close": o, "high": h, "low": l}).dropna()
    return df if len(df) > 100 else None


def atr(df, win=ATR_WIN):
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(win).mean()


def backtest_symbol(df, entry_n, exit_n, risk, capital_per_coin=1.0, allow_short=True):
    """单币趋势跟踪。返回逐日收益序列（以分配资金为 1）"""
    d = df.copy()
    d["atr"] = atr(d)
    d["hh"] = d["high"].rolling(entry_n).max().shift(1)   # 前 N 日最高（不含当日，避免前视）
    d["ll"] = d["low"].rolling(entry_n).min().shift(1)
    d["exit_hh"] = d["high"].rolling(exit_n).max().shift(1)
    d["exit_ll"] = d["low"].rolling(exit_n).min().shift(1)
    d = d.dropna()
    if len(d) < 30:
        return None

    pos = 0          # +1 多 / -1 空 / 0 空仓
    units = 0.0      # 名义本金倍数
    entry_px = 0.0
    rets, trades = [], []

    for i in range(1, len(d)):
        row, prev = d.iloc[i], d.iloc[i - 1]
        # 当日盈亏（按昨日持仓）
        if pos != 0 and entry_px > 0:
            r = (row["close"] / prev["close"] - 1) * pos * units
            rets.append((row.name, r))
        else:
            rets.append((row.name, 0.0))

        # 离场判断（用当日收盘）
        exit_now = False
        if pos > 0 and row["close"] < row["exit_ll"]:
            exit_now = True
        elif pos < 0 and row["close"] > row["exit_hh"]:
            exit_now = True

        if exit_now:
            pnl = (row["close"] / entry_px - 1) * pos * units
            trades.append({"exit": row.name, "side": pos, "pnl": pnl - COST * units})
            rets[-1] = (row.name, rets[-1][1] - COST * units)
            pos, units = 0, 0.0

        # 入场判断
        if pos == 0 and row["atr"] > 0:
            unit = (risk * capital_per_coin) / (row["atr"] / row["close"])   # 风险预算 / 波动率
            unit = min(unit, 1.0)                                            # 不加杠杆
            if row["close"] > row["hh"]:
                pos, units, entry_px = 1, unit, row["close"]
                rets[-1] = (row.name, rets[-1][1] - COST * unit)
            elif allow_short and row["close"] < row["ll"]:
                pos, units, entry_px = -1, unit, row["close"]
                rets[-1] = (row.name, rets[-1][1] - COST * unit)

    s = pd.Series(dict(rets)).sort_index()
    return s, trades


def run(entry_n, exit_n, risk, start=None, verbose=True):
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP_DIR}/*-1h-futures.feather"))
    per_coin, all_tr, used = {}, [], []
    for s in syms:
        df = load_daily(s)
        if df is None:
            continue
        if start:
            df = df[df.index >= pd.Timestamp(start, tz="UTC")]
        if len(df) < 100:
            continue
        r = backtest_symbol(df, entry_n, exit_n, risk)
        if r is None:
            continue
        ser, tr = r
        per_coin[s] = ser
        all_tr.extend(tr)
        used.append((s, df))

    if not per_coin:
        raise SystemExit("无可用数据")

    R = pd.DataFrame(per_coin).fillna(0.0)
    # 等权分配到各币
    port = R.div(len(per_coin))          # 每币分到 1/N 资金
    daily = port.sum(axis=1)
    eq = (1 + daily).cumprod()

    days = (daily.index[-1] - daily.index[0]).days
    years = days / 365
    total = eq.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = daily.std() * np.sqrt(365)
    sharpe = (daily.mean() * 365) / vol if vol > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()

    # 买入持有基准
    px = pd.DataFrame({s: d["close"] for s, d in used}).dropna(how="all")
    bh = (px.iloc[-1] / px.iloc[0] - 1).mean()
    bh_ann = (1 + bh) ** (1 / years) - 1

    yearly = {}
    for y, g in daily.groupby(daily.index.year):
        yearly[int(y)] = ((1 + g).prod() - 1) * 100

    # ── 严谨显著性：Sharpe 的 t 值 = Sharpe × √年数（这是唯一正确的检验）──
    sharpe_t = sharpe * np.sqrt(years) if years > 0 else np.nan
    ac1 = daily.autocorr(lag=1) if len(daily) > 10 else 0.0

    # ── 买入持有基准的风险指标（只看收益不公平，要比风险调整后）──
    bh_ret = px.pct_change().mean(axis=1).dropna()
    bh_vol = bh_ret.std() * np.sqrt(365)
    bh_sharpe = (bh_ret.mean() * 365) / bh_vol if bh_vol > 0 else np.nan
    bh_eq = (1 + bh_ret).cumprod()
    bh_mdd = ((bh_eq / bh_eq.cummax()) - 1).min()

    out = {
        "years": years, "sharpe_t": sharpe_t, "autocorr": ac1,
        "bh_vol": bh_vol * 100, "bh_sharpe": bh_sharpe, "bh_mdd": bh_mdd * 100,
        "entry_n": entry_n, "exit_n": exit_n, "risk": risk,
        "coins": len(per_coin), "trades": len(all_tr),
        "total": total * 100, "annual": ann * 100,
        "vol": vol * 100, "sharpe": sharpe, "mdd": mdd * 100,
        "win": (daily > 0).mean() * 100,
        "yearly": yearly,
        "bh_annual": bh_ann * 100,
        "avg_trade_pnl": np.mean([t["pnl"] for t in all_tr]) * 100 if all_tr else np.nan,
        "trade_win": np.mean([t["pnl"] > 0 for t in all_tr]) * 100 if all_tr else np.nan,
    }

    if verbose:
        print(f"\n  参数: 入场 {entry_n} 日突破 · 离场 {exit_n} 日 · 单笔风险 {risk:.1%}")
        print(f"  币种 {out['coins']} 个 · 交易 {out['trades']} 笔")
        print(f"  累计 {out['total']:+.2f}%   年化 {out['annual']:+.2f}%   "
              f"波动率 {out['vol']:.1f}%   Sharpe {out['sharpe']:.2f}")
        print(f"  最大回撤 {out['mdd']:.2f}%   日胜率 {out['win']:.1f}%   "
              f"单笔胜率 {out['trade_win']:.1f}%   平均单笔 {out['avg_trade_pnl']:+.3f}%")
        print(f"  样本 {out['years']:.1f} 年 · 日收益自相关 {out['autocorr']:+.3f}")
        print(f"  ★ Sharpe 的 t 值 = {out['sharpe_t']:.2f}  "
              f"{'✅ 显著' if abs(out['sharpe_t']) > 2 else '⚠ 不显著（t<2）'}")
        print(f"  买入持有基准: 年化 {out['bh_annual']:+.2f}%  波动 {out['bh_vol']:.1f}%  "
              f"Sharpe {out['bh_sharpe']:.2f}  回撤 {out['bh_mdd']:.1f}%")
        print(f"  分年: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in yearly.items()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--exit", type=int, default=10)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--start", default=None)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    print("=" * 104)
    print("趋势跟踪（海龟/Donchian 突破）回测 —— 15 币种，日线，含双边 0.10% 成本")
    print("=" * 104)

    if args.sweep:
        print(f"\n  {'入场':>5}{'离场':>5}{'风险':>7}{'交易':>7}{'年化':>10}{'波动':>9}"
              f"{'Sharpe':>9}{'SharpeT':>9}{'回撤':>9}{'基准Sharpe':>11}{'基准回撤':>10}")
        print("  " + "-" * 100)
        best = []
        for e, x in [(20, 10), (20, 20), (55, 20), (55, 10), (30, 15), (100, 50)]:
            try:
                r = run(e, x, args.risk, args.start, verbose=False)
            except SystemExit:
                continue
            best.append(r)
            print(f"  {e:>5}{x:>5}{args.risk:>7.1%}{r['trades']:>7}{r['annual']:>9.2f}%"
                  f"{r['vol']:>8.1f}%{r['sharpe']:>9.2f}{r['sharpe_t']:>9.2f}"
                  f"{r['mdd']:>8.1f}%{r['bh_sharpe']:>11.2f}{r['bh_mdd']:>9.1f}%")
        print("  " + "-" * 100)
        print(f"  样本长度: {best[0]['years']:.1f} 年")
        print("\n  分年一致性（符号必须一致）:")
        for r in best:
            vs = [v for v in r["yearly"].values()]
            same = all(v > 0 for v in vs) or all(v < 0 for v in vs)
            print(f"    {r['entry_n']}/{r['exit_n']}: {'✅ 一致' if same else '❌ 翻转'}  "
                  + "  ".join(f"{y}:{v:+.1f}%" for y, v in r["yearly"].items()))
    else:
        run(args.entry, args.exit, args.risk, args.start)


if __name__ == "__main__":
    main()
