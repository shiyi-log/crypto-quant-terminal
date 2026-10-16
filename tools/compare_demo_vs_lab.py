"""对比 Demo 实盘（f6cb33）与 lab 的 s07: 持有期 / 毛 / 费 / 净。只读。

目的: 解释"Demo 实盘 +9.79 bp vs lab −0.64 bp"的差异来源。
候选解释: (a) 采样（n=7 vs 450）(b) 持有期不同 (c) pair 不同 (d) 时段不同
"""
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

CURRENT = 'demo_strategy_f6cb33743f6146c385dddf3bd061892c'


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    cols = [d[0] for d in c.execute('select * from quant.demo_fills limit 0').description]
    fl = [dict(zip(cols, r)) for r in c.execute(
        'select * from quant.demo_fills where virtual_account_key=%s order by timestamp_ms',
        (CURRENT,)).fetchall()]
    ro = {}
    for oid, r in c.execute("""select exchange_order_id::text, reduce_only from quant.demo_orders
                               where exchange_order_id is not null"""):
        ro[oid] = bool(r)

    # 组往返
    open_legs = collections.defaultdict(list)
    trips = collections.OrderedDict()
    for f in fl:
        oid = str(f.get('exchange_order_id') or '')
        px, qty = float(f['price']), float(f['quantity'])
        notional, fee = px * qty, float(f['commission'] or 0)
        if not ro.get(oid, False):
            open_legs[f['pair']].append([int(f['timestamp_ms']), qty, notional, fee])
            continue
        close_ms = int(f['timestamp_ms'])
        t = trips.setdefault((f['pair'], close_ms),
                             {'pair': f['pair'], 'close': close_ms, 'open': None,
                              'gross': 0.0, 'fee': 0.0, 'notional': 0.0})
        rem = qty
        while rem > 1e-12 and open_legs[f['pair']]:
            leg = open_legs[f['pair']][0]
            take = min(rem, leg[1])
            fr = take / leg[1]
            ec, ef = leg[2] * fr, leg[3] * fr
            t['gross'] += px * take - ec
            t['fee'] += ef + fee * (take / qty)
            t['notional'] += ec
            if t['open'] is None or leg[0] < t['open']:
                t['open'] = leg[0]
            leg[1] -= take
            leg[2] -= ec
            leg[3] -= ef
            rem -= take
            if leg[1] <= 1e-12:
                open_legs[f['pair']].pop(0)
    trips = [t for t in trips.values() if t['notional'] > 0]

    print(f'  ═══ Demo 实盘（f6cb33）往返明细 ═══')
    print(f'  {"平仓":<12}{"pair":<16}{"持有(分)":>9}{"名义":>9}{"毛bp":>8}{"费bp":>7}{"净bp":>8}')
    for t in trips:
        hold = (t['close'] - t['open']) / 60000 if t['open'] else 0
        g = t['gross'] / t['notional'] * 10000
        fe = t['fee'] / t['notional'] * 10000
        n = g - fe
        print(f'  {dt.datetime.fromtimestamp(t["close"] / 1000, dt.timezone.utc):%m-%d %H:%M}'
              f'{t["pair"]:<16}{hold:>9.0f}{t["notional"]:>9.2f}{g:>+8.2f}{fe:>7.2f}{n:>+8.2f}')
    tot = sum(t['notional'] for t in trips)
    print(f'  ⇒ 合计: {len(trips)} 笔 · 名义 {tot:.2f} · '
          f'毛 {sum(t["gross"] for t in trips) / tot * 10000:+.2f} bp · '
          f'费 {sum(t["fee"] for t in trips) / tot * 10000:.2f} bp · '
          f'【净 {sum(t["gross"] - t["fee"] for t in trips) / tot * 10000:+.2f} bp】')
    holds = [(t['close'] - t['open']) / 60000 for t in trips if t['open']]
    if holds:
        print(f'  持有期: min {min(holds):.0f} · 中位 {statistics.median(holds):.0f} · max {max(holds):.0f} 分')

    # lab 对照（同策略/同窗口）
    rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                        where event_type='trade_closed' and strategy_id like 's07%'
                          and strategy_id like '%B15m%'""").fetchall()
    lab_holds, lab_g = [], []
    for sid, pl, ob in rows:
        p = J(pl)
        nn = float(p.get('notional') or 0)
        o, cl = p.get('opened_candle_ms'), p.get('closed_candle_ms')
        if not nn or not o or not cl:
            continue
        lab_holds.append((int(cl) - int(o)) / 60000)
        lab_g.append(float(p.get('gross_pnl') or 0) / nn * 10000)
    print()
    print('  ═══ lab 对照（s07 B15m · 450 笔）═══')
    print(f'    持有期中位 {statistics.median(lab_holds):.0f} 分 · 毛中位 {statistics.median(lab_g):+.2f} bp')
    print(f'    ⇒ Demo 持有中位 {statistics.median(holds) if holds else 0:.0f} 分'
          f' vs lab {statistics.median(lab_holds):.0f} 分')
