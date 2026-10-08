#!/usr/bin/env python3
"""
状态条件化检验（Regime Conditioning）

第 6 轮留下的线索：
    走查 IC 的符号在窗口间规律交替（+0.196 → -0.168），
    且 IC 与「当期平均收益」相关系数 -0.743 —— 行情差的时期模型"有效"、
    行情好的时期模型"反向"。

但有两个必须排除的替代解释：

    ① **基率镜像**：若模型主要学到「这笔多半会亏」，
       则在普遍亏损期它"对"、普遍盈利期它"错"。
       这**不是预测能力**，只是基率漂移的镜像。

    ② **事后变量**：ret_mean 是当期实现收益，实盘不可知。

本脚本的做法：

    1. 只用【可事前获知】的状态变量（shift(1)，全部来自历史）
         · mkt_vol    市场波动率（横截面中位数的 20 日波动）
         · breadth    市场宽度（收盘价在 50 日均线上方的币占比）
         · dispersion 横截面离散度（20 日收益的截面标准差）
         · btc_trend  BTC 相对 50 日均线的偏离
    2. 扩展窗口走查产生样本外预测（每段只用历史训练）
    3. 按状态分组计算 IC、基率、以及【基率中性化后的 IC】
    4. 判定：IC 在同状态内是否稳定为正 —— 且不能用基率解释

用法:
    python ml_regime.py
"""

import argparse
import json
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_experiment as ex
import ml_oos as oos


# ══════════════════ 市场状态变量（全部可事前获知） ══════════════════

def market_regimes(data: dict) -> pd.DataFrame:
    """
    构造市场级状态面板。全部 shift(1) 保证无前视。
    """
    closes, vols, ma_flags = {}, {}, {}
    for s, d in data.items():
        c = d["close"]
        closes[s] = c
        vols[s] = c.pct_change().rolling(20).std()
        ma_flags[s] = (c > c.rolling(50).mean()).astype(float)
    C = pd.DataFrame(closes).sort_index()
    V = pd.DataFrame(vols).sort_index()
    M = pd.DataFrame(ma_flags).sort_index()
    R = C.pct_change(20)

    out = pd.DataFrame(index=C.index)
    # 市场波动率：横截面中位
    out["mkt_vol"] = V.median(axis=1)
    # 市场宽度：多少比例的币在 50 日均线上方
    out["breadth"] = M.mean(axis=1)
    # 横截面离散度：20 日收益的截面标准差
    out["dispersion"] = R.std(axis=1)
    # BTC 趋势
    if "BTC" in C.columns:
        c = C["BTC"]
        out["btc_trend"] = c / c.rolling(50).mean() - 1
    else:
        out["btc_trend"] = C.mean(axis=1).pct_change(50)
    # 市场动量（等权组合的 20 日收益）
    out["mkt_mom"] = C.pct_change().mean(axis=1).rolling(20).sum()

    # ⚠️ 全部滞后一期：实盘只能用到昨天为止的信息
    return out.shift(1)


def label_regime(reg: pd.DataFrame, col: str, n=3):
    """按分位数把状态变量离散化"""
    v = reg[col]
    try:
        return pd.qcut(v, n, labels=[f"{col}_低", f"{col}_中", f"{col}_高"],
                       duplicates="drop")
    except Exception:
        return pd.Series(index=v.index, data=np.nan)


# ══════════════════ 走查产生样本外预测 ══════════════════

def walkforward_predictions(df, feat, models=("logit", "lgbm"),
                            step_months=6, start="2021-07-01",
                            embed=30):
    """扩展窗口走查：每段只用历史训练，产出样本外预测"""
    dts = pd.to_datetime(df.index.get_level_values("date"))
    cuts = []
    t = pd.Timestamp(start)
    end = dts.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=step_months), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=step_months)

    frames = []
    for a, b in cuts:
        m_tr = df[dts < a]
        m_te = df[(dts >= a) & (dts < b)]
        if len(m_tr) < 2000 or len(m_te) < 200:
            continue
        Xtr = m_tr[feat].values
        ytr = m_tr["label"].values
        row = m_te.reset_index()[["date", "coin"]].copy()
        row["ret"] = m_te["ret"].values
        row["label"] = m_te["label"].values
        row["period"] = str(a.date())
        for name in models:
            try:
                A, B = ex.impute(Xtr, m_te[feat].values)
                m = ex.make_model(name)
                m.fit(A, ytr)
                row[f"p_{name}"] = m.predict_proba(B)[:, 1]
            except Exception:
                row[f"p_{name}"] = np.nan
        frames.append(row)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ══════════════════ 基率中性化 ══════════════════

