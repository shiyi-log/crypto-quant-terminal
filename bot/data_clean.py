#!/usr/bin/env python3
"""
数据清洗与质检（数据层）

为什么这一层必须独立：
    回测里最容易骗自己的不是因子，是脏数据。
    时间戳错位、重复行、跳空缺口、极端插针、幸存者偏差 —— 
    任何一项都能造出一个「看起来很赚」的策略。

质检项目：
    1. 结构    必要列、数据类型、索引唯一性
    2. 时间    单调性、重复时间戳、缺口（缺失 K 线占比）
    3. 数值    NaN、0/负价、极端跳变（可疑插针）、成交量异常
    4. 一致性 OHLC 逻辑（high≥max(o,c)、low≤min(o,c)）
    5. 对齐    多币种时间轴对齐，输出可用面板
    6. 幸存者偏差  记录每个币的上市/退市时间，避免用「现在还在的币」回测过去

用法:
    python data_clean.py --check                 # 全量质检报告
    python data_clean.py --check --tf 1d
    python data_clean.py --panel --tf 1d         # 输出对齐后的面板
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
SPOT = "user_data/data/binance"
OUT = "user_data/data/_clean"

# 各周期的标准间隔
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


# ══════════════════════ 单币质检 ══════════════════════

def check_symbol(df: pd.DataFrame, tf: str, sym: str) -> dict:
    """返回该币的质检结果"""
    issues = []
    n = len(df)

    # 1. 结构
    need = {"date", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        return {"sym": sym, "rows": n, "fatal": f"缺列 {missing}", "issues": []}

    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
    d = d.sort_values("date")

    # 2. 时间
    dup = int(d["date"].duplicated().sum())
    if dup:
        issues.append(f"重复时间戳 {dup} 行")
        d = d.drop_duplicates("date", keep="last")

    step = pd.Timedelta(minutes=TF_MINUTES.get(tf, 1440))
    span = d["date"].iloc[-1] - d["date"].iloc[0]
    expected = int(span / step) + 1
    gaps = expected - len(d)
    gap_pct = gaps / expected * 100 if expected > 0 else 0
    if gap_pct > 1:
        issues.append(f"K线缺口 {gaps} 根（{gap_pct:.1f}%）")

    # 3. 数值
    for c in ["open", "high", "low", "close", "volume"]:
        nn = int(d[c].isna().sum())
        if nn:
            issues.append(f"{c} 有 {nn} 个 NaN")
    bad_px = int((d[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    if bad_px:
        issues.append(f"非正价格 {bad_px} 行")
    if (d["volume"] < 0).any():
        issues.append("负成交量")

    # 4. OHLC 逻辑一致性
    hi_bad = int((d["high"] < d[["open", "close"]].max(axis=1) - 1e-9).sum())
    lo_bad = int((d["low"] > d[["open", "close"]].min(axis=1) + 1e-9).sum())
    if hi_bad:
        issues.append(f"high 小于 open/close {hi_bad} 行")
    if lo_bad:
        issues.append(f"low 大于 open/close {lo_bad} 行")

    # 5. 极端跳变（可疑插针）：单根振幅 > 10 倍历史中位数且 > 50%
    ret = d["close"].pct_change()
    absret = ret.abs()
    med = absret.median()
    spikes = int(((absret > 0.5) & (absret > med * 20)).sum()) if med > 0 else 0
    if spikes:
        issues.append(f"极端跳变 {spikes} 根（疑插针/数据错误）")

    # 6. 僵尸期（连续多根完全相同收盘价 —— 停牌或数据卡住）
    same = (d["close"].diff() == 0)
    max_same = 0
    cur = 0
    for v in same:
        cur = cur + 1 if v else 0
        max_same = max(max_same, cur)
    if max_same >= 24 and tf in ("1h", "4h"):
        issues.append(f"最长 {max_same} 根收盘价不变（疑停牌/卡数据）")

    return {
        "sym": sym, "rows": len(d), "fatal": None,
        "start": str(d["date"].iloc[0].date()), "end": str(d["date"].iloc[-1].date()),
        "span_days": (d["date"].iloc[-1] - d["date"].iloc[0]).days,
        "gaps": gaps, "gap_pct": round(gap_pct, 2),
        "spikes": spikes, "issues": issues,
        "clean": len(issues) == 0,
    }


# ══════════════════════ 全量扫描 ══════════════════════

def scan(tf="1d", market="futures"):
    base = PERP if market == "futures" else SPOT
    pat = (f"{base}/*-{tf}-futures.feather" if market == "futures"
           else f"{base}/*-{tf}.feather")
    files = sorted(glob.glob(pat))
    rows = []
    for f in files:
        sym = os.path.basename(f).split("_")[0]
        try:
            d = pd.read_feather(f)
            rows.append(check_symbol(d, tf, sym))
        except Exception as exc:
            rows.append({"sym": sym, "rows": 0, "fatal": str(exc), "issues": []})
    return pd.DataFrame(rows)


# ══════════════════════ 对齐面板 ══════════════════════

def build_panel(tf="1d", market="futures", min_history=200):
    """
    输出对齐后的面板，并记录上市时间（用于避免幸存者偏差）
    返回 (面板dict, 元信息)
    """
    base = PERP if market == "futures" else SPOT
    pat = (f"{base}/*-{tf}-futures.feather" if market == "futures"
           else f"{base}/*-{tf}.feather")
    cols = {}
    meta = {}
    for f in sorted(glob.glob(pat)):
        sym = os.path.basename(f).split("_")[0]
        try:
            d = pd.read_feather(f)
        except Exception:
            continue
        r = check_symbol(d, tf, sym)
        if r.get("fatal"):
            continue
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
        if len(d) < min_history:
            continue
        for c in ["open", "high", "low", "close", "volume"]:
            cols.setdefault(c, {})[sym] = d[c].astype(float)
        meta[sym] = {"start": str(d.index[0].date()), "end": str(d.index[-1].date()),
                     "rows": len(d), "issues": r["issues"]}
    panel = {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}
    return panel, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--panel", action="store_true")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--market", default="futures", choices=["futures", "spot"])
    args = ap.parse_args()

    if args.check or not args.panel:
        print("=" * 108)
        print(f"数据质检报告 —— {args.market} / {args.tf}")
        print("=" * 108)
        df = scan(args.tf, args.market)
        if df.empty:
            print("  无数据文件"); return
        fatal = df[df["fatal"].notna()]
        if len(fatal):
            print(f"\n  ❌ 致命问题 {len(fatal)} 个:")
            for _, r in fatal.iterrows():
                print(f"     {r['sym']}: {r['fatal']}")

        ok = df[df["fatal"].isna()]
        clean = ok[ok["clean"]]
        dirty = ok[~ok["clean"]]
        print(f"\n  文件总数 {len(df)} · 正常 {len(clean)} · 有问题 {len(dirty)}")
        print()
        print(f"  {'币种':<12}{'行数':>7}{'起':>12}{'止':>12}{'天数':>7}{'缺口':>7}{'跳变':>6}  问题")
        print("  " + "-" * 96)
        for _, r in ok.sort_values("rows", ascending=False).iterrows():
            flag = "" if r["clean"] else "⚠"
            iss = "; ".join(r["issues"])[:46]
            print(f"  {r['sym']:<12}{r['rows']:>7}{r['start']:>12}{r['end']:>12}"
                  f"{r['span_days']:>7}{r['gaps']:>7}{r['spikes']:>6}  {flag} {iss}")
        print("  " + "-" * 96)

        # 汇总共性
        print("\n  共性问题统计:")
        allissues = []
        for _, r in dirty.iterrows():
            allissues.extend(r["issues"])
        import re
        kinds = {}
        for s in allissues:
            k = re.sub(r"\d+", "N", s)
            kinds[k] = kinds.get(k, 0) + 1
        for k, v in sorted(kinds.items(), key=lambda x: -x[1])[:8]:
            print(f"    {v:>3} 个币: {k}")

        # 幸存者偏差提示
        print("\n  幸存者偏差检查:")
        starts = ok.groupby("start").size()
        for y in sorted({s[:4] for s in ok["start"]}):
            n = sum(1 for s in ok["start"] if s[:4] == y)
            if n:
                print(f"    {y} 年首次可用: {n} 个币")
        print("    → 越早期币种越少，早期回测的横截面广度不足，需注意")

    if args.panel:
        print()
        print("=" * 108)
        print("构建对齐面板")
        print("=" * 108)
        panel, meta = build_panel(args.tf, args.market)
        os.makedirs(OUT, exist_ok=True)
        for k, v in panel.items():
            v.to_feather(f"{OUT}/panel_{args.market}_{args.tf}_{k}.feather")
        with open(f"{OUT}/panel_{args.market}_{args.tf}_meta.json", "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"  ✅ 输出 {len(panel)} 个字段 × {panel['close'].shape[1]} 币 × {panel['close'].shape[0]} 期")
        print(f"     目录 {OUT}/")
        print(f"     元信息 {len(meta)} 个币（含上市时间与问题记录）")


if __name__ == "__main__":
    main()
