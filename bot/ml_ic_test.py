#!/usr/bin/env python3
"""
逐笔口径的统计检验 —— 替代 Calmar 的高功效评估

为什么换口径：
    第 6 轮的分块自助法证明：在这个样本量下（313 笔交易），
    Calmar 的 95% 置信区间是 [-0.2, +2.4]。
    **连上帝视角（完美预测）都无法与基线区分。**

    → Calmar 在本场景下几乎没有统计功效，不能用来判断过滤器好坏。

改用：ML 分数与逐笔收益的【秩相关 IC】及其 t 值。

    优点：
      · 用全部样本（不去掉任何一笔），功效远高于"过滤前后比较"
      · 不依赖阈值选择，避免阈值噪声
      · 直接度量「模型排序能力」这一核心问题
      · 与因子研究的 IC 口径一致，可横向对比

    判读：
      IC 的 t 值 > 2 才有意义；> 3 才算稳健。

用法:
    python ml_ic_test.py
    python ml_ic_test.py --dense
"""

import argparse
import json
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_oos as oos


def rank_ic(score, ret):
    """秩相关 IC（Spearman）"""
    s = pd.Series(score).rank().values
    r = pd.Series(ret).rank().values
    n = len(s)
    if n < 30:
        return np.nan, np.nan, 0
    cs = s - s.mean()
    cr = r - r.mean()
    denom = np.sqrt((cs ** 2).sum() * (cr ** 2).sum())
    if denom == 0:
        return np.nan, np.nan, 0
    ic = float((cs * cr).sum() / denom)
    t = ic * np.sqrt(n - 2) / np.sqrt(max(1 - ic ** 2, 1e-12))
    return ic, t, n


