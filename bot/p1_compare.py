#!/usr/bin/env python3
"""
P1 效果对照：t1 purge × 同日横截面排名 对【部署 IC】的影响。

四种组合：
  A 旧口径          purge=False · same_day=False   （= 我此前所有结论的口径）
  B 只修 purge      purge=True  · same_day=False
  C 只修同日排名    purge=False · same_day=True
  D 都修            purge=True  · same_day=True    （P1 目标口径）

输出：每种组合的部署点 IC / t / 正窗口比例 / 密集 IC。

⚠ 用 seeds=1 以控制耗时；多种子会更稳，但对照的重点是【同配置下的相对变化】，
   单种子对每个组合是同一条件，相对差值仍可比。绝对数不要当结论。
"""
import json
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shiyi/DeepSeek/量化/bot")

import panel_cache as pc
import deploy_check as dc
import ml_lab as ml


def main():
    cfg = dict(kind="lstm", seq_len=30, hidden=32, layers=2, dropout=0.5,
               lr=0.001, epochs=8)
    dev = "mps"
    seq, meta, feats = pc.load_or_build(30, dense=True, log=print)
    print(f"  面板 {seq.shape} · meta 列 {list(meta.columns)}")

    # 部署检验点（与既有流程一致）
    data = ml.load_ohlcv()
    try:
        pts = dc.deployment_points(log=print)
    except Exception as e:
        print(f"  deployment_points 不可用({type(e).__name__}: {e})，退回 None")
        pts = None

    results = {}
    combos = [("A 旧口径", False, False),
              ("B 只修 purge", True, False),
              ("C 只修同日", False, True),
              ("D 都修", True, True)]
    for label, purge, same in combos:
        t0 = time.time()
        print(f"\n  ══ {label} (purge={purge}, same_day={same}) ══")
        sc, _ = dc.score_panel(cfg, seq, meta, device=dev, step=6, seeds=1,
                               purge_by_t1=purge, same_day_rank=same, log=print)
        # 密集总体 IC
        ok = np.isfinite(sc) & np.isfinite(meta["ret"].values)
        dense_ic, dense_t, n = dc._ic_t(sc[ok], meta["ret"].values[ok])
        row = {"dense_ic": dense_ic, "dense_t": dense_t, "n": n,
               "secs": time.time() - t0}
        # 部署点 IC
        if pts is not None:
            try:
                ev = dc.evaluate_deployment(sc, meta, pts, log=print)
                row.update({k: v for k, v in ev.items()
                            if isinstance(v, (int, float, np.floating))})
            except Exception as e:
                print(f"    evaluate_deployment 失败: {type(e).__name__}: {e}")
        results[label] = row
        print(f"    → 密集 IC {dense_ic:+.4f} (t={dense_t:.2f}) · {row['secs']:.0f}s"
              + (f" · 部署 IC {row.get('ic'):+.4f} (t={row.get('t'):.2f})"
                 if "ic" in row else ""))

    print("\n  ══ 汇总 ══")
    print(f"  {'组合':<16}{'密集IC':>10}{'部署IC':>10}{'部署t':>9}{'正窗口':>9}{'耗时':>8}")
    for k, v in results.items():
        print(f"  {k:<16}{v.get('dense_ic',float('nan')):>10.4f}"
              f"{v.get('ic',float('nan')):>10.4f}"
              f"{v.get('t',float('nan')):>9.2f}"
              f"{v.get('pos_windows',float('nan')):>9}"
              f"{v.get('secs',0):>7.0f}s")
    a, d = results.get("A 旧口径", {}), results.get("D 都修", {})
    if "ic" in a and "ic" in d:
        print(f"\n  → 从 A 到 D，部署 IC {a['ic']:+.4f} → {d['ic']:+.4f} "
              f"（变化 {d['ic']-a['ic']:+.4f}）")
    with open("/tmp/p1_compare.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=float)
    print("  已写 /tmp/p1_compare.json")


if __name__ == "__main__":
    main()
