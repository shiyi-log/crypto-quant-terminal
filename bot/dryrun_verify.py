#!/usr/bin/env python3
"""
干跑逐笔校验 —— 重算 Donchian 信号，对照 bot 实际下的单。

════════════════════════════════════════════════════════════════════
为什么这样做
════════════════════════════════════════════════════════════════════
用户要求「先模拟验证模型，不做历史回测」——
即在【实盘模拟（dry_run）】里验证，而不是拿历史回测说服自己。

所以本脚本做的是：把 `TrendFollowing.py` 的信号规则【独立重算一遍】，
然后逐笔对照 bot 真实下的单，回答三个问题：

    Q1 方向对吗？   —— 开仓时该币的 trend_state 符号与持仓方向是否一致
    Q2 信号存在吗？ —— 开仓时该币是否真处于趋势状态（不是无趋势误开）
    Q3 选币对吗？   —— 该币在当时的 break_strength 排名里是否进了 top_n

════════════════════════════════════════════════════════════════════
规则复刻（与 TrendFollowing.py 对齐）
════════════════════════════════════════════════════════════════════
    hh_entry = close.rolling(20).max().shift(1)
    ll_entry = close.rolling(20).min().shift(1)
    hh_exit  = close.rolling(20).max().shift(1)
    ll_exit  = close.rolling(20).min().shift(1)

    state = 0
    if state == 0:  close > hh_entry → +1 ;  close < ll_entry → -1
    if state > 0:   close < ll_exit  →  0
    if state < 0:   close > hh_exit  →  0

    开仓：state > 0 做多 / state < 0 做空
    离场：state <= 0 平多 / state >= 0 平空
    选币：state != 0 的币按 break_strength 降序取前 top_n

    break_strength = max((close-hh_entry)/span, (ll_entry-close)/span)
    span = hh_entry - ll_entry

════════════════════════════════════════════════════════════════════
时序约定（关键）
════════════════════════════════════════════════════════════════════
Freqtrade 在【第 i 根 K 线收盘后】才拿到该根的信号，成交发生在之后。
所以对一笔 open_date 落在第 D 根的交易，其信号来自【第 D-1 根】。

本脚本按此约定取信号 K 线，并同时检查 D 与 D-1 两根以便诊断。

用法:
    python dryrun_verify.py                    # 用默认库
    python dryrun_verify.py --db <path>
    python dryrun_verify.py --json out.json
"""
import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_DB = os.path.join(_HERE, "tradesv3.dryrun.sqlite")

# 与 TrendFollowing.py 的默认参数一致
ENTER_PERIOD = 20
EXIT_PERIOD = 20
TOP_N = 8


def load_meta():
    p = os.path.join(_HERE, "user_data", "config_trend_live.json")
    with open(p, encoding="utf-8") as fh:
        c = json.load(fh)
    wl = (c.get("exchange") or {}).get("pair_whitelist") or []
    return c, wl


def signals(df, enter=ENTER_PERIOD, exit_=EXIT_PERIOD):
    """复刻 TrendFollowing.populate_indicators 的状态机。"""
    out = pd.DataFrame(index=df.index)
    c = df["close"]
    hh_e = c.rolling(enter).max().shift(1)
    ll_e = c.rolling(enter).min().shift(1)
    hh_x = c.rolling(exit_).max().shift(1)
    ll_x = c.rolling(exit_).min().shift(1)

    span = (hh_e - ll_e).replace(0, np.nan)
    up = (c - hh_e) / span
    dn = (ll_e - c) / span
    out["break_strength"] = pd.concat([up, dn], axis=1).max(axis=1)
    out["hh_entry"] = hh_e
    out["ll_entry"] = ll_e

    cv = c.values
    a, b = hh_e.values, ll_e.values
    x, y = hh_x.values, ll_x.values
    st = np.zeros(len(cv))
    cur = 0.0
    for i in range(len(cv)):
        if cur == 0.0:
            if np.isfinite(a[i]) and cv[i] > a[i]:
                cur = 1.0
            elif np.isfinite(b[i]) and cv[i] < b[i]:
                cur = -1.0
        else:
            if cur > 0 and np.isfinite(y[i]) and cv[i] < y[i]:
                cur = 0.0
            elif cur < 0 and np.isfinite(x[i]) and cv[i] > x[i]:
                cur = 0.0
        st[i] = cur
    out["trend_state"] = st
    return out


def coin_of(pair):
    return pair.split("/")[0]


