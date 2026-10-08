#!/usr/bin/env python3
"""
短线信号扫描器 —— 找出能否跨过成本墙的信号

背景（成本墙）：
    5分钟/15分钟在 taker 费率下数学上不可能盈利；
    1 小时在 maker 费率下需要约 58% 胜率才能盈亏平衡。
    所以本次扫描的判据是：**扣掉 maker 成本后，每笔净期望是否为正且稳定**。

扫描的信号（全部是经典、无 ML、无参数挖掘）：
    ① 动量            近 N 根收益为正 → 做多
    ② 反转            近 N 根跌超 x% → 做多（反之做空）
    ③ 突破            破 N 根新高 → 做多
    ④ RSI 极值        RSI<30 做多 / >70 做空
    ⑤ 布林带         触碰下轨做多 / 上轨做空
    ⑥ 资金费极值      资金费极负做多（拥挤空头回补）
    ⑦ 量能突破        放量 + 方向

严格性：
    - 信号只用 t 时刻及之前的数据
    - 固定持有 H 根后平仓（不重叠地统计，避免 t 值虚高）
    - 成本按 maker 双边 0.06% 扣
    - 分年检查符号一致性

用法: python signal_scan.py --tf 1h --hold 24
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP_DIR = "user_data/data/binance/futures"
MAKER_RT = 0.0006      # maker 双边 0.06%


def load(tf):
    out = {}
    for f in sorted(glob.glob(f"{PERP_DIR}/*-{tf}-futures.feather")):
        sym = os.path.basename(f).split("_")[0]
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True)
        out[sym] = d.set_index("date")["close"].astype(float)
    F = {}
    for sym in out:
        fp = f"{PERP_DIR}/{sym}_USDT_USDT-1h-funding_rate.feather"
        if os.path.exists(fp):
            fr = pd.read_feather(fp).sort_values("date")
            fr["date"] = pd.to_datetime(fr["date"], utc=True)
            F[sym] = fr.set_index("date")["funding_rate"].astype(float)
    return out, F


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def build_signals(px, fr, tf):
    """返回 {信号名: (方向序列, 描述)}；方向 +1 做多 / -1 做空 / 0 空仓"""
    d = pd.DataFrame({"close": px})
    d["ret1"] = d["close"].pct_change()
    # 不同周期用不同回看长度（小时数）
    H = {"1h": 1, "4h": 4, "15m": 0.25, "5m": 1 / 12}.get(tf, 1)
    n_fast = max(2, int(12 / H))
    n_slow = max(4, int(72 / H))
    n_long = max(6, int(168 / H))

    mom = d["close"].pct_change(n_fast)
    mom_s = d["close"].pct_change(n_slow)
    hh = d["close"].rolling(n_slow).max().shift(1)
    ll = d["close"].rolling(n_slow).min().shift(1)
    r = rsi(d["close"], max(6, int(14 / H)))
    ma = d["close"].rolling(n_slow).mean()
    sd = d["close"].rolling(n_slow).std()
    upper, lower = ma + 2 * sd, ma - 2 * sd
    vol = d["ret1"].rolling(n_slow).std()
    volz = (d["ret1"].abs() - d["ret1"].abs().rolling(n_long).mean()) / (
        d["ret1"].abs().rolling(n_long).std() + 1e-12)

    sig = {}
    sig["① 动量(短期)"] = np.sign(mom)
    sig["① 动量(中期)"] = np.sign(mom_s)
    sig["② 反转(短期)"] = -np.sign(mom)
    sig["② 反转(中期)"] = -np.sign(mom_s)
    sig["③ 突破(新高/新低)"] = np.where(d["close"] > hh, 1, np.where(d["close"] < ll, -1, 0))
    sig["④ RSI 极值"] = np.where(r < 30, 1, np.where(r > 70, -1, 0))
    sig["⑤ 布林带触碰"] = np.where(d["close"] < lower, 1, np.where(d["close"] > upper, -1, 0))
    sig["⑦ 放量+方向"] = np.where(volz > 1.5, np.sign(d["ret1"]), 0)
    sig["⑦ 放量+反向"] = np.where(volz > 1.5, -np.sign(d["ret1"]), 0)
    if fr is not None and len(fr) > 0:
        f = fr.reindex(d.index, method="ffill")
        fz = (f - f.rolling(n_long).mean()) / (f.rolling(n_long).std() + 1e-12)
        sig["⑥ 资金费极负→多"] = np.where(fz < -1.5, 1, np.where(fz > 1.5, -1, 0))
    return {k: pd.Series(v, index=d.index).fillna(0) for k, v in sig.items()}, d


def evaluate(px, sig, hold):
    """不重叠统计：每个信号在 t 触发，持有 hold 根后平仓"""
    fwd = px.shift(-hold) / px - 1
    rows = []
    for t in range(0, len(px) - hold, hold):     # 不重叠采样
        ts = px.index[t]
        s = sig.iloc[t]
        if s == 0:
            continue
        gross = float(fwd.iloc[t]) * s
        rows.append({"t": ts, "gross": gross, "net": gross - MAKER_RT})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--hold", type=int, default=24)
    ap.add_argument("--min-trades", type=int, default=200)
    args = ap.parse_args()

    px_all, fr_all = load(args.tf)
    print(f"  周期 {args.tf} · 持有 {args.hold} 根 · 币种 {len(px_all)} · 成本 maker 双边 {MAKER_RT*100:.2f}%")

    per_sig = {}
    for sym, px in px_all.items():
        if len(px) < 500:
            continue
        sigs, d = build_signals(px, fr_all.get(sym), args.tf)
        for name, sg in sigs.items():
            df = evaluate(px, sg, args.hold)
            if not df.empty:
                df["sym"] = sym
                per_sig.setdefault(name, []).append(df)

    print()
    print("=" * 108)
    print(f"短线信号扫描结果（{args.tf}，持有 {args.hold} 根，不重叠采样，已扣 maker 成本）")
    print("=" * 108)
    print(f"  {'信号':<20}{'交易数':>8}{'胜率':>9}{'毛/笔':>10}{'净/笔':>10}"
          f"{'t值':>8}{'年化(单币满仓)':>16}{'分年一致':>10}")
    print("  " + "-" * 94)

    results = []
    for name, dfs in sorted(per_sig.items()):
        df = pd.concat(dfs)
        n = len(df)
        if n < args.min_trades:
            continue
        net = df["net"]
        mean = net.mean()
        t = mean / (net.std() / np.sqrt(n)) if net.std() > 0 else np.nan
        periods_per_year = {"1h": 8760, "4h": 2190, "15m": 35040, "5m": 105120}[args.tf] / args.hold
        ann = (1 + mean) ** periods_per_year - 1
        df["year"] = df["t"].dt.year
        yv = {int(y): (1 + g["net"]).prod() - 1 for y, g in df.groupby("year")}
        same = all(v > 0 for v in yv.values()) or all(v < 0 for v in yv.values()) if len(yv) > 1 else False
        results.append(dict(name=name, n=n, win=(net > 0).mean() * 100,
                            gross=df["gross"].mean() * 100, net=mean * 100,
                            t=t, ann=ann * 100, same=same, yearly=yv))
        print(f"  {name:<20}{n:>8}{(net>0).mean()*100:>8.1f}%{df['gross'].mean()*100:>9.3f}%"
              f"{mean*100:>9.3f}%{t:>8.2f}{ann*100:>15.0f}%"
              f"{'✅' if same else '❌':>10}")

    print("  " + "-" * 94)
    print()
    print("  分年明细:")
    for r in results:
        print(f"    {r['name']:<20} " + "  ".join(f"{y}:{v*100:+.1f}%" for y, v in r["yearly"].items()))

    print()
    good = [r for r in results if r["net"] > 0 and r["t"] > 2 and r["same"]]
    print("=" * 108)
    if good:
        print(f"  ✅ 通过全部三项检验（净期望>0 · t>2 · 分年符号一致）的信号: {len(good)} 个")
        for r in good:
            print(f"     {r['name']}  净/笔 {r['net']:+.3f}%  t={r['t']:.2f}  年化 {r['ann']:+.0f}%")
    else:
        print("  ❌ 没有任何信号同时满足：净期望>0 · t>2 · 分年符号一致")
        pos = [r for r in results if r["net"] > 0]
        print(f"     净期望为正的只有 {len(pos)} 个: " +
              ", ".join(f"{r['name']}(t={r['t']:.1f})" for r in pos) if pos else "     一个都没有")
    print("=" * 108)


if __name__ == "__main__":
    main()
