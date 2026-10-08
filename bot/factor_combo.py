#!/usr/bin/env python3
"""
因子组合回测 —— 把存活因子合成一个可交易策略

存活因子（修正 IC 自相关后 |t|>2）：
    dist_high_90  +0.0756  t=+3.54   靠近 90 日高点（强势/动量）
    vol_30d       -0.0902  t=-3.48   低波动异象
    funding       -0.0520  t=-3.41   高费率=拥挤→反转
    vol_7d        -0.0662  t=-3.11   低波动（短周期）
    channel_width -0.0670  t=-2.83   窄通道=低波动
    donchian_pos  +0.0306  t=+2.18   通道位置（动量）

合成方式：
    每个因子先做横截面 z-score，按 IC 符号翻转（使「越大越好」），等权平均。

组合构建：
    多头 = 综合分最高的 K 个币；空头 = 最低的 K 个币
    每 H 天调仓一次（不重叠），计双边成本

严格性：
    - 因子只用历史数据
    - 调仓不重叠（避免重叠样本让 t 虚高）
    - 统计量按调仓期计算（不按币）
    - 分年检查

用法:
    python factor_combo.py --horizon 7 --topk 3
    python factor_combo.py --sweep
"""

import argparse

import numpy as np
import pandas as pd

import factor_lib as fl

COST = 0.0010     # 双边 taker 0.10%


def build_composite(panels, F, factor_signs):
    """按符号翻转后做横截面 z-score，再等权平均"""
    zs = []
    for name, sign in factor_signs.items():
        if name not in panels:
            continue
        f = panels[name]
        # 横截面 z-score（每天在 15 个币之间标准化）
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1) + 1e-12, axis=0)
        zs.append(z * sign)
    if not zs:
        return None
    comp = sum(zs) / len(zs)
    return comp


def backtest(comp, C, horizon=7, topk=3, long_only=False):
    """每 horizon 天调仓，不重叠"""
    idx = comp.index
    # 调仓点
    points = list(idx[::horizon])
    recs = []
    prev_long, prev_short = set(), set()

    for t in points[:-1]:
        if t not in C.index:
            continue
        t_end_pos = C.index.searchsorted(t + pd.Timedelta(days=horizon))
        if t_end_pos >= len(C.index):
            break
        t_end = C.index[t_end_pos]

        score = comp.loc[t].dropna()
        if len(score) < 2 * topk + 2:
            continue
        ranked = score.sort_values(ascending=False)
        longs = list(ranked.index[:topk])
        shorts = list(ranked.index[-topk:]) if not long_only else []

        px0, px1 = C.loc[t], C.loc[t_end]
        ret = (px1 / px0 - 1)

        long_ret = ret[longs].mean()
        short_ret = -ret[shorts].mean() if shorts else 0.0

        # 成本：换手部分才收
        if prev_long:
            turn_l = len(set(longs) - prev_long) / max(len(longs), 1)
        else:
            turn_l = 1.0
        if shorts:
            turn_s = len(set(shorts) - prev_short) / max(len(shorts), 1) if prev_short else 1.0
        else:
            turn_s = 0.0

        cost = COST * (turn_l * (1 if long_only else 0.5) +
                       turn_s * (0 if long_only else 0.5)) * 2  # 两腿

        gross = long_ret if long_only else (long_ret + short_ret) / 2
        net = gross - cost

        recs.append({
            "t": t, "t_end": t_end,
            "long": long_ret, "short": short_ret,
            "gross": gross, "cost": cost, "net": net,
            "n_long": len(longs), "n_short": len(shorts),
        })
        prev_long, prev_short = set(longs), set(shorts)

    return pd.DataFrame(recs)


