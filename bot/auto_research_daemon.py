#!/usr/bin/env python3
"""
模型自动迭代守护进程 —— 让 auto_research 持续跑（第 15 轮）

问题（用户实测发现）：
    `auto_research.py` 是【一次性任务】，跑完一轮（--max-trials）就退出，
    没有 --daemon。所以「模型自动迭代」在 round 9 结束后就停了。
    （对比：`auto_iterate.py` 有 --daemon，一直在跑）

方案：不改动 auto_research.py（那是项目所有者的代码），
      而是写一个外层守护，循环调用它。

与其它自动化的分工：
    auto_iterate.py           策略健康巡检 C1~C5（每 6 小时）
    refresh_research.py       策略研究页数据刷新（每 6 小时）
    auto_research.py          单轮模型搜索（一次性，产 challenger）
    auto_research_daemon.py   ← 本文件：让上一行持续跑

纪律不变：只产 challenger，不自动上线（promote 必须人工 + 写理由）。

用法:
    python auto_research_daemon.py                    # 常驻
    python auto_research_daemon.py --interval 300     # 每轮间隔 5 分钟
    python auto_research_daemon.py --once             # 只跑一轮（等价于直接调 auto_research）
    python auto_research_daemon.py --max-rounds 3     # 最多跑 3 轮
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

STATE = "user_data/auto_research_daemon.json"
LOG = "../logs/auto_research_daemon.log"
SPACE_DIR = "user_data/auto_spaces"

# ── 扩展后的搜索空间：比默认的 4×3×3×2×2×2 宽得多 ──
# 默认空间只有 288 种组合，且 propose() 的序列是确定性的；
# 一旦某个前缀被缓存，每轮 12 步就会全部落在缓存里（实测：缓存正好 12 个，
# 每轮正好 12 步 → 永远原地打转）。所以必须【每轮换空间】。
FULL_SPACE = {
    "kind": ["lstm", "gru", "transformer", "cnn"],
    "seq_len": [20, 30, 45, 60, 90, 120, 180],
    "hidden": [16, 32, 48, 64, 96, 128],
    "layers": [1, 2, 3],
    "dropout": [0.1, 0.2, 0.3, 0.5],
    "lr": [0.0003, 0.0005, 0.001, 0.002],
}

# 每轮的探索主题（轮换，保证覆盖不同方向）
THEMES = [
    ("architecture",  {"kind": ["lstm", "gru", "transformer", "cnn"],
                       "seq_len": [30, 60], "hidden": [32, 64, 128],
                       "layers": [1, 2], "dropout": [0.3], "lr": [0.001]}),
    ("seqlen_deep",   {"kind": ["lstm"], "seq_len": [20, 45, 90, 180],
                       "hidden": [48, 96], "layers": [1, 2],
                       "dropout": [0.2, 0.3], "lr": [0.0005, 0.001]}),
    ("regularize",    {"kind": ["lstm", "gru"], "seq_len": [30, 60],
                       "hidden": [32, 64], "layers": [1, 2, 3],
                       "dropout": [0.1, 0.2, 0.5], "lr": [0.0003, 0.001]}),
    ("learning_rate", {"kind": ["lstm"], "seq_len": [30, 60, 120],
                       "hidden": [64, 96], "layers": [1],
                       "dropout": [0.3], "lr": [0.0003, 0.002]}),
    ("capacity",      {"kind": ["lstm", "transformer"], "seq_len": [60, 120],
                       "hidden": [16, 96, 128], "layers": [1, 2],
                       "dropout": [0.2], "lr": [0.001]}),
    ("cnn_gru",       {"kind": ["cnn", "gru"], "seq_len": [20, 45, 60, 90],
                       "hidden": [32, 48, 64], "layers": [1, 2],
                       "dropout": [0.1, 0.3], "lr": [0.0005, 0.001]}),
]


def write_space(idx):
    """
    按轮次写一个搜索空间文件，返回路径。

    ⚠ 空间文件的语义：声明的是【各维度的候选值】，不是【本轮会评估的配置笛卡尔积】。
      auto_research.py 的 A 阶段以全局 BASELINE 为中心做单变量扫描
      （`c = {**baseline, dim: v}`），因此约一半 trial 的配置不落在本空间内 ——
      那些是 OFAT 对照，用来单独归因每个维度的方向。
      详见 深度学习-迭代日志.md 第 31 轮。
    """
    os.makedirs(SPACE_DIR, exist_ok=True)
    name, space = THEMES[(idx - 1) % len(THEMES)]
    path = os.path.join(SPACE_DIR, f"space_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(space, f, ensure_ascii=False, indent=2)
    return path, name, space


def get(url=None):
    pass


def freqtrade_busy():
    """freqtrade 在跑回测时不抢资源（与 auto_research 的 wait_idle 呼应）"""
    try:
        out = subprocess.run(["pgrep", "-f", "freqtrade backtesting"],
                             capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


def one_round(idx, args):
    # 每轮换搜索空间：否则缓存前缀会让 12 步全部命中缓存，永远原地打转
    if args.space_file:
        space_path, theme, space = args.space_file, "custom", None
    else:
        space_path, theme, space = write_space(idx)
    cmd = [sys.executable, "-u", "auto_research.py",
           "--max-trials", str(args.max_trials),
           "--max-minutes", str(args.max_minutes),
           "--panel-cache", str(args.panel_cache),
           "--threads", str(args.threads),
           "--seeds", str(args.seeds),
           "--space-file", space_path]
    print(f"\n  ▶ 第 {idx} 轮  主题={theme}  {' '.join(cmd[2:])}", flush=True)
    if space:
        n = 1
        for v in space.values():
            n *= len(v)
        print(f"    空间: {json.dumps(space, ensure_ascii=False)}", flush=True)
        print(f"    组合数上限 {n}", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    dur = time.time() - t0
    # 把子进程输出转发到本进程 stdout（保持日志完整）
    if r.stdout:
        for line in r.stdout.strip().split("\n")[-40:]:
            print(f"    {line}", flush=True)
    if r.returncode != 0:
        print(f"    ❌ 退出码 {r.returncode}", flush=True)
        if r.stderr:
            print(f"    {r.stderr.strip()[-500:]}", flush=True)
    return r.returncode == 0, round(dur, 1)


def write_state(d):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=120,
                    help="两轮之间的间隔秒数（默认 120）")
    ap.add_argument("--max-rounds", type=int, default=0,
                    help="最多跑几轮；0 = 无限（常驻）")
    ap.add_argument("--max-trials", type=int, default=15,
                    help="每轮 trial 数。必须大于缓存里的配置数，\n                          否则每轮都在复用历史结果、原地打转")
    ap.add_argument("--max-minutes", type=float, default=180)
    ap.add_argument("--panel-cache", type=int, default=1,
                    help="默认 1：实测 3 会在 seq_len=120 时 OOM")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--space-file", default=None)
    ap.add_argument("--seeds", type=int, default=3,
                    help="每配置随机种子数（第 24 轮加入；实测种子方差占 87%）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--start-round", type=int, default=1,
                    help="从第几轮开始编号（重启守护后接着主题轮换用）。\n"
                         "主题按 (轮次-1) %% 6 选择，所以 --start-round 2 会用第 2 个主题")
    args = ap.parse_args()

    if args.once:
        args.max_rounds = 1

    print("=" * 88)
    print("模型自动迭代守护进程")
    print("=" * 88)
    print(f"  每轮 {args.max_trials} trials / {args.max_minutes:.0f} 分钟 · "
          f"间隔 {args.interval}s")
    print(f"  最多 {args.max_rounds if args.max_rounds else '无限'} 轮")
    print(f"  起始轮次 {args.start_round}")
    print(f"  状态 → {STATE}")
    print(f"  ⚠ 只产 challenger，不自动上线")

    # 从 --start-round 起编号：重启守护后主题轮换能接着走，
    # 而不是回到第 1 个主题重复探索已经跑过的空间。
    idx = max(0, args.start_round - 1)
    while True:
        idx += 1
        if args.max_rounds and idx > args.max_rounds:
            print(f"\n  已达最大轮数 {args.max_rounds}，退出")
            break
        # 等 freqtrade 回测结束
        waits = 0
        while freqtrade_busy() and waits < 60:
            print("    ⏳ freqtrade 回测中，等待…", flush=True)
            time.sleep(30)
            waits += 1
        ok, dur = one_round(idx, args)
        st = {
            "daemon_started": None,
            "rounds_done": idx,
            "last_round_ok": ok,
            "last_duration_s": dur,
            "last_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "interval_s": args.interval,
        }
        write_state(st)
        print(f"    第 {idx} 轮 {'✅ 成功' if ok else '❌ 失败'} · {dur:.0f}s", flush=True)
        if args.max_rounds and idx >= args.max_rounds:
            break
        print(f"    {args.interval}s 后开始第 {idx+1} 轮", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
