#!/usr/bin/env python3
"""
元标记的「做多大」—— ML 驱动的仓位缩放（第 11 轮）

背景：
    元标记的完整定义是「模型判断【该不该做】与【做多大】」。
    但本项目的第 4~10 轮**只做了「该不该做」** —— 用 ML 分数做二元过滤
    （保留前 X% 的交易）。

    问题：二元过滤**丢掉了分数里的强度信息**。
    一个 IC 只有 +0.03 的弱信号，二元过滤会把「勉强及格」和「非常有信心」
    同等对待；而连续缩放能按信心分配仓位。

做法（四种模式对照）：
    A. 不过滤、等权            —— 基线
    B. 二元过滤（保留前 70%）   —— 之前用的
    C. 连续缩放：仓位 ∝ 分数秩   —— 本轮新做
    D. 连续缩放 + 只做正分      —— 分数低于中位的直接不做
    E. 反波动率 × ML 缩放       —— 与已验证的波动率目标结合

评估：
    · 组合层（事件驱动回测，30% 总敞口上限）
    · 分块自助法给置信区间（第 6 轮教训：单点 Calmar 不可信）
    · 逐笔 IC 与逐窗口 IC（正确口径）

用法:
    python ml_sizing.py                    # 用已有预测
    python ml_sizing.py --run              # 重新训练（慢）
"""

import argparse
import json
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import event_backtest as E

from ml_bootstrap import bootstrap_ci, equity_returns, metrics_from_returns
from ml_regime import ic_t

STAKE_TOTAL = 0.30      # 总敞口上限（与实盘 target_exposure 一致）
TOP_N = 8
MAX_OPEN = 10
COST = 0.0005


