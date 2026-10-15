#!/usr/bin/env python3
"""Demo 入场可成交性修复的验收检查（规格 §3）。

用法:
    QUANT_DATABASE_URL=$(grep '^QUANT_DATABASE_URL=' /tmp/bot_env.txt | cut -d= -f2-) \
        .venv/bin/python3 tools/check_demo_entry_fillability.py

判据（对应 research_multi_strategy/verified/SPEC_demo_entry_fillability_20261014.md §3）:
  R1  新入场单的 intent_payload 含 limit_deviation_bps 且 submitted_limit_price <= reference_price
  R2  OrderNotFillable 不再产生 unknown ⇒ 不阻断全部会话
  R3  费率不退化（maker 应 ≈0.0199999%/边，taker ≈0.0400000%/边）
  R4  unfilled_entry_ratio 可见且被跟踪
  R5  连续 >=10 次入场尝试中 OrderNotFillable <=1

只读。不写库、不重启、不下单。
"""
from __future__ import annotations

import json
import os
import sys
import datetime as dt

import psycopg

BASELINE_ORDER_COUNT = 7
BASELINE_POLICY_HASH = "898ca54d31cf8fd944a6be573f583fa08cb5b46b60b8599c8b0686326d587e83"
MAKER_RATE_PCT = 0.0199999  # 已验证
TAKER_RATE_PCT = 0.0400000  # 已验证
FILLS_SINCE_MS = 1791938400000  # 开闸前一刻


def _j(v):
    return v if isinstance(v, dict) else json.loads(v)


