#!/usr/bin/env python3
"""
ML 诊断 —— 在建复杂模型之前，先确认有没有信号

为什么要先做这个：
    第一轮实验 AUC 0.486（比随机还差）。这时最容易犯的错是
    「换更深的模型」，但如果特征本身没信号，再深的模型也学不出来。
    必须先做单变量诊断，把问题定位到【特征】还是【模型】。

诊断内容：
    ① 单变量 AUC     —— 每个特征单独对标签的区分度
    ② 标签结构       —— 标签是否有可利用的结构（时间/币种/方向）
    ③ 市场状态依赖   —— 波动率、资金费率等状态是否影响胜率
    ④ 特征稳定性     —— 信号在训练期和测试期是否一致（防假象）

用法:
    python ml_diagnose.py
    python ml_diagnose.py --label triple   # 换三重障碍标签对比
"""

import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import ml_lab as ml
import ml_experiment as ex


def univariate_auc(X: pd.DataFrame, y: pd.Series):
    """每个特征单独的 AUC（秩相关法，无 sklearn 依赖）"""
    rows = []
    yr = pd.Series(y).rank().values
    n1 = (y == 1).sum()
    n0 = (y == 0).sum()
    for c in X.columns:
        v = X[c]
        ok = v.notna().values
        if ok.sum() < 200:
            continue
        xr = pd.Series(v.values[ok]).rank().values
        yy = y.values[ok]
        n1o, n0o = (yy == 1).sum(), (yy == 0).sum()
        if n1o < 50 or n0o < 50:
            continue
        auc = (xr[yy == 1].sum() - n1o * (n1o + 1) / 2) / (n1o * n0o)
        # 用正态近似给个 t 值（Hanley-McNeil 简化）
        se = np.sqrt((auc * (1 - auc)) / min(n1o, n0o))
        rows.append({"feature": c, "auc": auc, "n": int(ok.sum()),
                     "t": (auc - 0.5) / se if se > 0 else 0})
    return pd.DataFrame(rows).sort_values("t", key=lambda s: s.abs(), ascending=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="strategy", choices=["strategy", "triple"])
    args = ap.parse_args()

    data = ml.load_ohlcv()
    fund = ml.load_funding()
    dvol = ml.load_dvol()
    X = ml.build_features(data, fund, dvol, cross_sectional=True)
    ev = ml.make_events(data)

    if args.label == "strategy":
        y = ml.strategy_label(data, ev)
        print("  标签：策略忠实（反向突破离场）")
    else:
        y = ml.triple_barrier(data, ev, pt_sl=(2.0, 1.0), max_days=30)
        print("  标签：三重障碍 2:1 / 30天")

    y = y.set_index(["date", "coin"])
    X = X[~X.index.duplicated(keep="first")]
    df = X.join(y[["label", "ret", "hold_days", "side"]], how="inner").dropna(subset=["label"])
    feat = [c for c in df.columns if c not in ("label", "ret", "hold_days", "side")]

    print(f"  样本 {len(df)} · 特征 {len(feat)} · 正样本率 {df['label'].mean():.3f}")
    print(f"  区间 {df.index.get_level_values('date').min().date()} ~ "
          f"{df.index.get_level_values('date').max().date()}")

    # ══ ① 单变量 AUC ══
    print()
    print("=" * 96)
    print("① 单变量 AUC —— 每个特征单独能区分多少")
    print("=" * 96)
    uni = univariate_auc(df[feat], df["label"])
    print(f"  {'特征':<24}{'AUC':>9}{'t值':>9}{'样本':>8}   方向")
    print("  " + "-" * 62)
    for _, r in uni.head(20).iterrows():
        mark = "✅" if abs(r["t"]) > 3 else ("🟡" if abs(r["t"]) > 2 else "")
        d = "越大越赢" if r["auc"] > 0.5 else "越小越赢"
        print(f"  {r['feature']:<24}{r['auc']:>9.4f}{r['t']:>9.2f}{r['n']:>8}   {d} {mark}")
    print("  " + "-" * 62)
    strong = uni[uni["t"].abs() > 3]
    print(f"  |t|>3 的特征: {len(strong)}/{len(uni)}")
    print(f"  |t|>2 的特征: {len(uni[uni['t'].abs()>2])}/{len(uni)}")
    if len(strong):
        print(f"  最强: {strong.iloc[0]['feature']}  AUC {strong.iloc[0]['auc']:.4f}  "
              f"t={strong.iloc[0]['t']:.2f}")

    # ══ ② 标签结构 ══
    print()
    print("=" * 96)
    print("② 标签结构 —— 有没有时间/币种/方向上的可利用规律")
    print("=" * 96)
    d = df.reset_index()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    print(f"  {'维度':<14}{'分组':<16}{'样本':>8}{'胜率':>9}{'平均收益':>11}")
    print("  " + "-" * 58)
    for dim, col in [("方向", "side")]:
        for k, g in d.groupby(col):
            lab = "多头" if k > 0 else "空头"
            print(f"  {dim:<14}{lab:<16}{len(g):>8}{g['label'].mean():>9.3f}"
                  f"{g['ret'].mean()*100:>10.2f}%")
    for k, g in d.groupby("year"):
        print(f"  {'年份':<14}{str(k):<16}{len(g):>8}{g['label'].mean():>9.3f}"
              f"{g['ret'].mean()*100:>10.2f}%")
    print("  " + "-" * 58)

    # ══ ③ 市场状态依赖 ══
    print()
    print("=" * 96)
    print("③ 市场状态依赖 —— 什么环境下趋势交易胜率更高")
    print("=" * 96)
    for col, lab in [("vol_20", "波动率(20日)"), ("funding", "资金费率"),
                     ("rsi_14", "RSI"), ("chan_pos_20", "通道位置")]:
        if col not in df.columns:
            continue
        v = df[col]
        try:
            q = pd.qcut(v, 4, labels=["低", "中低", "中高", "高"], duplicates="drop")
        except Exception:
            continue
        t = df.groupby(q, observed=True)["label"].agg(["mean", "count"])
        print(f"  {lab}:")
        for k, r in t.iterrows():
            print(f"    {str(k):<6} 样本 {int(r['count']):>5}  胜率 {r['mean']:.3f}")
        spread = t["mean"].max() - t["mean"].min()
        print(f"    → 极差 {spread:.3f}")

    # ══ ④ 训练/测试稳定性 ══
    print()
    print("=" * 96)
    print("④ 前 60% 时间 vs 后 40% 时间：单变量信号是否一致（防假象）")
    print("=" * 96)
    dts = pd.to_datetime(df.index.get_level_values("date"))
    split = dts.to_series().quantile(0.6)
    a = df[dts <= split]
    b = df[dts > split]
    ua = univariate_auc(a[feat], a["label"]).set_index("feature")["auc"]
    ub = univariate_auc(b[feat], b["label"]).set_index("feature")["auc"]
    both = pd.DataFrame({"前段": ua, "后段": ub}).dropna()
    both["一致"] = np.sign(both["前段"] - 0.5) == np.sign(both["后段"] - 0.5)
    print(f"  前段 {len(a)} 条 · 后段 {len(b)} 条 · 分割点 {pd.Timestamp(split).date()}")
    print(f"  方向一致的特征: {both['一致'].sum()}/{len(both)} "
          f"({both['一致'].mean()*100:.0f}%)")
    print(f"  相关系数: {both['前段'].corr(both['后段']):.3f}")
    print()
    print("  前后段都强的特征（|AUC-0.5| 都 > 0.02）:")
    st = both[(both["前段"] - 0.5).abs() > 0.02]
    st = st[(st["后段"] - 0.5).abs() > 0.02]
    st = st[st["一致"]]
    if len(st):
        print(f"    {'特征':<24}{'前段AUC':>10}{'后段AUC':>10}")
        print("    " + "-" * 44)
        for k, r in st.sort_values("后段", ascending=False).iterrows():
            print(f"    {k:<24}{r['前段']:>10.4f}{r['后段']:>10.4f}")
    else:
        print("    ❌ 没有")
    print()
    print("=" * 96)
    print("诊断结论")
    print("=" * 96)
    print(f"  单变量 |t|>3 特征数: {len(strong)}")
    print(f"  前后段方向一致率:   {both['一致'].mean()*100:.0f}%")
    print(f"  → ", end="")
    if len(strong) == 0:
        print("特征层面就没有信号 → 需要换特征（而非换模型）")
    elif both["一致"].mean() < 0.6:
        print("有特征但前后段不一致 → 信号不稳定，需检查是否过拟合")
    else:
        print("存在可用信号 → 值得上更复杂的模型")


if __name__ == "__main__":
    main()
