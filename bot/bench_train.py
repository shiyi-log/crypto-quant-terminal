#!/usr/bin/env python3
"""
训练侧参数基准 —— 设备 / 批量 / 线程

用 profile_speed.py 落盘的面板缓存，避免重复构建。
用法: python bench_train.py [--seq-len 30] [--epochs 2]
"""

import argparse
import gc
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

CACHE = "user_data/cache"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--step", type=int, default=6)
    args = ap.parse_args()

    import torch
    import ml_seq

    seq = np.load(f"{CACHE}/panel_{args.seq_len}_seq.npy")
    meta = pd.read_parquet(f"{CACHE}/panel_{args.seq_len}_meta.parquet")
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    print(f"面板 {seq.shape} · {seq.nbytes / 1e6:.0f} MB（磁盘缓存）")

    cuts, t = [], pd.Timestamp("2021-07-01")
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=args.step), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=args.step)
    # 取中间一段（训练集规模有代表性）
    a_, b_ = cuts[len(cuts) // 2]
    tr = (dates < a_).values
    te = ((dates >= a_) & (dates < b_)).values
    mu = seq[tr].reshape(-1, F).mean(axis=0)
    sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
    Xtr = seq[tr].astype(np.float32)
    Xtr -= mu
    Xtr /= sd
    np.nan_to_num(Xtr, copy=False)
    ytr = y[tr]
    print(f"训练样本 {Xtr.shape} · epochs {args.epochs}\n")

    rows = []
    for dev in ("mps", "cpu"):
        for th in (2, 4, 8):
            for bs in (512, 1024, 2048):
                torch.set_num_threads(th)
                try:
                    t0 = time.time()
                    m = ml_seq.train_seq(Xtr, ytr, F, "lstm", dev, epochs=args.epochs,
                                         bs=bs, lr=0.001, hidden=64, layers=1, dropout=0.3)
                    dt = time.time() - t0
                    rows.append((dt, dev, th, bs))
                    print(f"  {dev:<4} threads={th:<2} bs={bs:<5}  "
                          f"{args.epochs} epochs {dt:>6.2f}s   "
                          f"({dt / args.epochs:>5.2f}s/epoch)", flush=True)
                    del m
                except Exception as e:
                    print(f"  {dev:<4} threads={th:<2} bs={bs:<5}  失败 {type(e).__name__}: {e}")
                gc.collect()

    rows.sort()
    print("\n最快的 5 组：")
    for dt, dev, th, bs in rows[:5]:
        print(f"  {dev:<4} threads={th:<2} bs={bs:<5}  {dt:>6.2f}s")
    best = rows[0]
    print(f"\n最优：device={best[1]} threads={best[2]} bs={best[3]} "
          f"→ {best[0] / args.epochs:.2f}s/epoch/cut")


if __name__ == "__main__":
    main()
