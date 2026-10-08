#!/usr/bin/env python3
"""
因子库 + 横截面 IC 分析

为什么不再做「端到端预测模型」：
    FreqAI 那种做法是把几百个特征塞进一个模型输出一个预测值，
    出问题时分不清是哪个因子有效、哪个在噪声。
    专业做法是【因子化】：每个因子单独度量，用横截面 IC 评估，
    再做组合，并且持续监控 IC 衰减。

因子分类（全部只用历史数据，无前视）：
    动量     1/3/7/14/30/60/90 日收益
    反转     1/3/7 日反转、距均线偏离
    波动     7/30 日实现波动、波动变化率、ATR 比率
    量能     成交量 z-score、量能趋势、换手
    资金费   均值、z-score、动量
    基差     永续/现货 − 1 及其变化
    趋势结构 Donchian 位置、均线斜率、通道宽度
    极值     RSI、布林带位置、距高低点距离

评估方式：
    横截面 IC = 每个交易日，15 个币的因子值与未来收益的 Spearman 相关
    输出：IC 均值、IC 标准差、ICIR、t 值（t = ICIR × √期数）、
          分年 IC、因子间相关性

用法:
    python factor_lib.py --tf 1d              # 分析全部因子
    python factor_lib.py --tf 1d --horizon 7  # 指定前瞻期
"""

import argparse
import glob
import os

import warnings

import numpy as np
import pandas as pd
from scipy.stats import ConstantInputWarning, spearmanr

warnings.filterwarnings("ignore", category=ConstantInputWarning)

PERP = "user_data/data/binance/futures"
SPOT = "user_data/data/binance"


# ══════════════════════ 因子定义 ══════════════════════

def compute_factors(d: pd.DataFrame, fr: pd.Series | None) -> pd.DataFrame:
    """d 需含列: close, high, low, volume（index 为时间）"""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    ret = c.pct_change()
    f = pd.DataFrame(index=d.index)

    # ── 动量 ──
    for n in [1, 3, 7, 14, 30, 60, 90]:
        f[f"mom_{n}d"] = c.pct_change(n)

    # ── 反转 ──
    f["rev_1d"] = -ret
    f["rev_3d"] = -(c.pct_change(3))
    f["rev_7d"] = -(c.pct_change(7))

    # ── 波动 ──
    f["vol_7d"] = ret.rolling(7).std()
    f["vol_30d"] = ret.rolling(30).std()
    f["vol_ratio"] = f["vol_7d"] / (f["vol_30d"] + 1e-12)      # 波动放大/收缩
    f["vol_change"] = f["vol_30d"] / (f["vol_30d"].shift(30) + 1e-12)

    # ── 量能 ──
    vm = v.rolling(30).mean()
    vs = v.rolling(30).std()
    f["vol_z"] = (v - vm) / (vs + 1e-12)
    f["vol_trend"] = v.rolling(7).mean() / (v.rolling(30).mean() + 1e-12)
    f["turnover"] = v / (vm + 1e-12)

    # ── 趋势结构 ──
    hh = c.rolling(20).max()
    ll = c.rolling(20).min()
    f["donchian_pos"] = (c - ll) / (hh - ll + 1e-12)          # 0=通道底 1=通道顶
    f["ma20"] = c.rolling(20).mean()
    f["ma_slope"] = f["ma20"] / (f["ma20"].shift(5) + 1e-12) - 1
    f["dist_ma20"] = c / (f["ma20"] + 1e-12) - 1
    f["channel_width"] = (hh - ll) / (c + 1e-12)

    # ── 极值 ──
    d_ = c.diff()
    up = d_.clip(lower=0).rolling(14).mean()
    dn = (-d_.clip(upper=0)).rolling(14).mean()
    f["rsi_14"] = 100 - 100 / (1 + up / (dn + 1e-12))
    ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    f["boll_pos"] = (c - ma20) / (2 * sd20 + 1e-12)
    f["dist_high_90"] = c / (c.rolling(90).max() + 1e-12) - 1
    f["dist_low_90"] = c / (c.rolling(90).min() + 1e-12) - 1

    # ── 资金费 ──
    if fr is not None and len(fr) > 0:
        ff = fr.reindex(d.index, method="ffill")
        f["funding"] = ff
        f["funding_mean_30d"] = ff.rolling(30).mean()
        f["funding_z"] = (ff - ff.rolling(90).mean()) / (ff.rolling(90).std() + 1e-12)
        f["funding_mom"] = ff.rolling(7).mean() - ff.rolling(30).mean()

    return f


FACTOR_GROUPS = {
    "动量": ["mom_1d", "mom_3d", "mom_7d", "mom_14d", "mom_30d", "mom_60d", "mom_90d"],
    "反转": ["rev_1d", "rev_3d", "rev_7d"],
    "波动": ["vol_7d", "vol_30d", "vol_ratio", "vol_change"],
    "量能": ["vol_z", "vol_trend", "turnover"],
    "趋势结构": ["donchian_pos", "ma_slope", "dist_ma20", "channel_width"],
    "极值": ["rsi_14", "boll_pos", "dist_high_90", "dist_low_90"],
    "资金费": ["funding", "funding_mean_30d", "funding_z", "funding_mom"],
}


# ══════════════════════ 数据加载 ══════════════════════

