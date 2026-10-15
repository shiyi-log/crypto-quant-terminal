#!/usr/bin/env python3
"""策略前向评估（去重 + 稳健统计）。

为什么需要这个脚本
------------------
实验室为每个策略跑 9 个变体（风险预算 R50/R200/R1000 × 止损宽度 k1.0/1.5/2.5）。
这些变体【共享同一个入场信号】—— 同一个 K 线时刻会被全部 9 个变体同时交易。

因此把 9 个变体当作独立样本会让样本量膨胀约 9 倍，bootstrap 置信区间被虚窄
（实测：s07 族膨胀 3.5 倍、CI 宽度虚窄 2.7 倍），从而把"边缘正向"误判成
"BH-FDR 显著正向"（2026-10-15 实际发生过这个错误）。

正确做法：按【入场 K 线时刻】去重，每个时刻取各变体收益的均值，得到有效独立样本。

用法
----
    QUANT_DATABASE_URL="$(tools/dburl.sh)" \\
        .venv/bin/python3 tools/evaluate_strategy_robust.py [--risk-cap 50] [--min-n 10]

只读。不写库、不重启、不下单。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import sys

import numpy as np

BOOTSTRAP_ROUNDS = 30_000
SEED = 20261015


def _j(value):
    return value if isinstance(value, dict) else json.loads(value)


def _database_url() -> str | None:
    url = os.environ.get("QUANT_DATABASE_URL")
    if url:
        return url
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(root, ".env"), "/tmp/bot_env.txt"):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("QUANT_DATABASE_URL="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return None


def _trimmed_mean(values: np.ndarray, frac: float) -> float:
    ordered = np.sort(values)
    k = int(len(ordered) * frac)
    if len(ordered) - 2 * k <= 0:
        return float("nan")
    return float(ordered[k:len(ordered) - k].mean())


def _bh_fdr(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (same order as input)."""
    indexed = sorted(range(len(p_values)), key=lambda i: p_values[i])
    m = len(p_values)
    adjusted = [1.0] * m
    running = 1.0
    for rank, idx in enumerate(reversed(indexed), start=1):
        i = m - rank + 1
        value = p_values[idx] * m / i
        running = min(running, value)
        adjusted[idx] = min(1.0, running)
    return adjusted


def _normal_p(t_stat: float) -> float:
    return math.erfc(abs(t_stat) / math.sqrt(2))


