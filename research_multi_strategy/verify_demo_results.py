#!/usr/bin/env python3
"""DSH 的 Demo 结果验证脚本（只读）。

用法: python3 research_multi_strategy/verify_demo_results.py

在 runner 启动、demo_orders/demo_fills 有数据后运行。
直接回答目标的三个问题:
  Q1 真实手续费（从成交回报读 commission/commissionAsset/is_maker）
  Q3 限价单真实未成交率 + 内部模拟 vs Demo 对照

⚠️ 只读: 不写 DB、不下单、不碰凭据。
"""
import os, sys, json, collections, datetime as dt
import psycopg

URL = os.environ.get("QUANT_DATABASE_URL")
if not URL:
    print("⚠️ 未设置 QUANT_DATABASE_URL"); sys.exit(2)

Q = lambda conn, sql, args=None: conn.execute(sql, args or ()).fetchall()

def main():
    with psycopg.connect(URL) as conn:
        print("═" * 72)
        print("Demo 结果验证 · DSH")
        print("═" * 72)

        # ── 0. 数据量检查（没有数据就明确说） ──
        counts = {}
        for t in ("demo_orders", "demo_fills", "demo_positions",
                  "demo_account", "demo_signal_comparisons"):
            with conn.transaction():
                try:
                    counts[t] = Q(conn, f"select count(*) from quant.{t}")[0][0]
                except Exception:
                    counts[t] = None
        print("\n[0] 数据量")
        for t, n in counts.items():
            print(f"    {t:<28}{'查询失败' if n is None else f'{n} 行'}")

        if not counts.get("demo_orders"):
            print("\n⚠️ demo_orders 仍为 0 → runner 未启动，无法验证")
            print("   下一步: 在币安 Demo 前端选一个已冻结的单腿账户 → 点「开始」")
            return 2

        # ── Q1: 真实手续费（从成交回报） ──
        print("\n" + "─" * 72)
        print("[Q1] 真实手续费 —— 必须来自成交回报，不是假设值")
        print("─" * 72)
        with conn.transaction():
            cols = [c for c, _ in Q(conn, """select column_name, data_type
                from information_schema.columns where table_schema='quant'
                and table_name='demo_fills' order by ordinal_position""")]
            rows = Q(conn, "select * from quant.demo_fills")
        if not rows:
            print("    ⚠️ demo_fills 为 0 → Q1 的成交部分【尚未可答】")
            print("       （账户报价已有: maker 0.0200% / taker 0.0400%，但那不是成交回报）")
        else:
            print(f"    成交笔数: {len(rows)}")
            maker = taker = None
            assets = collections.Counter()
            for r in rows:
                d = dict(zip(cols, r))
                is_maker = d.get("is_maker")
                comm = d.get("commission"); px = d.get("price"); qty = d.get("quantity")
                if d.get("commission_asset"): assets[d["commission_asset"]] += 1
                try:
                    rate = float(comm) / (float(px) * float(qty))
                except Exception:
                    continue
                if is_maker is True and maker is None: maker = rate
                if is_maker is False and taker is None: taker = rate
            print(f"    实际 maker 费率: {maker*100:.4f}%" if maker is not None else "    实际 maker 费率: 无 maker 成交")
            print(f"    实际 taker 费率: {taker*100:.4f}%" if taker is not None else "    实际 taker 费率: 无 taker 成交")
            print(f"    commissionAsset 分布: {dict(assets)}")
            print(f"    ⚠️ 对照账户报价: maker 0.0200% / taker 0.0400%")
            print(f"    ⚠️ 对照项目假设: maker 0.0200% / taker 0.0500%")

        # ── Q3a: 限价单真实未成交率 ──
        print("\n" + "─" * 72)
        print("[Q3] 限价单真实未成交率 + 撤单行为")
        print("─" * 72)
        with conn.transaction():
            ocols = [c for c, _ in Q(conn, """select column_name, data_type
                from information_schema.columns where table_schema='quant'
                and table_name='demo_orders' order by ordinal_position""")]
            orders = Q(conn, "select * from quant.demo_orders")
        stat = collections.Counter()
        tif = collections.Counter()
        attempts = collections.Counter()
        for r in orders:
            d = dict(zip(ocols, r))
            stat[d.get("status")] += 1
            tif[d.get("time_in_force")] += 1
            attempts[d.get("submission_attempts")] += 1
        total = sum(stat.values())
        filled = stat.get("filled", 0) + stat.get("partially_filled", 0)
        unfilled = stat.get("canceled", 0) + stat.get("expired", 0)
        print(f"    订单总数 {total} · 状态分布: {dict(stat)}")
        print(f"    time_in_force 分布: {dict(tif)}   ← 应为 GTX（post-only）")
        print(f"    submission_attempts 分布: {dict(attempts)}   ← 应全为 0/1")
        if total:
            print(f"    ✅ 成交率: {filled/total*100:.1f}%  ·  未成交率: {unfilled/total*100:.1f}%")
            print(f"    ⚠️ 对照内部模拟假设: 100% 成交（next_open_proxy_assumes_fill）")
            print(f"    ⚠️ 对照我的实盘订单簿推算: 1bp/30s ≈ 23%(BTC)/34%(ETH)")

        # ── Q3b: 内部模拟 vs Demo 对照 ──
        print("\n" + "─" * 72)
        print("[Q3] 内部模拟 vs Demo 对照（纪律第5条: 没有对照无法归因）")
        print("─" * 72)
        with conn.transaction():
            ccols = [c for c, _ in Q(conn, """select column_name, data_type
                from information_schema.columns where table_schema='quant'
                and table_name='demo_signal_comparisons' order by ordinal_position""")]
            comps = Q(conn, "select * from quant.demo_signal_comparisons limit 20")
        if not comps:
            print("    ⚠️ demo_signal_comparisons 为 0 → 【Demo 侧尚未产生记录】")
            print("       （纸面侧在 strategy_sim_events；两侧按 signal_id 关联）")
        else:
            print(f"    Demo 侧记录: {len(comps)} 条 · 列: {ccols}")
            stages = collections.Counter(dict(zip(ccols, r)).get("stage") for r in comps)
            print(f"    stage 分布: {dict(stages)}")

        # ── 真正的对照: 同一 signal_id 下的 paper_result vs cycle_result ──
        print("\n    ── 正确口径: 同表内按 signal_id 关联 paper vs cycle ──")
        with conn.transaction():
            rows = Q(conn, """select signal_id, stage, payload from quant.demo_signal_comparisons
                where stage in ('second_level_paper_result','second_level_cycle_result')""")
        import collections as _c
        bysig = _c.defaultdict(dict)
        for sid, stage, pl in rows:
            bysig[sid][stage] = pl if isinstance(pl, dict) else json.loads(pl)
        paired = [s for s, d in bysig.items()
                  if 'second_level_paper_result' in d and 'second_level_cycle_result' in d]
        print(f"    paper+cycle 配对: {len(paired)} / {len(bysig)}")
        for arm in ('maker', 'taker'):
            sel = [(s, d) for s, d in bysig.items()
                   if d['second_level_cycle_result'].get('cycle', {}).get('arm') == arm]
            if not sel:
                continue
            filled = sum(1 for _, d in sel
                         if d['second_level_cycle_result']['cycle'].get('entry_terminal_status')
                         in ('filled', 'partially_filled'))
            fees = [float(d['second_level_cycle_result']['cycle']['fees']['commission_usdt'])
                    for _, d in sel
                    if d['second_level_cycle_result']['cycle']['fees'].get('commission_usdt')]
            print(f"      {arm:<6}{len(sel)} 笔信号 · entry 成交 {filled}"
                  f" · 真实费 {len(fees)} 笔"
                  + (f" 合计 {sum(fees):.6f} USDT" if fees else ""))
        print("    ⚠️ paper 的 hypothetical_crossing_fee/net_pnl 为 null（拒绝用报价费率）")
        print("       → P&L 差额目前算不出来（见 DEMO_VS_PAPER_COMPARISON_20261012.md）")

        print("\n" + "═" * 72)
        print("判读原则: 全部标前向研究假设；禁止标盈利/通过/可用；上线是人工闸门")
        print("═" * 72)
        return 0

if __name__ == "__main__":
    sys.exit(main())