def summarize(df, label, periods_per_year):
    if df.empty:
        return None
    s = df["net"]
    n = len(s)
    mean = s.mean()
    t = mean / (s.std() / np.sqrt(n)) if s.std() > 0 else np.nan
    eq = (1 + s).cumprod()
    mdd = ((eq / eq.cummax()) - 1).min()
    years = (df["t_end"].iloc[-1] - df["t"].iloc[0]).days / 365
    ann = (1 + eq.iloc[-1] - 1) ** (1 / years) - 1 if years > 0 else np.nan
    yearly = {}
    for y, g in df.groupby(df["t"].dt.year):
        yearly[int(y)] = ((1 + g["net"]).prod() - 1) * 100
    return {
        "label": label, "n": n, "years": years,
        "mean": mean * 100, "t": t, "ann": ann * 100,
        "mdd": mdd * 100, "win": (s > 0).mean() * 100,
        "gross": df["gross"].mean() * 100, "cost": df["cost"].mean() * 100,
        "yearly": yearly,
        "sharpe": (mean / s.std() * np.sqrt(periods_per_year)) if s.std() > 0 else np.nan,
    }


SIGNS = {
    "dist_high_90": +1, "vol_30d": -1, "funding": -1,
    "vol_7d": -1, "channel_width": -1, "donchian_pos": +1,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--horizon", type=int, default=7)
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    print(f"  加载面板 {args.tf}…")
    panels, C = fl.build_panels(args.tf)
    F = fl.forward_returns(C, args.horizon)
    comp = build_composite(panels, F, SIGNS)
    if comp is None:
        print("  合成失败"); return

    ppy = 365 / args.horizon

    print()
    print("=" * 100)
    print(f"因子组合回测（{args.tf}，每 {args.horizon} 天调仓，不重叠）")
    print("=" * 100)
    print(f"  合成因子: {' + '.join(f'{k}({v:+d})' for k, v in SIGNS.items())}")
    print()

    if args.sweep:
        print(f"  {'配置':<28}{'期数':>6}{'毛/期':>10}{'净/期':>10}{'t值':>8}"
              f"{'年化':>10}{'Sharpe':>8}{'回撤':>9}")
        print("  " + "-" * 82)
        out = []
        for h in [5, 7, 10, 14, 30]:
            for k in [2, 3, 5]:
                comp_h = build_composite(panels, F, SIGNS)
                df = backtest(comp_h, C, horizon=h, topk=k)
                ppy_h = 365 / h
                st = summarize(df, f"H{h} K{k}", ppy_h)
                if not st:
                    continue
                out.append((st, h, k))
                print(f"  H{h}d 多空各{k}币{'':<12}{st['n']:>6}{st['gross']:>9.3f}%"
                      f"{st['mean']:>9.3f}%{st['t']:>8.2f}{st['ann']:>9.1f}%"
                      f"{st['sharpe']:>8.2f}{st['mdd']:>8.1f}%")
        print("  " + "-" * 82)
        good = [x for x in out if x[0]["t"] > 2 and x[0]["mean"] > 0]
        print(f"\n  t>2 且净收益为正的配置: {len(good)} / {len(out)}")
        print("\n  分年一致性:")
        for st, h, k in out[:6]:
            ys = "  ".join(f"{y}:{v:+.1f}%" for y, v in sorted(st["yearly"].items()))
            print(f"    H{h} K{k}: {ys}")
    else:
        df = backtest(comp, C, horizon=args.horizon, topk=args.topk)
        st = summarize(df, "combo", ppy)
        if st:
            print(f"  调仓次数 {st['n']} · 样本 {st['years']:.1f} 年")
            print(f"  毛/期 {st['gross']:+.3f}%   成本/期 {st['cost']:.3f}%   "
                  f"净/期 {st['mean']:+.3f}%")
            print(f"  年化 {st['ann']:+.2f}%   Sharpe {st['sharpe']:.2f}   "
                  f"t = {st['t']:.2f}   {'✅ 显著' if st['t'] > 2 else '⚠ 不显著'}")
            print(f"  最大回撤 {st['mdd']:.2f}%   按期胜率 {st['win']:.1f}%")
            print(f"  分年: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in sorted(st["yearly"].items())))

    return comp


if __name__ == "__main__":
    main()
