#!/usr/bin/env python3
"""
种子集成（Seed Ensemble）—— 第 10 轮

动机（来自第 9 轮的两个发现）：
    ① 超参扫描：**越简单越好**
       L=30 lstm h=64 1层 d=0.3 → t=3.61（最好）
       h=128 2层 → t=0.94 · transformer 2层 → t=0.38
       → 加深加宽没有帮助

    ② 唯一的未达标项是【正窗口占比】（74% vs 阈值 80%）
       这是**方差问题**，不是偏差问题 —— 单次随机初始化的结果抖动大。

    → 对策不是加复杂度，而是**多个简单模型的种子集成**：
      每个成员保持极简（1 层、h=64），只换随机种子，
      对预测做秩平均。既保持简单，又把初始化方差平均掉。

这与第 9 轮的「简单更好」完全一致：**降低方差，而不是提高容量。**

做法：
    · 每个走查段训练 K 个同架构不同种子的 LSTM
    · 对 K 个预测做秩平均（秩平均对尺度不敏感，第 6 轮校准漂移教训）
    · 与单模型对照：IC / t值 / 正窗口占比

用法:
    python ml_seed_ensemble.py                    # K=5
    python ml_seed_ensemble.py --k 8
    python ml_seed_ensemble.py --members lgbm     # 换成员类型
"""

import argparse
import json
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import torch

torch.set_num_threads(1)

import ml_experiment as ex
import ml_seq

from ml_regime import ic_t

WALK_START = "2021-07-01"
STEP_MONTHS = 6
EVAL_WINDOW = 6


def rank_avg(list_of_scores):
    """秩平均：对尺度/校准漂移免疫（第 6 轮教训）"""
    R = np.vstack([pd.Series(s).rank(pct=True).values for s in list_of_scores])
    return R.mean(axis=0)


def period_ic_multi(meta, score, freq="MS", months=EVAL_WINDOW, min_n=150):
    """移动窗口逐窗口 IC（判据口径，绝不池化）"""
    dates = pd.to_datetime(meta["date"])
    ics = []
    dts = pd.date_range(pd.Timestamp(WALK_START),
                        dates.max() - pd.DateOffset(months=months), freq=freq)
    for a in dts:
        b = a + pd.DateOffset(months=months)
        m = ((dates >= a) & (dates < b)).values & np.isfinite(score)
        if m.sum() < min_n:
            continue
        ic, _ = ic_t(score[m], meta["ret"].values[m])
        if np.isfinite(ic):
            ics.append(ic)
    if len(ics) < 5:
        return np.nan, np.nan, 0, 0
    arr = np.array(ics)
    t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
    return float(arr.mean()), float(t), int((arr > 0).sum()), len(arr)