def _t(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M:%S") + "Z"


def _database_url() -> str | None:
    """解析连接串：环境变量 → 仓库 .env → /tmp/bot_env.txt。

    /tmp 会被 macOS 周期清理（2026-10-15 实际发生），因此 .env 必须是可用的后备，
    否则验收脚本会在环境清理后静默失效。
    """
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
    database_url = _database_url()
    if not database_url:
        print("缺少 QUANT_DATABASE_URL（环境变量、仓库 .env、/tmp/bot_env.txt 均未提供）",
              file=sys.stderr)
        return 2
    os.environ["QUANT_DATABASE_URL"] = database_url

    problems: list[str] = []
    with psycopg.connect(database_url) as conn:
        cols = [d[0] for d in conn.execute("select * from quant.demo_orders limit 0").description]
        orders = [dict(zip(cols, r)) for r in conn.execute(
            "select * from quant.demo_orders where virtual_account_key like 'demo_strategy_%' "
            "order by created_ms").fetchall()]

        print(f"══ 订单 {len(orders)} 笔（基线 {BASELINE_ORDER_COUNT}）══")
        new_entries = 0
        with_dev = 0
        dev_violations = 0
        for i, o in enumerate(orders, 1):
            ip = o.get("intent_payload")
            ip = ip if isinstance(ip, dict) else json.loads(ip or "{}")
            dev = ip.get("limit_deviation_bps")
            ref, lim = ip.get("reference_price"), ip.get("submitted_limit_price")
            is_new = i > BASELINE_ORDER_COUNT
            if o["order_type"] == "limit" and not o["reduce_only"]:
                new_entries += 1 if is_new else 0
            if dev is not None:
                with_dev += 1
                try:
                    if ref is not None and lim is not None and float(lim) > float(ref):
                        dev_violations += 1
                        problems.append(f"R1 违反: 订单 {i} limit {lim} > ref {ref}")
                except (TypeError, ValueError):
                    problems.append(f"R1 无法核对: 订单 {i} ref={ref} limit={lim}")
            tag = "【新】" if is_new else ""
            extra = (f" 偏离={dev}bps ref={ref} limit={lim}" if dev is not None
                     else " 【无 limit_deviation_bps ← 旧代码字段缺失】")
            print(f"  {tag}{i}. {_t(o['created_ms'])} {o['pair']:<16}{o['side']:<5}"
                  f"{o['status']:<32}成交 {o['filled_quantity']}/{o['quantity']}{extra}")

        print()
        ss = _j(conn.execute("select payload from quant.documents "
                             "where key='binance_demo_shared_sessions.json'").fetchone()[0])
        sessions = ss.get("sessions") or []
        on = sum(1 for s in sessions if s.get("entriesEnabled"))
        states: dict[str, int] = {}
        for s in sessions:
            k = (s.get("status") or {}).get("status")
            states[k] = states.get(k, 0) + 1
        print(f"══ 会话 {len(sessions)}(开闸 {on}) {states} ══")
        for s in sessions:
            st = s.get("status") or {}
            if st.get("ownership_conflicts"):
                print(f"  ⚠️ {str(s.get('selectedAccountId'))[:44]} conflicts={st['ownership_conflicts']}")

        r = _j(conn.execute("select payload from quant.documents "
                            "where key='binance_demo_runner_controller.json'").fetchone()[0])
        st = r.get("status") or {}
        print(f"══ runner ══")
        print(f"  status={st.get('status')} entries_enabled={st.get('entries_enabled')} "
              f"terminal={st.get('terminal_entry_count')} unfilled={st.get('unfilled_entry_count')} "
              f"ratio={st.get('unfilled_entry_ratio')}")
        print(f"  blocked_pair_reasons={st.get('blocked_pair_reasons')} "
              f"submission_unknown={st.get('submission_unknown')} "
              f"alert={st.get('reconciliation_alert')}")
        print(f"  policy_hash={st.get('demo_policy_hash')}")

        if states and states.get("blocked_unknown_exposure"):
            problems.append("R2 未生效: 仍有会话处于 blocked_unknown_exposure")
        if st.get("blocked_pair_reasons"):
            problems.append(f"R2: blocked_pair_reasons 非空 {st['blocked_pair_reasons']}")
        if st.get("submission_unknown"):
            problems.append("unknown 提交仍存在（submission_unknown=True）")

        print()
        print("══ 开闸后成交的费率（R3 不回归）══")
        fills = conn.execute(
            "select price,quantity,commission,is_maker,timestamp_ms from quant.demo_fills "
            "where timestamp_ms > %s order by timestamp_ms", (FILLS_SINCE_MS,)).fetchall()
        if not fills:
            print("  （暂无新成交）")
        for pr, q, cm, mk, ts in fills:
            n = float(pr) * float(q)
            rate = float(cm) / n * 100 if n else 0.0
            expect = MAKER_RATE_PCT if mk else TAKER_RATE_PCT
            ok = abs(rate - expect) < 0.0005
            if not ok:
                problems.append(f"R3 费率异常: {_t(ts)} maker={mk} 实测 {rate:.7f}% 期望 {expect:.7f}%")
            print(f"  {_t(ts)} {pr}×{q}={n:.4f} 手续费 {cm} maker={mk} ⇒ {rate:.7f}%/边 "
                  f"{'✅' if ok else '⚠️'}")

        rc = conn.execute("select observed_ms,payload from quant.demo_signal_comparisons "
                          "where stage='submission_error' order by observed_ms desc limit 1").fetchone()
        if rc:
            p = _j(rc[1])
            print(f"\n══ 最近 submission_error: {_t(rc[0])} kind={p.get('error_kind')} ══")

        print()
        print("══ 验收进度 ══")
        print(f"  R1: 带 limit_deviation_bps 的订单 {with_dev} 笔 · 新入场单 {new_entries} 笔"
              f" · 违反 limit<=ref {dev_violations} 笔")
        if with_dev == 0:
            print("      ⇒ 【尚无新代码产出的订单】。基线 7 笔无该字段；出现即证明 R1 生效。")
        else:
            print("      ⇒ ✅ 新代码路径已产出带 limit_deviation_bps 的订单")
        print(f"  R4: unfilled_entry_ratio={st.get('unfilled_entry_ratio')}（基线 0.8）")
        print(f"  R5: 目标 连续 ≥10 次入场中 OrderNotFillable ≤1（当前新样本 {new_entries} 笔）")

    print()
    if problems:
        print("🔴 发现问题:")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("✅ 本次检查未发现问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
