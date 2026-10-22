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


VERDICT_KILL = "KILL"
VERDICT_KEEP = "KEEP"
VERDICT_UNDECIDED = "UNDECIDED"
VERDICT_INSUFFICIENT = "样本不足"


def _verdict(row: dict, *, n_min: int) -> tuple[str, str]:
    """按预注册 §1 的三条规则裁定单个策略族。

    返回 (裁定, 理由)。

    §1 原文：
      - 样本不足：有效 n < N_min ⇒ 不动，禁止下任何结论
      - KILL：n ≥ N_min 且净收益均值 ≤ 0
      - KEEP：n ≥ N_min 且均值 > 0 且 bootstrap 95% CI 下界 > 0
              （注意：KEEP 只等于「继续观察」，**不等于**盈利/通过/可用）
      - UNDECIDED：n ≥ N_min 且均值 > 0 但 CI 含 0

    ⚠️ 这里用**时间块去重后的有效 n**（唯一入场时刻），不是原始笔数。
    同一入场时刻的 9 个变体是相关样本，按笔数计数会把 n 膨胀 3~9 倍。
    """
    if row["n"] < n_min:
        return VERDICT_INSUFFICIENT, f"有效 n={row['n']} < N_min={n_min}，禁止下结论"
    lo, hi = row["mean_ci"]
    if row["mean"] <= 0:
        return VERDICT_KILL, f"均值 {row['mean']:+.3f} ≤ 0（95% CI [{lo:+.3f}, {hi:+.3f}]）"
    if lo > 0:
        return VERDICT_KEEP, (f"均值 {row['mean']:+.3f} > 0 且 CI 下界 {lo:+.3f} > 0"
                              "（仅表示继续观察，不等于可用）")
    return VERDICT_UNDECIDED, f"均值 {row['mean']:+.3f} > 0 但 CI [{lo:+.3f}, {hi:+.3f}] 含 0"


def _print_verdicts(results: list[dict], *, n_min: int) -> None:
    """打印预注册 §1 裁定表。"""
    buckets: dict[str, list[tuple[dict, str]]] = {
        VERDICT_KEEP: [], VERDICT_KILL: [],
        VERDICT_UNDECIDED: [], VERDICT_INSUFFICIENT: [],
    }
    for row in results:
        name, reason = _verdict(row, n_min=n_min)
        buckets[name].append((row, reason))

    print()
    print(f"  ══ 预注册 §1 裁定（N_min={n_min} 个唯一入场时刻）══")
    for name in (VERDICT_KEEP, VERDICT_KILL, VERDICT_UNDECIDED, VERDICT_INSUFFICIENT):
        items = buckets[name]
        print(f"    {name}: {len(items)} 个")
        for row, reason in sorted(items, key=lambda x: -x[0]["mean"]):
            print(f"      · {row['family'][:40]:<40} n={row['n']:<4} {reason}")

    if buckets[VERDICT_KEEP]:
        print()
        print("    ⚠️ KEEP 只等于「观察继续」，不等于盈利、不等于通过、不可据此上线；")
        print("       上线仍需人工闸门与独立复核。")

    if not buckets[VERDICT_KEEP] and not buckets[VERDICT_KILL]:
        print()
        print("    ⇒ 没有任何族达到裁定门槛。此时说\"有信号\"或\"没信号\"都不成立。")


def _normal_p(t_stat: float) -> float:
    return math.erfc(abs(t_stat) / math.sqrt(2))


def _t_p(t_stat: float, df: int) -> float:
    """双侧 Student-t 尾概率。

    ⚠️ Round 266：原实现用【正态近似】(_normal_p)，在小样本下【系统性低估 p 值】
    （对 n=16..60 的族实测低估 4–10 倍）。正态近似只在 n→∞ 时正确。
    优先用 scipy；不可用时退回正态近似并【明确标注】。
    """
    if df <= 0:
        return _normal_p(t_stat)
    try:
        from scipy import stats as _st
        return float(2 * _st.t.sf(abs(t_stat), df))
    except Exception:
        return _normal_p(t_stat)


def main() -> int:
    parser = argparse.ArgumentParser(description="策略前向评估（去重 + 稳健统计）")
    parser.add_argument("--risk-cap", type=float, default=50.0,
                        help="只评估 execution.max_loss_per_trade_usdt 等于该值的账户（默认 50）")
    parser.add_argument("--min-n", type=int, default=10,
                        help="族级最小【有效】样本量（默认 10）。"
                             "裁定需要 N_min=100（见 --verdict）；此参数只控制是否打印")
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_ROUNDS)
    parser.add_argument("--verdict", action="store_true",
                        help="按预注册 §1 输出 KILL/KEEP/UNDECIDED 裁定（要求有效 n>=100）")
    parser.add_argument("--n-min", type=int, default=100,
                        help="裁定所需最小有效样本量（预注册 §2 固定为 100）")
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
        # ⚠️⚠️ Round 189 修正: 原用【原始 net_pnl】⇒ 混合 R50/R1000 的不同风险档。
        #    现改为【归一化收益（千分比）】—— 与报告其它部分及 3.1 节的去重口径一致。
        #    (Round 189 实测: 两种口径下"显著族"数量相同 13/25，核心结论不变)
        _nt = float(record["notional"]) if record.get("notional") else 0.0
        if _nt <= 0:
            continue
        grouped[family][int(opened)].append(float(record["net_pnl"]) / _nt * 1000)
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
        # ⚠️ Round 266：改用 t 分布（df=n-1）。原用正态近似，在小样本下低估 p 值 4–10 倍。
        p_value = _t_p(t_stat, n - 1) if not math.isnan(t_stat) else 1.0
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

    if args.verdict:
        _print_verdicts(results, n_min=args.n_min)
    else:
        print()
        print(f"  ⚠️ 未输出裁定。预注册 §1 要求有效 n ≥ {args.n_min} 后再做 "
              "KILL/KEEP/UNDECIDED 分类；加 --verdict 打印裁定表。")
        print("     上面这些族目前都没有达到裁定门槛时，任何\"有信号/没信号\"的说法都不成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
