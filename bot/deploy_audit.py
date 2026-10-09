#!/usr/bin/env python3
"""
状态：已禁用，仅保留历史源码；下述旧收益对照与部署结论已经作废。

部署总体审计 —— 对某一轮（或全部）通过判据的配置，批量跑【部署总体检验】。

════════════════════════════════════════════════════════════════════
为什么需要这个
════════════════════════════════════════════════════════════════════
自动迭代的判据只看【密集总体】（全部趋势中段的 K 线）。实测证据表明它
**不能预测部署表现**：

    配置          密集 IC    部署 IC
    919f860b      +0.057     +0.0305
    aa874035      +0.057     +0.0070      ← 同样密集 IC，部署 IC 差 4 倍

而且组合层对照（第 86 轮）显示：用 ML 分数过滤开仓，年化从 40.89% 降到
36.39%（随机封锁的 20 分位）—— **没有任何实用价值**。

所以「通过判据」不等于「可用」。这个脚本补上那一层检验。

════════════════════════════════════════════════════════════════════
三条判据（第 81/82/85 轮定稿）
════════════════════════════════════════════════════════════════════
  C1  部署总体 IC > 0                      （方向正确）
  C2  「最低分组 vs 其余」经济差 95%CI 不含 0   （经济上可辨）
  C3  最近 12~24 个月方向不为负              （不是只在早期有效）

注意 C3 用的是【方向】而不是【显著】—— 子样本功率不足时要求显著会
把所有信号都挡掉，而方向为负能识别真正的反转。

════════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════════
    # 审计某一轮所有通过判据的配置
    python deploy_audit.py --run 20261009-110239 --seeds 3

    # 只审计指定 key
    python deploy_audit.py --keys d466b3fb,919f860b --seeds 3

    # 复用已有分数（跳过训练，秒出）
    python deploy_audit.py --run 20261009-110239 --reuse

产出：
    user_data/deploy_audit_<run或keys>.json   结构化结果
    终端表格                                   C1/C2/C3 逐项通过与否
"""
raise SystemExit(
    "LEGACY_DISABLED: deploy_audit.py 已禁用。旧 40.89%/36.39% 对照已作废，"
    "分数缓存缺少 purge/排名口径版本，不能据此重算可用性或复用旧分数。"
    "历史源码与研究记录保留；当前证据口径见 docs/DEVELOPMENT_LEDGER.md。"
)

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

LEDGER = os.path.join(_HERE, "user_data", "research_trials.jsonl")
SCORES = os.path.join(_HERE, "user_data", "deploy_scores")


