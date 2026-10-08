#!/usr/bin/env python3
"""
迭代速度剖析 —— 先量再优化

拆三段：
    ① 面板构建 build_panel（一次性，但每次进程启动都要重来）
    ② 每个 cut 的数据准备（统计量 + 标准化 + 拷贝）
    ③ 每个 cut 的训练（MPS）

顺带把面板缓存到磁盘，后续测量不用重复构建。
用法: python profile_speed.py [--seq-len 30] [--cuts 3]
"""

import argparse
import gc
import json
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import sys

sys.path.insert(0, ".")

CACHE = "user_data/cache"


def timed(label, fn):
    t0 = time.time()
    out = fn()
    dt = time.time() - t0
    print(f"  {label:<46}{dt:>8.1f}s", flush=True)
    return out, dt


def load_or_build(seq_len):
    os.makedirs(CACHE, exist_ok=True)
    f_seq = f"{CACHE}/panel_{seq_len}_seq.npy"
    f_meta = f"{CACHE}/panel_{seq_len}_meta.parquet"
    if os.path.exists(f_seq) and os.path.exists(f_meta):
        seq, dt1 = timed("① 面板：磁盘缓存加载", lambda: np.load(f_seq))
        meta = pd.read_parquet(f_meta)
        return seq, meta, dt1
    import ml_seq
    (seq, meta, _feats), dt1 = timed("① 面板：首次构建 build_panel",
                                     lambda: ml_seq.build_panel(dense=True, seq_len=seq_len))
    t0 = time.time()
    np.save(f_seq, seq)
    meta.to_parquet(f_meta)
    print(f"  {'   写入缓存':<46}{time.time() - t0:>8.1f}s  "
          f"({seq.nbytes / 1e6:.0f} MB)")
    return seq, meta, dt1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--cuts", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--threads", default="1,2,4,8")
    ap.add_argument("--step", type=int, default=6)
    args = ap.parse_args()

    import torch
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"\n设备 {device} · seq_len {args.seq_len} · epochs {args.epochs}\n")

    seq, meta, _ = load_or_build(args.seq_len)
    F = seq.shape[2]
    L = seq.shape[1]
    print(f"  张量 {seq.shape} · {seq.nbytes / 1e6:.0f} MB · 特征 {F}")

    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    print(f"  日期单调递增: {dates.is_monotonic_increasing}")

    cuts, t = [], pd.Timestamp("2021-07-01")
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=args.step), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=args.step)
    print(f"  走查段数: {len(cuts)}")

    # ── ② 数据准备：旧写法 vs 就地写法（要求逐位一致）──
    print("\n② 数据准备（每个 cut 一次）")
    use_cuts = cuts[-args.cuts:]
    t_stats = t_old = t_new = 0.0
    max_diff = 0.0

    def old_prep(tr, te, mu, sd):
        Xtr = np.nan_to_num(((seq[tr] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[te] - mu) / sd).astype(np.float32))
        return Xtr, Xte

    def new_prep(tr, te, mu, sd):
        # 就地：1 个副本而不是 3 个（去掉 astype 的额外拷贝 + 中间临时数组）
        a = seq[tr].astype(np.float32)          # 唯一必要的副本
        a -= mu
        a /= sd
        np.nan_to_num(a, copy=False)
        b = seq[te].astype(np.float32)
        b -= mu
        b /= sd
        np.nan_to_num(b, copy=False)
        return a, b

    for a_, b_ in use_cuts:
        tr = (dates < a_).values
        te = ((dates >= a_) & (dates < b_)).values
        if tr.sum() < 2000 or te.sum() < 100:
            continue
        t0 = time.time()
        mu = seq[tr].reshape(-1, F).mean(axis=0)
        sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
        t_stats += time.time() - t0

        t0 = time.time()
        Xtr_old, Xte_old = old_prep(tr, te, mu, sd)
        t_old += time.time() - t0

        t0 = time.time()
        Xtr_new, Xte_new = new_prep(tr, te, mu, sd)
        t_new += time.time() - t0

        max_diff = max(max_diff, float(np.abs(Xtr_old - Xtr_new).max()),
                       float(np.abs(Xte_old - Xte_new).max()))
        del Xtr_old, Xte_old, Xtr_new, Xte_new
        gc.collect()

    n = len(use_cuts)
    print(f"  {'统计量 reshape+mean/std':<40}{t_stats / n:>8.2f}s / cut")
    print(f"  {'旧写法 标准化（3 份拷贝）':<40}{t_old / n:>8.2f}s / cut")
    print(f"  {'新写法 就地标准化（1 份拷贝）':<40}{t_new / n:>8.2f}s / cut")
    print(f"  {'两者最大差异（要求 0）':<40}{max_diff:>8.2e}")

    # ── ③ 训练 ──
    print("\n③ 训练（单个 cut，一次完整训练）")
    a_, b_ = use_cuts[len(use_cuts) // 2]
    tr = (dates < a_).values
    te = ((dates >= a_) & (dates < b_)).values
    mu = seq[tr].reshape(-1, F).mean(axis=0)
    sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
    Xtr, Xte = new_prep(tr, te, mu, sd)
    print(f"  训练样本 {Xtr.shape} · 测试样本 {Xte.shape}")

    import ml_seq
    base = {"kind": "lstm", "hidden": 64, "layers": 1, "dropout": 0.3, "lr": 0.001}
    for th in [int(x) for x in args.threads.split(",")]:
        torch.set_num_threads(th)
        t0 = time.time()
        m = ml_seq.train_seq(Xtr, y[tr], F, base["kind"], device,
                             epochs=args.epochs, hidden=base["hidden"],
                             layers=base["layers"], dropout=base["dropout"],
                             lr=base["lr"])
        dt_train = time.time() - t0
        t0 = time.time()
        sc = ml_seq.predict_seq(m, Xte, device)
        dt_pred = time.time() - t0
        print(f"  threads={th:<3} 训练 {dt_train:>7.1f}s · 预测 {dt_pred:>5.1f}s "
              f"· 合计 {dt_train + dt_pred:>7.1f}s  (pred mean {sc.mean():.4f})")
        del m
        gc.collect()

    # ── 估算 ──
    print("\n④ 单 trial 估算（11 段）")
    torch.set_num_threads(1)
    t0 = time.time()
    ml_seq.train_seq(Xtr, y[tr], F, base["kind"], device, epochs=1,
                     hidden=64, layers=1, dropout=0.3, lr=0.001)
    one_epoch = time.time() - t0
    n_cuts = len(cuts)
    est_train = one_epoch * args.epochs * n_cuts
    est_prep = (t_old + t_stats) / n * n_cuts
    print(f"  单 epoch 单 cut 训练: {one_epoch:.1f}s")
    print(f"  训练合计 ≈ {est_train:.0f}s · 数据准备合计 ≈ {est_prep:.0f}s "
          f"· 数据准备占比 {est_prep / (est_train + est_prep) * 100:.0f}%")
    print(json.dumps({"one_epoch_s": round(one_epoch, 2),
                      "cuts": n_cuts, "est_train_s": round(est_train),
                      "est_prep_s": round(est_prep)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
