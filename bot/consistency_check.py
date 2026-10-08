#!/usr/bin/env python3
"""
验证/部署一致性校验（规则 7 的落地工具）

背景：
    曾经发生过「研究框架验证的策略」与「实盘部署的策略」不是同一个 ——
    研究用收盘价通道（Calmar 0.82），实盘用高低价通道（Calmar 0.26）。
    只有把实盘策略放进真实引擎、与研究框架对同一区间复算，才能发现。

本脚本自动做这件事：
    ① 读实盘配置的币对与参数
    ② 用研究框架（walkforward.py）复算同区间
    ③ 跑 Freqtrade 对同一份策略文件回测
    ④ 比较 Calmar，偏差 > 容差即报警

用法:
    python consistency_check.py                   # 默认对账
    python consistency_check.py --tolerance 0.2   # 调整容差（默认 20%）
    python consistency_check.py --timerange 20230101-20261008
"""

import argparse
import json
import os
import re
import subprocess
import sys

import pandas as pd

import walkforward as wf

LIVE = "user_data/config_trend_live.json"
STRATEGY = "TrendFollowing"


def research_side(coins, top_n, timerange):
    """研究框架复算（用配置里的币对）"""
    start = timerange.split("-")[0]
    start_fmt = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    data_all = wf.load(wf.discover())
    data = {k: v for k, v in data_all.items() if k in coins}
    r = wf.backtest(data, sizing="equal", top_n=top_n, start=start_fmt)
    e = wf.evaluate(r, "research")
    return e, len(data)


def freqtrade_side(cfg, timerange):
    """跑 Freqtrade 回测并解析关键指标"""
    cmd = [".venv/bin/python", "-m", "freqtrade", "backtesting",
           "--config", cfg, "--strategy", STRATEGY, "--timerange", timerange]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800).stdout
    except subprocess.TimeoutExpired:
        return None
    res = {}
    for line in out.split("\n"):
        if "│" not in line:
            continue
        cells = [c.strip() for c in line.split("│") if c.strip()]
        if len(cells) < 2:
            continue
        k, v = cells[0], cells[1]
        num = v.replace("%", "").replace(",", "").strip()
        try:
            if k == "CAGR %":
                res["cagr"] = float(num)
            elif k.startswith("Absolute drawdown (wallet"):
                m = re.search(r"\(([\d.]+)%\)", v)
                if m:
                    res["mdd"] = float(m.group(1))
            elif k == "Total profit %":
                res["total"] = float(num)
            elif k == "Total/Daily Avg Trades":
                res["trades"] = int(v.split("/")[0].strip())
        except ValueError:
            continue
    if "cagr" in res and "mdd" in res and res["mdd"] > 0:
        res["calmar"] = res["cagr"] / res["mdd"]
    return res or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=float, default=0.20)
    ap.add_argument("--timerange", default="20230101-20261008")
    ap.add_argument("--cfg", default=LIVE)
    args = ap.parse_args()

    if not os.path.exists(args.cfg):
        print(f"  ❌ 找不到配置 {args.cfg}")
        sys.exit(1)
    cfg = json.load(open(args.cfg))
    coins = [p.split("/")[0] for p in cfg["exchange"]["pair_whitelist"]]
    top_n = 8            # 与策略默认值一致

    print("=" * 100)
    print("验证 / 部署 一致性校验（规则 7）")
    print("=" * 100)
    print(f"  配置 {args.cfg} · {len(coins)} 币 · top_n={top_n} · 区间 {args.timerange}")

    print("\n  ① 研究框架复算…")
    e, n_used = research_side(coins, top_n, args.timerange)
    if not e:
        print("     ❌ 研究框架无结果")
        sys.exit(1)
    print(f"     {n_used} 币 · 年化 {e['ann']:.2f}% · 回撤 {e['mdd']:.2f}% · "
          f"Calmar {e['calmar']:.2f} · t={e['t']:.2f}")

    print("\n  ② Freqtrade 回测…")
    ft = freqtrade_side(args.cfg, args.timerange)
    if not ft:
        print("     ❌ Freqtrade 无结果")
        sys.exit(1)
    print(f"     交易 {ft.get('trades','?')} 笔 · CAGR {ft['cagr']:.2f}% · "
          f"回撤 {ft['mdd']:.2f}% · Calmar {ft['calmar']:.2f}")

    print("\n  ③ 对账")
    rc, fc = e["calmar"], ft["calmar"]
    dev = abs(rc - fc) / max(abs(rc), 1e-9)
    print(f"     研究 Calmar {rc:.2f}  vs  实盘 Calmar {fc:.2f}  →  偏差 {dev*100:.0f}%")
    print(f"     容差 {args.tolerance*100:.0f}%")

    ok = dev <= args.tolerance
    print()
    if ok:
        print("  ✅ 通过 —— 验证与部署是同一个策略")
    else:
        print("  ❌ 失败 —— 偏差超出容差，说明两套代码不是同一个策略")
        print("     排查清单（逐项比对研究框架与策略文件的实现）：")
        for item in ["通道定义（close vs high/low）", "趋势状态机", "突破强度公式",
                     "选币规则", "离场条件", "仓位计算", "成本假设"]:
            print(f"       · {item}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
