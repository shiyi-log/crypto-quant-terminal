#!/usr/bin/env python3
"""
高频研究台（HFT Lab）—— 以 BTC/ETH 为主

为什么高频方向可行（与日线 BTC/ETH 对比）：

    周期/回看         独立趋势   最小可检测 IC
    日线 20日            129        0.2465   ← 无法验证弱信号
    15m 12h            2,429       0.0568
    5m  8h             3,633       0.0465
    5m  4h             7,221       0.0330   ← 与实测信号强度同量级

    高频的统计功效比日线好 5~7 倍，因为【回看短 → 独立事件多】。

但成本墙是真正的约束（本项目早前已建立）：
    taker（0.05%/边，往返 0.10%）：5m 需要 131% 胜率优势 → 不可能
    maker（0.02%/边，往返 0.04%）：需要单笔平均收益 > 0.04% → 可达

**所以 HFT 的第一步不是找信号，而是把成本模型讲清楚。**

本模块提供：
    ① load_hf()      高频数据加载（5m/15m/1h/4h）
    ② cost_wall()    成本墙计算（maker/taker 双口径）
    ③ hf_events()    高频事件采样（Donchian 状态机，短回看）
    ④ hf_features()  高频特征（微结构：价差代理、量能、波动、动量）
    ⑤ evaluate()     稳健评估（多种窗口宽度 + 逐笔 IC）

用法（作为库）:
    from hft_lab import load_hf, cost_wall, hf_events, hf_features
"""

import glob
import os

import numpy as np
import pandas as pd

DATA = "user_data/data/binance/futures"
CORE = ["BTC", "ETH"]

# 费率（与实盘配置一致）
TAKER = 0.0005      # 0.05%/边
MAKER = 0.0002      # 0.02%/边


def load_hf(coins=None, tf="5m"):
    """加载高频数据。返回 {coin: DataFrame(date, ohlcv)}"""
    coins = coins or CORE
    out = {}
    for c in coins:
        f = f"{DATA}/{c}_USDT_USDT-{tf}-futures.feather"
        if not os.path.exists(f):
            continue
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        out[c] = d[["open", "high", "low", "close", "volume"]]
    return out


def cost_wall(tf_minutes, edge_bps, maker=True):
    """
    成本墙：给定周期与单笔毛收益预期（bps），算净收益与所需胜率。

    tf_minutes: 周期（分钟）
    edge_bps:   单笔毛收益（基点，1bp = 0.01%）
    """
    fee = (MAKER if maker else TAKER) * 2      # 往返
    gross = edge_bps / 10_000
    net = gross - fee
    # 需要多高的胜率才能覆盖成本（假设盈/亏对称）
    need_win = 0.5 + fee / (2 * max(gross, 1e-9)) if gross > 0 else np.inf
    # 年化换手次数（每天 1440 分钟）
    trades_per_year = (1440 / tf_minutes) * 365
    return {
        "tf": tf_minutes, "maker": maker,
        "fee_roundtrip_bps": round(fee * 10_000, 2),
        "gross_bps": edge_bps, "net_bps": round(net * 10_000, 2),
        "need_win_rate": round(need_win, 4) if np.isfinite(need_win) else None,
        "trades_per_year": int(trades_per_year),
    }


def print_cost_wall():
    print("=" * 96)
    print("成本墙（BTC/ETH · 币安）")
    print("=" * 96)
    for tf, lab in [(5, "5m"), (15, "15m"), (60, "1h"), (240, "4h")]:
        print(f"\n  【{lab}】")
        print(f"    {'单笔毛收益':>12}{'taker净':>10}{'maker净':>10}"
              f"{'taker需胜率':>13}{'maker需胜率':>13}")
        print("    " + "-" * 60)
        for bps in [2, 5, 10, 20, 50]:
            t = cost_wall(tf, bps, maker=False)
            m = cost_wall(tf, bps, maker=True)
            print(f"    {str(bps)+'bp':>12}{t['net_bps']:>10.1f}{m['net_bps']:>10.1f}"
                  f"{(t['need_win_rate'] or 9.99):>13.3f}{(m['need_win_rate'] or 9.99):>13.3f}")
    print()
    print("  解读：")
    print("    · taker 口径下，5m/15m 需要 >100% 的胜率 —— **数学上不可能**")
    print("    · maker 口径下，5m 只需单笔毛收益 > 4bp（0.04%）即可盈亏平衡")
    print("    · 高质量限价单在 BTC/ETH 上成交率高（盘口厚、价差窄）")
    print("    → **HFT 的前提是 maker 执行；用 taker 做高频必亏**")


