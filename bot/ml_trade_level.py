#!/usr/bin/env python3
"""
ML 元标记的交易层检验（第 22 轮）—— 与引擎无关

为什么要换方法：
    第 21 轮发现我的临时组合引擎有缺陷；第 22 轮试图修，又写坏了
    （portfolio_clean 给出 5 笔交易、473% 年化、Calmar 1551）。
    → 停止依赖临时引擎。

本方法【完全绕开组合回测】：
    ① 用已有的事件回测（event_backtest.run，已验证）取每一笔交易的
       开仓时点与盈亏
    ② 取该时点的 ML 分数
    ③ 直接检验：分数高的交易是否真的更赚钱？
       - 分位数分组（5 组）的平均盈亏
       - 逐笔 IC（分数 vs 盈亏）
       - 分块自助法给"高分组 - 低分组"的置信区间

    **这个检验不依赖任何权重/净值构造，因此不受引擎缺陷影响。**

用法:
    python ml_trade_level.py
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

import torch
torch.set_num_threads(1)

import event_backtest as E
import ml_lab
import ml_seq
from ml_regime import ic_t

START = "2021-07-01"
STEP = 6
COST = 6.8 / 10_000


def main():
    t0 = time.time()
    print("=" * 104)
    print("ML 元标记 · 交易层检验（不依赖组合引擎）")
    print("=" * 104)

    data = ml_lab.load_ohlcv()
    print(f"  币种 {len(data)}")

    # ── ① 取生产策略的每一笔交易 ──
    tr, eq, ret = E.run(data, start=START)
    tr = tr.copy()
    tr["open_d"] = pd.to_datetime(tr["open_date"], utc=True).dt.tz_localize(None).dt.normalize()
    tr["close_d"] = pd.to_datetime(tr["close_date"], utc=True).dt.tz_localize(None).dt.normalize()
    tr["pair"] = tr["pair"].str.split("/").str[0]
    tr["pnl"] = tr["profit_pct"] / 100.0
    print(f"  生产策略交易 {len(tr)} 笔 · {tr['open_d'].min().date()} ~ {tr['open_d'].max().date()}")
    print(f"    胜率 {(tr['pnl']>0).mean():.1%} · 平均 {tr['pnl'].mean()*100:+.2f}%")

    # ── ② 训练 ML 并取每笔交易开仓时的分数 ──
    seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=30, feature_set="base")
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    seeds = [100, 219, 287]

    cuts, t = [], pd.Timestamp(START)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=STEP), end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=STEP)

    per = [np.full(len(meta), np.nan) for _ in seeds]
    for a, b in cuts:
        trm = (dates < a).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 2000 or tem.sum() < 100:
            continue
        mu = seq[trm].reshape(-1, F).mean(axis=0)
        sd = seq[trm].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = np.nan_to_num(((seq[trm] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[tem] - mu) / sd).astype(np.float32))
        for i, sd_ in enumerate(seeds):
            m = ml_seq.train_seq(Xtr, y[trm], F, "lstm", device, epochs=8,
                                 hidden=64, layers=1, dropout=0.3, seed=sd_)
            per[i][tem] = ml_seq.predict_seq(m, Xte, device)

    ens = np.nanmean(np.vstack([pd.Series(p).rank(pct=True).values for p in per
                                if np.isfinite(p).sum() > 1000]), axis=0)
    sc = pd.DataFrame({"date": dates, "coin": meta["coin"], "score": ens}).dropna()
    print(f"  有效预测 {len(sc):,}")

    # 把分数贴到每笔交易的开仓日
    key = sc.set_index(["date", "coin"])["score"]
    tr["score"] = [key.get((d, c), np.nan) for d, c in zip(tr["open_d"], tr["pair"])]
    ok = tr["score"].notna()
    print(f"  匹配到分数的交易 {ok.sum()}/{len(tr)}")
    d = tr[ok].copy()

    # ── ③ 交易层检验（不依赖引擎）──
    print()
    print("=" * 104)
    print("交易层检验：ML 分数能区分赚钱与亏钱的交易吗？")
    print("=" * 104)
    ic, tt = ic_t(d["score"].values, d["pnl"].values)
    print(f"  逐笔 IC {ic:+.4f} (t={tt:.2f}, n={len(d)})")

    d["q"] = pd.qcut(d["score"], 5, labels=["最低", "偏低", "中性", "偏高", "最高"],
                     duplicates="drop")
    print()
    print(f"  {'分数分组':<10}{'笔数':>7}{'胜率':>9}{'平均盈亏':>11}{'中位盈亏':>11}")
    print("  " + "-" * 50)
    g = d.groupby("q", observed=True)
    for k, sub in g:
        print(f"  {str(k):<10}{len(sub):>7}{(sub['pnl']>0).mean()*100:>8.1f}%"
              f"{sub['pnl'].mean()*100:>10.2f}%{sub['pnl'].median()*100:>10.2f}%")
    print("  " + "-" * 50)
    hi = d[d["q"] == "最高"]["pnl"].values
    lo = d[d["q"] == "最低"]["pnl"].values
    spread = hi.mean() - lo.mean()
    print(f"\n  最高组 − 最低组 = {spread*100:+.2f} 个百分点")

    # 分块自助（按时间排序分块，处理自相关）
    d2 = d.sort_values("open_d")
    n = len(d2)
    rng = np.random.RandomState(7)
    block = 20
    diffs = []
    for _ in range(500):
        idx = []
        for _ in range(max(1, n // block)):
            s = rng.randint(0, max(1, n - block))
            idx.extend(range(s, min(s + block, n)))
        sub = d2.iloc[np.array(idx)]
        if sub["q"].nunique() < 2:
            continue
        h = sub[sub["q"] == "最高"]["pnl"].mean()
        l = sub[sub["q"] == "最低"]["pnl"].mean()
        if np.isfinite(h) and np.isfinite(l):
            diffs.append(h - l)
    diffs = np.array(diffs) * 100
    if len(diffs) > 30:
        print(f"  分块自助 95% 区间 [{np.percentile(diffs,2.5):+.2f}, "
              f"{np.percentile(diffs,97.5):+.2f}] · 正概率 {(diffs>0).mean():.2f}")
    print()
    print("  ⭐ 判读（不依赖组合引擎）:")
    if len(diffs) > 30 and np.percentile(diffs, 2.5) > 0:
        print("     ✅ ML 分数在交易层有显著区分力 → 值得继续")
    elif len(diffs) > 30 and np.percentile(diffs, 97.5) < 0:
        print("     ❌ ML 分数反向区分（高分组更差）")
    else:
        print("     🟡 交易层区分力不显著 —— 与组合层结论一致（无可靠增益）")

    with open("user_data/ml_trade_level.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "n_trades": int(ok.sum()), "ic": round(float(ic), 5),
                   "ic_t": round(float(tt), 3),
                   "spread_pct": round(float(spread * 100), 3),
                   "bootstrap": ({"lo": round(float(np.percentile(diffs, 2.5)), 3),
                                  "hi": round(float(np.percentile(diffs, 97.5)), 3),
                                  "p_pos": round(float((diffs > 0).mean()), 3)}
                                 if len(diffs) > 30 else None),
                   "groups": {str(k): {"n": int(len(s)),
                                       "win": round(float((s["pnl"] > 0).mean()), 3),
                                       "mean_pct": round(float(s["pnl"].mean() * 100), 3)}
                              for k, s in g}}, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/ml_trade_level.json  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
