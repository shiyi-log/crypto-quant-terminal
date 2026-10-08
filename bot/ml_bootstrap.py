#!/usr/bin/env python3
"""
评估的统计功效 —— 为组合层指标加自助法置信区间

为什么必须做：
    第 6 轮发现：Calmar 增益在过滤器微小变化下剧烈跳动
    （0% → +33% → -46% → +76% → -71%）。
    只有约 313 笔交易，少数大行情主导，单点 Calmar 完全不可靠。

    没有置信区间就无法区分「真实的策略改进」与「抽样噪声」——
    这会导致两个方向的错误：
      · 把噪声当成改进 → 上线一个没用的过滤器
      · 把真实改进当成噪声 → 放弃一个有效的方向

做法：分块自助法（block bootstrap）
    时间序列有自相关，不能逐笔独立重采样。
    按【时间块】重采样以保留自相关结构。

用法:
    python ml_bootstrap.py
"""

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_oos as oos

B = 400          # 自助法次数
BLOCK = 20       # 时间块长度（天）


def equity_returns(df_test, prob, thresh, start=None):
    """跑组合回测，返回日度权益收益序列"""
    import event_backtest as E
    coins = sorted(set(df_test.index.get_level_values("coin")))
    data = E.load_ohlc(coins)
    st = start or str(df_test.index.get_level_values("date").min().date())
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        dd = d[d.index >= st]
        if len(dd) < 120:
            continue
        S[s] = E.signals(dd["close"])
        ST[s] = E.strength(dd["close"])
        P[s] = dd[["open", "close"]]
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()

    mp = {}
    tmp = df_test.reset_index()[["date", "coin"]].copy()
    tmp["p"] = prob
    for r in tmp.itertuples():
        mp[(r.date, r.coin)] = r.p

    stg, state, px, opx = STd.values, Sd.values, Cd.values, Od.values
    T, K = state.shape
    cash, pos, eqc = 10_000.0, {}, np.zeros(T)
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                cash += p["stake"] + p["q"] * (opx[fi, k] - p["e"]) - p["stake"] * 0.0005
        if i > 0:
            allsig = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: stg[i - 1, k] for k in allsig if np.isfinite(stg[i - 1, k])}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:8]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= 10:
                    continue
                if thresh is not None:
                    pv = mp.get((Sd.index[i - 1], Cd.columns[k]))
                    if pv is not None and pv < thresh:
                        continue
                eq = cash + sum(pp["stake"] + pp["q"] * (px[i, kk] - pp["e"])
                                for kk, pp in pos.items())
                stake = eq * 0.30 / 8
                e = opx[i, k]
                if not np.isfinite(e) or e <= 0:
                    continue
                q = stake / e * np.sign(state[i, k])
                fee = stake * 0.0005
                if cash < stake + fee:
                    continue
                cash -= stake + fee
                pos[k] = {"stake": stake, "q": q, "e": e}
        eqc[i] = cash + sum(pp["stake"] + pp["q"] * (px[i, kk] - pp["e"])
                            for kk, pp in pos.items())
    eq = pd.Series(eqc, index=Sd.index)
    return eq.pct_change().fillna(0.0), eq


def metrics_from_returns(r):
    eq = (1 + r).cumprod()
    yrs = len(r) / 365
    if eq.iloc[-1] <= 0 or yrs <= 0:
        return np.nan, np.nan, np.nan
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    mdd = ((eq / eq.cummax()) - 1).min()
    return ann * 100, mdd * 100, (ann / abs(mdd) if mdd < 0 else np.nan)


def bootstrap_ci(r, b=B, block=BLOCK, seed=0):
    """分块自助法"""
    rng = np.random.default_rng(seed)
    x = r.values
    n = len(x)
    nb = int(np.ceil(n / block))
    out = []
    for _ in range(b):
        starts = rng.integers(0, max(n - block, 1), size=nb)
        idx = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]
        ann, mdd, cal = metrics_from_returns(pd.Series(x[idx]))
        if np.isfinite(cal):
            out.append(cal)
    out = np.array(out)
    if len(out) < 20:
        return None
    return {"med": float(np.median(out)), "lo": float(np.percentile(out, 5)),
            "hi": float(np.percentile(out, 95)), "n": len(out)}


def main():
    df, feat, emb = oos.build(dense=False)
    dts = pd.to_datetime(df.index.get_level_values("date"))
    te = df[dts >= oos.TRAIN_END]
    y = te["label"].values.astype(float)
    rng = np.random.default_rng(7)

    print("=" * 100)
    print("组合层指标的置信区间（分块自助法，B=%d，块长=%d 天）" % (B, BLOCK))
    print("=" * 100)
    print(f"  测试期 {te.index.get_level_values('date').min().date()} ~ "
          f"{te.index.get_level_values('date').max().date()} · {len(te)} 个事件")
    print()

    cases = [("不过滤", None, None), ("上帝视角(完美)", y + rng.normal(0, .001, len(y)), None),
             ("α=0.7 插值", 0.3 * y + 0.7 * rng.uniform(0, 1, len(y)), None),
             ("α=0.9 插值", 0.1 * y + 0.9 * rng.uniform(0, 1, len(y)), None),
             ("随机(α=1)", rng.uniform(0, 1, len(y)), None)]

    print(f"  {'过滤器':<20}{'年化':>9}{'回撤':>9}{'Calmar':>9}"
          f"{'自助中位':>10}{'5%分位':>10}{'95%分位':>10}{'是否显著':>10}")
    print("  " + "-" * 86)
    results = {}
    for label, prob, _ in cases:
        if prob is None:
            r, _ = equity_returns(te, np.full(len(te), .5), None)
        else:
            thr = np.quantile(prob, 0.5)
            r, _ = equity_returns(te, prob, thr)
        ann, mdd, cal = metrics_from_returns(r)
        ci = bootstrap_ci(r)
        results[label] = (cal, ci)
        sig = "—"
        if ci:
            sig = "✅ 显著" if ci["lo"] > 0.54 else ("🟡 边缘" if ci["hi"] > 0.54 else "❌ 不显著")
        print(f"  {label:<20}{ann:>8.2f}%{mdd:>8.2f}%{cal:>9.2f}"
              f"{(ci['med'] if ci else float('nan')):>10.2f}"
              f"{(ci['lo'] if ci else float('nan')):>10.2f}"
              f"{(ci['hi'] if ci else float('nan')):>10.2f}{sig:>10}")
    print("  " + "-" * 86)

    base = results.get("不过滤", (np.nan, None))[0]
    print()
    print("  ⭐ 判读标准：某个过滤器的 5% 分位 > 基线的 95% 分位，才算显著改善")
    if "不过滤" in results and results["不过滤"][1]:
        b_hi = results["不过滤"][1]["hi"]
        print(f"     基线的 95% 分位上界: {b_hi:.2f}")
        for k, (cal, ci) in results.items():
            if k == "不过滤" or not ci:
                continue
            if ci["lo"] > b_hi:
                print(f"     ✅ {k}: 5%分位 {ci['lo']:.2f} > 基线95% {b_hi:.2f} —— 显著")
            else:
                print(f"     ❌ {k}: 5%分位 {ci['lo']:.2f} ≤ 基线95% {b_hi:.2f} —— 不显著")
    print()
    print("  结论：")
    print("     若连【上帝视角】都不显著，说明当前样本量下无法用 Calmar 判定过滤器好坏，")
    print("     必须改用交易数量更多、统计功效更高的评估口径（如逐笔收益的 t 检验）。")


if __name__ == "__main__":
    main()
