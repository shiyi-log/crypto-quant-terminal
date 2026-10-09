#!/usr/bin/env python3
"""
状态：训练暂停，仅允许 --dry-plan 打印离线计划；历史判据不代表部署可用性。

自动迭代闭环 —— 自动【生产】新版本候选（challenger），绝不自动上线

与「自动迭代系统.md」的分工：
    auto_iterate.py   运维巡检：C1~C5，只诊断不改策略（每 6 小时）
    auto_research.py  模型研究：自动探索 → 评估 → 登记 challenger（本文件）

闭环四步：
    ① 探索    在声明的搜索空间内生成下一组配置（OFAT → 爬山 → 组合探索）
    ② 评估    扩展窗口走查 + 逐窗口 IC
    ③ 判定    t > 2（季度口径）且 FDR 校正后 q < 0.05
              且 多窗口宽度稳健（月度/季度/半年三种宽度下 t 都 > 2）
              —— 第 24 轮修订：旧判据「正窗口 >= 80%」与 t 门槛不自洽
                 （n=22 时 80% 正窗口等价于 t >= 3.95），且不是良定义指标
    ④ 登记    最优 trial 登记为 ml_model 层的新版本（challenger）
              达标与否都登记 —— 未达标也是证据，避免"只报喜"式过拟合

硬边界（代码级保证）：
    · 只写 research_trials.jsonl / model_versions.json / logs / 报告
    · 任何 user_data/config*.json 都在写入前被断言拦截 —— 不碰实盘
    · promote（上线）只能由人执行 model_registry.py promote

防 p-hacking：
    · 所有 trial（含失败）逐条落盘，可追溯、可复算
    · 多重比较用 Benjamini-Hochberg FDR 校正，挑出来的最优必须过校正
    · 搜索空间由 --space-file 显式声明，不接受"边跑边改判据"

用法：
    cd bot
    python auto_research.py --quick                 # 冒烟（1 trial，少窗口）
    python auto_research.py --max-trials 24 --max-minutes 240
    python auto_research.py --space-file my_space.json --max-trials 40
"""

import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
TZ_OFFSET = 8
os.chdir(BASE)
sys.path.insert(0, BASE)

import model_registry as registry  # noqa: E402

TRIALS_PATH = "user_data/research_trials.jsonl"
LOG_PATH = os.path.join(ROOT, "logs", "research.log")

# ══════════════════ 搜索空间（显式声明） ══════════════════
# 「最新版本寻找更多可能」= 在声明空间内系统探索，而不是随机试。
SPACE = {
    "kind": ["lstm", "gru", "transformer", "cnn"],
    "seq_len": [30, 60, 120],
    "hidden": [32, 64, 128],
    "layers": [1, 2],
    "dropout": [0.3, 0.5],
    "lr": [0.001, 0.0005],
}
BASELINE = {"kind": "lstm", "seq_len": 30, "hidden": 64, "layers": 1,
            "dropout": 0.3, "lr": 0.001, "epochs": 8}

# 判据（冻结，不许自动放宽）
# ── 判据（第 24 轮修订）──
# 修订理由（有实测支撑，见 深度学习-迭代日志.md 第 9/11/12/24 轮）：
#   旧判据 "t > 2 且 正窗口占比 >= 80%" 数学上不自洽：
#     正窗口占比 = Φ(t/√n)；n=22（季度窗口）时 80% 正窗口 ⟺ t >= 3.95
#     两个门槛差 2 倍，导致 12 个 trial 全部 t>2、8 个过 FDR、0 个过正窗口
#   且「正窗口占比」不是良定义的判据：宽度从 1 月调到 6 月，占比从 65% 变到 90%
#     15 个随机种子最高只有 76% —— 任何种子都过不了
# 新判据：把「正窗口占比」换成【多窗口宽度下 t 都 > 2】的稳健性检验，
#         不可被单一参数操纵。
CRIT_T = 2.0
CRIT_Q = 0.05
CRIT_ROBUST = True          # 要求 月度/季度/半年 三种窗口宽度下 t 都 > 2
CRIT_POS = 0.80             # 保留常量供历史记录比对，但不再作为门槛
CRITERIA_TEXT = ("逐窗口 IC 的 t > 2（季度口径）且 FDR 校正后 q < 0.05 "
                 "且 多窗口宽度稳健（月度/季度/半年三种宽度下 t 都 > 2）")


def forbidden(path: str) -> bool:
    """实盘配置是不可写区。"""
    p = os.path.abspath(path)
    return os.path.basename(p).startswith("config") and p.endswith(".json")


def safe_write(path: str, mode: str = "a"):
    if forbidden(path):
        raise RuntimeError(f"拒绝写入实盘配置区: {path}")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    return open(path, mode, encoding="utf-8")


class Tee:
    def __init__(self, path):
        self.f = safe_write(path, "a")
        self.f.write(f"\n{'═' * 90}\n  {datetime.now():%Y-%m-%d %H:%M:%S}\n{'═' * 90}\n")

    def __call__(self, *a):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        self.f.write(msg + "\n")
        self.f.flush()


