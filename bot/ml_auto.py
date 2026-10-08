#!/usr/bin/env python3
"""
⚠️ 已废弃（DEPRECATED）—— 请改用 bot/auto_research.py

    本脚本是「ML 自动迭代」的第一版，功能已被 auto_research.py 完全覆盖且更完善：
      · auto_research.py 有分阶段探索（OFAT 基线 → 邻域爬山 → 组合）
      · 有 BH-FDR 多重比较校正（本脚本没有）
      · 有版本登记（champion/challenger）与自动报告
      · 与 model_registry.py 打通

    保留本文件仅作历史参考，不再维护。

────────────────────────────────────────────────────────────

ML 自动迭代守护进程（Auto ML Iteration）

回答的问题：「现在是手动迭代的么，不能程序自动迭代么」
    —— 之前确实是手动的：每一轮都由人写脚本、跑实验、看结果。
    本脚本把这件事变成常驻进程。

与 auto_iterate.py 的分工：
    auto_iterate.py  —— 巡检【已部署策略】的健康度（C1~C5），只诊断报警
    ml_auto.py       —— 巡检【模型搜索空间】，自动跑实验、记录、追踪最优

设计原则：
    ① 评估协议固定，不可被搜索过程改变
         · 扩展窗口走查（每段只用历史训练）
         · 移动窗口逐窗口 IC（绝不池化 —— 第 7 轮辛普森悖论教训）
         · 判据：t 值 + 正窗口占比
    ② 随机搜索 + 去重：记录已试配置，重启后续跑，不重复劳动
    ③ **只研究，不自动上线** —— 与 auto_iterate 同一纪律
       （理由：本项目已两次发现"看起来显著"的假信号）
    ④ 每轮落盘，形成可追溯的实验日志

搜索空间：
    seq_len   {30, 60, 90, 120}
    model     {lstm, gru, transformer, cnn}
    hidden    {32, 64, 128}
    layers    {1, 2}
    dropout   {0.2, 0.3, 0.5}
    epochs    {6, 8, 12}

用法:
    python ml_auto.py                 # 跑一轮
    python ml_auto.py --n 5           # 跑 5 个配置
    python ml_auto.py --daemon        # 常驻，持续搜索
    python ml_auto.py --daemon --interval 120
    python ml_auto.py --status        # 看进度与最优
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import torch

torch.set_num_threads(1)

import ml_lab
import ml_seq

from ml_regime import ic_t

RESULTS = "user_data/ml_auto_results.jsonl"
BEST = "user_data/ml_auto_best.json"

SPACE = {
    "seq_len": [30, 60, 90, 120],
    "model": ["lstm", "gru", "transformer", "cnn"],
    "hidden": [32, 64, 128],
    "layers": [1, 2],
    "dropout": [0.2, 0.3, 0.5],
    "epochs": [6, 8, 12],
}

# 评估协议（固定，不可被搜索改变）
WALK_START = "2021-07-01"
WALK_STEP_MONTHS = 6
EVAL_WINDOW_MONTHS = 6

_panel_cache = {}


def cfg_key(cfg) -> str:
    s = json.dumps(cfg, sort_keys=True)
    return hashlib.md5(s.encode()).hexdigest()[:10]


def load_done():
    """已试过的配置（重启后续跑，不重复）"""
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    done[cfg_key(r["config"])] = r
                except Exception:
                    continue
    return done


def get_panel(L):
    if L not in _panel_cache:
        seq, meta, feats = ml_seq.build_panel(dense=True, seq_len=L)
        _panel_cache[L] = (seq, meta, feats)
    return _panel_cache[L]


def evaluate(cfg, verbose=True):
    """
    固定协议评估：
      · 扩展窗口走查（每段只用历史训练）
      · 移动窗口逐窗口 IC（绝不池化）
    返回 {mean_ic, t, pos, n_win, ...}
    """
    t0 = time.time()
    seq, meta, feats = get_panel(cfg["seq_len"])
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    cuts = []
    t = pd.Timestamp(WALK_START)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=WALK_STEP_MONTHS),
                            end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=WALK_STEP_MONTHS)

    sc = np.full(len(meta), np.nan)
    for a, b in cuts:
        trm = (dates < a).values
        tem = ((dates >= a) & (dates < b)).values
        if trm.sum() < 2000 or tem.sum() < 100:
            continue
        # 标准化统计量只来自训练期（防泄漏）
        mu = seq[trm].reshape(-1, F).mean(axis=0)
        sd = seq[trm].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = np.nan_to_num(((seq[trm] - mu) / sd).astype(np.float32))
        Xte = np.nan_to_num(((seq[tem] - mu) / sd).astype(np.float32))
        m = ml_seq.train_seq(Xtr, y[trm], F, cfg["model"], device,
                             epochs=cfg["epochs"], hidden=cfg["hidden"],
                             layers=cfg["layers"], dropout=cfg["dropout"])
        sc[tem] = ml_seq.predict_seq(m, Xte, device)

    # 移动窗口逐窗口 IC
    ics = []
    dts = pd.date_range(pd.Timestamp(WALK_START),
                        dates.max() - pd.DateOffset(months=EVAL_WINDOW_MONTHS),
                        freq="MS")
    for a in dts:
        b = a + pd.DateOffset(months=EVAL_WINDOW_MONTHS)
        m = ((dates >= a) & (dates < b)).values & (~np.isnan(sc))
        if m.sum() < 150:
            continue
        ic, _ = ic_t(sc[m], meta["ret"].values[m])
        if np.isfinite(ic):
            ics.append(ic)
    if len(ics) < 5:
        return None
    a = np.array(ics)
    tv = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else np.nan
    # 池化 IC 仅作对照记录，不作判据
    ok = ~np.isnan(sc)
    ic_pool, t_pool = ic_t(sc[ok], meta["ret"].values[ok])
    return {
        "config": cfg, "key": cfg_key(cfg),
        "mean_ic": float(a.mean()), "t": float(tv),
        "pos": int((a > 0).sum()), "n_win": len(a),
        "pos_pct": float((a > 0).mean()),
        "ic_pool": float(ic_pool), "t_pool": float(t_pool),
        "n_samples": int(ok.sum()),
        "elapsed": round(time.time() - t0, 1),
    }


def sample_cfg(done, rng):
    """随机搜索 + 去重（最多尝试 500 次找未试过的）"""
    for _ in range(500):
        cfg = {k: rng.choice(v) for k, v in SPACE.items()}
        if cfg_key(cfg) not in done:
            return cfg
    return None


def update_best(rec):
    """
    最优配置追踪。判据：
      ① t 值（主）
      ② 正窗口占比（次）
      ③ 平均 IC（再次）
    ⚠️ 不做多重比较校正的话，搜索出来的"最优"本身会过拟合搜索空间 ——
       所以同时记录「搜索次数」与「随机基准的期望最优」，供人工判断。
    """
    best = None
    if os.path.exists(BEST):
        try:
            best = json.load(open(BEST, encoding="utf-8"))
        except Exception:
            best = None
    better = (best is None
              or rec["t"] > best.get("t", -9)
              or (rec["t"] == best.get("t") and rec["pos_pct"] > best.get("pos_pct", 0)))
    if better:
        out = dict(rec)
        out["updated_at"] = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
        with open(BEST, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return True, out
    return False, best


def one_round(n=1, seed=None):
    rng = random.Random(seed)
    done = load_done()
    print(f"  已完成配置 {len(done)} / 搜索空间 "
          f"{np.prod([len(v) for v in SPACE.values()])}")
    for i in range(n):
        cfg = sample_cfg(done, rng)
        if cfg is None:
            print("  搜索空间已穷尽"); break
        print(f"\n  [{i+1}/{n}] {cfg}")
        try:
            rec = evaluate(cfg)
        except Exception as exc:
            print(f"    失败: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if rec is None:
            print("    样本不足"); continue
        with open(RESULTS, "a", encoding="utf-8") as f:
            rec["t_wall"] = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        done[rec["key"]] = rec
        is_best, b = update_best(rec)
        print(f"    平均IC {rec['mean_ic']:+.4f}  t={rec['t']:.2f}  "
              f"正窗口 {rec['pos']}/{rec['n_win']} ({rec['pos_pct']*100:.0f}%)  "
              f"({rec['elapsed']}s)"
              + ("  🏆 新最优" if is_best else ""))
    return done


def show_status():
    done = load_done()
    total = int(np.prod([len(v) for v in SPACE.values()]))
    print("=" * 92)
    print("ML 自动迭代 —— 状态")
    print("=" * 92)
    print(f"  搜索空间 {total} 个配置 · 已试 {len(done)} 个 "
          f"({len(done)/total*100:.1f}%)")
    if not done:
        print("  尚无结果"); return
    rows = sorted(done.values(), key=lambda r: -r["t"])
    print()
    print(f"  {'配置':<50}{'平均IC':>9}{'t值':>7}{'正窗口':>9}")
    print("  " + "-" * 76)
    for r in rows[:10]:
        c = r["config"]
        lab = f"L={c['seq_len']} {c['model']} h={c['hidden']} l={c['layers']} d={c['dropout']}"
        print(f"  {lab:<50}{r['mean_ic']:>+9.4f}{r['t']:>7.2f}"
              f"{str(r['pos'])+'/'+str(r['n_win']):>9}")
    print("  " + "-" * 76)

    # 多重比较提醒
    ts = np.array([r["t"] for r in rows])
    print()
    print(f"  t 值分布: 最好 {ts.max():.2f} · 中位 {np.median(ts):.2f} · 最差 {ts.min():.2f}")
    print(f"  |t|>2 的比例: {(np.abs(ts)>2).mean()*100:.0f}%")
    print()
    print("  ⚠️ 搜索出的『最优』会过拟合搜索空间。判断依据：")
    print(f"     在 {len(rows)} 次尝试中，t>2 的期望假阳性约 "
          f"{len(rows)*0.025:.1f} 个（单尾）")
    if os.path.exists(BEST):
        b = json.load(open(BEST, encoding="utf-8"))
        print()
        print(f"  当前最优: {b['config']}")
        print(f"    平均IC {b['mean_ic']:+.4f} · t={b['t']:.2f} · "
              f"正窗口 {b['pos']}/{b['n_win']} · 更新于 {b.get('updated_at')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60, help="每轮间隔（秒）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        show_status(); return

    if not args.daemon:
        print("=" * 92)
        print("ML 自动迭代（单轮）")
        print("=" * 92)
        one_round(args.n, args.seed)
        print()
        show_status()
        return

    print(f"  ML 自动迭代守护进程启动（每轮 {args.n} 个配置，间隔 {args.interval}s）")
    print(f"  结果 → {RESULTS}    最优 → {BEST}")
    while True:
        try:
            print(f"\n── {pd.Timestamp.now(tz='Asia/Shanghai'):%Y-%m-%d %H:%M} ──")
            one_round(args.n, args.seed)
        except Exception as exc:
            print(f"  ⚠ 异常: {type(exc).__name__}: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
