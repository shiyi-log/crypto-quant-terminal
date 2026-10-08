#!/usr/bin/env python3
"""
合并 Binance + OKX 标的池（第 18 轮）

目的：扩池 → 增加 ML 的横截面宽度与独立交易数
     （实测：活跃币数 23→51 时信号从「不稳健」变「稳健」）

必须处理的三个格式差异：
    ① 时区：Binance 是 UTC-aware，OKX 是 naive
    ② 日期偏移：OKX 的日线落在 16:00（UTC+8 边界）
    ③ 类型：OKX 的 volume 是字符串，需转 float

输出：user_data/data/merged/{COIN}-1d-merged.feather
     （统一为 UTC-aware、日期归一到 00:00、全部 float）

用法:
    python merge_universe.py --build
    python merge_universe.py --stats
"""

import argparse
import glob
import os
import shutil

import numpy as np
import pandas as pd

BN = "user_data/data/binance/futures"
OX = "user_data/data/okx/futures"
OUT = "user_data/data/merged"
COLS = ["date", "open", "high", "low", "close", "volume"]


def norm(d: pd.DataFrame, src: str) -> pd.DataFrame:
    d = d.copy()
    # ② 日期归一到当日 00:00（OKX 落在 16:00）
    dt = pd.to_datetime(d["date"])
    if getattr(dt.dtype, "tz", None) is not None:
        dt = dt.dt.tz_convert("UTC").dt.tz_localize(None)      # ① 统一去时区
    d["date"] = dt.dt.normalize()
    # ③ 数值化
    for c in ["open", "high", "low", "close", "volume"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[COLS].dropna()
    d = d[d["close"] > 0]
    d = d.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return d


def build():
    os.makedirs(OUT, exist_ok=True)
    bn = {}
    for f in glob.glob(f"{BN}/*-1d-futures.feather"):
        c = os.path.basename(f).split("_")[0]
        bn[c] = f
    ox = {}
    for f in glob.glob(f"{OX}/*.feather"):
        c = os.path.basename(f).split("_")[0]
        ox[c] = f

    print(f"  Binance {len(bn)} 个 · OKX {len(ox)} 个")
    only_bn = sorted(set(bn) - set(ox))
    only_ox = sorted(set(ox) - set(bn))
    both = sorted(set(bn) & set(ox))
    print(f"    仅 Binance {len(only_bn)} · 仅 OKX {len(only_ox)} · 两边都有 {len(both)}")

    n_ok = n_bad = 0
    for c in sorted(set(bn) | set(ox)):
        try:
            if c in bn and c in ox:
                # 两边都有 → 用 Binance（流动性更好、数据更干净）
                d = norm(pd.read_feather(bn[c]), "binance")
            elif c in bn:
                d = norm(pd.read_feather(bn[c]), "binance")
            else:
                d = norm(pd.read_feather(ox[c]), "okx")
            if len(d) < 200:
                n_bad += 1
                continue
            d.to_feather(f"{OUT}/{c}-1d-merged.feather")
            n_ok += 1
        except Exception as e:
            n_bad += 1
    print(f"  ✅ 合并 {n_ok} 个 · 跳过 {n_bad} 个 → {OUT}")


def stats():
    fs = glob.glob(f"{OUT}/*-1d-merged.feather")
    if not fs:
        print("  尚未构建，先跑 --build"); return
    print(f"  合并池 {len(fs)} 个币")
    rows = []
    for f in fs:
        d = pd.read_feather(f)
        rows.append({"coin": os.path.basename(f).split("-")[0], "n": len(d),
                     "start": d["date"].min(), "end": d["date"].max()})
    df = pd.DataFrame(rows).sort_values("start")
    print(f"  {'年份':<8}{'当年已有数据':>14}")
    print("  " + "-" * 24)
    for y in range(2019, 2027):
        n = (df["start"] <= f"{y}-12-31").sum()
        print(f"  {y:<8}{n:>14}")
    print("  " + "-" * 24)
    print(f"\n  最早 {df['start'].min().date()} · 最晚 {df['start'].max().date()}")
    print(f"  平均历史长度 {df['n'].mean():.0f} 天")
    print(f"\n  币种: {', '.join(df['coin'].tolist()[:30])} ...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.build or not a.stats:
        build()
    if a.stats:
        print(); stats()