def load_closes(tf="1d") -> pd.DataFrame:
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP}/*-{tf}-futures.feather"))
    out = {}
    for s in syms:
        p = f"{PERP}/{s}_USDT_USDT-{tf}-futures.feather"
        if not os.path.exists(p):
            continue
        d = pd.read_feather(p).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        out[s] = d.set_index("date")["close"].astype(float)
    return pd.DataFrame(out).sort_index()


def build_panels(tf="1d"):
    """返回 ({因子名: DataFrame(日期×币种)}, 收盘价面板)"""
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP}/*-{tf}-futures.feather"))
    panel, closes = {}, {}
    for s in syms:
        p = f"{PERP}/{s}_USDT_USDT-{tf}-futures.feather"
        if not os.path.exists(p):
            continue
        d = pd.read_feather(p).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if not {"close", "high", "low", "volume"}.issubset(d.columns):
            continue

        # 1h 资金费聚合到目标周期
        fr = None
        fp = f"{PERP}/{s}_USDT_USDT-1h-funding_rate.feather"
        if os.path.exists(fp):
            x = pd.read_feather(fp).sort_values("date")
            x["date"] = pd.to_datetime(x["date"], utc=True).dt.tz_localize(None)
            fr = x.set_index("date")["funding_rate"].astype(float)
            if tf != "1h":
                fr = fr.resample(tf).sum()

        f = compute_factors(d[["close", "high", "low", "volume"]], fr)
        closes[s] = d["close"]
        for col in f.columns:
            panel.setdefault(col, {})[s] = f[col]

    return {k: pd.DataFrame(v).sort_index() for k, v in panel.items()}, pd.DataFrame(closes).sort_index()


# ══════════════════════ IC 分析 ══════════════════════

def forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    return closes.shift(-horizon) / closes - 1


def cross_sectional_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """每个交易日的横截面 Spearman IC"""
    ics = {}
    for t in factor.index:
        if t not in fwd.index:
            continue
        a, b = factor.loc[t], fwd.loc[t]
        m = pd.concat([a, b], axis=1).dropna()
        if len(m) < 5:
            continue
        if m.iloc[:, 0].std() == 0 or m.iloc[:, 1].std() == 0:
            continue
        ics[t] = spearmanr(m.iloc[:, 0], m.iloc[:, 1]).statistic
    return pd.Series(ics).sort_index()


def summarize_ic(ic: pd.Series):
    ic = ic.dropna()
    n = len(ic)
    if n < 20:
        return None
    mean, sd = ic.mean(), ic.std()
    icir = mean / sd if sd > 0 else np.nan
    t = icir * np.sqrt(n) if not np.isnan(icir) else np.nan
    yearly = {}
    for y, g in ic.groupby(ic.index.year):
        yearly[int(y)] = round(g.mean(), 4)
    return {"n": n, "ic": mean, "ic_std": sd, "icir": icir, "t": t,
            "pos_rate": (ic > 0).mean(), "yearly": yearly}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--horizon", type=int, default=7, help="前瞻期数（根）")
    ap.add_argument("--min-ic", type=float, default=0.02)
    args = ap.parse_args()

    print(f"  加载 {args.tf} 面板…")
    panels, C = build_panels(args.tf)
    if not panels:
        print("  面板为空"); return None
    F = forward_returns(C, args.horizon)

    print(f"  币种 {C.shape[1]} · 日期 {C.shape[0]} · 前瞻 {args.horizon} 根")
    print()
    print("=" * 104)
    print(f"因子横截面 IC（{args.tf}，前瞻 {args.horizon} 根）")
    print("=" * 104)
    print(f"  {'因子':<18}{'分组':<10}{'期数':>6}{'IC均值':>10}{'IC标准差':>11}"
          f"{'ICIR':>8}{'t值':>8}{'IC>0占比':>10}{'判定':>10}")
    print("  " + "-" * 94)

    results = []
    for grp, names in FACTOR_GROUPS.items():
        for name in names:
            if name not in panels:
                continue
            ic = cross_sectional_ic(panels[name], F)
            st = summarize_ic(ic)
            if not st:
                continue
            verdict = "✅ 有效" if abs(st["t"]) > 2 and abs(st["ic"]) > args.min_ic else "—"
            results.append({"factor": name, "group": grp, **st})
            print(f"  {name:<18}{grp:<10}{st['n']:>6}{st['ic']:>10.4f}{st['ic_std']:>11.4f}"
                  f"{st['icir']:>8.2f}{st['t']:>8.2f}{st['pos_rate']*100:>9.1f}%{verdict:>10}")

    print("  " + "-" * 94)
    good = [r for r in results if abs(r["t"]) > 2]
    print(f"\n  |t| > 2 的因子: {len(good)} / {len(results)}")
    if good:
        print("  按 |t| 排序:")
        for r in sorted(good, key=lambda x: -abs(x["t"]))[:10]:
            print(f"    {r['factor']:<18} IC {r['ic']:+.4f}  ICIR {r['icir']:+.2f}  t {r['t']:+.2f}  "
                  f"[{r['group']}]")

    print()
    print("  分年 IC（检验稳定性，符号翻转=不稳定）:")
    for r in sorted(results, key=lambda x: -abs(x["t"]))[:12]:
        ys = "  ".join(f"{y}:{v:+.3f}" for y, v in sorted(r["yearly"].items()))
        print(f"    {r['factor']:<18} {ys}")

    return results


if __name__ == "__main__":
    main()
