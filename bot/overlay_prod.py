#!/usr/bin/env python3
"""
生产策略 + 横截面动量的叠加验证（第 21 轮）

第 20 轮发现：V（ATR 通道突破）+ M（横截面动量）组合有 20/26 参数稳健的增益。
但那用的是【简化重实现】的引擎，而生产策略实测 Calmar 1.53，简化版只有 0.31
—— 两者不是一回事。

本轮用【生产策略的真实净值】做叠加测试，回答：
    在确实在跑的那个策略上，叠加横截面动量能不能降低回撤、提升 Calmar？

生产策略净值来源：
    event_backtest.run() —— 已验证与 Freqtrade 在信号层相关性 1.0000
    （第 8 轮修复了「出场早一根 K 线」的 3.2 倍虚高问题）

同时做两件防伪影的事：
    ① M（横截面动量）自身的参数敏感性扫描 —— 它是新策略，可能过拟合
    ② 叠加增益对 M 参数是否稳健

用法:
    python overlay_prod.py
    python overlay_prod.py --weight 0.5
"""

import argparse
import glob
import json
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import event_backtest as E

COST_PROD = 0.0005      # 生产策略单边 taker
COST_M = 0.0005         # 横截面动量单边
START = "2021-07-01"


def sig_xsec_mom(rets, n=30, q=0.3):
    """横截面动量：按近 n 日收益排名，多前 q 空后 q"""
    mom = rets.rolling(n).sum()
    rank = mom.rank(axis=1, pct=True)
    s = pd.DataFrame(0.0, index=rank.index, columns=rank.columns)
    s[rank >= 1 - q] = 1.0
    s[rank <= q] = -1.0
    return s


def bt_from_wide(S, Od, Cd, cost, expo=0.30, top_n=8, max_open=10, wallet=10000.0):
    """用一个宽表信号跑事件回测（T+1 开盘成交，无前视）"""
    cols = [c for c in S.columns if c in Od.columns and c in Cd.columns]
    S, Od, Cd = S[cols], Od[cols], Cd[cols]
    state = S.values
    opx, clx = Od.values, Cd.values
    T, K = state.shape
    strength = np.abs(state)
    cash, pos, eqc = wallet, {}, np.zeros(T)
    n_tr = 0
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0 or np.sign(state[i, k]) != pos[k]["side"]:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                px = opx[fi, k]
                if np.isfinite(px):
                    cash += p["stake"] + p["q"] * (px - p["e"]) - p["stake"] * cost
                    n_tr += 1
        if i > 0:
            cand = [k for k in range(K) if state[i - 1, k] != 0]
            vals = {k: strength[i - 1, k] for k in cand}
            allowed = {k for k, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:top_n]}
            for k in range(K):
                if state[i, k] == 0 or k in pos or k not in allowed or len(pos) >= max_open:
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
                pos[k] = {"stake": stake, "q": q, "e": e, "side": np.sign(state[i, k])}
        eqc[i] = cash + sum(pp["stake"] + pp["q"] * (clx[i, kk] - pp["e"])
                            for kk, pp in pos.items())
    eq = pd.Series(eqc, index=S.index)
    return eq, eq.pct_change().fillna(0.0), n_tr