def evaluate_members(cfg, seq, meta, feats, seeds, member="lstm"):
    """每个走查段训练多个种子，返回每个种子的完整预测序列"""
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
        trm = (dates < a).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 2000 or tem.sum() < 100:
            continue
        mu = seq[trm].reshape(-1, F).mean(axis=0)
        sd = seq[trm].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = np.nan_to_num(((seq[trm] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[tem] - mu) / sd).astype(np.float32))
        for i, sd_seed in enumerate(seeds):
            m = ml_seq.train_seq(Xtr, y[trm], F, member, device, epochs=8,
                                 hidden=64, layers=1, dropout=0.3, seed=sd_seed)
            per_seed[i][tem] = ml_seq.predict_seq(m, Xte, device)
    return per_seed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="种子数")
    ap.add_argument("--members", default="lstm", help="成员模型类型")
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--features", default="base")
    args = ap.parse_args()

    t0 = time.time()
    seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=args.seq_len,
                                          feature_set=args.features)
    print(f"  序列 {seq.shape} · 成员 {args.members} · 种子数 {args.k}")

    seeds = [100 + i * 17 for i in range(args.k)]
    per_seed = evaluate_members(None, seq, meta, feats, seeds, args.members)

    print()
    print("=" * 96)
    print("单个种子（看方差有多大）")
    print("=" * 96)
    print(f"  {'种子':<10}{'平均IC':>10}{'t值':>8}{'正窗口':>10}{'占比':>8}")
    print("  " + "-" * 50)
    singles = []
    for i, s in enumerate(seeds):
        ok = np.isfinite(per_seed[i])
        if ok.sum() < 500:
            continue
        ic, t, pos, n = period_ic_multi(meta, per_seed[i])
        singles.append((ic, t, pos, n))
        print(f"  {s:<10}{ic:>+10.4f}{t:>8.2f}{str(pos)+'/'+str(n):>10}{pos/n*100:>7.0f}%")
    print("  " + "-" * 50)
    if singles:
        ts = [x[1] for x in singles]
        ps = [x[2] / x[3] for x in singles]
        print(f"  单模型 t 值: 均值 {np.mean(ts):.2f} · 范围 {min(ts):.2f}~{max(ts):.2f} · "
              f"标准差 {np.std(ts, ddof=1):.2f}")
        print(f"  单模型正窗口占比: 均值 {np.mean(ps)*100:.0f}% · "
              f"范围 {min(ps)*100:.0f}%~{max(ps)*100:.0f}%")

    # 集成
    common = np.ones(len(meta), dtype=bool)
    for s in per_seed:
        common &= np.isfinite(s)
    if common.sum() < 500:
        print("\n  共同样本不足"); return
    ens = rank_avg([s[common] for s in per_seed])
    sub = meta[common].copy()
    ic, t, pos, n = period_ic_multi(sub, ens)
    icp, tp = ic_t(ens, sub["ret"].values)

    print()
    print("=" * 96)
    print(f"⭐ 种子集成（K={args.k}，秩平均）")
    print("=" * 96)
    print(f"  平均IC {ic:+.4f} · t={t:.2f} · 正窗口 {pos}/{n} ({pos/n*100:.0f}%)")
    print(f"  池化IC（仅对照）{icp:+.4f} · t={tp:.2f}")
    print()
    if singles:
        b_ic = np.mean([x[0] for x in singles])
        b_t = np.mean([x[1] for x in singles])
        b_pos = np.mean([x[2] / x[3] for x in singles])
        print(f"  相对单模型均值:")
        print(f"    IC     {b_ic:+.4f} → {ic:+.4f}   ({(ic/b_ic-1)*100:+.0f}%)")
        print(f"    t      {b_t:.2f} → {t:.2f}   ({(t/b_t-1)*100:+.0f}%)")
        print(f"    正窗口 {b_pos*100:.0f}% → {pos/n*100:.0f}%   ({(pos/n-b_pos)*100:+.0f} 个百分点)")
        best_single_t = max(x[1] for x in singles)
        print(f"    对比最强单模型 t={best_single_t:.2f} → 集成 t={t:.2f}"
              + ("  ✅ 集成更好" if t > best_single_t else "  （未超过最强单模型）"))
    print()
    print(f"  判据（t>2 且 正窗口>=80%）："
          f"{'✅ 达标' if (t > 2 and pos/n >= 0.8) else '❌ 未达标'}")
    if t > 2 and pos / n < 0.8:
        from scipy import stats
        need = stats.norm.ppf(0.8) * np.sqrt(n)
        print(f"    t={t:.2f} 已过，但正窗口 {pos/n*100:.0f}% < 80%")
        print(f"    理论上 t={t:.2f} 对应期望正窗口 {stats.norm.cdf(t/np.sqrt(n))*100:.0f}%")
        print(f"    → 80% 门槛等价于要求 t >= {need:.2f}（判据不自洽，见迭代日志第 9 轮 D 节）")

    with open("user_data/ml_seed_ensemble.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "round": 10, "k": args.k, "member": args.members,
            "seq_len": args.seq_len, "features": args.features,
            "ensemble": {"ic": round(ic, 5), "t": round(t, 3),
                         "pos": pos, "n": n, "ic_pool": round(icp, 5)},
            "singles": [{"ic": round(x[0], 5), "t": round(x[1], 3),
                         "pos": x[2], "n": x[3]} for x in singles],
        }, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_seed_ensemble.jsonl  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