def load_ledger():
    cur = {}
    with open(LEDGER, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            cur[r.get("key")] = r
    return cur


def score_tag(key, cfg, seeds, offset=0):
    """与 deploy_check.py 的命名保持一致。"""
    return hashlib.md5(
        f"{key}|{json.dumps(cfg, sort_keys=True)}|s{seeds}|o{offset}".encode()
    ).hexdigest()[:10]


def spread_ci(sub, q_col, ret_col, thr, n_boot=1500, n_blocks=12, seed=0):
    """「其余 − 最低分组」均值差与其分块自助法 95%CI。"""
    s = sub.copy()
    s["_q"] = s[q_col].rank(pct=True)
    drop = s[s["_q"] <= thr]
    keep = s[s["_q"] > thr]
    if len(drop) < 15 or len(keep) < 15:
        return None
    d = keep[ret_col].mean() - drop[ret_col].mean()
    rng = np.random.default_rng(seed)
    blocks = np.array_split(np.arange(len(s)), n_blocks)
    ds = []
    for _ in range(n_boot):
        idx = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        b = s.iloc[idx]
        a = b.loc[b.index.isin(drop.index), ret_col]
        c = b.loc[b.index.isin(keep.index), ret_col]
        if len(a) > 2 and len(c) > 2:
            ds.append(c.mean() - a.mean())
    if not ds:
        return None
    return d, (float(np.percentile(ds, 2.5)), float(np.percentile(ds, 97.5))), len(drop), len(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None, help="run_id（审计该轮所有通过判据的配置）")
    ap.add_argument("--keys", default=None, help="逗号分隔的账本 key")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--entries", default="20,30,40,55")
    ap.add_argument("--threshold", type=float, default=0.20,
                    help="C2 的「最低分组」阈值（默认 20%%）")
    ap.add_argument("--recent-months", type=int, default=18,
                    help="C3 的近期窗口（默认 18 个月）")
    ap.add_argument("--reuse", action="store_true",
                    help="只复用已有分数，不重新训练")
    ap.add_argument("--device", default="mps")
    args = ap.parse_args()

    import deploy_check as D
    import ensemble_check as E

    cur = load_ledger()
    print("=" * 104)
    print("  部署总体审计 —— 通过密集判据 ≠ 可用")
    print("=" * 104)

    # ── 选目标 ──
    if args.keys:
        keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    elif args.run:
        keys = [k for k, v in cur.items()
                if v.get("run_id") == args.run and v.get("passed")]
    else:
        print("  ❌ 需要 --run 或 --keys"); sys.exit(1)

    if not keys:
        print("  该轮没有通过判据的配置（或 run_id 写错）"); sys.exit(0)
    print(f"  目标 {len(keys)} 个配置：{', '.join(keys)}")

    entries = [int(x) for x in args.entries.split(",")]
    pts = D.deployment_points(entries=entries, log=lambda *a: None)
    print(f"  部署点：{len(pts)} 笔（{len(entries)} 个 entry 周期）")
    print(f"  C2 阈值 {args.threshold:.0%} · C3 近期窗口 {args.recent_months} 个月")

    # ── 逐配置 ──
    results = []
    for i, k in enumerate(keys, 1):
        v = cur.get(k)
        if not v:
            print(f"\n  [{i}/{len(keys)}] {k}: 账本里没有"); continue
        cfg = v.get("config") or {}
        tag = score_tag(k, cfg, args.seeds)
        sp = os.path.join(SCORES, f"{tag}.npz")

        print(f"\n  [{i}/{len(keys)}] {k}  "
              f"{cfg.get('kind')} L={cfg.get('seq_len')} h={cfg.get('hidden')} "
              f"ly={cfg.get('layers')} dp={cfg.get('dropout')} lr={cfg.get('lr')}")
        print(f"        密集判据: t季={v.get('t_quarter')} 稳健={v.get('robust_windows')} q={v.get('q_value')}")

        if not os.path.exists(sp):
            if args.reuse:
                print(f"        ⚠ 没有分数缓存（{tag}.npz），跳过（去掉 --reuse 可现跑）")
                continue
            cmd = [sys.executable, "-u", os.path.join(_HERE, "deploy_check.py"),
                   "--key", k, "--seeds", str(args.seeds),
                   "--entries", args.entries, "--device", args.device]
            subprocess.run(cmd, cwd=_HERE, check=False)

        if not os.path.exists(sp):
            print(f"        ❌ 仍无分数，跳过"); continue

        z = np.load(sp, allow_pickle=True)
        df = pd.DataFrame({
            "date": pd.to_datetime(z["date"]),
            "coin": z["coin"].astype(str),
            "score": z["scores"].astype(float),
            "ret": z["ret"].astype(float),
        })
        df = df[np.isfinite(df["score"])]
        j = pts.merge(df, on=["date", "coin"], how="inner") \
               .drop_duplicates(["date", "coin"]).copy()
        if len(j) < 100:
            print(f"        ❌ 部署点匹配太少（{len(j)}）"); continue
        j["date"] = pd.to_datetime(j["date"])

        # C1：全期部署 IC
        ic, t, n = E._ic_t(j["score"].values, j["ret"].values)
        c1 = bool(np.isfinite(ic) and ic > 0 and np.isfinite(t) and t > 2)

        # C2：最低分组 vs 其余
        r2 = spread_ci(j, "score", "ret", args.threshold)
        if r2:
            d2, ci2, nd, nk = r2
            c2 = bool(ci2[0] > 0)
        else:
            d2, ci2, c2 = np.nan, (np.nan, np.nan), False

        # C3：近期方向不为负
        cut = j["date"].max() - pd.DateOffset(months=args.recent_months)
        rec = j[j["date"] >= cut]
        if len(rec) >= 80:
            ic3, t3, n3 = E._ic_t(rec["score"].values, rec["ret"].values)
            c3 = bool(np.isfinite(ic3) and ic3 >= 0)
        else:
            ic3, t3, n3, c3 = np.nan, np.nan, len(rec), False

        flag = "✅ 三项通过" if (c1 and c2 and c3) else "❌ 不通过"
        print(f"        C1 全期部署 IC   {ic:+.4f} (t={t:+.2f}, n={n})      {'✅' if c1 else '❌'}")
        # ⚠ 面板 ret 是【分数】(0.05 = 5%)，显示时 ×100 转百分点
        print(f"        C2 剔除最低{args.threshold:.0%}  {d2*100:+.2f}pp  CI[{ci2[0]*100:+.2f},{ci2[1]*100:+.2f}]pp  {'✅' if c2 else '❌'}")
        print(f"        C3 近{args.recent_months}月 IC   {ic3:+.4f} (t={t3:+.2f}, n={n3})    {'✅' if c3 else '❌'}")
        print(f"        → {flag}")

        results.append({
            "key": k, "config": cfg,
            "dense": {"t_quarter": v.get("t_quarter"),
                      "robust_windows": v.get("robust_windows"),
                      "q_value": v.get("q_value")},
            "deploy": {"n": int(n), "ic": float(ic), "t": float(t)},
            # 注：C1/C3 的 IC 用面板 ret（分数口径），C2 已换算为百分点
            "c2": {"diff_pp": float(d2) * 100 if np.isfinite(d2) else None,
                   "ci_pp": [float(ci2[0]) * 100, float(ci2[1]) * 100] if np.isfinite(ci2[0]) else None,
                   "drop_n": int(nd) if r2 else None},
            "c3": {"n": int(n3), "ic": float(ic3) if np.isfinite(ic3) else None,
                   "t": float(t3) if np.isfinite(t3) else None},
            "c1": c1, "c2_pass": c2, "c3_pass": c3,
            "all_pass": bool(c1 and c2 and c3),
        })

    # ── 汇总 ──
    print("\n" + "=" * 104)
    print("  汇总")
    print("=" * 104)
    if results:
        print(f"  {'key':<12}{'C1 部署IC':>12}{'C2 经济差':>12}{'C3 近期':>12}{'判定':>14}")
        print("  " + "-" * 62)
        for r in results:
            d = r["deploy"]
            c2s = f"{r['c2']['diff_pp']:+.2f}pp" if r["c2"].get("diff_pp") is not None else "n/a"
            c3s = f"{r['c3']['ic']:+.4f}" if r["c3"]["ic"] is not None else "n/a"
            tag = "✅ 三项通过" if r["all_pass"] else "❌"
            print(f"  {r['key']:<12}{d['ic']:>+12.4f}{c2s:>12}{c3s:>12}{tag:>14}")
        ok = sum(1 for r in results if r["all_pass"])
        print(f"\n  三项全过: {ok} / {len(results)}")
        if ok == 0:
            print("  ⚠ 没有任何配置通过部署审计 —— 与第 86 轮的组合层结论一致")
    else:
        print("  （没有可审计的结果）")

    name = args.run if args.run else "keys"
    out = os.path.join(_HERE, "user_data", f"deploy_audit_{name}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"target": name, "threshold": args.threshold,
                   "recent_months": args.recent_months,
                   "seeds": args.seeds, "results": results},
                  fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n  结果已存 {out}")


if __name__ == "__main__":
    main()
