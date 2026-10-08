#!/usr/bin/env python3
"""
走查参数优化（替代直接 hyperopt）

为什么不用 freqtrade hyperopt 直接跑：
    hyperopt 默认在【全样本】上最大化目标函数 —— 那就是在历史上找最优参数，
    样本外表现未知。本项目已经踩过 5 次类似的坑（前视/重叠/横截面相关/
    标签顺序/口径），所以参数优化必须走同一套纪律：

    ① 样本内（2020-2022）网格搜索选参数
    ② 样本外（2023-2026）只验证，不回头调参
    ③ 判据用【Calmar】（Sharpe 对零收益日敏感，已验证不可靠）
    ④ 报告参数平台宽度：如果只有孤立一点好，就是过拟合

网格：
    entry_period  ∈ {10, 15, 20, 30, 40, 55}
    exit_period   ∈ {5, 10, 20, 30}
    top_n         ∈ {3, 5, 8, 12}

用法:
    python wf_optimize.py                # 全网格走查
    python wf_optimize.py --stability    # 参数平台宽度分析
"""

import argparse
import itertools
import json
import os

import numpy as np
import pandas as pd

import walkforward as wf

OUT = "user_data/optimization_result.json"

INS = ("2020-01-01", "2023-01-01")
OOS = ("2023-01-01", None)

GRID = {
    "entry": [10, 15, 20, 30, 40, 55],
    "exit": [5, 10, 20, 30],
    "top_n": [3, 5, 8, 12],
}