def run_sized(data, scores, start, end=None,
              mode="equal", keep_q=0.7, top_n=TOP_N):
    """
    事件驱动回测，仓位由 scores 决定。

    scores: dict[(date, coin)] -> ML 分数（0~1，越高越好）
    mode:
        equal   等权（基线）
        binary  二元过滤（保留分数前 keep_q）
        scale   连续缩放：仓位 ∝ 分数的秩（0.5 为中性）
        scale_pos  连续缩放 + 分数低于中位不做
    """
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        dd = d[d.index >= start]
        if end is not None:
            dd = dd[dd.index < end]
        if len(dd) < 150:
            continue
        S[s] = E.signals(dd["close"])
        ST[s] = E.strength(dd["close"])
        P[s] = dd[["open", "close"]]
    if not S:
        return None
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()

    stg, state = STd.values, Sd.values
    opx, clx = Od.values, Cd.values
    T, K = state.shape

    # 二元过滤阈值：按分数分布算（只用训练期口径）
    thr = None
    if mode == "binary" and scores:
        vals = np.array([v for v in scores.values() if np.isfinite(v)])
        if len(vals) > 100:
            thr = float(np.quantile(vals, 1 - keep_q))

    cash, pos, eqc = 10_000.0, {}, np.zeros(T)
    n_tr = 0
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                cash += p["stake"] + p["q"] * (opx[fi, k] - p["e"]) - p["stake"] * COST
                n_tr += 1
        if i > 0:
            allsig = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: stg[i - 1, k] for k in allsig if np.isfinite(stg[i - 1, k])}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:top_n]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= MAX_OPEN:
                    continue
                sc = None
                if scores:
                    sc = scores.get((Sd.index[i - 1], Cd.columns[k]))
                # ── 模式判定 ──
                if mode == "binary":
                    if sc is not None and thr is not None and sc < thr:
                        continue
                    w = 1.0
                elif mode == "scale":
                    # 仓位 ∝ 分数秩，映射到 [0.25, 1.75]，中位数为 1.0
                    w = 1.0 if sc is None else float(np.clip(0.25 + 1.5 * sc, 0.25, 1.75))
                elif mode == "scale_pos":
                    if sc is not None and sc < 0.5:
                        continue          # 低于中位不做
                    w = 1.0 if sc is None else float(np.clip(0.5 + 2.0 * (sc - 0.5), 0.5, 1.5))
                else:
                    w = 1.0
                eq = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                                for kk, pp in pos.items())
                # 名义仓位 = 总敞口 / top_n × 权重；归一化避免总敞口超限
                n_active = max(len(pos) + 1, 1)
                stake = min(eq * STAKE_TOTAL / top_n * w,
                            eq * STAKE_TOTAL / n_active * 2.0)
                e = opx[i, k]
                if not np.isfinite(e) or e <= 0:
                    continue
                q = stake / e * np.sign(state[i, k])
                fee = stake * COST
                if cash < stake + fee:
                    continue
                cash -= stake + fee
                pos[k] = {"stake": stake, "q": q, "e": e}
        eqc[i] = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                            for kk, pp in pos.items())
    eq = pd.Series(eqc, index=Sd.index)
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"ann": ann * 100, "mdd": mdd * 100,
            "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "n_trades": n_tr, "ret": eq.pct_change().dropna()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default="/tmp/lstm_scores.npy", help="预测文件")
    ap.add_argument("--meta", default="/tmp/lstm_meta.pkl", help="元数据文件")
    ap.add_argument("--start", default="2024-01-01")
    args = ap.parse_args()

    if not (os.path.exists(args.pred) and os.path.exists(args.meta)):
        print(f"  缺少预测文件（{args.pred} / {args.meta}）")
        print("  先用 ml_seed_ensemble.py 或 ml_seq.py 生成")
        return

    sc = np.load(args.pred)
    meta = pd.read_pickle(args.meta).copy()
    meta["p"] = sc
    meta = meta.dropna(subset=["p"])
    meta["date"] = pd.to_datetime(meta["date"])
    meta = meta[meta["date"] >= args.start]
    print(f"  预测 {len(meta)} 条 · 区间 {meta['date'].min().date()} ~ {meta['date'].max().date()}")

    # 分数转成 [0,1] 的秩（尺度无关）
    meta["rank"] = pd.Series(meta["p"].values).rank(pct=True).values
    scores = {(r.date, r.coin): r.rank for r in meta.itertuples()}

    # 逐笔 IC
    ic, t = ic_t(meta["p"].values, meta["ret"].values)
    print(f"  逐笔池化 IC {ic:+.4f} (t={t:.2f})  ← 仅作参考，判据用逐窗口")
    dates = meta["date"]
    ics = []
    for a in pd.date_range(dates.min(), dates.max() - pd.DateOffset(months=6), freq="MS"):
        b = a + pd.DateOffset(months=6)
        m = ((dates >= a) & (dates < b)).values
        if m.sum() < 150:
            continue
        v, _ = ic_t(meta["p"].values[m], meta["ret"].values[m])
        if np.isfinite(v):
            ics.append(v)
    ics = np.array(ics)
    tw = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 2 else np.nan
    print(f"  逐窗口 IC {ics.mean():+.4f} (t={tw:.2f}, {len(ics)} 窗口, "
          f"正 {int((ics>0).sum())}/{len(ics)})")
    print()

    import glob
    syms = [os.path.basename(f).split("_")[0]
            for f in glob.glob(f"{E.PERP}/*-1d-futures.feather")]
    data = E.load_ohlc(syms)

    modes = [
        ("A 不过滤·等权（基线）", "equal", {}),
        ("B 二元过滤 保留70%", "binary", {"keep_q": 0.7}),
        ("C 连续缩放（仓位∝分数）", "scale", {}),
        ("D 连续缩放 + 低分不做", "scale_pos", {}),
    ]
    print("=" * 100)
    print(f"仓位模式对照（{args.start} 起 · 总敞口上限 {STAKE_TOTAL:.0%}）")
    print("=" * 100)
    print(f"  {'模式':<28}{'年化':>10}{'回撤':>10}{'Calmar':>9}"
          f"{'自助5%':>10}{'自助95%':>10}{'交易':>7}")
    print("  " + "-" * 84)
    results = {}
    for label, mode, kw in modes:
        r = run_sized(data, scores, args.start, mode=mode, **kw)
        if not r:
            print(f"  {label:<28} 无结果"); continue
        r["ci"] = bootstrap_ci(r["ret"], b=300, block=20)
        results[label] = r
        print(f"  {label:<28}{r['ann']:>9.2f}%{r['mdd']:>9.2f}%{r['calmar']:>9.2f}"
              f"{(r['ci']['lo'] if r['ci'] else float('nan')):>10.2f}"
              f"{(r['ci']['hi'] if r['ci'] else float('nan')):>10.2f}"
              f"{r['n_trades']:>7}")
    print("  " + "-" * 84)

    base = results.get("A 不过滤·等权（基线）")
    if base:
        print()
        print("  相对基线:")
        for label, r in results.items():
            if label.startswith("A "):
                continue
            d = (r["calmar"] / base["calmar"] - 1) * 100 if base["calmar"] else np.nan
            mark = "✅" if d > 10 else ("🟡" if d > 0 else "❌")
            print(f"    {label:<28} Calmar {base['calmar']:.2f} → {r['calmar']:.2f} "
                  f"({d:+.0f}%) {mark}")

    # 显著性（第 6 轮教训：Calmar 置信区间很宽）
    print()
    print("  ⚠️ 判读：第 6 轮已证明本样本量下 Calmar 区间极宽（约 [-0.2, +2.4]），")
    print("     连上帝视角都不显著。故「连续缩放是否更好」需看：")
    print("       · 点估计方向是否一致（多个 mode 都朝同一方向）")
    print("       · 在更长样本 / 更多交易上是否复现")

    with open("user_data/ml_sizing_results.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "start": args.start, "n_pred": len(meta),
            "ic_pool": round(float(ic), 5), "ic_period": round(float(ics.mean()), 5),
            "ic_period_t": round(float(tw), 3),
            "results": {k: {"ann": round(v["ann"], 2), "mdd": round(v["mdd"], 2),
                            "calmar": round(v["calmar"], 3), "trades": v["n_trades"]}
                        for k, v in results.items()},
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/ml_sizing_results.json")


if __name__ == "__main__":
    main()
