#!/usr/bin/env python3
"""
订单流数据下载 —— 买卖盘力量对比（第 15 轮）

用户要求：「买盘和卖盘的订单量对实盘模拟的影响，这个也要做探索」

现状：已下载的 feather 只有 6 列（date/ohlcv），**订单流字段被丢掉了**。
     Binance 原始 K 线有 12 个字段，其中关键的三个：

        [8]  num_trades       成交笔数        → 参与度
        [9]  taker_buy_base   主动买入量      → **买盘力量**
        [10] taker_buy_quote  主动买入额      → 大单代理

     派生：
        sell_volume  = volume - taker_buy_base    卖盘力量
        buy_ratio    = taker_buy_base / volume    买卖失衡（0~1，0.5 为均衡）
        flow_imbal   = 2*buy_ratio - 1            中心化到 [-1, 1]
        avg_trade_sz = quote_volume / num_trades  平均单笔规模（大单代理）

    这些是**真正可用的历史订单流**（逐笔级 aggTrades 数据量太大，
    但 K 线级的 taker_buy 已经能刻画买卖压力）。

另外补充两个免费的衍生品指标（对高频/短线有用）：
    · 资金费率历史（已有）
    · 持仓量 OI（Binance /futures/data/openInterestHist）

输出：user_data/data/orderflow/{COIN}_{tf}_orderflow.feather
     date, open, high, low, close, volume,
     num_trades, quote_volume, taker_buy_base, taker_buy_quote,
     sell_volume, buy_ratio, flow_imbal, avg_trade_sz

用法:
    python fetch_orderflow.py --tf 5m --days 1000
    python fetch_orderflow.py --tf 15m --days 1000
    python fetch_orderflow.py --tf 5m --oi          # 同时取持仓量
"""

import argparse
import json
import os
import time
import urllib.request

import numpy as np
import pandas as pd

OUT = "user_data/data/orderflow"
UA = {"User-Agent": "Mozilla/5.0"}
CORE = ["BTC", "ETH"]


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.8 + i * 0.5)


def fetch_klines_flow(coin, tf="5m", days=1000, max_req=200):
    """分页下载带订单流字段的 K 线"""
    sym = f"{coin}USDT"
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    rows, cur = [], start
    for _ in range(max_req):
        u = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
             f"&interval={tf}&startTime={cur}&limit=1500")
        try:
            d = get(u)
        except Exception:
            break
        if not d:
            break
        rows += d
        cur = d[-1][6] + 1
        time.sleep(0.25)
        if len(d) < 1500:
            break
    if len(rows) < 200:
        return None
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ig"])
    df["date"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms",
                                utc=True).dt.tz_localize(None)
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_base", "taker_buy_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["num_trades"] = pd.to_numeric(df["num_trades"], errors="coerce")
    df = df.drop_duplicates("date").sort_values("date")

    # ── 派生订单流指标 ──
    df["sell_volume"] = df["volume"] - df["taker_buy_base"]
    df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
    df["flow_imbal"] = 2 * df["buy_ratio"] - 1          # [-1, 1]
    df["avg_trade_sz"] = df["quote_volume"] / df["num_trades"].replace(0, np.nan)
    # 主动买卖的金额失衡
    df["flow_imbal_q"] = (2 * df["taker_buy_quote"]
                          / df["quote_volume"].replace(0, np.nan) - 1)

    keep = ["date", "open", "high", "low", "close", "volume", "num_trades",
            "quote_volume", "taker_buy_base", "taker_buy_quote",
            "sell_volume", "buy_ratio", "flow_imbal", "flow_imbal_q",
            "avg_trade_sz"]
    return df[keep].reset_index(drop=True)


def fetch_oi(coin, tf="5m", days=30):
    """持仓量历史（Binance 只保留最近 30 天，5m 粒度）"""
    sym = f"{coin}USDT"
    period = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}.get(tf, "5m")
    u = (f"https://fapi.binance.com/futures/data/openInterestHist?"
         f"symbol={sym}&period={period}&limit=500")
    try:
        d = get(u)
    except Exception:
        return None
    if not d:
        return None
    df = pd.DataFrame(d)
    df["date"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms",
                                utc=True).dt.tz_localize(None)
    df["oi"] = pd.to_numeric(df["sumOpenInterest"], errors="coerce")
    df["oi_val"] = pd.to_numeric(df["sumOpenInterestValue"], errors="coerce")
    return df[["date", "oi", "oi_val"]].sort_values("date").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--days", type=int, default=1000)
    ap.add_argument("--coins", default=",".join(CORE))
    ap.add_argument("--all-daily", action="store_true",
                    help="对 51 币日线全量下载订单流（供 ML 特征用）")
    ap.add_argument("--oi", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    if args.all_daily:
        import glob
        fs = glob.glob("user_data/data/binance/futures/*-1d-futures.feather")
        coins = sorted({os.path.basename(f).split("_")[0] for f in fs})
        args.tf = "1d"
        print(f"  全量日线模式: {len(coins)} 个币")
    else:
        coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    print(f"  目标: {coins} · {args.tf} · 回溯 {args.days} 天\n")

    for c in coins:
        f = f"{OUT}/{c}_{args.tf}_orderflow.feather"
        if os.path.exists(f):
            old = pd.read_feather(f)
            print(f"  {c}: 已存在 {len(old)} 行，跳过（删掉可重下）")
            continue
        t0 = time.time()
        try:
            d = fetch_klines_flow(c, args.tf, args.days)
        except Exception as exc:
            print(f"  {c}: 失败 {type(exc).__name__}: {exc}")
            continue
        if d is None:
            print(f"  {c}: 数据不足")
            continue
        d.to_feather(f)
        print(f"  ✅ {c}: {len(d):>7} 行  {d['date'].min()} ~ {d['date'].max()}  "
              f"({time.time()-t0:.0f}s)")
        print(f"      字段: {list(d.columns)}")
        print(f"      buy_ratio 均值 {d['buy_ratio'].mean():.4f} · "
              f"标准差 {d['buy_ratio'].std():.4f}")
        if args.oi:
            oi = fetch_oi(c, args.tf)
            if oi is not None:
                oi.to_feather(f"{OUT}/{c}_{args.tf}_oi.feather")
                print(f"      持仓量 {len(oi)} 行  "
                      f"{oi['date'].min()} ~ {oi['date'].max()}")

    print(f"\n  {OUT} 现有 {len(os.listdir(OUT))} 个文件")


if __name__ == "__main__":
    main()
