#!/usr/bin/env python3
"""
严格样本外元标记实验（Strict OOS Protocol）

解决第 5 轮暴露的问题：
    上一轮在 12 个（模型 × 阈值）格子里挑 Calmar 最大值 → 多重比较陷阱，
    而且用了全样本，没有真正的样本外。

本协议：
    ① 训练期 2020-01 ~ 2023-12   ← 只在这上面选模型与阈值
    ② 测试期 2024-01 ~ 2026-10   ← 冻结后直接套用，不回头调参
    ③ 阈值在训练期用【净化 K 折】的跨折一致性选，而不是挑最大值
    ④ 报告测试期的 AUC + 组合层 Calmar（扣成本）
    ⑤ 同时报告「训练期最优」与「测试期实际」的落差 —— 这是过拟合的直接度量

用法:
    python ml_oos.py
    python ml_oos.py --dense            # 用稠密采样
    python ml_oos.py --embargo 30
"""

import argparse
import json
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_experiment as ex

OUT = "user_data/ml_oos_results.jsonl"
TRAIN_END = "2024-01-01"
COST = 0.001


def build(dense=True, embargo=30):
    data = ml.load_ohlcv()
    X = ml.build_features(data, ml.load_funding(), ml.load_dvol())
    ev = ml.make_dense_events(data) if dense else ml.make_events(data)
    y = ml.strategy_label(data, ev)
    y = y.set_index(["date", "coin"])
    X = X[~X.index.duplicated(keep="first")]
    df = X.join(y[["label", "ret", "hold_days", "side"]], how="inner")
    df = df.dropna(subset=["label"])
    feat = [c for c in df.columns if c not in ("label", "ret", "hold_days", "side")]
    return df, feat, embargo


def fit_predict(train, test, feat, model_name, embargo, n_splits=5, **kw):
    """在 train 上用净化 K 折训练（返回训练期 OOF 概率），再拟合全 train 预测 test"""
    Xtr = train[feat].values
    ytr = train["label"].values
    ev = train.reset_index()[["date", "coin"]].copy()
    hold = np.nan_to_num(train["hold_days"].values, nan=30)
    ev["t1"] = ev["date"] + pd.to_timedelta(hold, unit="D")

    oof = np.full(len(train), np.nan)
    for tr, te in ml.purged_kfold(ev, n_splits=n_splits, embargo_days=embargo):
        if len(tr) < 200 or len(te) == 0:
            continue
        A, B = ex.impute(Xtr[tr], Xtr[te])
        m = ex.make_model(model_name, **kw)
        m.fit(A, ytr[tr])
        oof[te] = m.predict_proba(B)[:, 1]

    # 全 train 拟合 → 预测 test
    A, B = ex.impute(Xtr, test[feat].values)
    m = ex.make_model(model_name, **kw)
    m.fit(A, ytr)
    pte = m.predict_proba(B)[:, 1]
    return oof, pte, m


