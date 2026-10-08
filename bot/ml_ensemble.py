#!/usr/bin/env python3
"""
模型集成实验（第 9 轮）

动机：
    第 8 轮序列模型逐窗口 IC 的 t 值全部 > 2（LSTM 3.02），
    但【正窗口占比】只有 64%~73%，未达 80% 标准。
    单点噪声大 —— 这正是集成要解决的问题。

做法：
    ① 序列模型：LSTM / Transformer / CNN（吃 30 天路径）
    ② 表格模型：LightGBM（吃 88 个截面特征）
    ③ 集成：把各模型分数【按秩平均】（rank average）
       —— 秩平均比概率平均稳健：不同模型的概率尺度不可比，
          但秩是可比的。这直接对应第 6 轮发现的「概率校准漂移」问题。
    ④ 评估：扩展窗口走查 + 逐窗口 IC（正确口径）

判据（不变）：
    t > 2  且  正窗口占比 >= 80%

用法:
    python ml_ensemble.py
    python ml_ensemble.py --seq-len 60
    python ml_ensemble.py --no-tabular
"""

import argparse
import json
import time
import warnings

import numpy as np
import pandas as pd
import torch

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_experiment as ex
import ml_oos as oos
import ml_seq

from ml_regime import ic_t


def rank_avg(score_list, weights=None):
    """秩平均集成：先各自转成 [0,1] 秩，再加权平均"""
    rs = []
    for s in score_list:
        r = pd.Series(s).rank(pct=True).values
        rs.append(r)
    R = np.vstack(rs)
    if weights is None:
        return R.mean(axis=0)
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    return (R * w[:, None]).sum(axis=0)