class Progress:
    """
    给前端「回测与任务」页写的实时进度（user_data/research_progress.json）。

    字段刻意与 run_backtest_task.py 的 run_progress.json 保持同构，
    这样同一张任务卡片的展示逻辑可以复用。
    原子写：先写 .tmp 再 replace，避免前端读到半个文件。
    """

    def __init__(self, round_no, run_id, max_trials, max_minutes, space, smoke=False):
        self.path = "user_data/research_progress_smoke.json" if smoke else \
            "user_data/research_progress.json"
        self.started = time.time()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        # 只统计「真正训练过」的 trial：续跑复用的缓存 trial 耗时为 0，会污染速率/ETA
        self.real_done = 0
        self.real_seconds = 0.0
        self.state = {
            "status": "running",
            "kind": "auto_research",
            "phase": "启动中",
            "round": round_no,
            "run_id": run_id,
            "trial": 0,
            "total": max_trials,
            "percent": 0.0,
            "cuts": None,
            "current": None,
            "current_label": None,
            "best": None,
            "recent": [],
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "rate_per_min": None,
            "budget_minutes": max_minutes,
            "space": space,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "updated_at": None,
            "smoke": smoke,
            "result": None,
        }
        self.flush()
        # 心跳：单 trial 可能跑几分钟，期间也要让「已用时 / 预计剩余」在动
        threading.Thread(target=self._beat, daemon=True).start()

    def _beat(self):
        while not self._stop.wait(5):
            if self.state.get("status") == "running":
                try:
                    self.flush()
                except Exception:
                    pass

    def flush(self, **kw):
        with self.lock:
            self.state.update(kw)
            self.state["elapsed_seconds"] = round(time.time() - self.started)
            done = self.state.get("trial") or 0
            total = self.state.get("total") or 0
            if self.real_done > 0 and self.real_seconds > 0:
                per = self.real_seconds / self.real_done          # 真实单 trial 秒数
            elif done > 0 and self.state["elapsed_seconds"] > 0:
                per = self.state["elapsed_seconds"] / done
            else:
                per = None
            if per:
                self.state["rate_per_min"] = round(60 / per, 2)
                self.state["avg_trial_seconds"] = round(per)
                self.state["eta_seconds"] = round(per * max(total - done, 0))
            self.state["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds")
            tmp = self.path + ".tmp"
            with safe_write(tmp, "w") as f:
                json.dump(self.state, f, ensure_ascii=False, default=float)
            os.replace(tmp, self.path)

    def trial_start(self, idx, total, cfg, phase):
        self.flush(status="running", trial=idx - 1, total=total, current=dict(cfg),
                   current_label=label_of(cfg), cuts=None,
                   phase=f"trial {idx}/{total} · {phase}",
                   percent=round((idx - 1) / max(total, 1) * 100, 1))

    def trial_cuts(self, idx, total, done, cuts_total, label=""):
        """走查分段进度 —— 让进度条在单个 trial 内部也能动"""
        frac = done / max(cuts_total, 1)
        self.state["cuts"] = {"done": done, "total": cuts_total, "label": label}
        self.flush(
            phase=f"trial {idx}/{total} · 走查 {done}/{cuts_total} 段"
                  + (f" · {label}" if label else ""),
            percent=round(((idx - 1) + frac) / max(total, 1) * 100, 1),
        )

    def trial_done(self, idx, total, r, cached=False):
        if not cached:
            self.real_done += 1
            self.real_seconds += float(r.get("seconds") or 0)
        recent = ([{"label": label_of(r["config"]), "t": r.get("t_period"),
                    "ic": r.get("ic_period"), "pos": f"{r.get('pos_windows')}/"
                    f"{r.get('n_windows')}", "seconds": r.get("seconds"),
                    "phase": r.get("phase")}]
                  + self.state.get("recent", []))[:8]
        best = self.state.get("best")
        t = r.get("t_period")
        if t is not None and (best is None or t > (best.get("t_period") or -9e9)):
            best = {"label": label_of(r["config"]), "t_period": t,
                    "ic_period": r.get("ic_period"), "config": r["config"],
                    "pos_windows": r.get("pos_windows"),
                    "n_windows": r.get("n_windows")}
        self.flush(trial=idx, total=total, recent=recent, best=best,
                   percent=round(idx / max(total, 1) * 100, 1))

    def finish(self, status, phase, result=None):
        self._stop.set()
        self.flush(status=status, phase=phase, percent=100.0,
                   eta_seconds=0, cuts=None, result=result)


def label_of(cfg: dict) -> str:
    return (f"{cfg.get('kind')} L={cfg.get('seq_len')} h={cfg.get('hidden')} "
            f"ly={cfg.get('layers')} dp={cfg.get('dropout')} lr={cfg.get('lr')}")


# ══════════════════ 搜索策略 ══════════════════

def cfg_key(cfg: dict) -> str:
    core = {k: cfg[k] for k in sorted(SPACE) if k in cfg}
    return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:8]


# 选优口径（--select-by 设置）。
#   "t"          按集成 t（历史行为）
#   "worst-seed" 按【最差种子】的 t —— 直接优化最坏情况
# 为什么需要后者：第 38/41 轮实测表明，集成 t 不反映种子稳健性。
#   · lstm L=30 h=96 ly=1  集成 t季=3.37（3 种子）→ 5 种子只过 2/5，最差种子 1.95
#   · gru  L=30 h=128 ly=2 集成 t季=2.90（3 种子）→ 5 种子全过，  最差种子 2.60
#   按集成 t 排会把前者排在前面；按最差种子排会把后者排前面。
# ⚠ 注意：缺少 seed_t_min 的老记录会退回 t_period，
#   两类记录的数值不完全同尺度（seed_t_min 系统性地 ≤ t_period）——
#   这是过渡期的已知偏差，新记录都带 seed_ts 后会消失。
SELECT_BY = "t"


def objective(r: dict) -> tuple:
    """选优键；并列时看正窗口占比，再看 IC。

    --select-by worst-seed 时用最差种子的 t（没有则退回集成 t）。
    """
    if SELECT_BY == "worst-seed":
        _sm = r.get("seed_t_min")
        t = _sm if _sm is not None else r.get("t_period")
    else:
        t = r.get("t_period")
    t = -9e9 if t is None or not np.isfinite(t) else t
    pos = (r.get("pos_windows") or 0) / max(r.get("n_windows") or 1, 1)
    ic = r.get("ic_period") or 0.0
    return (round(t, 6), round(pos, 6), round(ic, 6))