def metrics(ret):
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 365
    if yrs <= 0 or eq.iloc[-1] <= 0:
        return None
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    dd = ((eq / eq.cummax()) - 1).min()
    sd = ret.std()
    return {"ann": ann * 100, "dd": dd * 100,
            "calmar": ann / abs(dd) if dd < 0 else np.nan,
            "sharpe": ret.mean() / sd * np.sqrt(365) if sd > 0 else np.nan}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weight", type=float, default=0.5,
                    help="横截面动量的风险预算占比")
    ap.add_argument("--start", default=START)
    args = ap.parse_args()

    print("=" * 104)
    print("生产策略 + 横截面动量 叠加验证")
    print("=" * 104)

    data = E.load_ohlc([os.path.basename(f).split("_")[0]
                        for f in glob.glob(f"{E.PERP}/*-1d-futures.feather")])
    print(f"  币种 {len(data)}")

    # ── 生产策略净值 ──
    tr, eq_prod, ret_prod = E.run(data, start=args.start)
    print(f"  生产策略: {len(tr)} 笔 · {eq_prod.index[0].date()} ~ {eq_prod.index[-1].date()}")

    # ── 横截面动量净值 ──
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index()
    rets = closes.pct_change()
    Od = pd.DataFrame({s: d["open"] for s, d in data.items()}).sort_index().ffill()
    Cd = closes.ffill()
    mask = closes.index >= args.start
    S = sig_xsec_mom(rets, 30, 0.3).loc[mask]
    eq_m, ret_m, n_m = bt_from_wide(S, Od.loc[mask], Cd.loc[mask], COST_M)

    # 对齐
    idx = eq_prod.index.intersection(ret_m.index)
    a = ret_prod.reindex(idx).fillna(0.0)
    b = ret_m.reindex(idx).fillna(0.0)

    print(f"  横截面动量: {n_m} 笔 · 相关性 "
          f"{np.corrcoef(a.values, b.values)[0,1]:.3f}")
    print()

    w = args.weight
    comb = (1 - w) * a + w * b

    print("=" * 104)
    print(f"对照（横截面动量权重 {w:.0%}）")
    print("=" * 104)
    print(f"  {'组合':<28}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}")
    print("  " + "-" * 66)
    rows = []
    for lab, r in [("生产策略（现状）", a),
                   ("横截面动量（单独）", b),
                   (f"叠加 {w:.0%} 动量", comb)]:
        m = metrics(r)
        if not m:
            continue
        rows.append((lab, r, m))
        print(f"  {lab:<28}{m['ann']:>8.1f}%{m['dd']:>9.1f}%{m['calmar']:>9.2f}{m['sharpe']:>8.2f}")
    print("  " + "-" * 66)
    base = metrics(a)
    cm = metrics(comb)
    print()
    print(f"  ΔCalmar {cm['calmar']-base['calmar']:+.2f} · "
          f"Δ回撤 {cm['dd']-base['dd']:+.1f} 个百分点 · "
          f"Δ年化 {cm['ann']-base['ann']:+.1f} 个百分点")
    print()

    # ── 权重扫描 ──
    print("=" * 104)
    print("权重扫描（横截面动量占比）")
    print("=" * 104)
    print(f"  {'权重':<8}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}  判定")
    print("  " + "-" * 56)
    best = None
    for ww in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
        r = (1 - ww) * a + ww * b
        m = metrics(r)
        if not m:
            continue
        mark = ""
        if best is None or m["calmar"] > best[1]["calmar"]:
            best = (ww, m)
        print(f"  {ww:<8.0%}{m['ann']:>8.1f}%{m['dd']:>9.1f}%"
              f"{m['calmar']:>9.2f}{m['sharpe']:>8.2f}")
    print("  " + "-" * 56)
    print(f"\n  最优权重 {best[0]:.0%} · Calmar {best[1]['calmar']:.2f} "
          f"（现状 {base['calmar']:.2f}，{(best[1]['calmar']/base['calmar']-1)*100:+.0f}%）")
    print(f"    年化 {best[1]['ann']:.1f}% · 回撤 {best[1]['dd']:.1f}%")
    print()

    # ── M 的参数敏感性 ──
    print("=" * 104)
    print("横截面动量的参数敏感性（它是新策略，须防过拟合）")
    print("=" * 104)
    print(f"  {'n(回看)':<10}{'q(分位)':<10}{'交易':>8}{'年化':>9}{'回撤':>10}"
          f"{'Calmar':>9}{'叠加50%后Calmar':>16}")
    print("  " + "-" * 74)
    mres = []
    for n in [10, 20, 30, 60]:
        for q in [0.2, 0.3, 0.4]:
            Sx = sig_xsec_mom(rets, n, q).loc[mask]
            eqx, rx, nx = bt_from_wide(Sx, Od.loc[mask], Cd.loc[mask], COST_M)
            bx = rx.reindex(idx).fillna(0.0)
            mx = metrics(bx)
            if not mx:
                continue
            cx = metrics(0.5 * a + 0.5 * bx)
            mres.append((n, q, mx["calmar"], cx["calmar"]))
            mark = " ⭐" if mx["calmar"] > base["calmar"] else ""
            print(f"  {n:<10}{q:<10}{nx:>8}{mx['ann']:>8.1f}%{mx['dd']:>9.1f}%"
                  f"{mx['calmar']:>9.2f}{cx['calmar']:>16.2f}{mark}")
    print("  " + "-" * 74)
    cs = np.array([x[2] for x in mres])
    cs5 = np.array([x[3] for x in mres])
    print(f"\n  M 单独 Calmar: 中位 {np.median(cs):.2f} · 范围 {cs.min():.2f}~{cs.max():.2f}")
    print(f"  叠加 50% 后 Calmar: 中位 {np.median(cs5):.2f} · "
          f"优于现状({base['calmar']:.2f})的配置 {(cs5>base['calmar']).sum()}/{len(cs5)}")
    print()
    if (cs5 > base["calmar"]).mean() >= 0.7:
        print("  ✅ 叠加增益对 M 参数稳健 → 建议落地")
    elif (cs5 > base["calmar"]).mean() >= 0.5:
        print("  🟡 半数以上配置有改善 → 可落地但需谨慎")
    else:
        print("  ❌ 叠加增益不稳定 → 不建议落地")

    with open("user_data/overlay_prod.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "n_prod_trades": len(tr), "n_mom_trades": n_m,
                   "corr": round(float(np.corrcoef(a.values, b.values)[0, 1]), 4),
                   "base": {k: round(float(v), 3) for k, v in base.items()},
                   "weight_scan": {str(w_): {k: round(float(v), 3) for k, v in metrics((1 - w_) * a + w_ * b).items()}
                                   for w_ in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
                                   if metrics((1 - w_) * a + w_ * b)},
                   "mom_sensitivity": [{"n": n, "q": q, "calmar": round(c, 3),
                                        "calmar_overlay": round(c5, 3)}
                                       for n, q, c, c5 in mres]}, f,
                  ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/overlay_prod.json")


if __name__ == "__main__":
    main()