def run_one(data, en, ex, top_n, start, end=None):
    r = wf.backtest(data, entry=en, exit_=ex, sizing="equal", top_n=top_n,
                    start=start)
    if r is None:
        return None
    if end:
        r = r[r.index < end]
    return wf.evaluate(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stability", action="store_true")
    args = ap.parse_args()

    syms = wf.discover()
    data = wf.load(syms)
    print(f"  币种 {len(data)} · 样本内 {INS[0]}~{INS[1]} · 样本外 {OOS[0]}~")

    combos = list(itertools.product(GRID["entry"], GRID["exit"], GRID["top_n"]))
    print(f"  网格组合 {len(combos)} 个")

    rows = []
    for en, ex, tn in combos:
        ei = run_one(data, en, ex, tn, INS[0], INS[1])
        eo = run_one(data, en, ex, tn, OOS[0])
        if not ei or not eo:
            continue
        rows.append({
            "entry": en, "exit": ex, "top_n": tn,
            "in_ann": round(ei["ann"], 2), "in_calmar": round(ei["calmar"], 2),
            "in_t": round(ei["t"], 2), "in_mdd": round(ei["mdd"], 2),
            "oos_ann": round(eo["ann"], 2), "oos_calmar": round(eo["calmar"], 2),
            "oos_t": round(eo["t"], 2), "oos_mdd": round(eo["mdd"], 2),
            "oos_vol_ret": round(eo["ann"] / eo["vol"], 2) if eo["vol"] else None,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        print("  无有效结果"); return

    print()
    print("=" * 108)
    print("① 样本内（2020-2022）按 Calmar 排序 —— 前 10 名")
    print("=" * 108)
    top_in = df.sort_values("in_calmar", ascending=False).head(10)
    print(f"  {'entry/exit':<12}{'top_n':>6}{'样本内年化':>11}{'样本内Calmar':>13}"
          f"{'样本外年化':>11}{'样本外Calmar':>13}{'样本外t':>9}{'样本外回撤':>11}")
    print("  " + "-" * 94)
    for _, r in top_in.iterrows():
        print(f"  {str(r['entry'])+'/'+str(r['exit']):<12}{r['top_n']:>6}"
              f"{r['in_ann']:>10.2f}%{r['in_calmar']:>13.2f}"
              f"{r['oos_ann']:>10.2f}%{r['oos_calmar']:>13.2f}"
              f"{r['oos_t']:>9.2f}{r['oos_mdd']:>10.2f}%")
    print("  " + "-" * 94)

    best = top_in.iloc[0]
    print(f"\n  样本内最优: entry={int(best['entry'])} exit={int(best['exit'])} "
          f"top_n={int(best['top_n'])}")
    print(f"    样本内 Calmar {best['in_calmar']} → 样本外 Calmar {best['oos_calmar']} "
          f"(t={best['oos_t']})")

    # 样本内最优在样本外的排名
    rank = int((df["oos_calmar"] > best["oos_calmar"]).sum()) + 1
    print(f"    它在【样本外】的 Calmar 排名: {rank} / {len(df)}")
    print(f"    → {'✅ 参数稳定' if rank <= len(df) * 0.25 else '⚠ 参数不稳定（样本内最优在样本外排名靠后）'}")

    print()
    print("=" * 108)
    print("② 样本外表现最好的 10 组（这才是能参考的）")
    print("=" * 108)
    top_oos = df.sort_values("oos_calmar", ascending=False).head(10)
    print(f"  {'entry/exit':<12}{'top_n':>6}{'样本外年化':>11}{'样本外Calmar':>13}"
          f"{'样本外t':>9}{'样本外回撤':>11}   样本内Calmar")
    print("  " + "-" * 88)
    for _, r in top_oos.iterrows():
        print(f"  {str(r['entry'])+'/'+str(r['exit']):<12}{r['top_n']:>6}"
              f"{r['oos_ann']:>10.2f}%{r['oos_calmar']:>13.2f}"
              f"{r['oos_t']:>9.2f}{r['oos_mdd']:>10.2f}%{r['in_calmar']:>13.2f}")
    print("  " + "-" * 88)

    print()
    print("=" * 108)
    print("③ 参数平台宽度（过拟合检验）")
    print("=" * 108)
    print(f"  样本外 Calmar 分布: 中位 {df['oos_calmar'].median():.2f}  "
          f"均值 {df['oos_calmar'].mean():.2f}  "
          f"最好 {df['oos_calmar'].max():.2f}  最差 {df['oos_calmar'].min():.2f}")
    good = (df["oos_calmar"] > 0.5).mean() * 100
    print(f"  样本外 Calmar > 0.5 的组合占比: {good:.0f}%  ({int((df['oos_calmar']>0.5).sum())}/{len(df)})")
    print(f"  样本外 t > 2 的组合占比: {(df['oos_t']>2).mean()*100:.0f}%  "
          f"({int((df['oos_t']>2).sum())}/{len(df)})")

    # 各维度的边际影响
    print()
    print("  各维度边际影响（样本外 Calmar 中位数）:")
    print(f"    {'entry':<10}" + "".join(f"{e:>8}" for e in GRID["entry"]))
    for ex in GRID["exit"]:
        sub = df[df["exit"] == ex]
        line = f"    exit={ex:<5}" + "".join(
            f"{sub[sub['entry']==e]['oos_calmar'].median():>8.2f}" for e in GRID["entry"])
        print(line)
    print()
    print(f"    {'top_n':<10}" + "".join(f"{t:>8}" for t in GRID["top_n"]))
    line = f"    {'':<10}" + "".join(
        f"{df[df['top_n']==t]['oos_calmar'].median():>8.2f}" for t in GRID["top_n"])
    print(line)

    # 保存
    payload = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "ins": f"{INS[0]}~{INS[1]}", "oos": f"{OOS[0]}~",
        "grid": GRID, "combos": len(df),
        "best_in": {k: (int(best[k]) if k != "in_calmar" else best[k])
                    for k in ["entry", "exit", "top_n", "in_calmar", "oos_calmar", "oos_t"]},
        "in_rank_of_best_in_oos": rank,
        "oos_summary": {
            "calmar_median": round(df["oos_calmar"].median(), 2),
            "calmar_best": round(df["oos_calmar"].max(), 2),
            "pct_t_gt2": round((df["oos_t"] > 2).mean() * 100, 1),
            "pct_calmar_gt05": round((df["oos_calmar"] > 0.5).mean() * 100, 1),
        },
        "stability": {
            "by_entry_exit": {
                f"{ex}": {str(e): round(df[(df['exit'] == ex) & (df['entry'] == e)]['oos_calmar'].median(), 2)
                          for e in GRID["entry"]} for ex in GRID["exit"]
            },
            "by_top_n": {str(t): round(df[df["top_n"] == t]["oos_calmar"].median(), 2)
                         for t in GRID["top_n"]},
        },
        "top_oos": top_oos[["entry", "exit", "top_n", "oos_ann", "oos_calmar",
                            "oos_t", "oos_mdd", "in_calmar"]].to_dict("records"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n  ✅ {OUT}")


if __name__ == "__main__":
    main()
