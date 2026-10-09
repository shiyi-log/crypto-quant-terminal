#!/usr/bin/env python3
"""
状态：已禁用，仅保留历史源码；下述旧集成检验与部署解释已经作废。

多配置集成检验 —— 把多个通过配置的分数合成一个信号，在部署总体上测。

════════════════════════════════════════════════════════════════════
为什么做这个
════════════════════════════════════════════════════════════════════
单配置的部署检验（deploy_check.py）结果是：

    密集 IC 0.0647 → 部署 IC 0.0709     IC 保住了
    但 t 只有 1.75（n=608）—— 5% 水平不显著

t 的瓶颈是【样本量】。增加部署点要改策略（entry 周期），代价大且改变了策略本身。

**另一条路：降低噪声。**
日频单配置的分数噪声很大。把多个配置的分数【秩平均】成一个信号，
信噪比会上升 —— 这正是第 38 轮"集成买的是稳健，不是分数"的延伸，
只是这次用在了【部署总体】上。

════════════════════════════════════════════════════════════════════
做法
════════════════════════════════════════════════════════════════════
① 读 user_data/deploy_scores/*.npz（deploy_check.py --key 时存下的分数）
② 按 (date,coin) 对齐，每个配置先【秩归一化】再平均
   （不能直接平均原始分数 —— 不同配置的分数尺度不同）
③ 同样只在【真正的开仓点】上算 IC / 分位数 / 自助法 CI
④ 与单配置的最好结果对照

用法:
    python ensemble_check.py                 # 用全部已存分数
    python ensemble_check.py --min 2         # 至少 2 个配置才做
"""
raise SystemExit(
    "LEGACY_DISABLED: ensemble_check.py 已禁用。全期池化排名使用未来分布，"
    "旧分数缓存缺少评估版本，且成交日与信号日对齐口径不同。"
    "历史源码与研究记录保留；当前证据口径见 docs/DEVELOPMENT_LEDGER.md。"
)

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

SCORE_DIR = os.path.join(_HERE, "user_data", "deploy_scores")


def load_scores():
    """读全部已存分数，返回 [(tag, date, coin, score, ret), ...]"""
    out = []
    for f in sorted(glob.glob(os.path.join(SCORE_DIR, "*.npz"))):
        try:
            z = np.load(f, allow_pickle=True)   # 文件由本脚本的 deploy_check 写出，可信
            out.append((os.path.basename(f)[:-4],
                        pd.to_datetime(z["date"]), z["coin"].astype(str),
                        z["scores"].astype(float), z["ret"].astype(float)))
        except Exception as e:
            print(f"  ⚠ 跳过 {os.path.basename(f)}: {type(e).__name__}: {e}")
    return out


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