def pick_threshold(oof, train, grid=None):
    """
    稳健阈值选择：不挑单点最优，而是要求【跨折一致】。

    做法：把训练期按时间切成 3 段，每段各自算「该阈值下的平均收益」，
    选三段都为正、且最差段最大的阈值。
    """
    # ⚠️ 用【分位数】而不是绝对概率：
    #    测试期概率分布与训练期不同（mlp 训练期保留 13.2% → 测试期只保留 2.4%），
    #    绝对阈值完全不可迁移。分位数只依赖排名，天然免疫校准漂移。
    grid = grid or [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
    ok = ~np.isnan(oof)
    sub = train[ok].copy()
    sub["p"] = oof[ok]
    sub = sub.reset_index()
    dts = pd.to_datetime(sub["date"])
    q = dts.quantile([1 / 3, 2 / 3]).values
    seg = np.where(dts <= q[0], 0, np.where(dts <= q[1], 1, 2))

    rows = []
    for q in grid:
        cut = sub["p"].quantile(1 - q)      # 保留概率最高的 q 比例
        seg_ret = []
        for s in range(3):
            m = (seg == s) & (sub["p"] >= cut).values
            if m.sum() < 30:
                seg_ret.append(np.nan)
            else:
                seg_ret.append(sub.loc[m, "ret"].mean() - COST)
        arr = np.array(seg_ret, dtype=float)
        valid = arr[~np.isnan(arr)]
        if len(valid) < 3:
            continue
        rows.append({
            "keep_q": q, "cut": float(cut),
            "mean": valid.mean(), "min": valid.min(),
            "all_pos": bool((valid > 0).all()),
            "keep": float((sub["p"] >= cut).mean()),
            "seg": [round(float(x), 4) for x in arr],
        })
    return pd.DataFrame(rows), sub


def portfolio(df_test, prob, thresh, label):
    """
    组合层评估：在测试期的事件驱动回测上做 ML 过滤。
    """
    import event_backtest as E
    coins = sorted(set(df_test.index.get_level_values("coin")))
    data = E.load_ohlc(coins)
    start = str(df_test.index.get_level_values("date").min().date())
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        dd = d[d.index >= start]
        if len(dd) < 120:
            continue
        S[s] = E.signals(dd["close"])
        ST[s] = E.strength(dd["close"])
        P[s] = dd[["open", "close"]]
    if not S:
        return None
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
    n_tr = 0
    for i in range(T):
        for k in list(pos):
            if state[i, k] == 0.0:
                p = pos.pop(k)
                fi = min(i + 1, T - 1)
                val = p["stake"] + p["q"] * (opx[fi, k] - p["e"])
                cash += val - p["stake"] * 0.0005
                n_tr += 1
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
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"label": label, "ann": ann * 100, "mdd": mdd * 100,
            "calmar": ann / abs(mdd) if mdd < 0 else np.nan, "trades": n_tr}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dense", action="store_true", default=True)
    ap.add_argument("--sparse", dest="dense", action="store_false")
    ap.add_argument("--embargo", type=int, default=30)
    args = ap.parse_args()

    t0 = time.time()
    df, feat, emb = build(dense=args.dense, embargo=args.embargo)
    dts = pd.to_datetime(df.index.get_level_values("date"))
    tr = df[dts < TRAIN_END]
    te = df[dts >= TRAIN_END]

    print(f"  特征 {len(feat)} · 采样 {'稠密' if args.dense else '稀疏'} · 禁运 {args.embargo} 天")
    print(f"  训练期 {tr.index.get_level_values('date').min().date()} ~ "
          f"{tr.index.get_level_values('date').max().date()}  {len(tr)} 条  "
          f"正样本率 {tr['label'].mean():.3f}")
    print(f"  测试期 {te.index.get_level_values('date').min().date()} ~ "
          f"{te.index.get_level_values('date').max().date()}  {len(te)} 条  "
          f"正样本率 {te['label'].mean():.3f}")
    print()

    models = ["logit", "lgbm", "xgb", "mlp"]
    summary = []
    for name in models:
        print("=" * 100)
        print(f"模型：{name}")
        print("=" * 100)
        try:
            oof, pte, _ = fit_predict(tr, te, feat, name, args.embargo)
        except Exception as exc:
            print(f"  失败: {type(exc).__name__}: {str(exc)[:70]}")
            continue

        # 训练期 OOF 的 AUC
        ok = ~np.isnan(oof)
        auc_tr = ml.score(tr["label"].values[ok],
                          (oof[ok] >= 0.5).astype(int), oof[ok])["auc"]
        auc_te = ml.score(te["label"].values,
                          (pte >= 0.5).astype(int), pte)["auc"]
        print(f"  AUC  训练期 {auc_tr:.4f}   测试期 {auc_te:.4f}   "
              f"落差 {auc_tr-auc_te:+.4f}")

        tbl, sub = pick_threshold(oof, tr)
        if tbl.empty:
            print("  阈值表为空"); continue
        # 稳健选择：三段全正 且 最差段最大
        cand = tbl[tbl["all_pos"]]
        if len(cand):
            pick = cand.sort_values("min", ascending=False).iloc[0]
            why = "三段全正，取最差段最大"
        else:
            pick = tbl.sort_values("mean", ascending=False).iloc[0]
            why = "无三段全正，退化为取均值最大"
        qkeep = float(pick["keep_q"])
        cut_tr = float(pick["cut"])
        # 测试期用【同样比例】重新取分位点（免疫校准漂移）
        cut_te = float(np.quantile(pte, 1 - qkeep))
        print(f"  阈值选择（仅用训练期）: 保留前 {qkeep*100:.0f}%   {why}")
        print(f"    {'保留%':>8}{'训练切点':>11}{'训练均值':>10}{'最差段':>10}   三段收益")
        for _, r in tbl.iterrows():
            mark = " ←选中" if r["keep_q"] == qkeep else ""
            print(f"    {r['keep']*100:>7.1f}%{r['cut']:>11.4f}{r['mean']*100:>9.2f}%"
                  f"{r['min']*100:>9.2f}%   {r['seg']}{mark}")
        th = cut_te

        # ── 测试期表现 ──
        print()
        print(f"  【测试期验证】（冻结模型与阈值）")
        m_tr = sub["p"] >= cut_tr
        print(f"    训练期该比例: 保留 {m_tr.mean()*100:.1f}%  "
              f"平均收益 {(sub.loc[m_tr,'ret'].mean()-COST)*100:+.2f}%")
        print(f"    测试期切点:   {cut_tr:.4f}（训练） → {cut_te:.4f}（测试，概率尺度已变）")
        m_te = pte >= th
        print(f"    测试期该阈值: 保留 {m_te.mean()*100:.1f}%  "
              f"平均收益 {(te['ret'].values[m_te].mean()-COST)*100:+.2f}%")
        print(f"    测试期无过滤: 平均收益 {(te['ret'].mean()-COST)*100:+.2f}%")
        lift = (te["ret"].values[m_te].mean()-COST) - (te["ret"].mean()-COST)
        print(f"    → 逐笔提升 {lift*100:+.2f}%")

        base = portfolio(te, pte, None, "无过滤")
        filt = portfolio(te, pte, th, f"ML@{th:.2f}")
        print()
        print(f"    {'组合层':<16}{'年化':>10}{'回撤':>10}{'Calmar':>9}{'交易':>8}")
        print("    " + "-" * 54)
        for r in [base, filt]:
            if r:
                print(f"    {r['label']:<16}{r['ann']:>9.2f}%{r['mdd']:>9.2f}%"
                      f"{r['calmar']:>9.2f}{r['trades']:>8}")
        if base and filt:
            print(f"    → 组合层 Calmar 变化 {(filt['calmar']-base['calmar']):+.2f} "
                  f"({(filt['calmar']/base['calmar']-1)*100:+.0f}%)")
        print()

        summary.append({
            "model": name, "auc_train": round(auc_tr, 4), "auc_test": round(auc_te, 4),
            "gap": round(auc_tr - auc_te, 4), "keep_q": qkeep, "thresh": th,
            "keep_test": round(float(m_te.mean()), 3),
            "lift_test": round(float(lift), 5),
            "calmar_base": round(base["calmar"], 3) if base else None,
            "calmar_filt": round(filt["calmar"], 3) if filt else None,
        })

    print("=" * 100)
    print("汇总")
    print("=" * 100)
    print(f"  {'模型':<10}{'训练AUC':>10}{'测试AUC':>10}{'落差':>9}{'阈值':>7}"
          f"{'保留%':>8}{'逐笔提升':>10}{'基线Calmar':>12}{'过滤Calmar':>12}{'变化':>9}")
    print("  " + "-" * 96)
    for r in summary:
        ch = ((r["calmar_filt"] / r["calmar_base"] - 1) * 100
              if r["calmar_base"] and r["calmar_filt"] else float("nan"))
        mark = "✅" if ch > 10 else ("🟡" if ch > 0 else "❌")
        print(f"  {r['model']:<10}{r['auc_train']:>10.4f}{r['auc_test']:>10.4f}"
              f"{r['gap']:>+9.4f}{r['keep_q']*100:>6.0f}%{r['keep_test']*100:>7.1f}%"
              f"{r['lift_test']*100:>9.2f}%{r['calmar_base']:>12.2f}"
              f"{r['calmar_filt']:>12.2f}{ch:>8.0f}% {mark}")
    print("  " + "-" * 96)

    with open(OUT, "a", encoding="utf-8") as f:
        rec = {"t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
               "round": 6, "train_end": TRAIN_END, "embargo": args.embargo,
               "n_features": len(feat), "dense": args.dense, "results": summary}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 {OUT}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
