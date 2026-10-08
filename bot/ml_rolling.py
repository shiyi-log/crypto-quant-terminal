#!/usr/bin/env python3
"""
滚动窗口自适应训练（第 13 轮）

动机（来自第 11、12 轮）：
    第 11 轮：LSTM 信号只在 2023 年后的样本里稳健（1月/2月/3月/4月/6月/9月
             六种窗口宽度下，2021-07 起始只有 5/7 通过，2023-01 起始 5/6 通过）
    第 12 轮：种子分布显示真实期望 t≈2.3~2.6（15 个种子，12/15 t>2）

    如果市场结构在变化，**用全部历史训练**会把早期（失效的）规律带进来。
    对策：**滚动窗口** —— 只用最近 N 个月的数据训练。

对照：
    expanding   扩展窗口（当前做法，用全部历史）
    rolling-N   滚动窗口（只用最近 N 个月）
    N ∈ {6, 12, 18, 24, 36}

评估（用第 11 轮确立的稳健口径）：
    · 多种窗口宽度（1/2/3/4/6/9 月）下的 t 值，要求多数 > 2
    · 多种子（每配置 3 个种子取均值），避免规则 13 的种子陷阱
    · 不报「正窗口占比」（第 11/12 轮已证明是坏指标）

用法:
    python ml_rolling.py
    python ml_rolling.py --seeds 3 --windows 6,12,24
"""

import argparse
import json
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import torch

torch.set_num_threads(1)

import ml_seq

from ml_regime import ic_t

WALK_START = "2021-07-01"
STEP_MONTHS = 6          # 走查步长
EVAL_WIDTHS = [1, 2, 3, 4, 6, 9]   # 稳健性检验用的窗口宽度


def walk_predict(seq, meta, seeds, train_mode, window_months=None,
                 seq_len=30, epochs=8):
    """
    走查预测。
    train_mode: 'expanding' | 'rolling'
    window_months: rolling 模式的训练窗口长度（月）
    返回 per_seed（每个种子的完整预测序列）
    """
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    cuts, t = [], pd.Timestamp(WALK_START)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=STEP_MONTHS),
                            end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=STEP_MONTHS)

    per_seed = [np.full(len(meta), np.nan) for _ in seeds]
    for a, b in cuts:
        if train_mode == "rolling" and window_months:
            lo = a - pd.DateOffset(months=window_months)
            trm = ((dates >= lo) & (dates < a)).values
        else:
            trm = (dates < a).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 800 or tem.sum() < 100:
            continue
        # 标准化统计量只用训练期
        mu = seq[trm].reshape(-1, F).mean(axis=0)
        sd = seq[trm].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = np.nan_to_num(((seq[trm] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[tem] - mu) / sd).astype(np.float32))
        for i, sd_seed in enumerate(seeds):
            m = ml_seq.train_seq(Xtr, y[trm], F, "lstm", device, epochs=epochs,
                                 hidden=64, layers=1, dropout=0.3, seed=sd_seed)
            per_seed[i][tem] = ml_seq.predict_seq(m, Xte, device)
    return per_seed


def robust_eval(meta, score, widths=EVAL_WIDTHS):
    """
    稳健评估：多种窗口宽度下的 t 值（不重叠）。
    返回 {width: {t, ic, n_win}} 与通过数
    """
    dates = pd.to_datetime(meta["date"])
    out = {}
    for w in widths:
        ics = []
        a = pd.Timestamp(WALK_START)
        while a < dates.max() - pd.DateOffset(months=w):
            b = a + pd.DateOffset(months=w)
            m = ((dates >= a) & (dates < b)).values & np.isfinite(score)
            if m.sum() >= 150:
                v, _ = ic_t(score[m], meta["ret"].values[m])
                if np.isfinite(v):
                    ics.append(v)
            a = a + pd.DateOffset(months=w)
        if len(ics) < 4:
            continue
        arr = np.array(ics)
        t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
        out[w] = {"t": float(t), "ic": float(arr.mean()), "n_win": len(arr)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--windows", default="6,12,18,24,36")
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=8)
    args = ap.parse_args()

    t0 = time.time()
    seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=args.seq_len,
                                          feature_set="base")
    print(f"  序列 {seq.shape}")
    seeds = [100, 219, 287][:args.seeds]
    print(f"  种子 {seeds}（取第 12 轮分布里的好/中/上三等分点）")

    windows = [int(x) for x in args.windows.split(",")]
    configs = [("expanding", None)] + [("rolling", w) for w in windows]

    print()
    print("=" * 100)
    print(f"训练窗口模式对照（每配置 {len(seeds)} 个种子，报【均值】）")
    print("=" * 100)
    header = f"  {'模式':<16}"
    for w in EVAL_WIDTHS:
        header += f"{str(w)+'月t':>9}"
    header += f"{'通过':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    results = {}
    for mode, wm in configs:
        label = "expanding(全部历史)" if mode == "expanding" else f"rolling {wm}月"
        per_seed = walk_predict(seq, meta, seeds, mode, wm,
                                seq_len=args.seq_len, epochs=args.epochs)
        # 每种子算稳健评估，再对种子取均值
        per_width = {w: [] for w in EVAL_WIDTHS}
        for s in per_seed:
            ok = np.isfinite(s)
            if ok.sum() < 500:
                continue
            sub = meta[ok].copy()
            r = robust_eval(sub, s[ok])
            for w in EVAL_WIDTHS:
                if w in r:
                    per_width[w].append(r[w]["t"])
        row = {}
        line = f"  {label:<16}"
        npass = 0
        for w in EVAL_WIDTHS:
            if per_width[w]:
                tm = float(np.mean(per_width[w]))
                row[w] = round(tm, 2)
                line += f"{tm:>9.2f}"
                if tm > 2:
                    npass += 1
            else:
                line += f"{'—':>9}"
        line += f"{npass}/{len(EVAL_WIDTHS):>8}"
        print(line)
        results[label] = {"per_width_t": row, "n_pass": npass,
                          "n_widths": len(EVAL_WIDTHS)}
    print("  " + "-" * (len(header) - 2))

    print()
    print("  判据（第 11 轮口径）：多种窗口宽度下 t 都 > 2，不可被单一参数操纵")
    best = max(results.items(), key=lambda kv: kv[1]["n_pass"])
    print(f"  最优: {best[0]}  通过 {best[1]['n_pass']}/{best[1]['n_widths']}")
    print()
    print("  解读：")
    exp = results.get("expanding(全部历史)")
    if exp:
        print(f"    expanding 通过 {exp['n_pass']}/{exp['n_widths']}")
    for k, v in results.items():
        if k.startswith("rolling") and v["n_pass"] > (exp["n_pass"] if exp else 0):
            print(f"    ✅ {k} 通过 {v['n_pass']}/{v['n_widths']} —— 优于扩展窗口")
    if exp and all(v["n_pass"] <= exp["n_pass"] for k, v in results.items()
                   if k.startswith("rolling")):
        print(f"    ❌ 所有滚动窗口都不优于扩展窗口 —— 说明「只用近期数据」没有帮助")

    with open("user_data/ml_rolling_results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "round": 13, "seeds": seeds, "seq_len": args.seq_len,
            "results": results,
        }, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_rolling_results.jsonl  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
