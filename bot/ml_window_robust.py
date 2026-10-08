#!/usr/bin/env python3
"""
窗口定义稳健性检验 —— 替代「正窗口占比」判据（第 11 轮）

为什么需要：
    「正窗口占比 ≥ 80%」不是良定义的判据 —— 它完全由窗口宽度决定。
    同一起始期，宽度从 1 月调到 6 月，正窗口占比从 65% → 90%。
    **任何结论都能靠调窗口宽度来"达标"。**

本工具给出不可被单一参数操纵的稳健性判据：
    在【多种窗口宽度】下分别计算 t 值，
    要求【全部或绝大多数】宽度下 t > 2。

用法:
    python ml_window_robust.py --pred /tmp/lstm_scores.npy --meta /tmp/lstm_meta.pkl
    python ml_window_robust.py --pred ... --meta ... --start 2023-01-01
"""

import argparse
import json
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from ml_regime import ic_t


def window_t(meta, start, months, step=None):
    """不重叠窗口的 IC 序列与 t 值（step 默认等于宽度 → 不重叠）"""
    step = step or months
    d = meta[meta["date"] >= start]
    if len(d) < 300:
        return None
    ics = []
    a = pd.Timestamp(start)
    while a < d["date"].max() - pd.DateOffset(months=months):
        b = a + pd.DateOffset(months=months)
        m = ((d["date"] >= a) & (d["date"] < b)).values
        if m.sum() >= 150:
            v, _ = ic_t(d["p"].values[m], d["ret"].values[m])
            if np.isfinite(v):
                ics.append(v)
        a = a + pd.DateOffset(months=step)
    if len(ics) < 4:
        return None
    arr = np.array(ics)
    t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
    return {"months": months, "n_win": len(arr), "ic": float(arr.mean()),
            "t": float(t), "pos": int((arr > 0).sum()),
            "pos_pct": float((arr > 0).mean())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--widths", default="1,2,3,4,6,9,12")
    args = ap.parse_args()

    sc = np.load(args.pred)
    meta = pd.read_pickle(args.meta).copy()
    meta["p"] = sc
    meta = meta.dropna(subset=["p"])
    meta["date"] = pd.to_datetime(meta["date"])

    widths = [int(x) for x in args.widths.split(",")]
    starts = [args.start]
    for s in ["2022-01-01", "2023-01-01"]:
        if s > args.start:
            starts.append(s)

    print("=" * 96)
    print("窗口定义稳健性检验（判据：多种宽度下 t 都 > 2）")
    print("=" * 96)
    summary = {}
    for st in starts:
        print(f"\n  起始期 {st}")
        print(f"    {'宽度':>6}{'窗口数':>8}{'平均IC':>10}{'t值':>8}{'正窗口':>10}{'占比':>8}  判定")
        print("    " + "-" * 60)
        rows = []
        for w in widths:
            r = window_t(meta, st, w)
            if not r:
                continue
            rows.append(r)
            print(f"    {str(w)+'月':>6}{r['n_win']:>8}{r['ic']:>+10.4f}{r['t']:>8.2f}"
                  f"{str(r['pos'])+'/'+str(r['n_win']):>10}{r['pos_pct']*100:>7.0f}%"
                  f"  {'✅' if r['t']>2 else '❌'}")
        print("    " + "-" * 60)
        if rows:
            ts = [r["t"] for r in rows]
            passed = sum(1 for t in ts if t > 2)
            robust = passed >= len(ts) * 0.8
            summary[st] = {"widths": rows, "n_pass": passed, "n_total": len(ts),
                           "robust": bool(robust)}
            print(f"    t 值范围 {min(ts):.2f}~{max(ts):.2f} · "
                  f"通过 {passed}/{len(ts)} · "
                  f"{'✅ 稳健' if robust else '❌ 不稳健'}")
    print()
    print("=" * 96)
    print("总结")
    print("=" * 96)
    for st, v in summary.items():
        print(f"  {st}: t 通过 {v['n_pass']}/{v['n_total']} —— "
              f"{'✅ 稳健' if v['robust'] else '❌ 不稳健'}")
    print()
    print("  说明：本判据不可被单一窗口参数操纵（对比「正窗口≥80%」可被调宽度达标）。")

    with open("user_data/ml_window_robust.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "summary": summary}, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/ml_window_robust.json")


if __name__ == "__main__":
    main()
