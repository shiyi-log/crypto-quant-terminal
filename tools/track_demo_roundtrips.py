"""Demo（实盘模拟）【往返级】P&L —— 把入场腿与出场腿配对，算每笔往返的净盈亏。

与 track_demo_pnl.py 的区别：
  · track_demo_pnl.py 是【现金流式】—— 累加所有 fill 的 realized_pnl 与 commission
    （含尚未平仓的入场费）
  · 本工具是【往返式】—— 按 FIFO 把出场腿与入场腿配对，只在【平仓时】结算一笔往返
    （这才是"策略能否盈利"的正确单位）

配对规则: 同一账户 + 同一 pair，按时间 FIFO；入场 = 非 reduce_only 的成交腿，
         出场 = reduce_only 的成交腿。手续费双向计入。
只读。
"""
import argparse, collections, datetime as dt, os
import psycopg

TEST_PREFIXES = ('manual_', 'second_level_', 'fee_capture_', 'smoke_')


def classify(vk: str) -> str:
    return 'test' if vk.startswith(TEST_PREFIXES) else (
        'strategy' if vk.startswith('demo_strategy_') else 'other')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', help='ISO8601 UTC，只统计此后【平仓】的往返')
    ap.add_argument('--by-trip', action='store_true', help='逐笔打印往返')
    args = ap.parse_args()

    since_ms = None
    if args.since:
        since_ms = int(dt.datetime.fromisoformat(args.since.replace('Z', '+00:00')).timestamp() * 1000)

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        cols = [d[0] for d in c.execute('select * from quant.demo_fills limit 0').description]
        fills = [dict(zip(cols, r)) for r in c.execute(
            'select * from quant.demo_fills order by timestamp_ms').fetchall()]
        # reduce_only 不在 fills 上 ⇒ 从 demo_orders 取
        ro = {}
        for oid, r in c.execute("""select exchange_order_id::text, reduce_only
                                     from quant.demo_orders
                                    where exchange_order_id is not null"""):
            ro[oid] = bool(r)

    open_legs = collections.defaultdict(list)   # (account, pair) -> [(ts, qty, notional, fee)]
    trips = []
    for f in fills:
        oid = str(f.get('exchange_order_id') or '')
        is_exit = ro.get(oid, False)
        px, qty = float(f['price']), float(f['quantity'])
        notional, fee = px * qty, float(f['commission'] or 0)
        k = (f['virtual_account_key'], f['pair'])
        if not is_exit:
            open_legs[k].append([int(f['timestamp_ms']), qty, notional, fee])
            continue
        remaining = qty
        while remaining > 1e-12 and open_legs[k]:
            leg = open_legs[k][0]
            take = min(remaining, leg[1])
            frac = take / leg[1]
            entry_cost = leg[2] * frac
            entry_fee = leg[3] * frac
            exit_value = px * take
            exit_fee = fee * (take / qty)
            trips.append({
                'account': f['virtual_account_key'], 'pair': f['pair'],
                'open_ms': leg[0], 'close_ms': int(f['timestamp_ms']),
                'qty': take, 'gross': exit_value - entry_cost,
                'fee': entry_fee + exit_fee, 'notional': entry_cost,
            })
            leg[1] -= take
            leg[2] -= entry_cost
            leg[3] -= entry_fee
            remaining -= take
            if leg[1] <= 1e-12:
                open_legs[k].pop(0)

    if since_ms is not None:
        trips = [t for t in trips if t['close_ms'] >= since_ms]

    print('  ═══ Demo 往返级 P&L ═══')
    print(f'  窗口: {"全部" if since_ms is None else args.since}')
    print(f'  已完成往返: {len(trips)} 笔')
    if not trips:
        return

    if args.by_trip:
        print()
        print(f'  {"账户":<44}{"pair":<16}{"持有":>7}{"名义":>10}{"毛":>10}{"费":>9}{"净":>10}{"净 bp":>9}')
        for t in trips:
            hold = (t['close_ms'] - t['open_ms']) / 60000
            net = t['gross'] - t['fee']
            bp = net / t['notional'] * 10000 if t['notional'] else 0
            print(f'  {t["account"][:42]:<44}{t["pair"]:<16}{hold:>6.0f}m'
                  f'{t["notional"]:>10.2f}{t["gross"]:>+10.4f}{t["fee"]:>9.5f}{net:>+10.4f}{bp:>+9.2f}')

    print()
    for cls in ('strategy', 'test', 'other'):
        sub = [t for t in trips if classify(t['account']) == cls]
        if not sub:
            continue
        g = sum(t['gross'] for t in sub)
        f = sum(t['fee'] for t in sub)
        n = sum(t['notional'] for t in sub)
        wins = sum(1 for t in sub if t['gross'] - t['fee'] > 0)
        print(f'  {cls:<10}{len(sub):>5} 笔 · 名义 {n:>10.2f} · 毛 {g:>+9.4f} · 费 {f:>8.5f} · '
              f'【净 {g - f:>+9.4f}】· 胜率 {wins / len(sub) * 100:>5.1f}% · '
              f'净 bp {(g - f) / n * 10000 if n else 0:>+7.2f}')
    print()
    print('  ⚠️ 口径: 【往返式】—— 只在平仓时结算，未平仓的入场费不计入（与现金流式不同）')


if __name__ == '__main__':
    main()