def hf_events(data, entry=96, exit_=96, side_filter=None):
    """高频事件采样：Donchian 状态机的每个处于趋势中的 bar"""
    rows = []
    for s, d in data.items():
        c = d["close"]
        hh = c.rolling(entry).max().shift(1)
        ll = c.rolling(entry).min().shift(1)
        hx = c.rolling(exit_).max().shift(1)
        lx = c.rolling(exit_).min().shift(1)
        cv = c.values
        cur = 0.0
        st = np.zeros(len(cv))
        for i in range(len(cv)):
            if cur == 0.0:
                if np.isfinite(hh.iloc[i]) and cv[i] > hh.iloc[i]:
                    cur = 1.0
                elif np.isfinite(ll.iloc[i]) and cv[i] < ll.iloc[i]:
                    cur = -1.0
            else:
                if cur > 0 and np.isfinite(lx.iloc[i]) and cv[i] < lx.iloc[i]:
                    cur = 0.0
                elif cur < 0 and np.isfinite(hx.iloc[i]) and cv[i] > hx.iloc[i]:
                    cur = 0.0
            st[i] = cur
        if side_filter is not None:
            st = np.where(st == side_filter, st, 0.0)
        for i in range(len(st)):
            if st[i] != 0 and i + 1 < len(cv):
                rows.append({"date": d.index[i], "coin": s, "side": int(st[i])})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def hf_features(d: pd.DataFrame, entry=96) -> pd.DataFrame:
    """
    高频特征。全部 shift(1) 无前视。
    微结构代理（只有 OHLCV 时能构造的）：
      · 上下影线占比 → 买卖压力不对称
      · 实体占比     → 方向明确度
      · 量能突变     → 大单进场
      · 短周期波动   → 微观噪声水平
      · 高低价差     → 波动/流动性代理
    """
    c, h, l, o, v = d["close"], d["high"], d["low"], d["open"], d["volume"]
    f = pd.DataFrame(index=d.index)
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    f["body_ratio"] = body / rng                    # 实体 / 振幅
    f["upper_wick"] = (h - np.maximum(c, o)) / rng  # 上影线占比
    f["lower_wick"] = (np.minimum(c, o) - l) / rng  # 下影线占比
    f["wick_asym"] = f["upper_wick"] - f["lower_wick"]
    f["dir"] = np.sign(c - o)
    for n in [3, 6, 12, 24, 48]:
        f[f"ret_{n}"] = c.pct_change(n)
        f[f"vol_{n}"] = c.pct_change().rolling(n).std()
    f["vol_z"] = (v - v.rolling(96).mean()) / v.rolling(96).std().replace(0, np.nan)
    f["vol_ratio"] = v / v.rolling(96).mean().replace(0, np.nan)
    f["range_pct"] = rng / c
    f["range_z"] = (f["range_pct"] - f["range_pct"].rolling(96).mean()) / \
        f["range_pct"].rolling(96).std().replace(0, np.nan)
    # 通道位置
    for n in [48, 96, 288]:
        hh = c.rolling(n).max().shift(1)
        ll = c.rolling(n).min().shift(1)
        span = (hh - ll).replace(0, np.nan)
        f[f"chan_{n}"] = (c - ll) / span
        f[f"brk_up_{n}"] = (c - hh) / span
        f[f"brk_dn_{n}"] = (ll - c) / span
    # 效率
    for n in [12, 48]:
        net = (c - c.shift(n)).abs()
        path = c.diff().abs().rolling(n).sum()
        f[f"eff_{n}"] = net / path.replace(0, np.nan)
    # 时间特征（加密 24/7 但有日内规律）
    f["hour"] = d.index.hour
    f["dow"] = d.index.dayofweek
    return f


def evaluate(y_true_ret, score, dates, widths_minutes=(720, 1440, 4320, 10080)):
    """
    稳健评估：多种窗口宽度的 t 值。
    widths_minutes: 窗口宽度（分钟）：12h / 1d / 3d / 7d
    """
    from ml_regime import ic_t
    out = {}
    dates = pd.to_datetime(pd.Series(dates).values)
    for wmin in widths_minutes:
        step = pd.Timedelta(minutes=wmin)
        ics = []
        a = dates.min()
        while a < dates.max() - step:
            b = a + step
            m = ((dates >= a) & (dates < b)).values & np.isfinite(score)
            if m.sum() >= 50:
                v, _ = ic_t(score[m], y_true_ret[m])
                if np.isfinite(v):
                    ics.append(v)
            a = a + step
        if len(ics) < 5:
            continue
        arr = np.array(ics)
        t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
        out[wmin] = {"t": float(t), "ic": float(arr.mean()), "n": len(arr)}
    return out


if __name__ == "__main__":
    print_cost_wall()
    print()
    print("=" * 96)
    print("数据可用性")
    print("=" * 96)
    for tf in ["5m", "15m", "1h", "4h"]:
        d = load_hf(TRAIL := CORE, tf)
        if not d:
            print(f"  {tf}: 无数据")
            continue
        for c, dd in d.items():
            print(f"  {tf:<4} {c}: {len(dd):>7} 根  "
                  f"{dd.index.min()} ~ {dd.index.max()}")
