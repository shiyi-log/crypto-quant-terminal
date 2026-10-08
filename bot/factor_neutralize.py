#!/usr/bin/env python3
"""
因子正交与增量 IC（因子层）

为什么必须做正交：
    上一轮筛出 6 个「显著」因子：
        dist_high_90、vol_30d、funding、vol_7d、channel_width、donchian_pos
    但 vol_7d / vol_30d / channel_width 都在量「波动」——
    它们很可能是同一个因子的三种写法。
    不去重就合成，等于给同一个因子三倍权重，还会高估组合的分散度。

正交方法（横截面回归取残差）：
    对每个因子 f_i，在每一天做横截面回归：
        f_i = Σ_{j≠i} β_j · f_j + ε
    取残差 ε 作为正交化后的因子。
    这样 ε 与其余因子在横截面上线性无关。

评估：
    1. 因子相关矩阵（时间平均的横截面相关）
    2. 正交化前后的 IC 对比
    3. **增量 IC**：正交化后仍显著的，才是真正带来新信息的因子
    4. 对称化（Gram-Schmidt）排序，避免「先来后到」影响结果

用法:
    python factor_neutralize.py                # 全流程
    python factor_neutralize.py --corr 0.7     # 调整去重阈值
"""

import argparse
import itertools

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

import factor_lib as fl

# 上一轮通过假象修正的因子
CANDIDATES = ["dist_high_90", "vol_30d", "funding", "vol_7d", "channel_width", "donchian_pos"]


# ══════════════════ 相关性 ══════════════════

def factor_correlation(panels, names, min_coins=5):
    """时间平均的横截面 Spearman 相关矩阵"""
    corrs = {k: [] for k in itertools.combinations(names, 2)}
    dates = None
    for n in names:
        if n not in panels:
            return None
        dates = panels[n].index if dates is None else dates.intersection(panels[n].index)
    stacked = {n: panels[n].loc[dates] for n in names}

    for t in dates[::5]:           # 抽样加速
        vals = {}
        for n in names:
            v = stacked[n].loc[t].dropna()
            if len(v) >= min_coins:
                vals[n] = v
        if len(vals) < len(names):
            continue                      # 该日有因子全为 NaN，跳过
        common = None
        for n, v in vals.items():
            common = v.index if common is None else common.intersection(v.index)
        if common is None or len(common) < min_coins:
            continue
        for a, b in corrs:
            va, vb = vals[a][common], vals[b][common]
            if va.std() > 0 and vb.std() > 0:
                corrs[(a, b)].append(spearmanr(va, vb).statistic)
    M = pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    for (a, b), v in corrs.items():
        if v:
            M.loc[a, b] = M.loc[b, a] = float(np.mean(v))
    return M


# ══════════════════ 横截面正交化 ══════════════════

def orthogonalize(panels, target, controls, min_coins=6):
    """
    对 target 做横截面回归，去掉 controls 能解释的部分，返回残差面板。
        target = Σ β·control + ε   →   返回 ε
    """
    if target not in panels:
        return None
    dates = panels[target].index
    for c in controls:
        if c in panels:
            dates = dates.intersection(panels[c].index)

    T = panels[target].loc[dates]
    Cs = [panels[c].loc[dates] for c in controls if c in panels]
    out = pd.DataFrame(index=T.index, columns=T.columns, dtype=float)

    for t in T.index:
        y = T.loc[t]
        X = pd.concat([c.loc[t] for c in Cs], axis=1)
        X.columns = [f"c{i}" for i in range(X.shape[1])]
        m = pd.concat([y.rename("y"), X], axis=1).dropna()
        if len(m) < min_coins:
            continue
        Y = m["y"].values
        XX = m[[c for c in m.columns if c != "y"]].values
        # 加截距
        XX = np.column_stack([np.ones(len(XX)), XX])
        try:
            beta, *_ = np.linalg.lstsq(XX, Y, rcond=None)
            resid = Y - XX @ beta
        except np.linalg.LinAlgError:
            continue
        out.loc[t, m.index] = resid
    return out


def ic_series(factor, fwd):
    return fl.cross_sectional_ic(factor, fwd)