def block_bootstrap_ci(q, n_boot=2000, n_blocks=20, seed=0):
    """分块自助法 —— 按时间分块，避免逐笔相关性低估方差。"""
    rng = np.random.default_rng(seed)
    blocks = np.array_split(np.arange(len(q)), n_blocks)
    diffs = []
    for _ in range(n_boot):
        idx = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        s = q.iloc[idx]
        h = s.loc[s["score"] >= s["score"].quantile(0.8), "profit_pct"]
        l = s.loc[s["score"] <= s["score"].quantile(0.2), "profit_pct"]
        if len(h) > 2 and len(l) > 2:
            diffs.append(h.mean() - l.mean())
    if not diffs:
        return None
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=2, help="至少几个配置才做集成")
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--entries", default=None,
                    help="多 entry 周期合并以提高功效，例 --entries 20,30,40,55")
    args = ap.parse_args()

    print("=" * 100)
    print("  多配置集成检验 —— 合成信号在部署总体上是否更显著？")
    print("=" * 100)

    import deploy_check as D
    _entries = ([int(x) for x in args.entries.split(",")] if args.entries else None)
    pts = D.deployment_points(entries=_entries)

    blobs = load_scores()
    print(f"  读到 {len(blobs)} 个配置的分数")
    if len(blobs) < args.min:
        print(f"  ❌ 不足 {args.min} 个，先跑 deploy_check.py --key ... 攒分数")
        sys.exit(1)

    # ── 逐个配置：密集 IC 与部署 IC（对照）──
    print(f"\n  {'配置':<14}{'密集IC':>9}{'密集t':>8}{'部署IC':>9}{'部署t':>8}{'部署n':>7}")
    print("  " + "-" * 58)
    per = []
    for tag, date, coin, sc, ret in blobs:
        dic, dt, dn = _ic_t(sc, ret)
        base = pd.DataFrame({"date": date, "coin": coin, "score": sc})
        j = pts.merge(base, on=["date", "coin"], how="left")
        h = j["score"].notna()
        eic, et, en = _ic_t(j.loc[h, "score"].values, j.loc[h, "profit_pct"].values)
        per.append((tag, dic, eic, et, en))
        print(f"  {tag:<14}{dic:>9.4f}{dt:>8.2f}{eic:>9.4f}{et:>8.2f}{en:>7d}")

    # ── 集成：按 (date,coin) 对齐 + 秩归一化后平均 ──
    print(f"\n  ── 集成（秩归一化后平均）──")
    frames = []
    for tag, date, coin, sc, ret in blobs:
        df = pd.DataFrame({"date": date, "coin": coin, "s": sc})
        df = df[np.isfinite(df["s"])]
        # 秩归一化到 [0,1]，消除各配置的尺度差异
        df["s"] = df["s"].rank(pct=True)
        df = df.rename(columns={"s": tag})
        frames.append(df.set_index(["date", "coin"]))
        # 保留 ret 用于密集对照（取自第一个配置即可，ret 与配置无关）
    merged = pd.concat(frames, axis=1)
    ens = merged.mean(axis=1, skipna=True).rename("score").reset_index()
    n_cfg_used = merged.notna().sum(axis=1)
    print(f"  对齐后事件数 {len(ens)}（每个事件平均用到 {n_cfg_used.mean():.1f} 个配置）")

    # 密集总体（集成）
    ret_map = pd.DataFrame({"date": blobs[0][1], "coin": blobs[0][2], "ret": blobs[0][4]})
    ens2 = ens.merge(ret_map, on=["date", "coin"], how="left")
    dic, dt, dn = _ic_t(ens2["score"].values, ens2["ret"].values)

    # 部署总体（集成）
    j = pts.merge(ens[["date", "coin", "score"]], on=["date", "coin"], how="left")
    hit = j["score"].notna()
    eic, et, en = _ic_t(j.loc[hit, "score"].values, j.loc[hit, "profit_pct"].values)
    q = j.loc[hit].copy()

    print(f"\n  ┌─ 集成结果 ────────────────────────────────────────")
    print(f"  │ 密集总体   n={dn:<6} IC={dic:+.4f}  t={dt:+.2f}")
    print(f"  │ 部署总体   n={en:<6} IC={eic:+.4f}  t={et:+.2f}   ← 关键")
    if len(q) >= 50:
        q["grp"] = pd.qcut(q["score"], 5, labels=False, duplicates="drop")
        g = q.groupby("grp")["profit_pct"].agg(["mean", "count"])
        qs = "  ".join(f"Q{int(k)}={v['mean']:+.2f}%" for k, v in g.iterrows())
        print(f"  │ 分位: {qs}")
        hi, lo = int(g.index.max()), int(g.index.min())
        diff = float(g.loc[hi, "mean"] - g.loc[lo, "mean"])
        ci = block_bootstrap_ci(q)
        print(f"  │ 高20% − 低20% = {diff:+.3f}%"
              + (f"   95%CI [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else ""))
        if ci and (ci[0] > 0 or ci[1] < 0):
            print(f"  │ ✅ 置信区间不含 0 —— 这个方向有统计支撑")
        else:
            print(f"  │ ⚠ 置信区间仍含 0 —— 统计上还不能下结论")
    # 与最好的单配置对照
    best_single = max((p[3] for p in per if np.isfinite(p[3])), default=float("nan"))
    print(f"  │ 对照：最好的单配置部署 t = {best_single:+.2f}")
    print(f"  └──────────────────────────────────────────────────")

    out = os.path.join(_HERE, "user_data", "ensemble_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"n_configs": len(blobs),
                   "per_config": [{"tag": t, "dense_ic": d, "deploy_ic": e, "deploy_t": tt, "n": n}
                                  for t, d, e, tt, n in per],
                   "ensemble": {"dense_ic": dic, "dense_t": dt, "deploy_ic": eic,
                                "deploy_t": et, "deploy_n": en}},
                  fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n  结果已存 {out}")


if __name__ == "__main__":
    main()
