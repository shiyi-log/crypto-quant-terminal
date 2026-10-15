#!/usr/bin/env python3
"""实验室覆盖率监控（逐小时）。

为什么需要这个脚本
------------------
2026-10-15 发现实验室的失效是【渐进退化】而非突然死锁：
先是从 4 条/小时降到 ~53%（12 小时窗口内只处理了 26/49 根 15m），
然后完全停止处理，最后进入 data_stale 死锁（提前 return 导致自我维持）。

"整体覆盖率"这种口径无法捕捉渐进退化 —— 平均值会把坏时段稀释掉。
本脚本按【小时】统计一个代表变体的 decision 数（正常应为 4 条/小时，即 4 根 15m），
并按【15m 收盘时刻命中率】给出更严格的口径。

用法
----
    QUANT_DATABASE_URL="$(tools/dburl.sh)" \\
        .venv/bin/python3 tools/check_lab_coverage.py [--hours 24] [--variant ...]

退出码: 0 = 无告警 · 1 = 存在覆盖率不足的小时（便于接入巡检）
只读，不写库、不重启、不下单。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys

DEFAULT_VARIANT = "s07_surge_continuation_B15m_R1000_k1.0_100x_maker"
BAR_MS = 15 * 60 * 1000
EXPECTED_PER_HOUR = 4
# 低于该比例即告警（正常应接近 100%）
ALERT_RATIO = 0.75


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


def _utc(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="实验室覆盖率监控（逐小时）")
    parser.add_argument("--hours", type=int, default=24, help="回看小时数（默认 24）")
    parser.add_argument("--variant", default=DEFAULT_VARIANT,
                        help="用作探针的策略变体（默认 s07 的 Demo 开闸变体）")
    args = parser.parse_args()

    url = _database_url()
    if not url:
        print("缺少 QUANT_DATABASE_URL（环境变量、仓库 .env、/tmp/bot_env.txt 均未提供）",
              file=sys.stderr)
        return 2

    import psycopg

    now = dt.datetime.now(dt.timezone.utc)
    since = int((now - dt.timedelta(hours=args.hours)).timestamp() * 1000)

    with psycopg.connect(url) as conn:
        stamps = [int(r[0]) for r in conn.execute(
            "select observed_at_ms from quant.strategy_sim_events "
            "where strategy_id=%s and event_type='decision' and observed_at_ms >= %s "
            "order by observed_at_ms", (args.variant, since)).fetchall()]

    print(f"  ══ 实验室覆盖率 · 探针 {args.variant[:46]} ══")
    print(f"     回看 {args.hours} 小时（自 {_utc(since):%m-%d %H:%M}Z）· "
          f"decision {len(stamps)} 条")
    print()

    per_hour = collections.Counter(_utc(s).strftime("%m-%d %H") for s in stamps)
    minutes = {_utc(s).strftime("%m-%d %H:%M") for s in stamps}

    alerts = []
    print(f"     {'小时':<10}{'decision':>9}{'覆盖率':>9}  判定")
    cursor = now.replace(minute=0, second=0, microsecond=0)
    for step in range(args.hours):
        hour_dt = cursor - dt.timedelta(hours=step)
        key = hour_dt.strftime("%m-%d %H")
        count = per_hour.get(key, 0)
        ratio = count / EXPECTED_PER_HOUR
        if step == 0:                      # 当前小时尚未走完
            verdict = "（进行中）"
        elif ratio >= ALERT_RATIO:
            verdict = "✅"
        else:
            verdict = "⚠️ 覆盖不足"
            alerts.append((key, count, ratio))
        print(f"     {key}Z{'' :<5}{count:>9}{ratio*100:>8.0f}%  {verdict}")

    print()
    print("  ══ 严格口径: 15m 收盘时刻命中率 ══")
    hit = miss = 0
    missing: list[str] = []
    for step in range(args.hours):
        hour_dt = cursor - dt.timedelta(hours=step)
        for minute in (0, 15, 30, 45):
            mark = hour_dt + dt.timedelta(minutes=minute)
            if mark > now - dt.timedelta(minutes=1):
                continue
            key = mark.strftime("%m-%d %H:%M")
            if key in minutes:
                hit += 1
            else:
                miss += 1
                missing.append(key)
    total = hit + miss
    if total:
        print(f"     应有 {total} 个收盘 · 命中 {hit} · 缺失 {miss} "
              f"⇒ 【{hit / total * 100:.1f}%】")
    if missing:
        print(f"     缺失时刻: {missing[:12]}{' …' if len(missing) > 12 else ''}")

    print()
    if alerts:
        print(f"  ⚠️ 有 {len(alerts)} 个小时覆盖率低于 {ALERT_RATIO*100:.0f}%:")
        for key, count, ratio in alerts:
            print(f"       {key}Z · {count} 条（{ratio*100:.0f}%）")
        print("     ⇒ 实验室可能正在退化（历史上先降到 ~53% 再完全停止）。")
        print("     ⇒ 检查: 15m/1h/1d 采集器是否在跑 · 实验室 status 是否 data_stale")
        return 1

    print("  ✅ 所有小时的覆盖率均达标（未发现渐进退化）")
    print()
    print("  说明: 正常应为 4 条/小时（每根 15m 收盘一条 decision）。")
    print("        历史上曾出现 10-14 09:45Z→22Z 仅 26/49 根（53%）的渐进退化，")
    print("        随后完全停止并进入 data_stale 死锁。逐小时口径可及早发现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
