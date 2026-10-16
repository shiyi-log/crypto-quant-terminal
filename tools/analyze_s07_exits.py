"""s07 的出场机制与"限价（maker）出场"可行性初步分析。只读。"""
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
                        where event_type='trade_closed' and strategy_id like 's07%'
                        order by observed_at_ms""").fetchall()

    reasons = collections.Counter()
    holds = []
    recs = []
    for sid, pl, ob in rows:
        p = J(pl)
        r = str(p.get('exit_reason') or p.get('reason') or '?')
        reasons[r] += 1
        o, cl = p.get('opened_candle_ms'), p.get('closed_candle_ms')
        nn = float(p.get('notional') or 0)
        if not nn:
            continue
        recs.append({
            'sid': sid, 'reason': r, 'pair': p.get('pair'), 'side': p.get('side'),
            'entry': float(p.get('entry_price') or 0), 'exit': float(p.get('exit_price') or 0),
            'notional': nn, 'gross': float(p.get('gross_pnl') or 0),
            'fee': float(p.get('fees') or 0), 'net': float(p.get('net_pnl') or 0),
            'open_ms': int(o or 0), 'close_ms': int(cl or ob),
            'entry_oid': p.get('entry_order_id') or p.get('exchange_order_id'),
            'exit_oid': p.get('exit_order_id'),
        })
        if o and cl:
            holds.append((int(cl) - int(o)) / 60000)

    print(f'  s07 平仓 {len(rows)} 笔')
    print(f'  ═══ 出场原因分布 ═══')
    for r, n in reasons.most_common():
        print(f'    {r:<28}{n:>5}')
    if holds:
        holds.sort()
        print(f'  持有期: min {holds[0]:.0f} · p25 {holds[len(holds)//4]:.0f} · '
              f'中位 {holds[len(holds)//2]:.0f} · p75 {holds[3*len(holds)//4]:.0f} · max {holds[-1]:.0f} 分')

    print()
    print('  ═══ 按出场原因看毛/净（判断哪一类在赚钱）═══')
    byr = collections.defaultdict(list)
    for r in recs:
        byr[r['reason']].append(r)
    print(f'  {"原因":<28}{"笔":>5}{"毛中位":>9}{"净中位":>9}{"持有中位":>10}')
    for reason, rs in sorted(byr.items(), key=lambda x: -len(x[1])):
        g = statistics.median([x['gross'] / x['notional'] * 10000 for x in rs])
        ne = statistics.median([x['net'] / x['notional'] * 10000 for x in rs])
        h = statistics.median([(x['close_ms'] - x['open_ms']) / 60000 for x in rs if x['open_ms']])
        print(f'  {reason[:26]:<28}{len(rs):>5}{g:>+9.2f}{ne:>+9.2f}{h:>10.0f}')

    print()
    print('  ═══ 最新版本（B15m）的逐笔样例（前 8 笔）══')
    b = [r for r in recs if 'B15m' in r['sid'] and r['open_ms']]
    b.sort(key=lambda r: r['close_ms'])
    for r in b[:8]:
        t0 = dt.datetime.fromtimestamp(r['open_ms'] / 1000, dt.timezone.utc).strftime('%m-%d %H:%M')
        t1 = dt.datetime.fromtimestamp(r['close_ms'] / 1000, dt.timezone.utc).strftime('%m-%d %H:%M')
        move = (r['exit'] - r['entry']) / r['entry'] * 10000 * (1 if r['side'] == 'long' else -1)
        print(f'    {t0}→{t1} {r["pair"]:<15}{r["side"]:<6} 利向 {move:>+7.1f}bp · '
              f'毛 {r["gross"] / r["notional"] * 10000:>+7.2f} · 净 {r["net"] / r["notional"] * 10000:>+7.2f} · {r["reason"]}')