def propose(evaluated: list, space: dict, baseline: dict, skip=None):
    """
    纯函数：给定已评估集合，返回下一组待跑配置和所处阶段。
    三个阶段（顺序确定，可中断续跑 —— 因为只依赖已评估集合）：
      A ofat-baseline  以基线为中心的单变量扫描（先看各维度方向）
      B hill           以当前最优为中心的单变量爬山（收敛即结束）
      C explore        组合探索：2~3 维组合（剩余预算内继续找更多可能）

    skip：已经评估过、且一定会命中缓存的配置 key 集合。
        ⚠ 必须传！否则 propose 只看到本轮，会从同一条确定性序列的开头重新走，
        前面全是缓存命中 —— 预算被"重走已知区域"吃光（实测重复率 71%，
        搜索空间覆盖率只有 6%）。
        只传【会命中缓存的】key：需要重跑的（如种子数不符）不能跳，
        否则那些配置永远得不到重评。
    """
    have = {cfg_key(e["config"]) for e in evaluated}
    if skip:
        have |= set(skip)
    if not evaluated:
        return dict(baseline), "A-ofat:baseline"

    best = max(evaluated, key=objective)["config"]

    # A：基线单变量扫描
    for dim, vals in space.items():
        for v in vals:
            c = {**baseline, dim: v}
            if cfg_key(c) not in have:
                return c, f"A-ofat-baseline:{dim}={v}"

    # B：当前最优的单变量邻域
    for dim, vals in space.items():
        for v in vals:
            if v == best.get(dim):
                continue
            c = {**best, dim: v}
            if cfg_key(c) not in have:
                return c, f"B-hill:{dim}={v}"

    # C：组合探索（确定性伪随机，可复现）
    rng = np.random.RandomState(20261008)
    dims = sorted(space)
    for _ in range(4000):
        k = rng.randint(2, 4)
        pick = rng.choice(dims, size=k, replace=False)
        c = dict(best)
        for d in pick:
            c[d] = space[d][rng.randint(len(space[d]))]
        c = {**baseline, **{d: c[d] for d in dims}}
        if cfg_key(c) not in have:
            return c, "C-explore:" + "+".join(sorted(pick))
    return None, "done"


# ══════════════════ 评估（判据冻结） ══════════════════

class PanelCache:
    """序列张量很占内存（seq_len=120 约 1GB），只缓存最近 N 个长度。

    底层走 panel_cache：进程内 LRU + 磁盘缓存，避免每次启动重建（66~220s）。
    """

    def __init__(self, maxsize=1):
        self.maxsize, self.store, self.order = maxsize, {}, []
        self.on_note = None  # 可选：构建面板时向外报进度
        self.disk_hits = 0

    def get(self, seq_len, log):
        if seq_len in self.store:
            self.order.remove(seq_len)
            self.order.append(seq_len)
            return self.store[seq_len]
        import panel_cache
        disk_before = os.path.exists(panel_cache.paths(seq_len)[0])
        if self.on_note:
            self.on_note("加载序列面板 seq_len=" + str(seq_len) if disk_before
                         else f"构建序列面板 seq_len={seq_len}（约 1~3 分钟）")
        seq, meta, feats = panel_cache.load_or_build(seq_len, dense=True, log=log)
        if disk_before:
            self.disk_hits += 1
        if seq.nbytes > 1.2e9:
            log("    ⚠ 张量较大，注意内存")
        self.store[seq_len] = (seq, meta, feats)
        self.order.append(seq_len)
        while len(self.order) > self.maxsize:
            old = self.order.pop(0)
            self.store.pop(old, None)
            gc.collect()
        return self.store[seq_len]