def period_ic(meta, score, min_n=100, freq="Q"):
    d = meta.copy()
    d["p"] = score
    d["period"] = pd.to_datetime(d["date"]).dt.to_period(freq).astype(str)
    out = []
    for _, g in d.groupby("period"):
        if len(g) < min_n:
            continue
        ic, _ = ic_t(g["p"].values, g["ret"].values)
        if np.isfinite(ic):
            out.append(ic)
    if len(out) < 3:
        return np.nan, np.nan, 0, 0
    a = np.array(out)
    t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else np.nan
    return float(a.mean()), float(t), int((a > 0).sum()), len(a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq-len", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--no-tabular", action="store_true")
    ap.add_argument("--seq-models", default="lstm,transformer,cnn")
    ap.add_argument("--tab-models", default="lgbm,logit")
    args = ap.parse_args()

    t0 = time.time()
    # ⚠️ 必须限制线程：LightGBM(OpenMP) 与 PyTorch(MPS) 同时抢线程会触发
    #    "OMP: Error #179: Function pthread_mutex_init failed"
    torch.set_num_threads(1)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"  设备 {device}")

    # ── 数据 ──
    seq, meta, sfeats = ml_seq.build_panel(dense=True, seq_len=args.seq_len)
    df, tfeats, _ = oos.build(dense=True, embargo=30)
    print(f"  序列 {seq.shape} · 表格特征 {len(tfeats)}")

    # 对齐（按 date, coin）
    meta_i = meta.copy()
    meta_i["date"] = pd.to_datetime(meta_i["date"])
    tab = df.reset_index()[["date", "coin"] + tfeats].copy()
    tab["date"] = pd.to_datetime(tab["date"])
    m = meta_i.merge(tab, on=["date", "coin"], how="inner", suffixes=("", "_t"))
    keep = meta_i.merge(tab[["date", "coin"]], on=["date", "coin"], how="inner")
    pos_map = {(r.date, r.coin): i for i, r in enumerate(meta_i.itertuples())}
    idx_seq = np.array([pos_map[(r.date, r.coin)] for r in keep.itertuples()])
    print(f"  对齐后样本 {len(keep)}")

    seqA = seq[idx_seq]
    Xtab = m[tfeats].values
    ret = m["ret"].values
    y = m["label"].values
    dates = pd.to_datetime(m["date"])
    metaA = pd.DataFrame({"date": dates, "coin": m["coin"], "ret": ret, "label": y})
    F = seqA.shape[2]

    # ── 走查分段 ──
    cuts = []
    t = pd.Timestamp("2021-07-01")
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=args.step),
                            end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=args.step)
    print(f"  走查 {len(cuts)} 段（每 {args.step} 个月）")

    seq_kinds = [s for s in args.seq_models.split(",") if s]
    tab_kinds = [] if args.no_tabular else [s for s in args.tab_models.split(",") if s]
    all_names = seq_kinds + tab_kinds
    scores = {k: np.full(len(metaA), np.nan) for k in all_names}

    print()
    print("=" * 100)
    print(f"走查训练（序列 {args.seq_len} 天 · {len(seq_kinds)} 个序列模型 + "
          f"{len(tab_kinds)} 个表格模型）")
    print("=" * 100)
    for wi, (a, b) in enumerate(cuts):
        trm = ((dates >= a - pd.Timedelta(days=365 * 20)) & (dates < a)).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 2000 or tem.sum() < 100:
            continue
        # 序列模型
        if seq_kinds:
            mu = seqA[trm].reshape(-1, F).mean(axis=0)
            sd = seqA[trm].reshape(-1, F).std(axis=0) + 1e-6
            Xtr = np.nan_to_num(((seqA[trm] - mu) / sd).astype(np.float32))
            Xte = np.nan_to_num(((seqA[tem] - mu) / sd).astype(np.float32))
            for k in seq_kinds:
                mm = ml_seq.train_seq(Xtr, y[trm], F, k, device,
                                      epochs=args.epochs, hidden=args.hidden)
                scores[k][tem] = ml_seq.predict_seq(mm, Xte, device)
        # 表格模型
        for k in tab_kinds:
            try:
                A, B = ex.impute(Xtab[trm], Xtab[tem])
                mm = ex.make_model(k, num_threads=1)
                mm.fit(A, y[trm])
                scores[k][tem] = mm.predict_proba(B)[:, 1]
            except Exception as exc:
                print(f"    {k} 失败: {type(exc).__name__}")
        print(f"  段 {wi+1}/{len(cuts)}  {str(a.date())}  训练 {trm.sum():>6}  测试 {tem.sum():>6}")

    print()
    print("=" * 100)
    print("结果（判据只用逐窗口 IC）")
    print("=" * 100)
    print(f"  {'模型':<24}{'逐窗口IC':>11}{'t值':>8}{'正窗口':>9}{'池化IC':>10}{'达标':>7}")
    print("  " + "-" * 72)

    results = {}
    ok_scores = {}
    for k in all_names:
        ok = ~np.isnan(scores[k])
        if ok.sum() < 500:
            continue
        sub = metaA[ok].copy()
        icw, tw, npos, nwin = period_ic(sub, scores[k][ok])
        icp, tp = ic_t(scores[k][ok], sub["ret"].values)
        good = (tw is not None and tw > 2 and nwin and npos / nwin >= 0.8)
        results[k] = {"ic_period": icw, "t_period": tw, "pos": npos, "n": nwin,
                      "ic_pool": icp, "pass": bool(good)}
        ok_scores[k] = ok
        print(f"  {k:<24}{icw:>+11.4f}{tw:>8.2f}{str(npos)+'/'+str(nwin):>9}"
              f"{icp:>+10.4f}{'✅' if good else '❌':>7}")

    # ── 集成 ──
    print("  " + "-" * 72)
    common = np.ones(len(metaA), dtype=bool)
    for k in all_names:
        common &= ok_scores[k]
    if common.sum() > 500:
        sub = metaA[common].copy()
        ens = rank_avg([scores[k][common] for k in all_names])
        icw, tw, npos, nwin = period_ic(sub, ens)
        icp, tp = ic_t(ens, sub["ret"].values)
        good = (tw is not None and tw > 2 and nwin and npos / nwin >= 0.8)
        results["__ensemble__"] = {"ic_period": icw, "t_period": tw, "pos": npos,
                                   "n": nwin, "ic_pool": icp, "pass": bool(good),
                                   "members": all_names}
        print(f"  {'★ 集成（秩平均）':<24}{icw:>+11.4f}{tw:>8.2f}"
              f"{str(npos)+'/'+str(nwin):>9}{icp:>+10.4f}{'✅' if good else '❌':>7}")
        print(f"    成员: {', '.join(all_names)}   共同样本 {common.sum()}")
        # 序列模型集成（不含表格）
        if len(seq_kinds) > 1:
            ens_s = rank_avg([scores[k][common] for k in seq_kinds])
            icw2, tw2, npos2, nwin2 = period_ic(sub, ens_s)
            good2 = (tw2 is not None and tw2 > 2 and nwin2 and npos2 / nwin2 >= 0.8)
            results["__ensemble_seq__"] = {"ic_period": icw2, "t_period": tw2,
                                           "pos": npos2, "n": nwin2, "pass": bool(good2)}
            print(f"  {'  仅序列集成':<24}{icw2:>+11.4f}{tw2:>8.2f}"
                  f"{str(npos2)+'/'+str(nwin2):>9}{'':>10}{'✅' if good2 else '❌':>7}")
        # 表格集成
        if len(tab_kinds) > 1:
            ens_t = rank_avg([scores[k][common] for k in tab_kinds])
            icw3, tw3, npos3, nwin3 = period_ic(sub, ens_t)
            good3 = (tw3 is not None and tw3 > 2 and nwin3 and npos3 / nwin3 >= 0.8)
            results["__ensemble_tab__"] = {"ic_period": icw3, "t_period": tw3,
                                           "pos": npos3, "n": nwin3, "pass": bool(good3)}
            print(f"  {'  仅表格集成':<24}{icw3:>+11.4f}{tw3:>8.2f}"
                  f"{str(npos3)+'/'+str(nwin3):>9}{'':>10}{'✅' if good3 else '❌':>7}")
    print("  " + "-" * 72)

    print()
    print(f"  达标标准: 逐窗口 IC 的 t > 2  且  正窗口占比 >= 80%")
    passed = [k for k, v in results.items() if v.get("pass")]
    if passed:
        print(f"  → ✅ 达标: {', '.join(passed)}")
    else:
        best = max(results.items(), key=lambda kv: kv[1]["t_period"] or -9)
        print(f"  → ❌ 无达标组合。最好: {best[0]} "
              f"t={best[1]['t_period']:.2f} 正窗口 {best[1]['pos']}/{best[1]['n']}"
              f"（{best[1]['pos']/best[1]['n']*100:.0f}%）")
        print(f"     距 80% 还差 {int(np.ceil(0.8*best[1]['n'])) - best[1]['pos']} 个窗口")

    with open("user_data/ml_ensemble_results.jsonl", "a", encoding="utf-8") as f:
        rec = {"t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
               "round": 9, "seq_len": args.seq_len, "epochs": args.epochs,
               "seq_models": seq_kinds, "tab_models": tab_kinds,
               "results": results}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_ensemble_results.jsonl  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