def neutralized_ic(score, ret, labels):
    """
    基率中性化 IC：
        在每个状态组内，分别把 score 与 ret 减去组均值（或组内秩），
        再看剩余部分的相关性。
    若 IC 完全由基率驱动（组内 score 无差异），中性化后 IC 会趋近 0。
    """
    s = pd.Series(score)
    r = pd.Series(ret)
    g = pd.Series(labels).values
    sn = s.copy()
    rn = r.copy()
    for grp in pd.unique(g):
        if pd.isna(grp):
            continue
        m = (g == grp)
        if m.sum() < 30:
            continue
        sn[m] = s[m].rank(pct=True) - 0.5
        rn[m] = r[m].rank(pct=True) - 0.5
    ok = sn.notna() & rn.notna()
    if ok.sum() < 100:
        return np.nan, np.nan
    a, b = sn[ok].values, rn[ok].values
    cs, cr = a - a.mean(), b - b.mean()
    den = np.sqrt((cs ** 2).sum() * (cr ** 2).sum())
    if den == 0:
        return np.nan, np.nan
    ic = float((cs * cr).sum() / den)
    n = len(a)
    t = ic * np.sqrt(n - 2) / np.sqrt(max(1 - ic ** 2, 1e-12))
    return ic, t


def period_avg_ic(preds, pcol, min_n=200):
    """
    逐窗口计算 IC 再平均 —— 必须用这个口径，不能用池化。

    ⚠️ 辛普森悖论（第 7 轮实测）：
        池化 IC  = -0.0312 (t=-7.77)
        逐窗口平均 IC = +0.0313 (t=1.07)
        **符号完全相反。**
        原因是池化把「跨期基率差异」混进了「期内排序能力」，
        使得一个没有排序能力的模型看起来"高度显著反向"。
    """
    out = []
    for _, g in preds.groupby("period"):
        if len(g) < min_n:
            continue
        ic, _ = ic_t(g[pcol].values, g["ret"].values)
        if np.isfinite(ic):
            out.append(ic)
    if len(out) < 3:
        return np.nan, np.nan, 0
    a = np.array(out)
    t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else np.nan
    return float(a.mean()), float(t), len(a)