def ic_stats(ic):
    ic = ic.dropna()
    if len(ic) < 100:
        return None
    mean, sd = ic.mean(), ic.std()
    if sd == 0:
        return None
    icir = mean / sd
    ac = max(min(ic.autocorr(1), 0.98), -0.98)
    n_eff = len(ic) * (1 - ac) / (1 + ac)
    return {"ic": mean, "icir": icir, "t_raw": icir * np.sqrt(len(ic)),
            "t_adj": icir * np.sqrt(max(n_eff, 1)), "n": len(ic), "ac": ac}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--horizon", type=int, default=7)
    ap.add_argument("--corr", type=float, default=0.7, help="去重相关阈值")
    args = ap.parse_args()

    print(f"  加载面板 {args.tf}…")
    panels, C = fl.build_panels(args.tf)
    F = fl.forward_returns(C, args.horizon)
    names = [n for n in CANDIDATES if n in panels]
    print(f"  候选因子 {len(names)} 个 · 币种 {C.shape[1]} · 期数 {C.shape[0]}")

    # ── 1. 相关矩阵 ──
    print()
    print("=" * 100)
    print("① 因子相关矩阵（时间平均的横截面 Spearman）")
    print("=" * 100)
    M = factor_correlation(panels, names)
    if M is not None:
        print("        " + "".join(f"{n[:11]:>13}" for n in names))
        for a in names:
            print(f"  {a:<8}" + "".join(f"{M.loc[a,b]:>13.2f}" for b in names))
        print()
        print("  高相关对（|ρ| > 阈值，视为同一因子）:")
        seen = set()
        for a, b in itertools.combinations(names, 2):
            if abs(M.loc[a, b]) >= args.corr:
                print(f"    {a:<16} {b:<16} ρ = {M.loc[a,b]:+.2f}")
                seen.add((a, b))
        if not seen:
            print("    （无）")

    # ── 2. 原始 IC ──
    print()
    print("=" * 100)
    print(f"② 原始 IC（前瞻 {args.horizon} 根）")
    print("=" * 100)
    raw = {}
    print(f"  {'因子':<18}{'IC':>10}{'ICIR':>9}{'t(朴素)':>11}{'t(自相关修正)':>15}{'判定':>10}")
    print("  " + "-" * 74)
    for n in names:
        st = ic_stats(ic_series(panels[n], F))
        if not st:
            continue
        raw[n] = st
        v = "✅" if abs(st["t_adj"]) > 2 else "—"
        print(f"  {n:<18}{st['ic']:>10.4f}{st['icir']:>9.2f}{st['t_raw']:>11.2f}"
              f"{st['t_adj']:>15.2f}{v:>10}")

    # ── 3. 正交化 ──
    print()
    print("=" * 100)
    print("③ 正交化：去掉其他因子能解释的部分，看是否还有增量信息")
    print("=" * 100)
    print(f"  {'因子':<18}{'原始IC':>10}{'正交后IC':>11}{'保留比例':>11}"
          f"{'正交后t':>11}{'增量判定':>12}")
    print("  " + "-" * 76)
    results = []
    for n in names:
        controls = [x for x in names if x != n]
        orth = orthogonalize(panels, n, controls)
        if orth is None:
            continue
        st = ic_stats(ic_series(orth, F))
        if not st:
            continue
        keep = st["ic"] / raw[n]["ic"] if raw[n]["ic"] != 0 else np.nan
        inc = "✅ 有增量" if abs(st["t_adj"]) > 2 else "❌ 被解释掉"
        results.append({"factor": n, "raw_ic": raw[n]["ic"], "orth_ic": st["ic"],
                        "keep": keep, "t_adj": st["t_adj"]})
        print(f"  {n:<18}{raw[n]['ic']:>10.4f}{st['ic']:>11.4f}{keep:>10.0%}"
              f"{st['t_adj']:>11.2f}{inc:>12}")
    print("  " + "-" * 76)

    # ── 4. 结论 ──
    print()
    print("=" * 100)
    print("④ 结论：哪些因子是独立的")
    print("=" * 100)
    inc_factors = [r for r in results if abs(r["t_adj"]) > 2]
    print(f"  通过正交化仍有增量的因子: {len(inc_factors)} / {len(results)}")
    for r in sorted(inc_factors, key=lambda x: -abs(x["t_adj"])):
        print(f"    {r['factor']:<18} 原始 IC {r['raw_ic']:+.4f} → 正交后 {r['orth_ic']:+.4f}"
              f"  (保留 {r['keep']:.0%}, t={r['t_adj']:+.2f})")

    explained = [r for r in results if abs(r["t_adj"]) <= 2]
    if explained:
        print(f"\n  被其他因子解释掉（冗余，应从因子池剔除）: {len(explained)} 个")
        for r in explained:
            print(f"    {r['factor']:<18} 原始 IC {r['raw_ic']:+.4f} → 正交后 {r['orth_ic']:+.4f}"
                  f"  (仅保留 {r['keep']:.0%})")

    return results


if __name__ == "__main__":
    main()
