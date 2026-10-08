#!/usr/bin/env python3
"""
高频元标记实验 —— BTC/ETH · 5m/15m（第 15 轮）

问题设定：
    主信号：5m/15m 上的 Donchian 状态机（短回看，真正的"高频"）
    标签：  按策略规则交易，扣除 maker 成本后是否盈利
    ML：    预测这笔高频交易会不会赚（元标记）

为什么这次在统计上可行（第 14 轮实测）：
    周期/回看      独立趋势   最小可检测 IC
    日线 20日         129        0.2465   ← 不可行
    15m 12h         2,429       0.0568
    5m  8h          3,633       0.0465   ← 与实测信号强度同量级
    5m  4h          7,221       0.0330

成本口径（关键）：
    taker 往返 10bp → 5m/15m 数学上不可能
    maker 往返  4bp → 单笔毛收益 >4bp 即盈亏平衡  ← 本实验用这个

纪律（沿用前 14 轮）：
    · 特征全部 shift(1)，无前视
    · 走查评估（每段只用历史训练）
    · 报【多窗口宽度的 t 值】，不报「正窗口占比」（第 11/12 轮已证伪）
    · 报【多种子均值】，不报单次运行（规则 13）
    · 成本必须扣除（maker 口径）

用法:
    python hft_experiment.py --tf 5m --entry 96 --seeds 3
    python hft_experiment.py --tf 15m --entry 48 --maker
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

import hft_lab as H

from ml_regime import ic_t

COST_MAKER = H.MAKER * 2      # 往返 4bp
COST_TAKER = H.TAKER * 2      # 往返 10bp


def build_dataset(tf="5m", entry=96, exit_=None, coins=("BTC", "ETH")):
    """构造 (特征, 标签, 元数据)"""
    data = H.load_hf(list(coins), tf)
    exit_ = exit_ or entry
    Xs, ys = [], []
    for s, d in data.items():
        f = H.hf_features(d, entry)
        f["coin"] = s
        Xs.append(f)
    X = pd.concat(Xs).sort_index()
    X = X[~X.index.duplicated(keep="first")]

    ev = H.hf_events(data, entry, exit_)
    print(f"  事件 {len(ev)}")

    # 标签：按策略规则交易，扣成本后是否盈利
    rows = []
    for s, d in data.items():
        c = d["close"]
        lx = c.rolling(exit_).min().shift(1)
        hx = c.rolling(exit_).max().shift(1)
        sub = ev[ev["coin"] == s]
        idx = {t: i for i, t in enumerate(d.index)}
        cv = c.values
        lxv, hxv = lx.values, hx.values
        for r in sub.itertuples():
            i = idx.get(r.date)
            if i is None or i + 1 >= len(cv):
                continue
            entry_px = d["open"].iloc[i + 1]
            if not np.isfinite(entry_px) or entry_px <= 0:
                continue
            j = i + 1
            while j < len(cv):
                if r.side > 0 and np.isfinite(lxv[j]) and cv[j] < lxv[j]:
                    break
                if r.side < 0 and np.isfinite(hxv[j]) and cv[j] > hxv[j]:
                    break
                j += 1
            j = min(j + 1, len(cv) - 1)
            exit_px = d["open"].iloc[j]
            ret = (exit_px / entry_px - 1) * r.side
            rows.append({"date": r.date, "coin": s, "side": r.side,
                         "ret": ret, "hold_bars": j - (i + 1)})
    y = pd.DataFrame(rows)
    print(f"  有效样本 {len(y)} · 平均持仓 {y['hold_bars'].mean():.1f} 根")
    return X, y, data


def evaluate(meta, score, label="", maker=True):
    """稳健评估：多窗口宽度 t 值 + 成本后逐笔收益"""
    cost = COST_MAKER if maker else COST_TAKER
    ret_net = meta["ret"].values - cost
    ic, t = ic_t(score, meta["ret"].values)
    print(f"    {label:<22} IC {ic:+.4f}  毛收益 {meta['ret'].mean()*1e4:+.2f}bp  "
          f"净收益 {ret_net.mean()*1e4:+.2f}bp  持仓 {meta['hold_bars'].mean():.0f}根")
    rt = H.evaluate(meta["ret"].values, score, meta["date"].values)
    return ic, t, ret_net.mean(), rt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--entry", type=int, default=96)
    ap.add_argument("--exit", type=int, default=None)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--maker", action="store_true", default=True)
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 100)
    print(f"高频元标记 · {args.tf} · entry={args.entry}")
    print("=" * 100)
    X, y, data = build_dataset(args.tf, args.entry, args.exit)
    if len(y) < 500:
        print("  样本不足"); return

    y = y.set_index(["date", "coin"])
    X = X[~X.index.duplicated(keep="first")]
    df = X.join(y, how="inner").dropna(subset=["ret"])
    print(f"  对齐后 {len(df)} · 特征 {df.shape[1]-4}")

    feats = [c for c in df.columns if c not in ("ret", "hold_bars", "side")]
    feats = [c for c in feats if df[c].notna().mean() > 0.5]
    print(f"  可用特征 {len(feats)}")

    # 标签：净收益为正
    cost = COST_MAKER if args.maker else COST_TAKER
    df["label"] = ((df["ret"] - cost) > 0).astype(int)
    print(f"  正样本率（净收益>0）: {df['label'].mean():.3f}")
    print(f"  成本口径: {'maker 4bp' if args.maker else 'taker 10bp'}")
    print()

    # 走查（按时间 70/30，前段训练后段测试；再用扩展窗口）
    dts = pd.to_datetime(df.index.get_level_values("date"))
    print("=" * 100)
    print("基线（不做 ML，全做）")
    print("=" * 100)
    m0 = df.reset_index()
    evaluate(m0, np.full(len(m0), 0.5), "全做基线")

    # 走查训练 LSTM（用序列）
    print()
    print("=" * 100)
    print("ML 过滤（走查训练）")
    print("=" * 100)
    import ml_seq
    seq, meta, _ = ml_seq.build_panel(dense=False, seq_len=30,
                                      feature_set="base")
    # 用高频数据重建序列（ml_seq 用的是日线，这里手工构造）
    print("  ⚠️ 高频序列需独立构造 —— 见下一步实现")
    print(f"  （本轮先完成：数据管道 + 成本模型 + 基线评估）")
    print()
    print("  基线已建立，这是判断 ML 是否有增益的参照点")

    with open("user_data/hft_experiment.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "t": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "tf": args.tf, "entry": args.entry, "n": len(df),
            "n_feats": len(feats), "pos_rate": round(float(df["label"].mean()), 4),
            "gross_bp": round(float(df["ret"].mean() * 1e4), 3),
            "net_bp": round(float((df["ret"].mean() - cost) * 1e4), 3),
            "maker": args.maker,
        }, ensure_ascii=False) + "\n")
    print(f"\n  ✅ 已记录到 user_data/hft_experiment.jsonl  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