def main() -> int:
    parser = argparse.ArgumentParser(description="策略前向评估（去重 + 稳健统计）")
    parser.add_argument("--risk-cap", type=float, default=50.0,
                        help="只评估 execution.max_loss_per_trade_usdt 等于该值的账户（默认 50）")
    parser.add_argument("--min-n", type=int, default=10,
                        help="族级最小【有效】样本量（默认 10）")
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_ROUNDS)
    args = parser.parse_args()

    url = _database_url()
    if not url:
        print("缺少 QUANT_DATABASE_URL（环境变量、仓库 .env、/tmp/bot_env.txt 均未提供）",
              file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(url) as conn:
        caps = {}
        for sid, execution in conn.execute(
                "select strategy_id, execution from quant.strategy_sim_accounts").fetchall():
            spec = _j(execution)
            caps[sid] = spec.get("max_loss_per_trade_usdt")
        rows = conn.execute(
            "select strategy_id, payload from quant.strategy_sim_events "
            "where event_type='trade_closed' order by sequence_id").fetchall()

    # family -> entered_candle_ms -> [net_pnl, ...]（同族各变体）
    grouped: dict[str, dict[int, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    raw_counts: collections.Counter = collections.Counter()
    for sid, payload in rows:
        if caps.get(sid) != args.risk_cap:
            continue
        record = _j(payload)
        if record.get("net_pnl") is None:
            continue
        opened = record.get("opened_candle_ms")
        if not opened:
            continue
        family = sid.split("_R50")[0].split("_R200")[0].split("_R1000")[0]
        family = family.split("_Fnew_Demo")[0]
        grouped[family][int(opened)].append(float(record["net_pnl"]))
        raw_counts[family] += 1

    rng = np.random.default_rng(SEED)
    results = []
    for family, by_time in grouped.items():
        effective = np.array([float(np.mean(v)) for v in by_time.values()])
        # 每个唯一信号时刻的【净 P&L 合计】（用于集中度诊断）
        signal_pnl = np.sort(np.array([float(np.sum(v)) for v in by_time.values()]))[::-1]
        n = len(effective)
        if n < args.min_n:
            continue
        draws = effective[rng.integers(0, n, size=(args.bootstrap, n))]
        mean_draws = draws.mean(axis=1)
        median_draws = np.median(draws, axis=1)
        mean_ci = np.percentile(mean_draws, [2.5, 97.5])
        median_ci = np.percentile(median_draws, [2.5, 97.5])
        std = effective.std(ddof=1)
        t_stat = effective.mean() / (std / math.sqrt(n)) if std > 0 else float("nan")
        p_value = _normal_p(t_stat) if not math.isnan(t_stat) else 1.0
        total_pnl = float(signal_pnl.sum())
        top1_share = float(signal_pnl[0] / total_pnl * 100) if total_pnl else float("nan")
        top3_share = (float(signal_pnl[:3].sum() / total_pnl * 100)
                      if total_pnl else float("nan"))
        results.append({
            "family": family,
            "raw_n": raw_counts[family],
            "n": n,
            "inflation": raw_counts[family] / n,
            "mean": float(effective.mean()),
            "median": float(np.median(effective)),
            "trim10": _trimmed_mean(effective, 0.10),
            "win": float((effective > 0).mean() * 100),
            "mean_ci": mean_ci,
            "median_ci": median_ci,
            "p_mean": float((mean_draws > 0).mean() * 100),
            "p": p_value,
            "total_pnl": total_pnl,
            "top1_share": top1_share,
            "top3_share": top3_share,
        })

    if not results:
        print("没有满足条件的族（检查 --risk-cap 与 --min-n）")
        return 1

    for value, adjusted in zip(results, _bh_fdr([r["p"] for r in results])):
        value["bh"] = adjusted

    print(f"  ══ {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z · "
          f"max_loss={args.risk_cap:g} · {len(results)} 族（有效 n>={args.min_n}）══")
    print()
    header = (f"    {'族':<36}{'原始n':>6}{'有效n':>6}{'膨胀':>6}{'均值':>9}{'中位':>9}"
              f"{'胜率':>7}{'BH':>8}{'净P&L':>11}{'top1':>7}{'top3':>7}  判定")
    print(header)
    for r in sorted(results, key=lambda x: -x["mean"]):
        mark = "✅" if (r["bh"] < 0.05 and r["mean_ci"][0] > 0) else (
            "⛔" if (r["bh"] < 0.05 and r["mean_ci"][1] < 0) else "  ")
        t1, t3 = r["top1_share"], r["top3_share"]
        if r["total_pnl"] <= 0:
            conc = "负收益"
        elif t1 > 100:
            conc = "⚠️单点决定"
        elif t3 > 100:
            conc = "⚠️少数点决定"
        elif t1 > 50 or t3 > 80:
            conc = "⚠️高度集中"
        else:
            conc = "✅分散"
        print(f"    {r['family'][:36]:<36}{r['raw_n']:>6}{r['n']:>6}{r['inflation']:>6.1f}"
              f"{r['mean']:>+9.2f}{r['median']:>+9.2f}{r['win']:>6.1f}%"
              f"{r['bh']:>8.4f}{r['total_pnl']:>+11.0f}{t1:>6.0f}%{t3:>6.0f}% {mark}{conc}")

    positive = [r for r in results if r["bh"] < 0.05 and r["mean_ci"][0] > 0]
    negative = [r for r in results if r["bh"] < 0.05 and r["mean_ci"][1] < 0]
    robust_negative = [r for r in negative if r["total_pnl"] > 0 or r["top1_share"] < 25]
    print()
    print(f"  ══ 结论（BH-FDR 校正 + 去重口径）══")
    print(f"    显著为正: {len(positive)} 个" + ("".join(
        f"\n      ✅ {r['family']} · 有效 n={r['n']} · 均值 {r['mean']:+.2f}"
        f" · top1 占 {r['top1_share']:.0f}%"
        + ("  ⚠️正收益靠少数观测" if r["top1_share"] > 60 else "")
        for r in positive) if positive else " ⇒ 无"))
    print(f"    显著为负: {len(negative)} 个" + ("".join(
        f"\n      ⛔ {r['family']} · 有效 n={r['n']} · 均值 {r['mean']:+.2f} · 胜率 {r['win']:.1f}%"
        for r in sorted(negative, key=lambda x: x["mean"])) if negative else " ⇒ 无"))
    print()
    print("  ══ 集中度分层（决定退役建议的置信度）══")
    if negative:
        print("    【负收益分散（top1<25%）⇒ 退役置信度高】")
        for r in sorted(negative, key=lambda x: x["mean"]):
            if r["top1_share"] < 25:
                print(f"      {r['family']} · top1 {r['top1_share']:.0f}% · top3 {r['top3_share']:.0f}%"
                      f" · 净 {r['total_pnl']:+.0f}")
        print("    【负收益集中（top1≥25%）⇒ 退役依据较弱，建议继续观察】")
        for r in sorted(negative, key=lambda x: x["mean"]):
            if r["top1_share"] >= 25:
                print(f"      {r['family']} · top1 {r['top1_share']:.0f}% · top3 {r['top3_share']:.0f}%")
    print()
    print("  ⚠️ 注意: 本脚本按入场 K 线时刻去重（有效 n = 唯一信号数）。")
    print("     直接用原始笔数做 bootstrap/BH-FDR 会把 n 膨胀 3~9 倍并产生假显著。")
    print("     top1/top3 是该族【唯一信号时刻】净 P&L 中最大 1/3 个占总 P&L 的比例；")
    print("     top1>100% 意味着去掉最好的那一个信号观测，该族就转负。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
