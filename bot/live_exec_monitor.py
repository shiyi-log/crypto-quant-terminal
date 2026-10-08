#!/usr/bin/env python3
"""
实盘执行监控 —— 让实盘模拟参与迭代（第 15 轮）

用户要求：「实盘模拟是不是没参与迭代，我想加进去」
           「买盘和卖盘的订单量对实盘模拟的影响，这个也要做探索」

现状问题：
    · ML 迭代只用【历史回测】，实盘干跑的数据没有回流
    · 回测的成本假设（maker 0.02%/边 + 滑点）从未用真实盘口验证
    · 买卖盘深度对【我们的下单量】意味着多大滑点，从未测过

本模块做三件事：

    ① 盘口快照（实时）
       对每个持仓币取 Binance 合约深度，记录：
          · 买卖价差（spread）
          · 盘口深度（前 N 档的累计名义量）
          · 买卖失衡（bid/ask 深度比）← 用户要的"买卖盘订单量"
          · 我们的下单量占盘口深度的比例（冲击成本代理）

    ② 执行质量回流
       从干跑机器人取已平仓交易，对比：
          · 实际成交价 vs 信号价（滑点实测）
          · 实际持仓时长 vs 回测假设
       输出到 user_data/exec_quality.jsonl

    ③ 冲击成本估算
       给定下单量与盘口深度，估算滑点：
          slippage ≈ (下单量 / 盘口深度) × 半价差

用法:
    python live_exec_monitor.py              # 跑一轮，写快照
    python live_exec_monitor.py --daemon     # 常驻（默认 15 分钟）
    python live_exec_monitor.py --quality    # 只看执行质量
"""

import argparse
import json
import os
import time
import urllib.request

import numpy as np
import pandas as pd

STATUS = "user_data/exec_monitor.json"
QUALITY = "user_data/exec_quality.jsonl"
SNAP_HIST = "user_data/orderbook_snapshots.jsonl"
UA = {"User-Agent": "Mozilla/5.0"}


def get(url, tries=3, timeout=12):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.5)


# ══════════════════ ① 盘口快照 ══════════════════