def evaluate(cfg: dict, cache: PanelCache, device: str, step: int, log,
             quick: bool = False, on_step=None, bs: int = 512,
             seeds: int = 1) -> dict:
    """扩展窗口走查 + 逐窗口 IC（quarterly，与第 6/7/8 轮完全同口径）。"""
    raise RuntimeError(
        "LEGACY_DISABLED: auto_research.evaluate 训练已暂停。旧切分未按 t1 净化、"
        "整段排名不可部署；修正前不得继续训练或登记候选。"
    )
    import torch
    import ml_seq
    from ml_regime import ic_t

    seq_len = int(cfg["seq_len"])
    seq, meta, feats = cache.get(seq_len, log)
    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values

    start = pd.Timestamp("2021-07-01")
    cuts, t0 = [], start
    end = dates.max()
    while t0 < end:
        cuts.append((t0, min(t0 + pd.DateOffset(months=step), end + pd.Timedelta(days=1))))
        t0 = t0 + pd.DateOffset(months=step)
    if quick:
        cuts = cuts[-2:]

    epochs = 1 if quick else int(cfg.get("epochs", 8))
    scores = np.full(len(meta), np.nan)
    # 逐种子分数容器：必须在段循环【外面】初始化，
    # 否则每段都重置，只剩最后一段（第 36 轮踩过这个坑）
    n_seeds = max(1, int(seeds))
    per_seed_scores = [np.full(len(meta), np.nan) for _ in range(n_seeds)]
    n_cuts = len(cuts)
    if on_step:
        on_step(0, n_cuts, f"面板就绪 {seq.shape[0]} 事件")
    for ci, (a, b) in enumerate(cuts, 1):
        tr = (dates < a).values
        te = ((dates >= a) & (dates < b)).values
        if tr.sum() < 2000 or te.sum() < 100:
            if on_step:
                on_step(ci, n_cuts, "样本不足跳过")
            continue
        # 标准化统计量只来自训练期；就地运算，避免 astype/nan_to_num 产生 3 份拷贝
        mu = seq[tr].reshape(-1, F).mean(axis=0)
        sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = seq[tr].astype(np.float32)
        Xtr -= mu
        Xtr /= sd
        np.nan_to_num(Xtr, copy=False)
        Xte = seq[te].astype(np.float32)
        Xte -= mu
        Xte /= sd
        np.nan_to_num(Xte, copy=False)
        # 多种子（第 24 轮）：同一配置跑 n_seeds 个随机初始化，
        # **段内秩平均** —— 不能全局平均，否则分数尺度跨段漂移会造假（第 13 轮教训）
        preds = []
        for si in range(n_seeds):
            m = ml_seq.train_seq(Xtr, y[tr], F, cfg["kind"], device, epochs=epochs, bs=bs,
                                 lr=float(cfg["lr"]), hidden=int(cfg["hidden"]),
                                 layers=int(cfg["layers"]), dropout=float(cfg["dropout"]),
                                 seed=42 + si * 37)
            preds.append(ml_seq.predict_seq(m, Xte, device))
            del m
        if len(preds) == 1:
            scores[te] = preds[0]
        else:
            R = np.vstack([pd.Series(x).rank(pct=True).values for x in preds])
            scores[te] = R.mean(axis=0)
        # 逐种子分数也要留：集成分只说明"平均能到多少"，
        # 逐种子分才能回答"稳不稳"（规则 13：种子方差占总方差 87%）
        if n_seeds > 1:
            for si, pr in enumerate(preds):
                per_seed_scores[si][te] = pr
        del Xtr, Xte, preds
        if on_step:
            on_step(ci, n_cuts, f"训练 {pd.Timestamp(b).strftime('%Y-%m')} 前")
    gc.collect()

    ok = ~np.isnan(scores)
    if ok.sum() < 500:
        return {"config": cfg, "ic_period": None, "t_period": None, "pos_windows": 0,
                "n_windows": 0, "n": int(ok.sum()), "error": "样本不足"}

    sub = meta[ok].copy()
    scores = scores[ok]
    icw, tw, npos, nwin = ml_seq.period_ic(sub, scores)
    icp, tp = ic_t(scores, sub["ret"].values)
    # 多窗口宽度稳健性（第 24 轮加入）
    try:
        rmulti = ml_seq.period_ic_multi(sub, scores)
    except Exception:
        rmulti = {}

    # 逐窗口 IC 的 p 值（t 分布，df = 窗口数-1）
    p = None
    if nwin > 2 and tw is not None and np.isfinite(tw):
        try:
            from scipy import stats
            p = float(2 * stats.t.sf(abs(tw), df=nwin - 1))
        except Exception:
            from math import erfc, sqrt
            p = float(erfc(abs(tw) / sqrt(2)))

    # 逐种子 t（仅多种子时）：回答"这个配置是真稳，还是靠集成分兜住了"
    seed_ts = []
    if n_seeds > 1:
        for si in range(n_seeds):
            sc = per_seed_scores[si]
            okk = ~np.isnan(sc)
            if okk.sum() < 500:
                continue
            try:
                # ⚠ 必须与集成分用【同一口径】：逐窗口 IC 的 t（period_ic），
                # 不能用 ic_t() —— 那是池化 t，把全部事件当一个样本，
                # n 被夸大导致 t 虚高（实测集成分 2.5 vs 池化 7.9，差 3 倍）。
                _, t_si, _, _ = ml_seq.period_ic(meta[okk], sc[okk])
                if t_si is not None and np.isfinite(t_si):
                    seed_ts.append(round(float(t_si), 4))
            except Exception:
                pass

    return {"config": cfg, "n_seeds": max(1, int(seeds)),
                "seed_ts": seed_ts,
                "seed_t_min": (min(seed_ts) if seed_ts else None),
                "seed_t_mean": (round(float(np.mean(seed_ts)), 4) if seed_ts else None),
                "seed_t_std": (round(float(np.std(seed_ts, ddof=1)), 4)
                               if len(seed_ts) > 1 else None),
            "ic_period": icw, "t_period": tw, "pos_windows": npos,
            "n_windows": nwin, "n": int(ok.sum()), "ic_pool": icp, "t_pool": tp,
            "robust": rmulti.get("robust"),
            "robust_pass": rmulti.get("n_pass"),
            "robust_total": rmulti.get("n_total"),
            "t_month": (rmulti.get("month") or {}).get("t"),
            "t_quarter": (rmulti.get("quarter") or {}).get("t"),
            "t_half": (rmulti.get("half") or {}).get("t"),
            "p_value": p, "quick": quick}


# ══════════════════ 多重比较校正 ══════════════════

