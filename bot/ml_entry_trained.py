#!/usr/bin/env python3
"""
只在【部署总体】（开仓点）上训练 —— 规则 16 的直接检验（第 23 轮）

规则 16 的发现：
    密集采样 61,640 个事件里只有 604 个是真正的开仓决策点。
    模型在密集总体上 IC +0.0286 (t=3.30)，但在开仓点上 IC +0.0045 (t=0.11)。
    → 训练/部署总体不一致。

本脚本的直接检验：
    把训练样本【限制在开仓点】，即在部署总体上训练、也在部署总体上评估。
    若这样能显著，说明框架可以修好；若仍不能，则目标 ⑤ 的结论成立。

样本来源：
    · 生产策略（event_backtest.run）的每一笔开仓，用其开仓日的特征 + 实际盈亏标签
    · 为增加样本量，同时纳入【多种 entry 周期】的策略（20/30/40/55 日）
      —— 每种都给出一批开仓点，但都在各自的开仓时点上

模型：逻辑回归基线（604 样本撑不起 LSTM）+ LightGBM（同数据对照）

用法:
    python ml_entry_trained.py
"""

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

import event_backtest as E
import ml_lab
from ml_regime import ic_t

START = "2021-07-01"
COST = 6.8 / 10_000


def entry_events(data, entry):
    """某 entry 周期下，策略的全部开仓点与结果"""
    rows = []
    for s, d in data.items():
        dd = d[d.index >= START]
        if len(dd) < 200:
            continue
        sig = E.signals(dd["close"], entry, entry)
        cv = dd["close"].values
        opx = dd["open"].values
        # 找开仓：状态由 0 变非 0
        st = sig.values
        for i in range(1, len(st)):
            if st[i] != 0 and st[i - 1] == 0 and i + 1 < len(cv):
                # 找平仓
                j = i + 1
                while j < len(cv) and st[j] != 0:
                    j += 1
                j = min(j + 1, len(cv) - 1)
                e = opx[i + 1]
                if not np.isfinite(e) or e <= 0:
                    continue
                ret = (opx[j] / e - 1) * np.sign(st[i])
                if np.isfinite(ret):
                    rows.append({"date": dd.index[i], "coin": s, "ret": ret,
                                 "side": np.sign(st[i]), "entry": entry})
    return pd.DataFrame(rows)


def features_at(data, dates_coins):
    """在指定 (date, coin) 上取特征（无前视：用该日及之前）"""
    import ml_lab as ml
    X = ml.build_features(data, ml.load_funding(), ml.load_dvol(),
                          cross_sectional=True)
    X = X[~X.index.duplicated(keep="first")]
    return X


def main():
    t0 = time.time()
    print("=" * 104)
    print("只在部署总体（开仓点）上训练 —— 规则 16 的直接检验")
    print("=" * 104)

    data = ml_lab.load_ohlcv()
    print(f"  币种 {len(data)}")

    # ── 收集多种 entry 周期的开仓点 ──
    frames = []
    for en in [20, 30, 40, 55]:
        ev = entry_events(data, en)
        print(f"  entry={en:<3} 开仓点 {len(ev):>5}")
        frames.append(ev)
    EV = pd.concat(frames, ignore_index=True)
    print(f"  合计开仓点 {len(EV):,}")

    # ── 特征 ──
    print("\n  构建特征…")
    X = ml_lab.build_features(data, ml_lab.load_funding(), ml_lab.load_dvol(),
                              cross_sectional=True)
    X = X[~X.index.duplicated(keep="first")]
    print(f"    特征矩阵 {X.shape}")

    EV = EV.set_index(["date", "coin"])
    df = X.join(EV[["ret", "side", "entry"]], how="inner").dropna(subset=["ret"])
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"])
    feats = [c for c in X.columns if df[c].notna().mean() > 0.6]
    print(f"    对齐后 {len(df):,} · 可用特征 {len(feats)}")
    print(f"    正样本率（净收益>0）{((df['ret']-COST)>0).mean():.3f}")

    # ── 走查：训练在开仓点，测试在开仓点（同一总体）──
    print()
    print("=" * 104)
    print("走查（训练=开仓点，测试=开仓点，同一总体）")
    print("=" * 104)
    dates = df["date"]
    cuts, t = [], pd.Timestamp(START)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=6), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=6)

    results = {}
    for model in ["logit", "lgbm"]:
        sc = np.full(len(df), np.nan)
        for a, b in cuts:
            trm = (dates < a).values
            tem = ((dates >= a) & (dates < b)).values
            if trm.sum() < 100 or tem.sum() < 20:
                continue
            Xtr = np.nan_to_num(df.loc[trm, feats].values)
            Xte = np.nan_to_num(df.loc[tem, feats].values)
            ytr = ((df.loc[trm, "ret"].values - COST) > 0).astype(int)
            if model == "logit":
                from sklearn.linear_model import LogisticRegression
                from sklearn.preprocessing import StandardScaler
                s_ = StandardScaler().fit(Xtr)
                m = LogisticRegression(max_iter=1000, C=0.1).fit(s_.transform(Xtr), ytr)
                sc[tem] = m.predict_proba(s_.transform(Xte))[:, 1]
            else:
                import lightgbm as lgb
                m = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.05,
                                       num_leaves=7, min_child_samples=40,
                                       random_state=42, n_jobs=1, verbose=-1)
                m.fit(Xtr, ytr)
                sc[tem] = m.predict_proba(Xte)[:, 1]
        ok = np.isfinite(sc)
        if ok.sum() < 100:
            print(f"  {model}: 有效预测不足"); continue
        ic, tt = ic_t(sc[ok], df["ret"].values[ok])
        # 分组
        d2 = df[ok].copy(); d2["sc"] = sc[ok]
        d2["q"] = pd.qcut(d2["sc"], 5, labels=False, duplicates="drop")
        hi = d2[d2["q"] == d2["q"].max()]["ret"].mean()
        lo = d2[d2["q"] == 0]["ret"].mean()
        results[model] = dict(n=int(ok.sum()), ic=float(ic), t=float(tt),
                              hi=float(hi), lo=float(lo), spread=float(hi - lo))
        print(f"  {model:<6} n={ok.sum():<6} IC {ic:+.4f} (t={tt:.2f}) · "
              f"最高组 {hi*100:+.2f}% · 最低组 {lo*100:+.2f}% · "
              f"价差 {(hi-lo)*100:+.2f}pp")

    print()
    print("=" * 104)
    print("判读")
    print("=" * 104)
    print("  对照：密集采样训练 → 开仓点评估")
    print("         逐笔 IC +0.0045 (t=0.11, n=604)")
    print()
    best = max(results.items(), key=lambda kv: abs(kv[1]["t"])) if results else None
    if best:
        m = best[1]
        print(f"  本次：开仓点训练 → 开仓点评估")
        print(f"         逐笔 IC {m['ic']:+.4f} (t={m['t']:.2f}, n={m['n']})")
        print()
        if m["t"] > 2:
            print("  ✅ **修好了** —— 在正确的总体上训练，信号变得显著")
            print("     → 规则 16 的发现不只是诊断，还是修复方案")
        else:
            print("  ❌ **仍然不显著** —— 即使训练与部署总体一致，信号依然不成立")
            print("     → 目标 ⑤ 的结论在修正总体后【依然成立】")
            print("     → 这排除了「只是总体不一致」这个解释")

    with open("user_data/ml_entry_trained.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "n_total": len(df), "results": results,
                   "baseline_dense_trained": {"ic": 0.0045, "t": 0.11, "n": 604}},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/ml_entry_trained.json  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