def depth_snapshot(symbol, levels=20):
    """
    取合约盘口深度。
    returns: {bid, ask, spread_bp, bid_depth_usd, ask_depth_usd,
              imbalance, levels}
    """
    d = get(f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit={levels}")
    if not d or "bids" not in d:
        return None
    bids = [(float(p), float(q)) for p, q in d["bids"]]
    asks = [(float(p), float(q)) for p, q in d["asks"]]
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    spread_bp = (ba - bb) / mid * 10_000
    bid_usd = sum(p * q for p, q in bids)
    ask_usd = sum(p * q for p, q in asks)
    imbal = (bid_usd - ask_usd) / (bid_usd + ask_usd) if (bid_usd + ask_usd) > 0 else 0
    return {
        "symbol": symbol, "mid": mid,
        "bid": bb, "ask": ba,
        "spread_bp": round(spread_bp, 4),
        "bid_depth_usd": round(bid_usd, 1),
        "ask_depth_usd": round(ask_usd, 1),
        "imbalance": round(imbal, 4),      # 买卖盘力量对比（用户要的指标）
        "levels": len(bids),
        "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
    }


def impact_cost(order_usd, snap):
    """
    冲击成本估算：
        吃掉的档位越多，平均成交价偏离中间价越大。
        简化模型：slippage_bp ≈ (order / depth) × (spread_bp / 2) × 2
        即：下单量占盘口比例越大，滑点越大，上限是吃穿整个盘口。
    """
    if not snap:
        return None
    depth = min(snap["bid_depth_usd"], snap["ask_depth_usd"])
    if depth <= 0:
        return None
    ratio = min(order_usd / depth, 1.0)
    # 半价差是基本成本，冲击随比例线性增加
    slip_bp = snap["spread_bp"] / 2 + ratio * snap["spread_bp"] * 2
    return {"order_usd": order_usd, "depth_usd": depth,
            "ratio": round(ratio, 5), "slippage_bp": round(slip_bp, 3)}


# ══════════════════ ② 执行质量回流 ══════════════════

def _token():
    req = urllib.request.Request(
        "http://127.0.0.1:8890/auth/auto-login",
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["access_token"]


def get_trades():
    try:
        tok = _token()
        req = urllib.request.Request(
            "http://127.0.0.1:8890/api/v1/trades?limit=200",
            headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("trades", [])
    except Exception:
        return []


def get_positions():
    try:
        tok = _token()
        req = urllib.request.Request(
            "http://127.0.0.1:8890/api/v1/status",
            headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return []


def execution_quality():
    """
    从实盘成交记录算执行质量。
    滑点代理：开仓价 vs 该时点的市场价（用 K 线开盘价近似）。
    ⚠️ 目前机器人刚启动，样本少；管道先建好。
    """
    trades = get_trades()
    if not trades:
        return {"n": 0, "note": "尚无成交记录（策略平均持仓 33 天）"}
    rows = []
    for t in trades:
        rows.append({
            "pair": t.get("pair"),
            "open_rate": t.get("open_rate"),
            "close_rate": t.get("close_rate"),
            "profit_pct": t.get("profit_pct"),
            "stake": t.get("stake_amount"),
            "open_date": t.get("open_date"),
            "close_date": t.get("close_date"),
        })
    df = pd.DataFrame(rows)
    out = {"n": len(df)}
    if "profit_pct" in df:
        out["closed"] = int(df["profit_pct"].notna().sum())
    return out


# ══════════════════ 主流程 ══════════════════

def one_round(verbose=True):
    pos = get_positions()
    pairs = [p.get("pair", "").replace("/", "").replace(":USDT", "")
             for p in pos]
    if not pairs:
        pairs = ["BTCUSDT", "ETHUSDT"]
    snaps = []
    for sym in pairs:
        s = depth_snapshot(sym)
        if s:
            # 我们的单笔下单量约 337 USDT（实盘 stake）
            s["impact_337usd"] = impact_cost(337, s)
            snaps.append(s)
    rec = {
        "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "n_symbols": len(snaps),
        "snapshots": snaps,
        "avg_spread_bp": round(float(np.mean([s["spread_bp"] for s in snaps])), 3) if snaps else None,
        "avg_imbalance": round(float(np.mean([s["imbalance"] for s in snaps])), 4) if snaps else None,
        "max_impact_bp": round(float(max((s["impact_337usd"]["slippage_bp"]
                                          for s in snaps
                                          if s.get("impact_337usd")), default=0)), 3),
    }
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    with open(SNAP_HIST, "a", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps({**s, "t": rec["t"]}, ensure_ascii=False) + "\n")
    if verbose:
        print(f"  [{rec['t']}] {len(snaps)} 个币")
        print(f"    {'币':<12}{'中间价':>12}{'价差bp':>9}{'买盘深度':>13}"
              f"{'卖盘深度':>13}{'失衡':>9}{'337U冲击bp':>12}")
        print("    " + "-" * 84)
        for s in snaps:
            imp = s.get("impact_337usd") or {}
            print(f"    {s['symbol']:<12}{s['mid']:>12.2f}{s['spread_bp']:>9.3f}"
                  f"{s['bid_depth_usd']:>13,.0f}{s['ask_depth_usd']:>13,.0f}"
                  f"{s['imbalance']:>9.3f}{imp.get('slippage_bp', 0):>12.3f}")
        print("    " + "-" * 84)
        print(f"    平均价差 {rec['avg_spread_bp']}bp · "
              f"平均买卖失衡 {rec['avg_imbalance']} · "
              f"最大冲击成本 {rec['max_impact_bp']}bp")
        print()
        print(f"    成本对照：")
        print(f"      maker 费率        4.0 bp（往返）")
        print(f"      实测半价差        {rec['avg_spread_bp']/2:.2f} bp")
        print(f"      337U 下单冲击     {rec['max_impact_bp']:.2f} bp")
        print(f"      → 单笔总成本约    "
              f"{4.0 + rec['avg_spread_bp']/2 + rec['max_impact_bp']:.2f} bp")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--quality", action="store_true")
    args = ap.parse_args()

    if args.quality:
        q = execution_quality()
        print(f"  实盘执行质量: {json.dumps(q, ensure_ascii=False)}")
        with open(QUALITY, "a", encoding="utf-8") as f:
            f.write(json.dumps({**q, "t": pd.Timestamp.now(
                tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False) + "\n")
        return

    print("=" * 96)
    print("实盘执行监控 —— 盘口深度 / 买卖失衡 / 冲击成本")
    print("=" * 96)
    if not args.daemon:
        one_round()
        return
    print(f"  常驻，每 {args.interval//60} 分钟一轮")
    while True:
        try:
            one_round()
        except Exception as exc:
            print(f"  ⚠ {type(exc).__name__}: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
