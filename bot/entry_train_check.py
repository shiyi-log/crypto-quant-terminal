#!/usr/bin/env python3
"""
状态：已禁用，仅保留历史源码；下述旧训练检验与解释不作为有效结论。

在【开仓点】上训练并检验 —— 第 23 轮那个阴性结论到底是不是样本量问题？

════════════════════════════════════════════════════════════════════
背景
════════════════════════════════════════════════════════════════════
第 68 轮的机制结论：
    模型在【全部密集事件】上训练，而密集事件绝大多数是趋势【中段】的 K 线。
    所以它学的是「中段延续性」的排序，不是「入场点优劣」。
    拿到真正的入场点上，这个排序失效甚至反向。

自然的问题是：**那就在入场点上训练。**

第 23 轮（`ml_entry_trained.py`）做过这件事，结论是"仍不成立"——
但当时只有 **604 个**开仓点（单一 20 日周期），样本太少。

**本脚本用多周期合并把开仓点提到 ~4900**，再问一次同一个问题。

════════════════════════════════════════════════════════════════════
做法
════════════════════════════════════════════════════════════════════
① 开仓点：`ml_entry_trained.entry_events(data, p)`，p ∈ {20,30,40,55}
② 特征：直接用面板（密集事件）的序列 —— 面板里本来就有每个 (date,coin) 的窗口
   **只是把训练/测试集合限制在开仓点上**
③ 走查：扩展窗口，训练集 = T 之前的开仓点，测试集 = [T, T+6月) 的开仓点
④ 评价：OOS 逐笔 IC（分数 vs 面板 ret）—— 与 deploy_check 同口径，可比

对照：
  · 同一批开仓点，用【密集事件训练】的模型打分（= 现有迭代的做法）→ 第 68 轮已测，IC ≈ -0.02
  · 本脚本：用【开仓点训练】的模型打分

用法:
    python entry_train_check.py --entries 20,30,40,55 --seeds 3
    python entry_train_check.py --entries 20,30,40,55 --min-train 800
"""
raise SystemExit(
    "LEGACY_DISABLED: entry_train_check.py 已禁用。旧训练切分未按 t1 净化，"
    "整段排名不可部署，旧正面与阴性结论均不可继续使用。"
    "历史源码与研究记录保留；当前证据口径见 docs/DEVELOPMENT_LEDGER.md。"
)

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

CFG = {"kind": "lstm", "seq_len": 45, "hidden": 64, "layers": 2,
       "dropout": 0.2, "lr": 0.001, "epochs": 8}