def _signal_row(frame, date):
    if frame is None or frame.empty:
        return None
    dates = pd.to_datetime(frame.index, utc=True, errors="coerce").tz_localize(None)
    matches = np.flatnonzero(dates == date)
    return frame.iloc[matches[0]] if len(matches) == 1 else None


def candidate_pool(sig, coins, signal_date):
    """只有完整白名单在同一根信号 K 线上可得，才允许验证排名。"""
    strengths, missing = {}, {}
    for coin in dict.fromkeys(coins):
        row = _signal_row(sig.get(coin), signal_date)
        if row is None:
            missing[coin] = "missing_or_ambiguous_signal_candle"
            continue
        try:
            state = float(row["trend_state"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            missing[coin] = "missing_signal_fields"
            continue
        if not np.isfinite(state) or not np.isfinite(close) or close <= 0:
            missing[coin] = "invalid_signal_fields"
            continue
        if state != 0:
            try:
                score = float(row["break_strength"])
            except (KeyError, TypeError, ValueError):
                score = float("nan")
            if not np.isfinite(score):
                missing[coin] = "invalid_break_strength"
                continue
            strengths[coin] = score
    ready = bool(coins) and not missing
    order = sorted(strengths, key=lambda coin: (-strengths[coin], coin)) if ready else []
    return {"ready": ready, "missing": missing,
            "ranks": {coin: rank for rank, coin in enumerate(order, 1)},
            "candidate_count": len(order) if ready else None}


def verify_entry(trade, sig, coins):
    """逐笔返回 verified 或 incomplete；不把缺行情当成校验通过。"""
    coin = coin_of(trade["pair"])
    result = {"pair": trade["pair"], "coin": coin,
              "open_date": str(trade["open_date"]), "is_short": bool(trade["is_short"]),
              "signal_date": None, "state_prev": None, "state_day": None,
              "break_strength": None, "rank": None, "n_candidates": None,
              "rank_verified": False, "validation_status": "incomplete",
              "ok": None, "issues": [], "missing": {}}
    opened = pd.Timestamp(trade["open_date"])
    if pd.isna(opened):
        result["missing"] = {coin: "invalid_open_date"}
        return result
    opened = opened.tz_localize("UTC") if opened.tzinfo is None else opened.tz_convert("UTC")
    day = opened.normalize().tz_localize(None)
    signal_date = day - pd.Timedelta(days=1)
    result["signal_date"] = str(signal_date)
    pool = candidate_pool(sig, coins, signal_date)
    result["missing"] = pool["missing"]
    if coin not in coins:
        result["missing"][coin] = "outside_current_whitelist"
        return result
    row = _signal_row(sig.get(coin), signal_date)
    if row is None or not pool["ready"]:
        return result
    state = float(row["trend_state"])
    result["state_prev"] = state
    current = _signal_row(sig.get(coin), day)
    if current is not None and pd.notna(current.get("trend_state")):
        result["state_day"] = float(current["trend_state"])
    score = row.get("break_strength")
    if score is not None and pd.notna(score) and np.isfinite(float(score)):
        result["break_strength"] = float(score)
    result["rank"] = pool["ranks"].get(coin)
    result["n_candidates"] = pool["candidate_count"]
    result["rank_verified"] = True
    side = -1 if result["is_short"] else 1
    if state == 0:
        result["issues"].append("信号日无趋势")
    elif np.sign(state) != side:
        result["issues"].append("方向反了")
    if result["rank"] is not None and result["rank"] > TOP_N:
        result["issues"].append(f"排名{result['rank']}>top_n")
    result["validation_status"] = "verified"
    result["ok"] = not result["issues"]
    return result


def verification_summary(rows):
    verified = [row for row in rows if row["validation_status"] == "verified"]
    mismatches = [row for row in verified if row["ok"] is False]
    incomplete = [row for row in rows if row["validation_status"] != "verified"]
    verdict = ("incomplete" if not verified or incomplete else
               "mismatch" if mismatches else "verified_entries")
    return {"total": len(rows), "n": len(verified),
            "ok": sum(row["ok"] is True for row in verified),
            "mismatch_count": len(mismatches), "incomplete_count": len(incomplete),
            "verdict": verdict, "all_entries_verified": bool(rows) and verdict == "verified_entries"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", default=None)
    ap.add_argument("--full-history", action="store_true",
                    help="用全部历史重算状态机（否则从 2019 起，效果相同但更快）")
    args = ap.parse_args()

    print("=" * 100)
    print("  干跑逐笔校验 —— 重算 Donchian 信号，对照 bot 实际下的单")
    print("=" * 100)

    cfg, wl = load_meta()
    coins = [coin_of(p) for p in wl]
    print(f"  配置：{os.path.basename('user_data/config_trend_live.json')} · 白名单 {len(wl)} 对")
    print(f"  规则：close 通道 {ENTER_PERIOD}/{EXIT_PERIOD} · top_n={TOP_N} · 1d")

    # ── 读交易 ──
    if not os.path.exists(args.db):
        print(f"  ❌ 找不到交易库 {args.db}")
        sys.exit(1)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    tr = pd.read_sql_query("select * from trades order by open_date", con)
    con.close()
    if tr.empty:
        print("  incomplete：交易库是空的，没有交易可验证；不能声称全部吻合")
        return 2
    for col in ("open_date", "close_date"):
        if col in tr:
            tr[col] = pd.to_datetime(tr[col], errors="coerce", utc=True).dt.tz_localize(None)
    tr["coin"] = tr["pair"].map(coin_of)
    print(f"\n  读到 {len(tr)} 笔交易"
          f"（持仓 {int(tr['is_open'].sum())} · 已平仓 {int((~tr['is_open'].astype(bool)).sum())}）")
    print(f"  时间 {tr['open_date'].min()} → {tr['open_date'].max()}")

    # ── 行情 ──
    import ml_lab as ml
    data = ml.load_ohlcv()
    missing = [c for c in coins if c not in data]
    if missing:
        print(f"  ⚠ 行情缺少 {len(missing)} 个币: {missing[:6]}")

    # ── 逐币重算 ──
    sig = {}
    for c in coins:
        d = data.get(c)
        if d is None or len(d) < ENTER_PERIOD + 5 or "close" not in d:
            continue
        s = signals(d)
        s["close"] = d["close"]
        sig[c] = s
    print(f"  已重算 {len(sig)} 个币的信号")

    # ── 逐笔校验 ──
    # 建一个「每日横截面强度」表，用于判断 top_n
    print(f"\n  {'交易对':<12}{'开仓日':<12}{'方向':<6}"
          f"{'状态(D-1)':>10}{'状态(D)':>9}{'强度':>9}{'排名':>7}{'判定':<22}")
    print("  " + "-" * 92)
    rows = []
    for _, trade in tr.iterrows():
        row = verify_entry(trade, sig, coins)
        rows.append(row)
        previous = "—" if row["state_prev"] is None else f"{row['state_prev']:.0f}"
        current = "—" if row["state_day"] is None else f"{row['state_day']:.0f}"
        score = "—" if row["break_strength"] is None else f"{row['break_strength']:.3f}"
        rank = (f"{row['rank']}/{row['n_candidates']}" if row["rank"] is not None else "—")
        if row["validation_status"] == "incomplete":
            verdict = f"⚠ incomplete：完整池排名未验证 {row['missing']}"
        else:
            verdict = "✅ 一致" if row["ok"] else "❌ " + " / ".join(row["issues"])
        print(f"  {row['pair']:<12}{row['open_date'][:10]:<12}"
              f"{'空' if row['is_short'] else '多':<6}"
              f"{previous:>10}{current:>9}{score:>9}{rank:>7}  {verdict}")

    summary = verification_summary(rows)
    print("\n" + "=" * 100)
    print("  汇总")
    print("=" * 100)
    print(f"  总交易 {summary['total']} · 可完整验证 {summary['n']} 笔 · "
          f"一致 {summary['ok']} · 不一致 {summary['mismatch_count']} · "
          f"未完成 {summary['incomplete_count']}")
    if summary["all_entries_verified"]:
        print("  ✅ 全部已记录开仓的信号、方向与完整池排名一致；不代表收益或运行可用性已验证")
    elif summary["verdict"] == "incomplete":
        print("  ⚠ incomplete：数据覆盖不完整或无可验证交易，不能声称全部吻合")
    else:
        print("  ❌ 有已验证交易不符合信号、方向或 top_n 规则")
    for row in rows:
        if row["validation_status"] == "incomplete":
            print(f"    {row['pair']:<12} {row['open_date'][:10]} 未验证: {row['missing']}")
        elif row["ok"] is False:
            print(f"    {row['pair']:<12} {row['open_date'][:10]} {' / '.join(row['issues'])}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"db": args.db, **summary, "rows": rows},
                      fh, ensure_ascii=False, indent=2)
        print(f"\n  明细已存 {args.json}")
    return 2 if summary["verdict"] == "incomplete" else 1 if summary["mismatch_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