def bh_fdr(pvals: list) -> list:
    """Benjamini-Hochberg FDR。返回与输入同序的 q 值。

    ⚠ m 必须用【有效检验数】（非 None 的个数），不能用 len(pvals)。
      否则一旦有 trial 失败（错误记录没有 p_value），
      函数内部用的 m 会大于落盘的 fdr_m，
      q 值算出来对不上 → audit_judgment 的「FDR 可复算」检查会永久失败。
      （实测：m=5/fdr_m=4 时 q 从 0.004 变成 0.005）
    """
    idx = [i for i, p in enumerate(pvals) if p is not None and np.isfinite(p)]
    m = len(idx)                      # ← 有效检验数，与 fdr_m 口径一致
    q = np.ones(len(pvals))
    if not idx:
        return list(q)
    ps = sorted(idx, key=lambda i: pvals[i])
    prev = 1.0
    for rank in range(len(ps), 0, -1):
        i = ps[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        q[i] = prev
    return [float(x) for x in q]


# ══════════════════ 主流程 ══════════════════

def load_trials() -> list:
    if not os.path.exists(TRIALS_PATH):
        return []
    out = []
    with open(TRIALS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def next_round_no() -> int:
    import re
    mx = 0
    for fn in os.listdir(ROOT):
        m = re.match(r"模型迭代-第(\d+)轮", fn)
        if m:
            mx = max(mx, int(m.group(1)))
    if os.path.exists("user_data/ml_seq_results.jsonl"):
        with open("user_data/ml_seq_results.jsonl", encoding="utf-8") as f:
            for line in f:
                try:
                    mx = max(mx, int(json.loads(line).get("round", 0)))
                except Exception:
                    pass
    return mx + 1


def wait_idle(log) -> None:
    waited = False
    while subprocess.run(["pgrep", "-f", "freqtrade backtesting"],
                         capture_output=True).returncode == 0:
        if not waited:
            log("  ⏳ 检测到 freqtrade 回测在跑，等待其结束以避让资源…")
            waited = True
        time.sleep(20)
    if waited:
        log("  ✅ 前序回测已结束，继续")


def write_report(run: dict, out_path: str) -> None:
    rows = sorted([r for r in run["results"] if r.get("t_period") is not None],
                  key=objective, reverse=True)
    L = []
    title = "冒烟测试（管道验证，非正式轮次）" if run.get("smoke") else \
        f"第 {run['round']} 轮"
    L.append(f"# 模型迭代{title} —— 自动搜索报告\n")
    L.append(f"> 由 `bot/auto_research.py` 自动生成 · {run['started']} 起 · "
             f"耗时 {run['elapsed'] / 60:.1f} 分钟\n>")
    L.append(f"> 搜索空间：{', '.join(f'{k}∈{v}' for k, v in run['space'].items())}\n>")
    L.append(f"> 判据（第 24 轮修订）：**{CRITERIA_TEXT}**\n")
    L.append("\n## 一、结论\n")
    best = run.get("best")
    if best:
        g = run["best_gate"]
        L.append(f"- 本轮最优：`{best['config']['kind']} L={best['config']['seq_len']} "
                 f"h={best['config']['hidden']} layer={best['config']['layers']} "
                 f"drop={best['config']['dropout']} lr={best['config']['lr']}`")
        qv = best.get("q_value")
        qtxt = "—" if qv is None else f"{qv:.4f}"
        L.append(f"- 逐窗口 IC **{best['ic_period']:+.4f}** · t=**{best['t_period']:.2f}**"
                 f"（季度口径） · q={qtxt}")
        L.append(f"- 判定：{'✅ **达标**' if g['passed'] else '❌ **未达标**'}")
        for r in g["reasons"]:
            L.append(f"  - {r}")
        if run.get("smoke"):
            L.append("- 冒烟模式：未登记版本、未写正式账本")
        else:
            L.append(f"- 已登记为 challenger：`{run['version_id']}`（**未上线**）")
    else:
        L.append("- 本轮无有效 trial（样本不足或全部失败）")
    L.append("\n## 二、全部 trial（含失败，防选择性报告）\n")
    L.append("| # | 阶段 | 配置 | 逐窗口IC | t | 正窗口 | 池化IC | p | q | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(run["results"], 1):
        c = r["config"]
        spec = (f"{c['kind']} L={c['seq_len']} h={c['hidden']} "
                f"ly={c['layers']} dp={c['dropout']} lr={c['lr']}")
        ip = "—" if r.get("ic_period") is None else f"{r['ic_period']:+.4f}"
        tp = "—" if r.get("t_period") is None else f"{r['t_period']:.2f}"
        pos = (f"{r.get('pos_windows')}/{r.get('n_windows')}"
               if r.get("n_windows") else "—")
        pool = "—" if r.get("ic_pool") is None else f"{r['ic_pool']:+.4f}"
        p = "—" if r.get("p_value") is None else f"{r['p_value']:.4f}"
        q = "—" if r.get("q_value") is None else f"{r['q_value']:.4f}"
        mark = "✅" if r.get("passed") else "❌"
        L.append(f"| {i} | {r.get('phase', '')} | {spec} | {ip} | {tp} | {pos} | "
                 f"{pool} | {p} | {q} | {mark} |")
    L.append(f"\n> 共 {len(run['results'])} 个 trial；多重比较用 BH-FDR 校正 "
             f"（m={len(run['results'])}）。全部原始记录见 "
             f"`bot/user_data/research_trials.jsonl`。\n")
    ch = run.get("champion")
    if ch and best:
        L.append("\n## 三、与当前 champion 对比\n")
        L.append(f"| | {ch['id']}（正在使用） | {run.get('version_id')}（本轮候选） |")
        L.append("|---|---|---|")
        cm = ch.get("metrics") or {}
        L.append(f"| 逐窗口 IC | {cm.get('ic_period')} | {best['ic_period']:+.4f} |")
        L.append(f"| t 值 | {cm.get('t_period')} | {best['t_period']:.2f} |")
        L.append(f"| 正窗口 | {cm.get('pos_windows')}/{cm.get('n_windows')} | "
                 f"{best['pos_windows']}/{best['n_windows']} |")
        L.append(f"| 是否达标 | {ch.get('gate', {}).get('passed')} | "
                 f"{run.get('best_gate', {}).get('passed')} |")
        L.append("| 是否上线 | " + ("是" if ch.get("deployed") else "否") + " | 否 |")
    L.append("\n## 四、下一步（需人工确认）\n")
    L.append("- [ ] 复核本轮最优是否过 FDR（未过则视为噪声）")
    L.append("- [ ] 若要上线：`python model_registry.py promote "
             f"{run.get('version_id')} --note \"理由\"` 并同步实盘 config")
    L.append("- [ ] 继续扩大探索空间：`--space-file` 加入集成 / 标签 / 特征维度\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description="模型自动迭代闭环（只产 challenger）")
    ap.add_argument("--max-trials", type=int, default=24)
    ap.add_argument("--max-minutes", type=float, default=240)
    ap.add_argument("--step", type=int, default=6, help="走查步长（月）")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--space-file", help="自定义搜索空间 JSON（探索更多可能）")
    ap.add_argument("--panel-cache", type=int, default=3)
    ap.add_argument("--threads", type=int, default=8,
                    help="实测 8 比 2 快约 1.9 倍，且结果逐位一致（纯免费提速）")
    ap.add_argument("--seeds", type=int, default=3,
                    help="每配置的随机种子数（第 24 轮加入）。实测种子方差占 87%%，"
                         "默认 3 个做段内秩平均；1 表示旧行为")
    ap.add_argument("--bs", type=int, default=512,
                    help="训练批量。⚠ 实测 2048 虽快 2.2 倍，但会把基线 t 从 3.02 打到 "
                         "2.00（欠训练），会静默改变判据口径 —— 非必要不要调大")
    ap.add_argument("--device", default=None, help="mps / cpu（默认自动）")
    ap.add_argument("--prewarm", action="store_true",
                    help="先把搜索空间里所有 seq_len 的面板构建/落盘，再开始搜索")
    ap.add_argument("--quick", action="store_true", help="冒烟：1 trial、少窗口、1 epoch")
    ap.add_argument("--no-wait", action="store_true", help="不等待 freqtrade 回测")
    ap.add_argument("--dry-plan", action="store_true", help="只打印搜索计划，不训练")
    ap.add_argument("--select-by", choices=["t", "worst-seed"], default="t",
                    help="选优口径：t=集成 t（默认）；"
                         "worst-seed=最差种子的 t（直接优化最坏情况）")
    args = ap.parse_args()

    if not args.dry_plan:
        raise SystemExit(
            "LEGACY_DISABLED: auto_research.py 训练已暂停。旧切分未按 t1 净化、"
            "整段排名不可部署；仅允许 --dry-plan，不写账本、不登记、不训练。"
        )

    global SELECT_BY
    SELECT_BY = args.select_by

    global SPACE
    if args.space_file:
        with open(args.space_file, encoding="utf-8") as f:
            SPACE = json.load(f)
    if args.quick:
        args.max_trials = min(args.max_trials, 1)
        args.max_minutes = min(args.max_minutes, 10)

    # The allowed plan path is deliberately before Tee, Progress, cache builds,
    # model imports and registry writes. It does not reopen a paused daemon.
    print("TRAINING_PAUSED: 仅打印离线计划，不训练、不读取旧 trial 分数、不写文件。")
    planned = []
    for _ in range(max(0, args.max_trials)):
        cfg, phase = propose(planned, SPACE, {**BASELINE, "epochs": args.epochs})
        if cfg is None:
            break
        print(f"  [plan] {phase} {cfg_key(cfg)} {cfg}")
        planned.append({"config": cfg, "t_period": 0, "pos_windows": 0, "n_windows": 1})
    return

    log = Tee(LOG_PATH)
    smoke = args.quick
    trials_path = "user_data/research_trials_smoke.jsonl" if smoke else TRIALS_PATH
    if smoke:
        log("  ⚠ 冒烟模式：只验证管道；写入 *_smoke.jsonl，不登记版本、不产出正式轮次报告")
    import torch
    torch.set_num_threads(args.threads)
    device = args.device or ("mps" if torch.backends.mps.is_available() else "cpu")

    if args.seeds > 1:
        log(f"  多种子：每配置 {args.seeds} 个种子，段内秩平均（规则 13）")
    log(f"🔬 自动迭代（研究层）· 设备 {device} · 上限 {args.max_trials} trials / "
        f"{args.max_minutes:.0f} 分钟 · bs={args.bs} · threads={args.threads}")
    log(f"  判据（第 24 轮修订）：{CRITERIA_TEXT}")
    log(f"  搜索空间：{json.dumps(SPACE, ensure_ascii=False)}")
    log("    （语义：以上是各维度的候选值；A 阶段以全局基线为中心做单变量扫描，"
        "因此部分 trial 的配置不在此空间内 —— 那些是 OFAT 对照）")

    t_start = time.time()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    trials = load_trials()
    done = {t["key"]: t for t in trials if t.get("key")} if not smoke else {}

    # 哪些配置【一定会命中缓存】—— propose 应该跳过它们。
    # 条件与下面缓存分支完全一致，只跳"必然复用"的，
    # 种子数不符 / 缺 robust 的仍然会被提议并重跑。
    _want_seeds = 1 if args.quick else args.seeds
    cacheable_keys = {
        k for k, v in done.items()
        if not v.get("quick")
        and v.get("robust") is not None
        and int(v.get("n_seeds") or 1) == _want_seeds
    } if not smoke else set()

    # 续跑：把历史同配置 trial 直接纳入本次评估集合（不重复训练）
    cache, evaluated_src = PanelCache(args.panel_cache), []
    round_no_early = next_round_no()
    # Progress 必须在预热之前创建：否则预热那十几分钟前端读到的还是上一轮的死数据
    prog = Progress(round_no_early, run_id, args.max_trials, args.max_minutes, SPACE,
                    smoke=smoke)
    if args.prewarm:
        import panel_cache as pc
        seen = []
        for v in SPACE.get("seq_len", []):
            if int(v) not in seen:
                seen.append(int(v))
        log(f"  🔥 预热面板：seq_len={seen}")
        for i, sl in enumerate(seen, 1):
            prog.flush(phase=f"预热面板 {i}/{len(seen)} · seq_len={sl}", percent=0.0,
                       current_label=f"panel_{sl}")
            pc.load_or_build(int(sl), log=log)
        prog.flush(phase="预热完成，开始搜索", current_label=None)
    if not args.no_wait and not args.dry_plan:
        wait_idle(log)

    if args.dry_plan:
        ev = []
        for _ in range(args.max_trials):
            c, phase = propose(ev, SPACE, {**BASELINE, "epochs": args.epochs},
                               skip=cacheable_keys)
            if c is None:
                break
            log(f"  [plan] {phase:<28} {cfg_key(c)}  {c}")
            ev.append({"config": c, "t_period": 0, "pos_windows": 0, "n_windows": 1})
        return

    results = []
    prog.flush(phase="等待空闲" if not args.no_wait else "准备中")
    try:
        while len(results) < args.max_trials:
            if (time.time() - t_start) / 60 > args.max_minutes:
                log(f"  ⏹ 达到时间预算 {args.max_minutes:.0f} 分钟，停止探索")
                prog.flush(status="stopped", phase=f"达到时间预算 "
                                                  f"{args.max_minutes:.0f} 分钟")
                break
            cfg, phase = propose(evaluated_src, SPACE,
                                 {**BASELINE, "epochs": args.epochs},
                                 skip=cacheable_keys)
            if cfg is None:
                log("  ⏹ 声明的搜索空间已穷尽")
                prog.flush(status="done", phase="搜索空间已穷尽")
                break
            key = cfg_key(cfg)

            # 第 24 轮：缓存必须同时满足
            #   ① 有 robust 字段（判据修订前的老记录没有 → 强制重跑）
            #   ② 种子数与本次一致（否则 3 种子跑会误用 1 种子的旧结果）
            if (key in done and not done[key].get("quick")
                    and done[key].get("robust") is not None
                    and int(done[key].get("n_seeds") or 1)
                        == (1 if args.quick else args.seeds)):
                hist = done[key]
                log(f"  ↺ {phase:<28} {key} 复用历史结果 "
                    f"(t={hist.get('t_period')})")
                # run_id 必须改写成本轮的：否则缓存 trial 会以旧 run_id 落盘，
                # 轮次账目（每组 trial 数 / 每组判定数）就对不上。
                r = {**hist, "config": cfg, "phase": phase + "(cached)",
                     "run_id": run_id, "cache_reused": True}
                results.append(r)
                evaluated_src.append(r)
                prog.trial_done(len(results), args.max_trials, r, cached=True)
                continue

            log(f"  ▶ trial {len(results) + 1}/{args.max_trials}  {phase}")
            log(f"    {cfg}")
            prog.trial_start(len(results) + 1, args.max_trials, cfg, phase)
            t0 = time.time()
            cache.on_note = (lambda msg, i=len(results) + 1: prog.flush(
                phase=f"trial {i}/{args.max_trials} · {msg}"))
            try:
                r = evaluate(cfg, cache, device, args.step, log, quick=args.quick,
                             bs=args.bs, seeds=(1 if args.quick else args.seeds),
                             on_step=lambda d, t, lab: prog.trial_cuts(
                                 len(results) + 1, args.max_trials, d, t, lab))
            except Exception as e:  # 单个 trial 失败不终止整轮
                log(f"    ❌ 失败：{type(e).__name__}: {e}")
                r = {"config": cfg, "ic_period": None, "t_period": None,
                     "pos_windows": 0, "n_windows": 0, "n": 0,
                     "error": f"{type(e).__name__}: {e}"}
            r["phase"] = phase
            r["key"] = key
            r["run_id"] = run_id
            r["seconds"] = round(time.time() - t0, 1)
            # 速度设置要落盘：不同 bs 的训练轨迹不可直接横向比较
            r["speed"] = {"device": device, "threads": args.threads, "bs": args.bs,
                          "epochs": (1 if args.quick else int(cfg.get("epochs", 8)))}
            ip = r.get("ic_period")
            tp = r.get("t_period")
            log(f"    → 逐窗口 IC {'—' if ip is None else f'{ip:+.4f}'} · "
                f"t {'—' if tp is None else f'{tp:.2f}'} · "
                f"正窗口 {r.get('pos_windows')}/{r.get('n_windows')} · {r['seconds']}s")
            with safe_write(trials_path) as f:
                f.write(json.dumps(r, ensure_ascii=False, default=float) + "\n")
            done[key] = r
            results.append(r)
            evaluated_src.append(r)
            prog.trial_done(len(results), args.max_trials, r)
    except Exception as e:
        prog.finish("failed", f"异常中止：{type(e).__name__}: {e}")
        raise

    if not results:
        log("  ❌ 无有效结果，退出")
        prog.finish("failed", "无有效结果")
        return

    # ── FDR 校正（对本轮全部 trial）──
    # 注：trial 在评估后立即写入账本（第 723 行），那时还没有 q/passed。
    #     所以下面算完后必须【回写】—— load_trials 用字典推导，后写的覆盖先写的。
    # FDR 的总体规模也要落盘：否则事后无法从账本复算 q
    # （「这轮的 m 是多少」只存在于内存里，一旦进程结束就丢了）
    pvals = [r.get("p_value") for r in results]
    fdr_m = sum(1 for x in pvals if x is not None)
    qs = bh_fdr(pvals)
    for r, q in zip(results, qs):
        r["q_value"] = q
        r["fdr_m"] = fdr_m
        r["passed"] = bool(
            r.get("t_period") is not None and np.isfinite(r["t_period"])
            and r["t_period"] > CRIT_T
            and r.get("n_windows", 0) > 0
            # 第 24 轮：正窗口门槛已移除（与 t 门槛不自洽，且非良定义指标）
            and (r.get("robust") is True if CRIT_ROBUST else True)
            and q is not None and q < CRIT_Q
        )
    # 回写带 q/passed 的记录（否则前端与后续轮次读不到判定结果）
    if not smoke:
        written = 0
        try:
            with safe_write(trials_path) as f:
                for r in results:
                    f.write(json.dumps(r, ensure_ascii=False, default=float) + "\n")
                    written += 1
        except Exception as e:
            log(f"  ⚠ 回写判定结果失败（已写 {written}/{len(results)}）："
                f"{type(e).__name__}: {e}")

        # 自检：账本里每个 key 的最后一条必须是已判定的。
        # 之前踩过的坑：回写只落了一部分，前端「判定」列全是「—」而没人发现。
        try:
            last = {}
            with open(trials_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    last[rec.get("key")] = rec
            missing = [r.get("key") for r in results
                       if (last.get(r.get("key")) or {}).get("passed") is None]
            if missing:
                log(f"  ⚠ 判定自检未通过：{len(missing)}/{len(results)} 个 trial "
                    f"在账本里没有判定结果 → {missing[:5]}")
            else:
                log(f"  ✔ 判定自检通过：{len(results)} 个 trial 均已落盘判定")
        except Exception as e:
            log(f"  ⚠ 判定自检失败：{type(e).__name__}: {e}")

    valid = [r for r in results if r.get("t_period") is not None]
    best = max(valid, key=objective) if valid else None

    # ── 判定与登记 ──
    round_no = round_no_early
    reg = registry.load()
    champ = registry.champion(reg, "ml_model")
    version_id, gate = None, {"criteria": CRITERIA_TEXT, "passed": False, "reasons": []}
    if best:
        pos_ratio = best["pos_windows"] / max(best["n_windows"], 1)
        reasons = [
            f"逐窗口 t={best['t_period']:.2f} {'✅' if best['t_period'] > CRIT_T else '❌'}"
            f"（阈值 >{CRIT_T}）",
            f"多窗口稳健性 "
            f"{'✅' if best.get('robust') else '❌'}"
            f"（月度 {best.get('t_month') if best.get('t_month') is None else round(best['t_month'],2)}"
            f" / 季度 {best.get('t_quarter') if best.get('t_quarter') is None else round(best['t_quarter'],2)}"
            f" / 半年 {best.get('t_half') if best.get('t_half') is None else round(best['t_half'],2)}，"
            f"通过 {best.get('robust_pass')}/{best.get('robust_total')}）",
            f"FDR 校正后 q={best.get('q_value'):.4f} "
            f"{'✅' if (best.get('q_value') or 1) < CRIT_Q else '❌'}（阈值 <{CRIT_Q}）",
        ]
        gate = {"criteria": CRITERIA_TEXT, "passed": bool(best.get("passed")),
                "reasons": reasons}
        version_id = f"auto-r{round_no}-{best['key']}"
        if not smoke:
            registry.upsert(reg, {
                "id": version_id,
                "layer": "ml_model",
                "label": f"第 {round_no} 轮自动搜索最优（{best['config']['kind']} "
                         f"L={best['config']['seq_len']}）",
                "status": "challenger",
                "source": "auto_research",
                "round": round_no,
                "run_id": run_id,
                "spec": dict(best["config"]),
                "metrics": {k: best.get(k) for k in
                            ("ic_period", "t_period", "pos_windows", "n_windows", "n",
                             "ic_pool", "t_pool", "p_value", "q_value")},
                "gate": gate,
                "deployed": None,
                "artifacts": [TRIALS_PATH],
                "notes": f"自动搜索 {len(results)} 个 trial 的最优；判据冻结，未自动上线。",
            })
            registry.save(reg)
            log(f"\n  🧪 已登记 challenger: {version_id}（未上线）")

    run = {"round": round_no, "started": datetime.fromtimestamp(t_start).strftime(
        "%Y-%m-%d %H:%M:%S"), "elapsed": time.time() - t_start, "space": SPACE,
        "results": results, "best": best, "best_gate": gate, "version_id": version_id,
        "champion": champ, "smoke": smoke}

    if smoke:
        report = os.path.join(ROOT, "logs", "research_smoke_report.md")
        run["version_id"] = "(冒烟，未登记)"
    else:
        report = os.path.join(ROOT, f"模型迭代-第{round_no}轮-自动报告.md")
    write_report(run, report)

    prog.finish("done", f"第 {round_no} 轮完成 · {len(results)} trials",
                result={"round": round_no, "version_id": version_id,
                        "passed": bool(best and best.get("passed")),
                        "best_label": label_of(best["config"]) if best else None,
                        "best_t": best.get("t_period") if best else None,
                        "n_trials": len(results),
                        "report": os.path.relpath(report, ROOT)})

    if not smoke:
        with safe_write("user_data/ml_seq_results.jsonl") as f:
            f.write(json.dumps({"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "round": round_no, "auto": True, "run_id": run_id,
                                "space": SPACE, "n_trials": len(results),
                                "best": best}, ensure_ascii=False, default=float) + "\n")

    # ── 汇总输出 ──
    log("")
    log("  " + "═" * 86)
    log(f"  {'冒烟测试' if smoke else f'第 {round_no} 轮'}自动迭代完成 · "
        f"{len(results)} trials · "
        f"{(time.time() - t_start) / 60:.1f} 分钟")
    log("  " + "═" * 86)
    top = sorted(valid, key=objective, reverse=True)[:5]
    log(f"  {'配置':<40}{'逐窗口IC':>10}{'t(季)':>8}{'t(月)':>8}"
        f"{'t(半年)':>9}{'稳健':>7}{'q':>9}  判定")
    log("  " + "-" * 96)
    for r in top:
        c = r["config"]
        lab = (f"{c['kind']} L={c['seq_len']} h={c['hidden']} "
               f"ly={c['layers']} dp={c['dropout']}")
        q = r.get("q_value")

        def _f(x):
            return "—" if x is None else f"{x:.2f}"

        log(f"  {lab:<40}{r['ic_period']:>+10.4f}{_f(r['t_period']):>8}"
            f"{_f(r.get('t_month')):>8}{_f(r.get('t_half')):>9}"
            f"{(str(r.get('robust_pass')) + '/' + str(r.get('robust_total'))):>7}"
            f"{('—' if q is None else f'{q:.4f}'):>9}  {'✅达标' if r['passed'] else '❌'}")
    log("  " + "-" * 96)
    log(f"  报告：{os.path.relpath(report, ROOT)}")
    log(f"  下次运行：cd bot && python auto_research.py --max-trials 24 "
        f"--max-minutes 240")
    log("  ⚠ 自动迭代只产候选，上线需人工：python model_registry.py promote "
        f"{version_id or '<id>'} --note \"理由\"")


if __name__ == "__main__":
    main()