def _ic_t(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 30:
        return np.nan, np.nan, n
    xr = pd.Series(x).rank().values
    yr = pd.Series(y).rank().values
    xr = (xr - xr.mean()) / (xr.std() + 1e-12)
    yr = (yr - yr.mean()) / (yr.std() + 1e-12)
    ic = float((xr * yr).mean())
    t = ic * np.sqrt((n - 2) / max(1e-12, 1 - ic ** 2))
    return ic, float(t), n


def entry_pairs(entries, log=print):
    """多周期开仓点的 (date, coin) 集合（去重）。"""
    import ml_entry_trained as MET
    import ml_lab as ml
    data = ml.load_ohlcv()
    frames = []
    for p in entries:
        ev = MET.entry_events(data, p)
        if ev is None or len(ev) == 0:
            log(f"  ⚠ entry={p} 无开仓点")
            continue
        ev = ev.copy(); ev["date"] = pd.to_datetime(ev["date"])
        frames.append(ev[["date", "coin"]])
        log(f"  entry={p:<3} → {len(ev):>5} 个开仓点")
    if not frames:
        return pd.DataFrame(columns=["date", "coin"])
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "coin"])
    log(f"  去重后开仓点：{len(out)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", default="20,30,40,55")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--min-train", type=int, default=800,
                    help="训练集最少开仓点数，不足则跳过该折")
    ap.add_argument("--min-test", type=int, default=60)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--seq-len", type=int, default=None)
    ap.add_argument("--hidden", type=int, default=None)
    ap.add_argument("--layers", type=int, default=None)
    ap.add_argument("--dropout", type=float, default=None)
    ap.add_argument("--lr", type=float, default=None)
    args = ap.parse_args()

    cfg = dict(CFG)
    for k, v in (("seq_len", args.seq_len), ("hidden", args.hidden),
                 ("layers", args.layers), ("dropout", args.dropout), ("lr", args.lr)):
        if v is not None:
            cfg[k] = v
    entries = [int(x) for x in args.entries.split(",")]

    print("=" * 100)
    print("  在【开仓点】上训练并检验 —— 第 23 轮的阴性是样本量还是方法论？")
    print("=" * 100)
    print(f"  配置：{cfg}")

    import panel_cache as pc
    import ml_seq

    pts = entry_pairs(entries)
    if len(pts) == 0:
        print("  ❌ 没有开仓点"); sys.exit(1)

    t0 = time.time()
    seq, meta, feats = pc.load_or_build(int(cfg["seq_len"]), dense=True, log=print)
    m = meta.copy()
    m["date"] = pd.to_datetime(m["date"])
    m["coin"] = m["coin"].astype(str)
    print(f"  面板 {seq.shape} · {len(m)} 事件 · 加载 {time.time()-t0:.0f}s")

    # 把面板行标成是否开仓点
    key = pd.MultiIndex.from_frame(m[["date", "coin"]])
    pkey = pd.MultiIndex.from_frame(pts[["date", "coin"]])
    is_entry = key.isin(pkey)
    print(f"  其中开仓点：{int(is_entry.sum())} / {len(m)}"
          f"  ({is_entry.sum()/len(m)*100:.1f}%)")
    if is_entry.sum() < 300:
        print("  ❌ 开仓点太少，无法训练"); sys.exit(1)

    dates = pd.to_datetime(m["date"])
    y = m["label"].values
    ret = m["ret"].values
    F = seq.shape[2]

    # 走查折：按时间切，但训练/测试都只取开仓点
    start = pd.Timestamp("2021-07-01")
    cuts, t1 = [], start
    end = dates.max()
    while t1 < end:
        cuts.append((t1, min(t1 + pd.DateOffset(months=args.step), end + pd.Timedelta(days=1))))
        t1 = t1 + pd.DateOffset(months=args.step)

    n_seeds = max(1, args.seeds)
    scores = np.full(len(m), np.nan)
    print(f"\n  {'折':>3}{'训练点':>9}{'测试点':>9}{'训练区间':>28}")
    print("  " + "-" * 54)
    for ci, (a, b) in enumerate(cuts, 1):
        tr = ((dates < a) & is_entry).values
        te = ((dates >= a) & (dates < b) & is_entry).values
        if tr.sum() < args.min_train or te.sum() < args.min_test:
            print(f"  {ci:>3}{tr.sum():>9}{te.sum():>9}   样本不足跳过")
            continue
        mu = seq[tr].reshape(-1, F).mean(axis=0)
        sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = seq[tr].astype(np.float32); Xtr -= mu; Xtr /= sd
        np.nan_to_num(Xtr, copy=False)
        Xte = seq[te].astype(np.float32); Xte -= mu; Xte /= sd
        np.nan_to_num(Xte, copy=False)

        preds = []
        for si in range(n_seeds):
            mod = ml_seq.train_seq(Xtr, y[tr], F, cfg["kind"], args.device,
                                   epochs=int(cfg["epochs"]), bs=512, lr=float(cfg["lr"]),
                                   hidden=int(cfg["hidden"]), layers=int(cfg["layers"]),
                                   dropout=float(cfg["dropout"]), seed=42 + si * 37)
            preds.append(ml_seq.predict_seq(mod, Xte, args.device))
            del mod
        if len(preds) == 1:
            scores[te] = preds[0]
        else:
            R = np.vstack([pd.Series(p).rank().values / len(p) for p in preds])
            scores[te] = R.mean(axis=0)
        lo = dates[tr].min().date(); hi = dates[tr].max().date()
        print(f"  {ci:>3}{tr.sum():>9}{te.sum():>9}   {lo} → {hi}")

    # ── 评价：OOS IC（只在开仓点上有分数的那些）──
    ok = is_entry & np.isfinite(scores)
    ic, t, n = _ic_t(scores[ok], ret[ok])
    print(f"\n  ┌─ 结果：用【开仓点训练】的模型，在【开仓点】上的 OOS IC ──")
    print(f"  │ n={n}   IC={ic:+.4f}   t={t:+.2f}")
    print(f"  │ 对照：用【密集事件训练】的模型在同一批点上 IC ≈ -0.02（第 68 轮）")
    if np.isfinite(t) and t > 2:
        print(f"  │ ✅ t > 2 —— 在开仓点上训练确实改善了 OOS 表现")
    else:
        print(f"  │ ❌ t = {t:+.2f}，仍不显著 —— 说明第 23 轮的阴性不是样本量问题")

    # 逐年
    yy = dates[ok].dt.year.values
    print(f"  │ 逐年：", end="")
    parts = []
    for yr in sorted(set(yy)):
        s = (yy == yr)
        if s.sum() < 60:
            continue
        ic_y, t_y, _ = _ic_t(scores[ok][s], ret[ok][s])
        parts.append(f"{yr}:{ic_y:+.3f}")
    print("  ".join(parts))
    print(f"  └─ 耗时 {time.time()-t0:.0f}s")

    out = os.path.join(_HERE, "user_data", "entry_train_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"config": cfg, "entries": entries, "n": int(n),
                   "ic": ic, "t": t, "annual": parts}, fh,
                  ensure_ascii=False, indent=2, default=float)
    print(f"\n  结果已存 {out}")


if __name__ == "__main__":
    main()
