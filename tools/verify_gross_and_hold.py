"""验证 gross_pnl 口径 + 检查"持有期 vs 净 bp"（成本是每笔固定 ⇒ 更长持有可能更划算）。只读。"""
import collections
import datetime as dt
import json
import os
import statistics

import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                        where event_type='trade_closed' and strategy_id like 's07_%'
                        order by observed_at_ms""").fetchall()

    print('  ═══ ① 口径验证: net = gross − fees ? ═══')
    worst = 0.0
    n = 0
    for sid, pl, ob in rows:
        p = J(pl)
        g, f, ne = p.get('gross_pnl'), p.get('fees'), p.get('net_pnl')
        if None in (g, f, ne):
            continue
        worst = max(worst, abs(float(g) - float(f) - float(ne)))
        n += 1
    print(f'    s07 样本 {n} 笔 · |gross − fees − net| 最大 = 【{worst:.9f}】'
          + ('  ✅ 口径成立' if worst < 1e-6 else '  ❌'))

    print()
    print('  ═══ ② s07 的毛/费/净（按 账户=参数组合）═══')
    by = collections.defaultdict(list)
    for sid, pl, ob in rows:
        p = J(pl)
        nn = float(p.get('notional') or 0)
        if not nn:
            continue
        by[sid].append({
            'gross': float(p.get('gross_pnl') or 0) / nn * 10000,
            'fee': float(p.get('fees') or 0) / nn * 10000,
            'net': float(p.get('net_pnl') or 0) / nn * 10000,
            'opened': int(p.get('opened_candle_ms') or 0),
            'closed': int(p.get('closed_candle_ms') or ob),
        })
    print(f'  {"账户":<52}{"笔":>4}{"毛中位":>9}{"净中位":>9}{"持有(分)":>9}')
    for sid in sorted(by):
        recs = by[sid]
        holds = [(r['closed'] - r['opened']) / 60000 for r in recs if r['opened'] and r['closed']]
        g = statistics.median([r['gross'] for r in recs])
        ne = statistics.median([r['net'] for r in recs])
        h = statistics.median(holds) if holds else float('nan')
        print(f'  {sid[:50]:<52}{len(recs):>4}{g:>+9.2f}{ne:>+9.2f}{h:>9.0f}')

    print()
    print('  ═══ ③ 全库: 持有期 与 毛/净 bp 的关系（成本每笔固定）═══')
    allrows = c.execute("""select payload from quant.strategy_sim_events
                           where event_type='trade_closed'""").fetchall()
    buckets = collections.defaultdict(list)
    for (pl,) in allrows:
        p = J(pl)
        nn = float(p.get('notional') or 0)
        if not nn:
            continue
        o, cl = p.get('opened_candle_ms'), p.get('closed_candle_ms')
        if not o or not cl:
            continue
        hold_min = (int(cl) - int(o)) / 60000
        if hold_min < 0 or hold_min > 24 * 60:
            continue
        buckets[round(hold_min / 60)].append({
            'gross': float(p.get('gross_pnl') or 0) / nn * 10000,
            'fee': float(p.get('fees') or 0) / nn * 10000,
            'net': float(p.get('net_pnl') or 0) / nn * 10000})
    print(f'  {"持有(h)":>8}{"笔":>6}{"毛中位":>9}{"费中位":>8}{"净中位":>9}')
    for h in sorted(buckets):
        if len(buckets[h]) < 20:
            continue
        r = buckets[h]
        print(f'  {h:>8}{len(r):>6}{statistics.median([x["gross"] for x in r]):>+9.2f}'
              f'{statistics.median([x["fee"] for x in r]):>8.2f}'
              f'{statistics.median([x["net"] for x in r]):>+9.2f}')
