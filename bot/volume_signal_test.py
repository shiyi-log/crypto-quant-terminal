#!/usr/bin/env python3
"""
「根据下单量预测」这一说法是否成立 —— 用真实数据检验

检验三类「下单量」信号：
  1. 主动买卖失衡 (order flow imbalance)：taker 主动买入量占比
  2. 成交量水平 (volume level)：成交量本身
  3. 成交笔数 (trade count)：大单/小单结构的代理

分别检验它们能否预测：
  A. 收益的【方向】（这才是能赚钱的东西）
  B. 收益的【绝对值】（波动率 —— 容易预测，但不赚钱）

并与价格动量基线对比，最后换算成「扣掉手续费后还剩多少」。

数据源：币安合约 fapi/v1/klines（含 takerBuyBase / trades 字段）

用法: python volume_signal_test.py [--symbols BTCUSDT ETHUSDT] [--start 2025-01-01]
"""

import argparse
import json
import time
import urllib.request

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

BASE = "https://fapi.binance.com/fapi/v1/klines"
FIELDS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "_",
]


def fetch_klines(symbol: str, interval: str, start_ms: int) -> pd.DataFrame:
    rows, cursor = [], start_ms
    while True:
        url = f"{BASE}?symbol={symbol}&interval={interval}&startTime={cursor}&limit=1500"
        req = urllib.request.Request(url, headers={"User-Agent": "quant-research"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read().decode())
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][6] + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1500:
            break
        time.sleep(0.25)  # 限频
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=FIELDS)
    df = df[FIELDS[:11]].copy()
    for c in ("open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_base", "taker_buy_quote"):
        df[c] = df[c].astype(float)
    df["trades"] = df["trades"].astype(int)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.sort_values("date").reset_index(drop=True)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    eps = 1e-12
    # ① 主动买卖失衡：净主动买入占成交量比例（经典订单流失衡）
    d["ofi"] = (2 * d["taker_buy_base"] - d["volume"]) / (d["volume"] + eps)
    # ② 主动买入占比
    d["taker_ratio"] = d["taker_buy_base"] / (d["volume"] + eps)
    # ③ 成交量水平（滚动 z-score，消除量纲）
    d["vol_z"] = (d["volume"] - d["volume"].rolling(168).mean()) / (
        d["volume"].rolling(168).std() + eps
    )
    # ④ 成交笔数（大单/小单结构代理）
    d["log_trades"] = np.log1p(d["trades"])
    # ⑤ 单笔平均成交额（大单代理）
    d["avg_trade_size"] = d["quote_volume"] / (d["trades"] + eps)
    d["avg_trade_z"] = (
        d["avg_trade_size"] - d["avg_trade_size"].rolling(168).mean()
    ) / (d["avg_trade_size"].rolling(168).std() + eps)
    # ⑥ 价格动量（基线）
    d["mom24"] = d["close"] / d["close"].shift(24) - 1
    return d


HORIZONS = [1, 4, 24, 72, 168]  # 小时
FEATURES = {
    "ofi": "主动买卖失衡",
    "taker_ratio": "主动买入占比",
    "vol_z": "成交量水平(z)",
    "log_trades": "成交笔数(log)",
    "avg_trade_z": "单笔均额(z)",
    "mom24": "价格动量24h(基线)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--interval", default="1h")
    args = ap.parse_args()

    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)

    all_rows = {}
    for sym in args.symbols:
        print(f"  拉取 {sym} {args.interval} K线…", end="", flush=True)
        raw = fetch_klines(sym, args.interval, start_ms)
        print(f" {len(raw)} 根")
        if raw.empty:
            continue
        all_rows[sym] = build_features(raw)

    if not all_rows:
        print("无数据")
        return

    print()
    print("=" * 96)
    print("检验 A：能否预测收益的【方向】（能做多空的信号）")
    print("  指标 = Spearman 秩相关（IC），正值代表预测方向正确")
    print("=" * 96)
    hdr = f"{'信号':<20}" + "".join(f"{h}h".rjust(10) for h in HORIZONS)
    print(hdr)
    print("-" * 96)
    direction = {}
    for key, name in FEATURES.items():
        cells = []
        for h in HORIZONS:
            ics = []
            for sym, d in all_rows.items():
                fwd = d["close"].shift(-h) / d["close"] - 1
                m = pd.concat([d[key], fwd], axis=1).dropna()
                if len(m) > 100 and m.iloc[:, 0].std() > 0:
                    ics.append(spearmanr(m.iloc[:, 0], m.iloc[:, 1]).statistic)
            cells.append(np.mean(ics) if ics else np.nan)
        direction[key] = cells
        print(f"{name:<20}" + "".join(
            (f"{c:+.4f}".rjust(10) if not np.isnan(c) else "—".rjust(10)) for c in cells
        ))

    print()
    print("=" * 96)
    print("检验 B：能否预测收益的【绝对值】（波动率 —— 好预测但不赚钱）")
    print("=" * 96)
    print(hdr)
    print("-" * 96)
    for key, name in FEATURES.items():
        cells = []
        for h in HORIZONS:
            ics = []
            for sym, d in all_rows.items():
                fwd = (d["close"].shift(-h) / d["close"] - 1).abs()
                m = pd.concat([d[key], fwd], axis=1).dropna()
                if len(m) > 100 and m.iloc[:, 0].std() > 0:
                    ics.append(spearmanr(m.iloc[:, 0], m.iloc[:, 1]).statistic)
            cells.append(np.mean(ics) if ics else np.nan)
        print(f"{name:<20}" + "".join(
            (f"{c:+.4f}".rjust(10) if not np.isnan(c) else "—".rjust(10)) for c in cells
        ))

    # ── 经济性：把 IC 换算成钱 ──
    print()
    print("=" * 96)
    print("检验 C：换算成钱 —— 按信号排序取多空两端，扣手续费后还剩多少？")
    print("=" * 96)
    COST = 0.0010  # 双边 taker 0.05% × 2
    for key in ["ofi", "vol_z", "mom24"]:
        name = FEATURES[key]
        print(f"\n  【{name}】")
        for h in [4, 24, 168]:
            spreads, ns = [], []
            for sym, d in all_rows.items():
                fwd = d["close"].shift(-h) / d["close"] - 1
                m = pd.concat([d[key], fwd], axis=1).dropna()
                m.columns = ["f", "y"]
                if len(m) < 300:
                    continue
                q = pd.qcut(m["f"], 5, labels=False, duplicates="drop")
                hi = m.loc[q == q.max(), "y"].mean()
                lo = m.loc[q == q.min(), "y"].mean()
                spreads.append(hi - lo)
                ns.append(len(m))
            if not spreads:
                continue
            sp = float(np.mean(spreads))
            gross_long = sp / 2  # 单边毛收益近似
            print(f"    {h:>3}h 调仓: 多空毛价差 {sp*100:+.3f}%  "
                  f"单边毛 {gross_long*100:+.3f}%  "
                  f"扣费后 {(gross_long-COST)*100:+.3f}%  "
                  f"{'✅ 覆盖成本' if gross_long > COST else '❌ 覆盖不了'}")

    print()
    print("  成本基准: 双边 taker = 0.10%")


if __name__ == "__main__":
    main()
