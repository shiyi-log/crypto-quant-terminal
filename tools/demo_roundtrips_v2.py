"""Demo 往返级 P&L（**修正版**: 按【唯一出场】聚合，不被 FIFO 拆分放大）。只读。

修正要点（R427 的教训）:
  · 旧版把一笔大额出场的 FIFO 拆成多个小往返 ⇒ 计数被放大（例: 1 笔出场被拆成 20+）
  · 本版: 以【一次出场（账户+pair+平仓毫秒）】为一个往返单位，
    其名义/毛/费 = 该次出场配对的所有入场腿【之和】
  · 输出: 单位往返表 + 分类汇总 + 现金式对照

用法:
  python3 tools/demo_roundtrips_v2.py [--since ISO8601] [--by-trip]
"""
import argparse
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

TEST_PREFIXES = ('manual_', 'second_level_', 'fee_capture_', 'smoke_')


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def classify(vk):
    if vk.startswith(TEST_PREFIXES):
        return 'test'
    return 'strategy' if vk.startswith('demo_strategy_') else 'other'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', help='ISO8601 UTC；只统计此后【平仓】的往返')
    ap.add_argument('--by-trip', action='store_true')
    args = ap.parse_args()

    since_ms = None
    if args.since:
        since_ms = int(dt.datetime.fromisoformat(args.since.replace('Z', '+00:00')).timestamp() * 1000)

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        cols = [d[0] for d in c.execute('select * from quant.demo_fills limit 0').description]
        fills = [dict(zip(cols, r)) for r in c.execute(
            'select * from quant.demo_fills order by timestamp_ms').fetchall()]
        ro = {}
        for oid, r in c.execute("""select exchange_order_id::text, reduce_only
                                     from quant.demo_orders
                                    where exchange_order_id is not null"""):
            ro[oid] = bool(r)

    open_legs = collections.defaultdict(list)   # (account,pair) -> [ts, qty, notional, fee]
    # 以【一次出场】为单位聚合: key = (account, pair, close_ms)
    trips = collections.OrderedDict()
    for f in fills:
        oid = str(f.get('exchange_order_id') or '')
        is_exit = ro.get(oid, False)
        px, qty = float(f['price']), float(f['quantity'])
        notional, fee = px * qty, float(f['commission'] or 0)
        k = (f['virtual_account_key'], f['pair'])
        if not is_exit:
            open_legs[k].append([int(f['timestamp_ms']), qty, notional, fee])
            continue
        close_ms = int(f['timestamp_ms'])
        key = (f['virtual_account_key'], f['pair'], close_ms)
        t = trips.setdefault(key, {'account': f['virtual_account_key'], 'pair': f['pair'],
                                   'open_ms': None, 'close_ms': close_ms,
                                   'qty': 0.0, 'gross': 0.0, 'fee': 0.0, 'notional': 0.0,
                                   'legs': 0})
        remaining = qty
        while remaining > 1e-12 and open_legs[k]:
            leg = open_legs[k][0]
            take = min(remaining, leg[1])
            frac = take / leg[1]
            entry_cost = leg[2] * frac
            entry_fee = leg[3] * frac
            t['qty'] += take
            t['gross'] += px * take - entry_cost
            t['fee'] += entry_fee + fee * (take / qty)
            t['notional'] += entry_cost
            t['legs'] += 1
            if t['open_ms'] is None or leg[0] < t['open_ms']:
                t['open_ms'] = leg[0]
            leg[1] -= take
            leg[2] -= entry_cost
            leg[3] -= entry_fee
            remaining -= take
            if leg[1] <= 1e-12:
                open_legs[k].pop(0)

    trips = [t for t in trips.values() if t['notional'] > 0]
    if since_ms is not None:
        trips = [t for t in trips if t['close_ms'] >= since_ms]

    print('  ═══ Demo 往返级 P&L（修正版 · 按【唯一出场】计数）═══')
    print(f'  窗口: {"全部" if since_ms is None else args.since}')
    print(f'  已完成往返: 【{len(trips)}】笔（旧 FIFO 口径会放大此数）')
    if not trips:
        return

    if args.by_trip:
        print()
        print(f'  {"账户":<44}{"pair":<16}{"持有":>7}{"名义":>10}{"毛":>9}{"费":>8}{"净":>9}{"净bp":>8}{"腿":>4}')
        for t in trips:
            hold = (t['close_ms'] - t['open_ms']) / 60000 if t['open_ms'] else 0
            net = t['gross'] - t['fee']
            bp = net / t['notional'] * 10000
            print(f'  {t["account"][:42]:<44}{t["pair"]:<16}{hold:>6.0f}m'
                  f'{t["notional"]:>10.2f}{t["gross"]:>+9.4f}{t["fee"]:>8.5f}{net:>+9.4f}{bp:>+8.2f}{t["legs"]:>4}')

    print()
    for cls in ('strategy', 'test', 'other'):
        sub = [t for t in trips if classify(t['account']) == cls]
        if not sub:
            continue
        g = sum(t['gross'] for t in sub)
        fe = sum(t['fee'] for t in sub)
        n = sum(t['notional'] for t in sub)
        nets = [(t['gross'] - t['fee']) / t['notional'] * 10000 for t in sub]
        wins = sum(1 for x in nets if x > 0)
        print(f'  {cls:<10}{len(sub):>4} 笔 · 名义 {n:>10.2f} · 毛 {g:>+9.4f} · 费 {fe:>8.5f} · '
              f'【净 {g - fe:>+9.4f}】· 净bp {(g - fe) / n * 10000 if n else 0:>+7.2f} · '
              f'中位净bp {statistics.median(nets):>+7.2f} · 胜率 {wins / len(sub) * 100:>5.1f}%')
    print()
    print('  ⚠️ 口径: 每个往返 = 【一次平仓】；其名义/毛/费为配对入场腿之和（不被 FIFO 拆分放大）')


if __name__ == '__main__':
    main()
