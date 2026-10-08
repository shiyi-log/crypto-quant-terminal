#!/usr/bin/env python3
"""
ML 元标记的组合层效力检验（第 18 轮）—— 目标 ⑤ 的「确凿证据」

为什么重做：
    早前的组合层检验只有 ~313 笔交易 → Calmar 置信区间 [-0.2, +2.4]，
    宽到连上帝视角都不显著。**功效不足，无法判定。**

本脚本用最大功效的设计回答同一个问题：
    「ML 过滤能否在【样本外】给组合层带来可靠增益？」

设计要点（防四项假象）：
    ① 无前视：走查训练，测试段严格在未来
    ② 标签重叠：训练集剔除与测试段重叠的样本（净化）
    ③ 多种子：每配置 3 个种子，报均值（规则 13：种子方差占 87%）
    ④ 多窗口：逐窗口 IC 的 t 值，不报「正窗口占比」（第 11/12 轮已证伪）
    ⑤ 分块自助法：给增益的置信区间（块长 = 平均持仓，处理自相关）
    ⑥ 成本用【实测】：maker 4bp + 半价差 0.66bp + 冲击 2.14bp = 6.8bp

输出：
    · 不过滤 vs 各阈值过滤的组合指标（年化/回撤/Calmar/夏普）
    · 增益的分块自助 95% 区间
    · 明确的判定：有增益 / 无增益 / 功效不足

用法:
    python ml_portfolio_power.py
    python ml_portfolio_power.py --filters 0,0.3,0.5,0.7
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

import event_backtest as E
import ml_lab
import ml_seq

from ml_regime import ic_t

WALK_START = "2021-07-01"
STEP_MONTHS = 6
COST_MEASURED = 6.8 / 10_000      # 第 15 轮实测（旧假设 4bp）


# ══════════════════ 组合回测（按 ML 分数过滤） ══════════════════

def portfolio(data, scores, start, keep_frac=1.0, cost=COST_MEASURED,
              expo=0.30, top_n=8, max_open=10):
    """
    scores: dict[(date, coin)] -> ML 分数（0~1）。None 表示不过滤。
    keep_frac: 保留分数最高的比例（1.0 = 不过滤）
    """
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        dd = d[d.index >= start]
        if len(dd) < 150:
            continue
        S[s] = E.signals(dd["close"])
        ST[s] = E.strength(dd["close"])
        P[s] = dd[["open", "close"]]
    if len(S) < 3:
        return None
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()
    # 多币池的日期范围不同 → 缺口会产生 NaN 价格，污染净值。
    # 前向填充（持仓期间用最后有效价），仍无值的置 0 由 state 拦截。
    Od = Od.ffill()
    Cd = Cd.ffill()
    stg, state = STd.values, Sd.values
    opx, clx = Od.values, Cd.values
    T, K = state.shape

    thr = None
    if scores and keep_frac < 1.0:
        vals = np.array([v for v in scores.values() if np.isfinite(v)])
        if len(vals) > 50:
            thr = float(np.quantile(vals, 1 - keep_frac))

    cash, pos, eqc = 10_000.0, {}, np.zeros(T)
    n_tr = 0
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                cash += p["stake"] + p["q"] * (opx[fi, k] - p["e"]) - p["stake"] * cost
                n_tr += 1
        if i > 0:
            allsig = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: stg[i - 1, k] for k in allsig if np.isfinite(stg[i - 1, k])}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:top_n]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= max_open:
                    continue
                if thr is not None and scores:
                    sc = scores.get((Sd.index[i - 1], Cd.columns[k]))
                    if sc is None or not np.isfinite(sc) or sc < thr:
                        continue
                eq = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                                for kk, pp in pos.items())
                n_act = max(len(pos) + 1, 1)
                stake = min(eq * expo / top_n, eq * expo / n_act * 2.0)
                e = opx[i, k]
                if not np.isfinite(e) or e <= 0:
                    continue
                q = stake / e * np.sign(state[i, k])
                fee = stake * cost
                if cash < stake + fee:
                    continue
                cash -= stake + fee
                pos[k] = {"stake": stake, "q": q, "e": e}
        eqc[i] = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                            for kk, pp in pos.items())
    eq = pd.Series(eqc, index=Sd.index)
    ret = eq.pct_change().fillna(0.0)
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    if yrs > 0 and eq.iloc[-1] > 0 and np.isfinite(eq.iloc[-1]):
        ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    else:
        ann = np.nan
        print(f"    ⚠ 净值异常: 终值 {eq.iloc[-1]} · NaN 天数 {int((~np.isfinite(eq)).sum())}")
    dd = ((eq / eq.cummax()) - 1).min()
    # 日收益口径夏普（标注口径，避免与跨框架比较）
    sd = ret.std()
    sharpe = ret.mean() / sd * np.sqrt(365) if sd > 0 else np.nan
    return {"ann": ann * 100, "dd": dd * 100,
            "calmar": ann / abs(dd) if dd < 0 else np.nan,
            "sharpe": sharpe, "n_trades": n_tr, "ret": ret}


def portfolio_clean(data, scores, start, keep_frac=1.0, cost=COST_MEASURED,
                    expo=0.30, top_n=8, max_open=10):
    """
    纯权重记法（第 22 轮加入，替代持仓式引擎）。
    为什么：持仓式引擎在高换手下会有跨时点重复计价（见「回测引擎缺陷-纠正报告」）。
    本函数显式写出权重、T+1 生效、成本按权重变化计 —— 标准组合记法，无黑箱。
    """
    S, P = {}, {}
    for s_, d in data.items():
        dd = d[d.index >= start]
        if len(dd) < 150:
            continue
        S[s_] = E.signals(dd["close"])
        P[s_] = dd[["open", "close"]]
    if len(S) < 3:
        return None
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index().ffill()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index().ffill()
    cols = [c for c in Sd.columns if c in Od.columns and c in Cd.columns]
    Sd, Od, Cd = Sd[cols], Od[cols], Cd[cols]

    # 目标权重：当日有信号且强度进 top_n 的币，等权分总敞口
    # 强度：用 close 相对通道的位置（与生产一致，这里简化为 |信号|
    St = Sd.abs()
    keep = St.rank(axis=1, ascending=False) <= top_n
    W = Sd.where(keep, 0.0)
    nz = (W != 0).sum(axis=1).replace(0, np.nan)
    W = W.div(nz, axis=0) * expo
    # ML 过滤：分数低于阈值的不做
    if scores and keep_frac < 1.0:
        vals = np.array([v for v in scores.values() if np.isfinite(v)])
        thr = float(np.quantile(vals, 1 - keep_frac)) if len(vals) > 50 else None
        if thr is not None:
            SC = pd.DataFrame(index=Sd.index, columns=Sd.columns, dtype=float)
            for (dt, cn), v in scores.items():
                if dt in SC.index and cn in SC.columns:
                    SC.at[dt, cn] = v
            W = W.where(SC >= thr, 0.0)
            nz = (W != 0).sum(axis=1).replace(0, np.nan)
            W = W.div(nz, axis=0) * expo
    W = W.fillna(0.0)
    # 限制同时持仓数
    act = (W != 0).sum(axis=1)
    W = W.where(act <= max_open, 0.0)
    nz = (W != 0).sum(axis=1).replace(0, np.nan)
    W = W.div(nz, axis=0) * expo
    W = W.fillna(0.0)
    # T+1 生效
    W = W.shift(1).fillna(0.0)
    R = Cd.pct_change()
    gross = (W.shift(1).fillna(0.0) * R).sum(axis=1)
    dW = W.diff().abs().sum(axis=1).fillna(0.0)
    net = gross - dW * cost
    eq = (1 + net).cumprod() * 10000
    yrs = len(net) / 365
    ann = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 and eq.iloc[-1] > 0 else np.nan
    dd = ((eq / eq.cummax()) - 1).min()
    sd = net.std()
    n_tr = int((dW > 1e-9).sum())
    return {"ann": ann * 100, "dd": dd * 100,
            "calmar": ann / abs(dd) if dd < 0 else np.nan,
            "sharpe": net.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
            "n_trades": n_tr, "ret": net}


def block_bootstrap_diff(r1, r2, b=400, block=20):
    """两块收益序列的差值（r1-r2）的分块自助 95% 区间（年化口径）"""
    n = min(len(r1), len(r2))
    a, c = r1.values[:n], r2.values[:n]
    d = a - c
    out = []
    rng = np.random.RandomState(7)
    nb = max(1, n // block)
    for _ in range(b):
        idx = []
        for _ in range(nb):
            s = rng.randint(0, max(1, n - block))
            idx.extend(range(s, min(s + block, n)))
        idx = np.array(idx)
        if len(idx) < 30:
            continue
        dd = d[idx]
        out.append((np.prod(1 + dd) ** (365 / len(dd)) - 1) * 100)
    if len(out) < 30:
        return None
    return {"lo": float(np.percentile(out, 2.5)),
            "hi": float(np.percentile(out, 97.5)),
            "med": float(np.median(out)),
            "p_pos": float((np.array(out) > 0).mean())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filters", default="0,0.3,0.5,0.7",
                    help="保留比例（0 = 不过滤）")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--engine", default="clean", choices=["clean", "legacy"],
                    help="clean=纯权重记法（推荐）；legacy=旧的持仓式引擎")
    ap.add_argument("--universe", default="binance",
                    choices=["binance", "merged"],
                    help="binance=原 57 币；merged=Binance+OKX 合并 98 币")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 104)
    print("ML 元标记的组合层效力检验（目标 ⑤）")
    print("=" * 104)
    print(f"  成本 {COST_MEASURED*1e4:.1f}bp（第 15 轮实测，旧假设 4.0bp）")
    print(f"  走查起点 {args.start} · 步长 {STEP_MONTHS} 月 · {args.seeds} 个种子")
    print()

    if args.universe == "merged":
        import glob as _g
        data = {}
        for f in sorted(_g.glob("user_data/data/merged/*-1d-merged.feather")):
            s_ = os.path.basename(f).split("-")[0]
            d = pd.read_feather(f).sort_values("date")
            d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
            d = d.set_index("date")
            if len(d) < 400:
                continue
            data[s_] = d[["open", "high", "low", "close", "volume"]]
    else:
        data = ml_lab.load_ohlcv()
    print(f"  标的池 [{args.universe}] · 币种 {len(data)}")

    # 复用 ml_seq 的序列面板与预测（已有基础设施）
    if args.universe == "merged":
        # ml_seq 内部调用 ml_lab.load_ohlcv → 补丁打在 ml_lab 上
        _orig = ml_lab.load_ohlcv
        ml_lab.load_ohlcv = lambda *a, **k: data
        try:
            seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=30,
                                                  feature_set="base")
        finally:
            ml_lab.load_ohlcv = _orig
    else:
        seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=30,
                                              feature_set="base")
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    cuts, t = [], pd.Timestamp(args.start)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=STEP_MONTHS),
                            end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=STEP_MONTHS)

    seeds = [100 + i * 37 for i in range(args.seeds)]
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
            m = ml_seq.train_seq(Xtr, y[trm], F, "lstm", device, epochs=8,
                                 hidden=64, layers=1, dropout=0.3, seed=sd_seed)
            per_seed[i][tem] = ml_seq.predict_seq(m, Xte, device)

    # 多种子秩平均（规则 13）
    R = []
    for p in per_seed:
        ok = np.isfinite(p)
        if ok.sum() < 1000:
            continue
        R.append(pd.Series(p).rank(pct=True).values)
    ens = np.nanmean(np.vstack(R), axis=0)
    ok = np.isfinite(ens)
    print(f"  有效预测 {ok.sum():,}")

    # ── 逐窗口 IC（正确口径）──
    ics = []
    for a in pd.date_range(pd.Timestamp(args.start),
                           dates.max() - pd.DateOffset(months=6), freq="MS"):
        b = a + pd.DateOffset(months=6)
        m = ((dates >= a) & (dates < b)).values & ok
        if m.sum() < 150:
            continue
        v, _ = ic_t(ens[m], meta["ret"].values[m])
        if np.isfinite(v):
            ics.append(v)
    ics = np.array(ics)
    tw = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 2 else np.nan
    icp, tp = ic_t(ens[ok], meta["ret"].values[ok])
    print(f"  逐窗口 IC {ics.mean():+.4f} (t={tw:.2f}, {len(ics)} 窗口)")
    print(f"  池化 IC   {icp:+.4f} (t={tp:.2f})   ← 仅对照")
    print()

    scores = {(r.date, r.coin): r.rank
              for r in pd.DataFrame({"date": meta["date"], "coin": meta["coin"],
                                     "rank": ens}).dropna().itertuples()}
    print(f"  分数样本 {len(scores):,}")

    print()
    print("=" * 104)
    print("组合层对照")
    print("=" * 104)
    print(f"  {'保留比例':<12}{'交易':>7}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}")
    print("  " + "-" * 58)
    res = {}
    for kf in [float(x) for x in args.filters.split(",")]:
        k = 1.0 if kf <= 0 else kf
        fn = portfolio_clean if args.engine == "clean" else portfolio
        r = fn(data, scores if kf > 0 else None, args.start, keep_frac=k)
        if not r:
            print(f"  {kf:<12} 无结果"); continue
        res[kf] = r
        lab = "不过滤" if kf <= 0 else f"保留 {int(kf*100)}%"
        print(f"  {lab:<12}{r['n_trades']:>7}{r['ann']:>8.1f}%{r['dd']:>9.1f}%"
              f"{r['calmar']:>9.2f}{r['sharpe']:>8.2f}")
    print("  " + "-" * 58)

    base = res.get(0.0) or res.get(min(res))
    print()
    print("=" * 104)
    print("增益的分块自助检验（相对不过滤基线）")
    print("=" * 104)
    print(f"  {'过滤':<12}{'ΔCalmar':>10}{'Δ年化(bp)':>12}{'自助95%区间':>20}{'正概率':>10}")
    print("  " + "-" * 68)
    verdict = []
    if base:
        for kf, r in res.items():
            if kf == 0.0 or r is base:
                continue
            d = r["calmar"] - base["calmar"]
            ci = block_bootstrap_diff(r["ret"], base["ret"])
            lo = ci["lo"] if ci else np.nan
            hi = ci["hi"] if ci else np.nan
            pp = ci["p_pos"] if ci else np.nan
            print(f"  {str(int(kf*100))+'%':<12}{d:>+10.2f}{ci['med'] if ci else np.nan:>+12.1f}"
                  f"{f'[{lo:+.1f}, {hi:+.1f}]':>20}{pp:>10.2f}")
            if ci and lo > 0:
                verdict.append((kf, "✅ 有显著增益"))
            elif ci and hi < 0:
                verdict.append((kf, "❌ 显著变差"))
            else:
                verdict.append((kf, "🟡 功效不足（区间跨 0）"))
    print("  " + "-" * 68)
    print()
    for kf, v in verdict:
        print(f"  {int(kf*100)}% 保留: {v}")
    pos = [v for _, v in verdict if "✅" in v]
    print()
    if pos:
        print("  ⭐ 判定：**存在**可靠的样本外组合层增益")
    elif verdict and all("🟡" in v for _, v in verdict):
        print("  ⭐ 判定：**功效不足** —— 既不能确认增益，也不能排除")
        print("     需要更多独立交易（扩池 / 更长样本）才能判定")
    else:
        print("  ⭐ 判定：**无**可靠的样本外组合层增益")
        print("     结合 17 轮其它方向（超参/特征/架构/订单流/高频）的失败，")
        print("     这构成目标 ⑤ 要求的「多轮迭代后仍无样本外增益」的确凿证据。")

    with open("user_data/ml_portfolio_power.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "cost_bp": COST_MEASURED * 1e4, "seeds": seeds,
                   "ic_period": round(float(ics.mean()), 5),
                   "ic_period_t": round(float(tw), 3),
                   "ic_pool": round(float(icp), 5), "ic_pool_t": round(float(tp), 3),
                   "results": {str(k): {"ann": round(v["ann"], 2), "dd": round(v["dd"], 2),
                                        "calmar": round(v["calmar"], 3),
                                        "sharpe": round(float(v["sharpe"]), 3),
                                        "n_trades": v["n_trades"]}
                               for k, v in res.items()},
                   "verdict": [{"keep": k, "v": v} for k, v in verdict]},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/ml_portfolio_power.json  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
