#!/usr/bin/env python3
"""反转候选筛查（🔴 已失效，仅作反面教材保留）。

🔴 2026-10-15 撤回说明
---------------------
本脚本的判据是【把每笔已实现的方向性收益取反】，据此筛出"可反转"的策略族。
**该近似是无效的** —— 已实现的收益【包含了原仓的止损/强平行为】，
而反转仓有【自己的】止损与强平：原仓亏 0.85% 是止损事件，反转仓此刻 +0.85%，
但它随后要扛住自己的止损（0.21~0.59%），小幅反弹即被打掉，把到手的利润吐回去。

用真实 K 线做完整模拟（反向入场 · 止损移到另一侧 · 持有至该族 H 根 · **同时检查强平**）后：

    族                          可模拟  反转净收益   前半      后半
    s17_overnight_range_fade      13   −0.3134%   −0.0659   −0.5256
    s14_vol_compression_release    8   +1.5613%   +3.1543   −0.0317   ← 两半翻转
    s34_vwap_anchor_reversion     57   −0.0624%   −0.1375   +0.0100

⇒ **反转方向不成立。请勿据此实现任何策略。**

保留本文件的原因是：它是一个具体、可复查的"口径错误"标本 ——
**「取反收益」不等于「反转策略」**。

另注：Cgen2 族（s17/s14）的 payload 缺少 `risk_sizing.stop_distance_pct` 与 `stop_level`，
无法从交易记录核验其止损行为。

═══════════════════════════════════════════════════════════════════════
以下为原始（已失效）实现，保留以便复查该错误是如何产生的。

原始 docstring：

反转候选筛查：找出"稳定负期望"的策略族，评估反转方向的 edge。

为什么需要这个脚本
------------------
2026-10-15 发现：实验室里【稳定亏损】的策略族，其信号具有【可靠的反向信息】——
把入场方向取反后，得到的正 edge 反而比现存所有"正收益"策略更稳健：

    维度                现存"正收益"族          反转版
    样本外两半同号      ❌ 4 个里仅 1 个         ✅ 5/5
    后半 CI 不含 0      ❌ 0 个                  ✅ 4/5
    收益分散度          ❌ top1 占 57~443%       ✅ top1 占 7~15%
    超过往返费率        ⚠️ 部分                  ✅ 5/5

原因：负期望族的亏损是【分散且多币种一致】的（不同于正收益族的少数观测驱动），
所以反向是有信息的。这也与"FADE（逆势）类在本期系统性亏损"自洽。

⚠️ 本脚本是【近似】——只翻转方向性收益的符号。
   真实反转策略的出场/止损逻辑也会随之改变（止损移到另一侧），
   因此这里的数字【可能偏保守】：原策略打止损时，反转仓正处获利方向、不会被打掉。
   结论必须由实验室实现反转版后【前向验证】。

用法
----
    QUANT_DATABASE_URL="$(tools/dburl.sh)" \\
        .venv/bin/python3 tools/screen_inversion_candidates.py [--risk-cap 50] [--min-n 10]

只读。不写库、不重启、不下单。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys

import numpy as np

FEE_ROUND_TRIP = 0.0004          # maker 0.02%/边 × 2
BOOTSTRAP_ROUNDS = 20_000
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


def main() -> int:
    parser = argparse.ArgumentParser(description="反转候选筛查")
    parser.add_argument("--risk-cap", type=float, default=50.0,
                        help="只评估 execution.max_loss_per_trade_usdt 等于该值的账户（默认 50）")
    parser.add_argument("--min-n", type=int, default=10,
                        help="族级最小【有效】样本量（默认 10）")
    parser.add_argument("--fee", type=float, default=FEE_ROUND_TRIP,
                        help="往返费率（默认 0.0004 = 0.04%%）")
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
            caps[sid] = _j(execution).get("max_loss_per_trade_usdt")
        rows = conn.execute(
            "select strategy_id, payload from quant.strategy_sim_events "
            "where event_type='trade_closed' order by sequence_id").fetchall()

    # family -> opened_candle_ms -> [反转后毛收益, ...]
    grouped: dict[str, dict[int, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    raw_counts: collections.Counter = collections.Counter()
    for sid, payload in rows:
        if caps.get(sid) != args.risk_cap:
            continue
        record = _j(payload)
        entry, exit_ = record.get("entry_price"), record.get("exit_price")
        side = record.get("side")
        if None in (entry, exit_) or not side:
            continue
        opened = record.get("opened_candle_ms")
        if not opened:
            continue
        entry, exit_ = float(entry), float(exit_)
        gross = (exit_ - entry) / entry if side == "long" else (entry - exit_) / entry
        family = sid.split("_R50")[0].split("_R200")[0].split("_R1000")[0]
        family = family.split("_Fnew_Demo")[0]
        grouped[family][int(opened)].append(-gross)      # 反转后的毛收益
        raw_counts[family] += 1

    rng = np.random.default_rng(SEED)
    results = []
    for family, by_time in grouped.items():
        points = sorted((t, float(np.mean(v))) for t, v in by_time.items())
        values = np.array([v for _, v in points])
        n = len(values)
        if n < args.min_n:
            continue
        half = n // 2
        first, second = values[:half], values[half:]
        draws = second[rng.integers(0, len(second), size=(args.bootstrap, len(second)))]
        oos_ci = np.percentile(draws.mean(axis=1), [2.5, 97.5])
        ordered = np.sort(values)[::-1]
        total = ordered.sum()
        results.append({
            "family": family,
            "raw_n": raw_counts[family],
            "n": n,
            "mean": float(values.mean()),
            "first": float(first.mean()),
            "second": float(second.mean()),
            "oos_ci": oos_ci,
            "top1": float(ordered[0] / total * 100) if total else float("nan"),
            "top3": float(ordered[:3].sum() / total * 100) if total else float("nan"),
        })

    if not results:
        print("没有满足条件的族（检查 --risk-cap 与 --min-n）")
        return 1

    fee = args.fee
    print()
    print("  " + "!" * 72)
    print("  !!! 🔴 本工具的输出【已失效】，请勿据此实现任何策略 !!!")
    print("  !!! 判据「取反收益」忽略了反转仓自身的止损/强平，已被完整模拟否证: !!!")
    print("  !!!   s17 −0.3134% · s14 两半翻转 · s34 −0.0624%  !!!")
    print("  !!! 下方所有「✅ 全过」标记均不代表可用候选。详见文件头撤回说明。 !!!")
    print("  " + "!" * 72)
    print()
    print(f"  ══ 反转候选筛查 · {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z · "
          f"max_loss={args.risk_cap:g} · 费率 {fee*100:.4f}% ══")
    print()
    print(f"    {'族':<36}{'有效n':>6}{'反转毛%':>10}{'前半':>9}{'后半':>9}"
          f"{'OOS CI':>21}{'top1':>7}  判定")
    for r in sorted(results, key=lambda x: -x["mean"]):
        ci = r["oos_ci"]
        checks = [
            r["mean"] > fee,
            r["first"] > fee,
            r["second"] > fee,
            (r["first"] > 0) == (r["second"] > 0),
            ci[0] > 0 and r["top1"] < 25,
        ]
        passed = sum(checks)
        if passed == 5:
            verdict = "✅ 全过（建议实现）"
        elif checks[0] and checks[1] and checks[2]:
            verdict = f"⚠️ 部分（{passed}/5）"
        else:
            verdict = "  "
        cis = f"[{ci[0]*100:>+7.4f},{ci[1]*100:>+7.4f}]"
        print(f"    {r['family'][:36]:<36}{r['n']:>6}{r['mean']*100:>+10.4f}"
              f"{r['first']*100:>+9.4f}{r['second']*100:>+9.4f}"
              f"{cis:>21}{r['top1']:>6.0f}%  {verdict}")

    full = [r for r in results if all([
        r["mean"] > fee, r["first"] > fee, r["second"] > fee,
        (r["first"] > 0) == (r["second"] > 0),
        r["oos_ci"][0] > 0, r["top1"] < 25])]
    print()
    print(f"  ══ 五项检验全过的候选: {len(full)} 个 ══")
    for r in sorted(full, key=lambda x: -x["n"]):
        print(f"    {r['family']} · 有效 n={r['n']} · 反转毛收益 {r['mean']*100:+.4f}% "
              f"(费率 {fee*100:.4f}%) · 两半 {r['first']*100:+.4f}/{r['second']*100:+.4f} "
              f"· OOS CI [{r['oos_ci'][0]*100:+.4f},{r['oos_ci'][1]*100:+.4f}] "
              f"· top1 {r['top1']:.0f}%")
    if not full:
        print("    （无）")

    print()
    print("  ══ 五项检验 ══")
    print("    ① 全样本反转毛收益 > 费率  ② 前半 > 费率  ③ 后半 > 费率")
    print("    ④ 两半同号  ⑤ 后半 OOS CI 不含 0 且 top1 < 25%（分散）")
    print()
    print("  ⚠️ 本筛查是【近似】: 只翻转方向性收益符号。真实反转策略的出场/止损会移到另一侧，")
    print("     原策略打止损时反转仓正处获利方向 ⇒ 数字可能偏保守。必须由实验室前向验证。")
    print("  ⚠️ 反转版与原型共用信号源 ⇒ 不应同时上线（组合集中度问题）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
