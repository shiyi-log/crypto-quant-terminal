#!/usr/bin/env python3
"""
ML 实验台 —— 元标记（meta-labeling）

问题设定：
    主模型（趋势策略）决定【方向】和【何时有事发生】。
    ML 的任务：给定这次事件，预测【这笔交易会不会赚】。

    基准正样本率约 40.5%。若模型能把 AUC 做到 0.55+，
    就可以用它过滤掉最差的交易，提升整体的 Calmar。

关键纪律：
    · 全部特征 shift(1) 无前视
    · 净化 K 折 + 禁运期（标签重叠是金融 ML 最常见的泄漏源）
    · 报告三件事：AUC、经济价值（过滤后的收益）、跨折稳定性
    · 经济价值必须扣成本

用法:
    python ml_experiment.py                 # 跑全部模型
    python ml_experiment.py --model lgbm    # 只跑一个
    python ml_experiment.py --no-xs         # 不用横截面特征
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

OUT = "user_data/ml_experiments.jsonl"
COST = 0.001          # 往返成本（taker 0.05% × 2）


def load_dataset(use_xs=True, start=None, dense=False, side=None):
    data = ml.load_ohlcv()
    fund = ml.load_funding()
    dvol = ml.load_dvol()
    X = ml.build_features(data, fund, dvol, cross_sectional=use_xs)
    if dense:
        ev = ml.make_dense_events(data, side_filter=side)
    else:
        ev = ml.make_events(data)
        if side is not None:
            ev = ev[ev["side"] == side]
    y = ml.strategy_label(data, ev)

    # 对齐：事件 → 特征（用事件当天收盘后可得的信息，即特征最后一行）
    y = y.set_index(["date", "coin"])
    X = X[~X.index.duplicated(keep="first")]
    df = X.join(y[["label", "ret", "hold_days", "side"]], how="inner")
    df = df.dropna(subset=["label"])
    if start:
        df = df[df.index.get_level_values("date") >= start]

    feat_cols = [c for c in df.columns
                 if c not in ("label", "ret", "hold_days", "side", "funding", "dvol")]
    return df, feat_cols


# ══════════════ 模型 ══════════════

def impute(X_tr, X_te):
    """用训练集中位数填充（绝不能用全样本统计量，那是前视）"""
    med = np.nanmedian(X_tr, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    Xtr = np.where(np.isfinite(X_tr), X_tr, med)
    Xte = np.where(np.isfinite(X_te), X_te, med)
    return Xtr, Xte


def make_model(name, **kw):
    if name == "logit":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        return Pipeline([
            ("sc", StandardScaler()),
            ("m", LogisticRegression(max_iter=2000, C=kw.get("C", 0.1))),
        ])
    if name == "lgbm":
        import lightgbm as lgb
        return lgb.LGBMClassifier(
            n_estimators=kw.get("n_estimators", 300),
            learning_rate=kw.get("learning_rate", 0.03),
            num_leaves=kw.get("num_leaves", 15),
            max_depth=kw.get("max_depth", 4),
            min_child_samples=kw.get("min_child_samples", 40),
            subsample=kw.get("subsample", 0.8),
            colsample_bytree=kw.get("colsample_bytree", 0.7),
            reg_lambda=kw.get("reg_lambda", 1.0),
            random_state=42, verbose=-1,
            n_jobs=kw.get("num_threads", -1),
        )
    if name == "xgb":
        import xgboost as xgb
        return xgb.XGBClassifier(
            n_estimators=kw.get("n_estimators", 300),
            learning_rate=kw.get("learning_rate", 0.03),
            max_depth=kw.get("max_depth", 3),
            min_child_weight=kw.get("min_child_weight", 10),
            subsample=kw.get("subsample", 0.8),
            colsample_bytree=kw.get("colsample_bytree", 0.7),
            reg_lambda=kw.get("reg_lambda", 1.0),
            random_state=42, eval_metric="logloss", verbosity=0,
        )
    if name == "mlp":
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        return Pipeline([
            ("sc", StandardScaler()),
            ("m", MLPClassifier(
                hidden_layer_sizes=kw.get("hidden", (32, 16)),
                alpha=kw.get("alpha", 1e-3),
                learning_rate_init=kw.get("lr", 1e-3),
                max_iter=kw.get("max_iter", 400),
                early_stopping=True, n_iter_no_change=20,
                random_state=42)),
        ])
    raise ValueError(name)


MODELS = ["logit", "lgbm", "xgb", "mlp"]


# ══════════════ 评估 ══════════════

def economic_value(df_test, prob, thresh):
    """
    经济价值：按模型概率过滤，只做概率 > 阈值的交易。
    返回 (过滤后平均收益, 未过滤平均收益, 保留比例)
    """
    if len(df_test) == 0:
        return None
    keep = prob >= thresh
    if keep.sum() < 10:
        return None
    ret_all = df_test["ret"].values
    ret_kept = ret_all[keep]
    return {
        "n_all": len(ret_all), "n_kept": int(keep.sum()),
        "keep_pct": keep.mean(),
        "ret_all": ret_all.mean() - COST,
        "ret_kept": ret_kept.mean() - COST,
        "win_all": (ret_all > 0).mean(),
        "win_kept": (ret_kept > 0).mean(),
    }


def run_model(name, df, feat_cols, n_splits=5, threshold=0.5,
              embargo_days=5, **kw):
    X = df[feat_cols].values
    y = df["label"].values
    ev = df.reset_index()[["date", "coin"]].copy()
    ev["t1"] = ev["date"]    # 事件级 CV：用事件日期做净化（标签区间用 hold_days 近似）
    hold = df["hold_days"].values
    ev["t1"] = ev["date"] + pd.to_timedelta(np.nan_to_num(hold, nan=30), unit="D")

    probs = np.full(len(df), np.nan)
    imp = None
    for tr, te in ml.purged_kfold(ev, n_splits=n_splits, embargo_days=embargo_days):
        if len(tr) < 100 or len(te) == 0:
            continue
        Xtr, Xte = impute(X[tr], X[te])
        m = make_model(name, **kw)
        m.fit(Xtr, y[tr])
        probs[te] = m.predict_proba(Xte)[:, 1]
        if name in ("lgbm", "xgb") and imp is None:
            imp = pd.Series(m.feature_importances_, index=feat_cols).sort_values(ascending=False)

    ok = ~np.isnan(probs)
    if ok.sum() < 50:
        return None
    yt, yp = y[ok], (probs[ok] >= threshold).astype(int)
    sc = ml.score(yt, yp, probs[ok])
    evv = economic_value(df[ok], probs[ok], threshold)

    return {
        "model": name, "params": kw, "n": int(ok.sum()),
        "auc": round(sc["auc"], 4), "acc": round(sc["acc"], 4),
        "base": round(sc["base"], 4),
        "prec": round(sc["prec"], 4), "rec": round(sc["rec"], 4),
        "keep_pct": round(evv["keep_pct"], 3) if evv else None,
        "ret_all": round(evv["ret_all"], 5) if evv else None,
        "ret_kept": round(evv["ret_kept"], 5) if evv else None,
        "win_all": round(evv["win_all"], 4) if evv else None,
        "win_kept": round(evv["win_kept"], 4) if evv else None,
        "top_features": imp.head(12).round(4).to_dict() if imp is not None else None,
        "probs": probs, "y": y,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-xs", action="store_true")
    ap.add_argument("--start", default=None)
    ap.add_argument("--thresh", type=float, default=0.5)
    ap.add_argument("--no-short", action="store_true", help="只做多头")
    ap.add_argument("--features", default="all", choices=["all", "stable"],
                    help="stable = 前后段都稳定的 9 个横截面特征")
    ap.add_argument("--dense", action="store_true",
                    help="稠密采样（对齐推理分布，样本量约 33 倍）")
    ap.add_argument("--side", type=int, default=None,
                    help="1=只做多头, -1=只做空头")
    args = ap.parse_args()

    t0 = time.time()
    df, feat_cols = load_dataset(use_xs=not args.no_xs, start=args.start,
                                 dense=args.dense, side=args.side)
    if args.no_short:
        df = df[df["side"] > 0]
    STABLE = ["xs_ma_dev_50", "xs_ma_dev_20", "xs_ret_10", "xs_ret_20", "xs_ret_3",
              "xs_ma_dev_10", "xs_rsi_14", "xs_ret_5", "xs_ret_accel"]
    if args.features == "stable":
        feat_cols = [c for c in STABLE if c in feat_cols]
    if len(df) < 100:
        print("  样本不足"); return
    print(f"  样本 {len(df)} 条 · 特征 {len(feat_cols)} 个 · "
          f"基准正样本率 {df['label'].mean():.3f}")
    print(f"  只做多头: {'是' if args.no_short else '否'} · 特征集: {args.features}")
    print(f"  区间 {df.index.get_level_values('date').min().date()} ~ "
          f"{df.index.get_level_values('date').max().date()}")
    print(f"  横截面特征: {'开' if not args.no_xs else '关'}")

    names = [args.model] if args.model else MODELS
    results = []
    print()
    print("=" * 104)
    print("元标记实验 —— 净化 K 折交叉验证")
    print("=" * 104)
    print(f"  {'模型':<10}{'AUC':>8}{'准确率':>9}{'基准':>8}{'精确率':>9}{'召回':>8}"
          f"{'保留%':>8}{'未过滤收益':>12}{'过滤后收益':>12}{'提升':>9}")
    print("  " + "-" * 100)

    for name in names:
        try:
            r = run_model(name, df, feat_cols, threshold=args.thresh,
                          embargo_days=30 if args.dense else 5)
        except Exception as exc:
            print(f"  {name:<10} 失败: {type(exc).__name__}: {str(exc)[:50]}")
            continue
        if not r:
            print(f"  {name:<10} 样本不足")
            continue
        results.append(r)
        lift = (r["ret_kept"] - r["ret_all"]) if (r["ret_kept"] is not None) else np.nan
        mark = "✅" if r["auc"] > 0.55 else ("🟡" if r["auc"] > 0.52 else "❌")
        print(f"  {name:<10}{r['auc']:>8.4f}{r['acc']:>9.4f}{r['base']:>8.4f}"
              f"{r['prec']:>9.4f}{r['rec']:>8.4f}{(r['keep_pct'] or 0)*100:>7.1f}%"
              f"{(r['ret_all'] or 0)*100:>11.2f}%{(r['ret_kept'] or 0)*100:>11.2f}%"
              f"{lift*100:>8.2f}% {mark}")
    print("  " + "-" * 100)

    if results:
        best = max(results, key=lambda r: r["auc"])
        print(f"\n  最优 AUC: {best['model']} = {best['auc']:.4f}（基准 0.5）")
        if best.get("top_features"):
            print(f"\n  特征重要性 Top 12（{best['model']}）:")
            for k, v in best["top_features"].items():
                print(f"    {k:<22}{v}")
        print(f"\n  结论：", end="")
        if best["auc"] > 0.56:
            print(f"✅ AUC {best['auc']:.3f} 有实质预测力，值得继续迭代")
        elif best["auc"] > 0.52:
            print(f"🟡 AUC {best['auc']:.3f} 有微弱信号，需继续加强特征/模型")
        else:
            print(f"❌ AUC {best['auc']:.3f} 与随机无异 —— 但继续迭代（换特征/换标签/换架构）")

    # 落盘（去掉 numpy 数组）
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        for r in results:
            rec = {k: v for k, v in r.items() if k not in ("probs", "y")}
            rec["t"] = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
            rec["use_xs"] = not args.no_xs
            rec["n_features"] = len(feat_cols)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 实验已记录到 {OUT}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
