#!/usr/bin/env python3
"""
BTC/ETH 聚焦的元标记实验（第 14 轮）

背景：用户要求「迭代任务以比特币和以太坊为主」。

⚠️ 先说清楚统计功效的硬约束（见第 13/14 轮的实测）：

    BTC/ETH 日线上的【独立趋势事件】只有：

        entry/exit   独立趋势   最小可检测 IC（t=2, 80% 功效）
        3/3            782         0.1001
        5/5            506         0.1245
        10/10          253         0.1760
        20/20          129         0.2465

    而实测的信号强度 IC ≈ 0.023~0.05。
    → **即使最宽松的 3/3 配置，最小可检测 IC 仍是实测信号的 2.9 倍。**

    1h 数据也救不了：BTC/ETH 1h 只有 2024 起（2.75 年），
    480 小时通道只有 58 个独立趋势，比日线还少。

本脚本因此做三件事，并严格区分「能推断的」与「不能推断的」：

    A. 描述性分析（可做）
       在 BTC/ETH 上，ML 分数的 IC 是多少？分年/分方向表现如何？
    B. 最佳努力的推断（功效不足，结论必须带保留）
       多种子 + 多窗口宽度的稳健口径；若测不出，只能写
       「样本量不足以判定」，**不能说「BTC/ETH 上无效」**
    C. 与「BTC/ETH 为主 + 其他币为辅」的对照
       用更宽的池子建立统计显著性，再看 BTC/ETH 是否在池中表现更好

用法:
    python ml_btceth.py --mode desc      # 只做描述性
    python ml_btceth.py --mode infer     # 最佳努力推断
    python ml_btceth.py --mode compare   # 与宽池对照
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

import ml_lab
import ml_seq

from ml_regime import ic_t

CORE = ["BTC", "ETH"]


def independent_trends(data, entry=20, exit_=20):
    """统计独立趋势数（去重后的事件数）"""
    n = 0
    for s, d in data.items():
        c = d["close"]
        hh = c.rolling(entry).max().shift(1)
        ll = c.rolling(entry).min().shift(1)
        hx = c.rolling(exit_).max().shift(1)
        lx = c.rolling(exit_).min().shift(1)
        cv = c.values
        cur = 0.0
        for i in range(len(cv)):
            prev = cur
            if cur == 0.0:
                if np.isfinite(hh.iloc[i]) and cv[i] > hh.iloc[i]:
                    cur = 1.0
                elif np.isfinite(ll.iloc[i]) and cv[i] < ll.iloc[i]:
                    cur = -1.0
            else:
                if cur > 0 and np.isfinite(lx.iloc[i]) and cv[i] < lx.iloc[i]:
                    cur = 0.0
                elif cur < 0 and np.isfinite(hx.iloc[i]) and cv[i] > hx.iloc[i]:
                    cur = 0.0
            if cur != 0 and cur != prev:
                n += 1
    return n


def train_eval(data, seeds, seq_len=30, epochs=8, feature_set="base",
               walk_start="2021-07-01", step=6):
    """走查训练 + 预测。返回 (meta, per_seed 预测列表)"""
    ev = ml_lab.make_dense_events(data)
    y = ml_lab.strategy_label(data, ev).set_index(["date", "coin"])
    X = ml_lab.build_features(data, ml_lab.load_funding(), ml_lab.load_dvol(),
                              cross_sectional=True)
    X = X[~X.index.duplicated(keep="first")]
    df = X.join(y[["label", "ret", "hold_days", "side"]], how="inner").dropna(subset=["label"])
    df = df[df.index.get_level_values("coin").isin(list(data.keys()))]

    # 序列
    want = ml_seq.FEATURE_SETS.get(feature_set, ml_seq.SEQ_FEATURES)
    feats = [c for c in want if c in df.columns]
    seq, meta, _ = ml_seq.build_panel(dense=True, seq_len=seq_len,
                                      feature_set=feature_set)
    # 只保留目标币
    keep = meta["coin"].isin(list(data.keys()))
    seq = seq[keep.values]
    meta = meta[keep].reset_index(drop=True)

    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    yv = meta["label"].values
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    cuts, t = [], pd.Timestamp(walk_start)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=step), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=step)

    per = [np.full(len(meta), np.nan) for _ in seeds]
    for a, b in cuts:
        trm = (dates < a).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 300 or tem.sum() < 30:
            continue
        mu = seq[trm].reshape(-1, F).mean(axis=0)
        sd = seq[trm].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = np.nan_to_num(((seq[trm] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[tem] - mu) / sd).astype(np.float32))
        for i, sd_seed in enumerate(seeds):
            m = ml_seq.train_seq(Xtr, yv[trm], F, "lstm", device, epochs=epochs,
                                 hidden=64, layers=1, dropout=0.3, seed=sd_seed)
            per[i][tem] = ml_seq.predict_seq(m, Xte, device)
    return meta, per


def robust_t(meta, score, widths=(1, 2, 3, 4, 6, 9)):
    """多窗口宽度的 t 值（第 11 轮口径）"""
    dates = pd.to_datetime(meta["date"])
    out = {}
    for w in widths:
        ics = []
        a = dates.min()
        while a < dates.max() - pd.DateOffset(months=w):
            b = a + pd.DateOffset(months=w)
            m = ((dates >= a) & (dates < b)).values & np.isfinite(score)
            if m.sum() >= 30:
                v, _ = ic_t(score[m], meta["ret"].values[m])
                if np.isfinite(v):
                    ics.append(v)
            a = a + pd.DateOffset(months=w)
        if len(ics) < 4:
            continue
        arr = np.array(ics)
        t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
        out[w] = {"t": float(t), "ic": float(arr.mean()), "n": len(arr)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="desc", choices=["desc", "infer", "compare"])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=8)
    args = ap.parse_args()

    t0 = time.time()
    all_data = ml_lab.load_ohlcv()
    core = {k: v for k, v in all_data.items() if k in CORE}
    print(f"  BTC/ETH 数据 {len(core)} 个")

    for en in [20, 5, 3]:
        n = independent_trends(core, en, en)
        print(f"    独立趋势 entry/exit={en}: {n}  （最小可检测 IC {2.8/np.sqrt(n):.4f}）")
    print()

    seeds = [100 + i * 37 for i in range(args.seeds)]

    if args.mode == "desc":
        print("=" * 96)
        print("A. 描述性分析（BTC/ETH 上的信号长什么样）")
        print("=" * 96)
        meta, per = train_eval(core, seeds, args.seq_len, args.epochs)
        ens = np.nanmean(np.vstack([pd.Series(p).rank(pct=True).values for p in per]), axis=0)
        ok = np.isfinite(ens)
        sub = meta[ok].copy()
        print(f"  样本 {ok.sum()} · 事件区间 {pd.to_datetime(sub['date']).min().date()} ~ "
              f"{pd.to_datetime(sub['date']).max().date()}")
        ic, t = ic_t(ens[ok], sub["ret"].values)
        print(f"  池化 IC {ic:+.4f} (t={t:.2f})  ← 描述用，不作判据")
        print()
        rt = robust_t(sub, ens[ok])
        print(f"  {'窗口宽度':>8}{'窗口数':>8}{'平均IC':>10}{'t值':>8}")
        print("  " + "-" * 36)
        for w, r in sorted(rt.items()):
            print(f"  {str(w)+'月':>8}{r['n']:>8}{r['ic']:>+10.4f}{r['t']:>8.2f}")
        print("  " + "-" * 36)
        if rt:
            ts = [r["t"] for r in rt.values()]
            print(f"  t 值范围 {min(ts):.2f}~{max(ts):.2f} · 通过 {sum(1 for x in ts if x>2)}/{len(ts)}")
        # 分年
        d = sub.copy()
        d["year"] = pd.to_datetime(d["date"]).dt.year
        d["p"] = ens[ok]
        print()
        print(f"  {'年份':<8}{'样本':>8}{'IC':>10}{'正样本率':>10}{'平均收益':>11}")
        print("  " + "-" * 48)
        for yr, g in d.groupby("year"):
            if len(g) < 50:
                continue
            v, _ = ic_t(g["p"].values, g["ret"].values)
            print(f"  {yr:<8}{len(g):>8}{v:>+10.4f}{g['label'].mean():>10.3f}"
                  f"{g['ret'].mean()*100:>10.2f}%")
        print("  " + "-" * 48)
        print()
        print("  ⚠️ 描述性结论不能替代统计检验 —— 见 --mode infer 的功效分析")

    elif args.mode == "infer":
        print("=" * 96)
        print("B. 最佳努力推断（功效不足，结论必须带保留）")
        print("=" * 96)
        meta, per = train_eval(core, seeds, args.seq_len, args.epochs)
        print(f"  {'种子':<8}{'池化IC':>10}{'t值':>8}")
        print("  " + "-" * 28)
        ts = []
        for i, p in enumerate(per):
            ok = np.isfinite(p)
            if ok.sum() < 200:
                continue
            ic, t = ic_t(p[ok], meta["ret"].values[ok])
            ts.append(t)
            print(f"  {seeds[i]:<8}{ic:>+10.4f}{t:>8.2f}")
        print("  " + "-" * 28)
        if ts:
            print(f"  t 值: 均值 {np.mean(ts):.2f} · 范围 {min(ts):.2f}~{max(ts):.2f}")
        n_ind = independent_trends(core, 20, 20)
        ic_min = 2.8 / np.sqrt(n_ind)
        print()
        print(f"  独立趋势 {n_ind} → 最小可检测 IC {ic_min:.4f}")
        print(f"  实测 IC 约 {np.mean([abs(x) for x in ts])/np.sqrt(len(meta)):.4f}（量级）")
        print()
        if ic_min > 0.1:
            print("  ❌ **样本量不足以判定** —— 不是「BTC/ETH 上无效」，")
            print(f"     而是需要 IC ≥ {ic_min:.3f} 才能检出来，远超实测信号强度。")
            print("     要在这个问题上得到统计结论，必须借助更宽的标的池。")

    else:  # compare
        print("=" * 96)
        print("C. BTC/ETH「为主」+ 其他币「为辅」的对照")
        print("=" * 96)
        meta_c, per_c = train_eval(core, seeds, args.seq_len, args.epochs)
        # 宽池
        wide = dict(all_data)
        meta_w, per_w = train_eval(wide, seeds, args.seq_len, args.epochs)

        def summary(meta, per, label):
            ens = np.nanmean(np.vstack([pd.Series(p).rank(pct=True).values for p in per]), axis=0)
            ok = np.isfinite(ens)
            sub = meta[ok].copy()
            ic, t = ic_t(ens[ok], sub["ret"].values)
            rt = robust_t(sub, ens[ok])
            npass = sum(1 for r in rt.values() if r["t"] > 2)
            print(f"  {label:<24}{len(sub):>8}{ic:>+10.4f}{t:>8.2f}"
                  f"{npass:>6}/{len(rt):<3}")
            return ens, sub, rt

        print(f"  {'池':<24}{'样本':>8}{'池化IC':>10}{'t值':>8}{'稳健通过':>10}")
        print("  " + "-" * 62)
        ec, sc, rtc = summary(meta_c, per_c, "仅 BTC/ETH")
        ew, sw, rtw = summary(meta_w, per_w, "全池（含 BTC/ETH）")
        print("  " + "-" * 62)
        print()
        print(f"  判读：")
        print(f"    仅 BTC/ETH 稳健通过 {sum(1 for r in rtc.values() if r['t']>2)}/{len(rtc)}")
        print(f"    全池      稳健通过 {sum(1 for r in rtw.values() if r['t']>2)}/{len(rtw)}")
        print()
        print("    → 若全池通过而 BTC/ETH 不通过，说明【统计功效】来自池宽，")
        print("      而不是 BTC/ETH 本身无效。此时应以全池建立显著性，")
        print("      再单独考察 BTC/ETH 在池中的相对表现。")

    with open("user_data/ml_btceth_results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "round": 14, "mode": args.mode, "seeds": seeds,
            "seq_len": args.seq_len,
        }, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_btceth_results.jsonl  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