def block_ic_t(score, ret, dates, block=20, b=300, seed=0):
    """分块自助法给出 IC 的置信区间（时间序列有自相关）"""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"s": score, "r": ret, "d": dates}).sort_values("d")
    n = len(df)
    nb = int(np.ceil(n / block))
    out = []
    for _ in range(b):
        starts = rng.integers(0, max(n - block, 1), size=nb)
        idx = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]
        sub = df.iloc[idx]
        ic, _, _ = rank_ic(sub["s"].values, sub["r"].values)
        if np.isfinite(ic):
            out.append(ic)
    out = np.array(out)
    if len(out) < 20:
        return None
    return {"lo": float(np.percentile(out, 5)), "hi": float(np.percentile(out, 95)),
            "med": float(np.median(out))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dense", action="store_true", default=True)
    ap.add_argument("--sparse", dest="dense", action="store_false")
    ap.add_argument("--embargo", type=int, default=30)
    args = ap.parse_args()

    df, feat, emb = oos.build(dense=args.dense, embargo=args.embargo)
    dts = pd.to_datetime(df.index.get_level_values("date"))
    tr = df[dts < oos.TRAIN_END]
    te = df[dts >= oos.TRAIN_END]
    te_dates = pd.to_datetime(te.index.get_level_values("date"))

    print(f"  特征 {len(feat)} · 采样 {'稠密' if args.dense else '稀疏'}")
    print(f"  训练 {len(tr)} 条（{tr.index.get_level_values('date').min().date()} ~ "
          f"{tr.index.get_level_values('date').max().date()}）")
    print(f"  测试 {len(te)} 条（{te.index.get_level_values('date').min().date()} ~ "
          f"{te.index.get_level_values('date').max().date()}）")
    print()

    print("=" * 104)
    print("逐笔秩相关 IC 检验（训练期拟合 → 测试期评估，不使用测试期标签做任何选择）")
    print("=" * 104)
    print(f"  {'模型':<10}{'训练IC':>10}{'训练t':>9}{'测试IC':>10}{'测试t':>9}"
          f"{'自助5%':>10}{'自助95%':>10}{'显著':>8}")
    print("  " + "-" * 76)

    results = []
    for name in ["logit", "lgbm", "xgb", "mlp"]:
        try:
            oof, pte, _ = oos.fit_predict(tr, te, feat, name, args.embargo)
        except Exception as exc:
            print(f"  {name:<10} 失败: {type(exc).__name__}")
            continue
        ok = ~np.isnan(oof)
        ic_tr, t_tr, n_tr = rank_ic(oof[ok], tr["ret"].values[ok])
        ic_te, t_te, n_te = rank_ic(pte, te["ret"].values)
        ci = block_ic_t(pte, te["ret"].values, te_dates.values)
        sig = "✅" if (ci and ci["lo"] > 0) else ("🟡" if (ci and ci["hi"] > 0) else "❌")
        results.append({"model": name, "ic_tr": ic_tr, "t_tr": t_tr,
                        "ic_te": ic_te, "t_te": t_te, "ci": ci})
        print(f"  {name:<10}{ic_tr:>10.4f}{t_tr:>9.2f}{ic_te:>10.4f}{t_te:>9.2f}"
              f"{(ci['lo'] if ci else float('nan')):>10.4f}"
              f"{(ci['hi'] if ci else float('nan')):>10.4f}{sig:>8}")
    print("  " + "-" * 76)

    # 对照：上帝视角
    y = te["label"].values.astype(float)
    ic_o, t_o, _ = rank_ic(y, te["ret"].values)
    ci_o = block_ic_t(y, te["ret"].values, te_dates.values)
    print(f"  {'上帝视角':<10}{'—':>10}{'—':>9}{ic_o:>10.4f}{t_o:>9.2f}"
          f"{(ci_o['lo'] if ci_o else float('nan')):>10.4f}"
          f"{(ci_o['hi'] if ci_o else float('nan')):>10.4f}"
          f"{'✅' if ci_o and ci_o['lo'] > 0 else '❌':>8}")
    # 对照：随机
    rng = np.random.default_rng(1)
    rnd = rng.uniform(0, 1, len(te))
    ic_r, t_r, _ = rank_ic(rnd, te["ret"].values)
    ci_r = block_ic_t(rnd, te["ret"].values, te_dates.values)
    print(f"  {'随机对照':<10}{'—':>10}{'—':>9}{ic_r:>10.4f}{t_r:>9.2f}"
          f"{(ci_r['lo'] if ci_r else float('nan')):>10.4f}"
          f"{(ci_r['hi'] if ci_r else float('nan')):>10.4f}"
          f"{'✅' if ci_r and ci_r['lo'] > 0 else '❌':>8}")
    print("  " + "-" * 76)

    print()
    print("  判读：")
    print("    上帝视角的 IC 是上界（模型最多能做到这么好）")
    print("    随机对照的 IC 应该在 0 附近")
    print("    模型 IC 要显著高于 0（5% 分位 > 0）才算有真实排序能力")
    print()
    valid = [r for r in results if r["ci"]]
    if valid:
        best = max(valid, key=lambda r: r["ic_te"])
        print(f"  最优: {best['model']}  测试 IC {best['ic_te']:.4f}  t={best['t_te']:.2f}  "
              f"自助5%分位 {best['ci']['lo']:.4f}")
        print(f"  上帝视角上界: IC {ic_o:.4f}")
        print(f"  达成度: {best['ic_te']/ic_o*100:.0f}% （模型 IC / 上帝视角 IC）")
        print()
        if best["ci"]["lo"] > 0:
            print("  ✅ 有统计显著的排序能力 —— 值得继续迭代放大")
        elif best["ci"]["hi"] > 0:
            print("  🟡 边缘显著 —— 需要更多样本或更强特征")
        else:
            print("  ❌ 排序能力不显著 —— 继续迭代（更强特征/序列模型/直接优化排序）")

    with open("user_data/ml_ic_results.jsonl", "a", encoding="utf-8") as f:
        rec = {"t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
               "round": 6, "dense": args.dense, "embargo": args.embargo,
               "n_features": len(feat), "oracle_ic": round(ic_o, 4),
               "results": [{k: (round(v, 5) if isinstance(v, float) else v)
                            for k, v in r.items() if k != "ci"} for r in results]}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_ic_results.jsonl")


if __name__ == "__main__":
    main()