def ic_t(score, ret):
    s = pd.Series(score).rank().values
    r = pd.Series(ret).rank().values
    n = len(s)
    if n < 30:
        return np.nan, np.nan
    cs, cr = s - s.mean(), r - r.mean()
    den = np.sqrt((cs ** 2).sum() * (cr ** 2).sum())
    if den == 0:
        return np.nan, np.nan
    ic = float((cs * cr).sum() / den)
    t = ic * np.sqrt(n - 2) / np.sqrt(max(1 - ic ** 2, 1e-12))
    return ic, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="logit,lgbm")
    ap.add_argument("--dense", action="store_true", default=True)
    ap.add_argument("--sparse", dest="dense", action="store_false")
    args = ap.parse_args()
    models = args.models.split(",")

    data = ml.load_ohlcv()
    df, feat, _ = oos.build(dense=args.dense, embargo=30)
    reg = market_regimes(data)

    print(f"  特征 {len(feat)} · 采样 {'稠密' if args.dense else '稀疏'}")
    print(f"  状态变量（全部 shift(1) 无前视）: {list(reg.columns)}")
    print()

    preds = walkforward_predictions(df, feat, models=models)
    if preds.empty:
        print("  走查无预测"); return
    preds["date"] = pd.to_datetime(preds["date"])
    print(f"  走查样本外预测 {len(preds)} 条 · "
          f"{preds['period'].nunique()} 个窗口")
    print(f"  覆盖 {preds['date'].min().date()} ~ {preds['date'].max().date()}")
    print()

    # 附加状态
    regx = reg.reindex(preds["date"].values)
    for c in reg.columns:
        preds[c] = regx[c].values
    preds = preds.dropna(subset=list(reg.columns))
    print(f"  附加状态后样本 {len(preds)} 条")
    print()

    # ══ 总体基准 ══
    print("=" * 104)
    print("① 总体（不分状态）")
    print("=" * 104)
    print(f"  {'模型':<8}{'池化IC':>10}{'池化t':>9}{'逐窗口IC':>11}{'窗口t':>9}"
          f"{'窗口数':>8}{'基率':>8}")
    print("  " + "-" * 66)
    for m in models:
        col = f"p_{m}"
        if col not in preds:
            continue
        icp, tp = ic_t(preds[col].values, preds["ret"].values)
        icw, tw, nw = period_avg_ic(preds, col)
        print(f"  {m:<8}{icp:>10.4f}{tp:>9.2f}{icw:>11.4f}{tw:>9.2f}"
              f"{nw:>8}{preds['label'].mean():>8.3f}")
    print("  " + "-" * 66)
    print("  ⚠️ 判据只认【逐窗口 IC】—— 池化 IC 会被跨期基率差异污染（辛普森悖论）")

    # ══ 按状态分组 ══
    summary = []
    for var in ["mkt_vol", "breadth", "dispersion", "btc_trend", "mkt_mom"]:
        lab = label_regime(reg, var, 3).reindex(preds["date"].values)
        preds["_g"] = lab.values
        sub = preds.dropna(subset=["_g"])
        if len(sub) < 500:
            continue
        print()
        print("=" * 104)
        print(f"② 按【{var}】分组（{sub['_g'].nunique()} 组）")
        print("=" * 104)
        header = f"  {'组':<20}{'样本':>8}{'基率':>8}"
        for m in models:
            header += f"{m+' IC':>11}{'t值':>8}"
        header += f"{'中性化IC':>11}{'t值':>8}"
        print(header)
        print("  " + "-" * 96)
        for g, gs in sub.groupby("_g", observed=True):
            line = f"  {str(g):<20}{len(gs):>8}{gs['label'].mean():>8.3f}"
            for m in models:
                ic, t, _ = period_avg_ic(gs, f"p_{m}", min_n=100)
                if not np.isfinite(ic):
                    ic, t = ic_t(gs[f"p_{m}"].values, gs["ret"].values)
                line += f"{ic:>11.4f}{t:>8.2f}"
            # 用主模型做基率中性化
            m0 = models[0]
            nic, nt = neutralized_ic(gs[f"p_{m0}"].values, gs["ret"].values,
                                     gs["_g"].values)
            line += f"{nic:>11.4f}{nt:>8.2f}"
            print(line)
            _ic, _t, _ = period_avg_ic(gs, f"p_{m0}", min_n=100)
            if not np.isfinite(_ic):
                _ic, _t = ic_t(gs[f"p_{m0}"].values, gs["ret"].values)
            summary.append({"var": var, "group": str(g), "n": len(gs),
                            "base": round(float(gs["label"].mean()), 4),
                            "ic": round(float(_ic), 4),
                            "t": round(float(_t), 2),
                            "n_ic": round(float(nic), 4) if np.isfinite(nic) else None,
                            "n_t": round(float(nt), 2) if np.isfinite(nt) else None})
        print("  " + "-" * 96)

    # ══ 判定 ══
    print()
    print("=" * 104)
    print("③ 判定")
    print("=" * 104)
    good = [x for x in summary if x["t"] is not None and x["t"] > 2]
    good_neutral = [x for x in summary if x["n_t"] is not None and x["n_t"] > 2]
    print(f"  分组总数: {len(summary)}")
    print(f"  IC 的 t>2 的组: {len(good)}")
    for x in good:
        print(f"    ✅ {x['var']}/{x['group']}: IC {x['ic']:.4f} t={x['t']:.2f} "
              f"基率 {x['base']:.3f} n={x['n']}")
    print(f"  基率中性化后 t>2 的组: {len(good_neutral)}")
    for x in good_neutral:
        print(f"    ✅ {x['var']}/{x['group']}: 中性化IC {x['n_ic']:.4f} "
              f"t={x['n_t']:.2f}（原始 IC {x['ic']:.4f}）")
    print()
    if good_neutral:
        print("  → ✅ 存在【不能用基率解释】的状态内信号 —— 值得做状态条件化")
    elif good:
        print("  → 🟡 有原始 IC 显著的组，但中性化后消失 → 基率镜像，不是预测能力")
    else:
        print("  → ❌ 没有任何状态下的 IC 显著 —— 继续迭代（序列模型/换标签/换目标）")

    with open("user_data/ml_regime_results.jsonl", "a", encoding="utf-8") as f:
        rec = {"t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
               "round": 7, "n_pred": len(preds), "models": models,
               "groups": summary}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/ml_regime_results.jsonl")


if __name__ == "__main__":
    main()
