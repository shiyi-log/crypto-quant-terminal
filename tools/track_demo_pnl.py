"""Demo（实盘模拟）P&L 跟踪器 —— 目标是【前瞻证据】，不是历史考古。

口径声明（与报告 §一点五 一致）：
  · 数据源: quant.demo_fills（真实交易所成交回执）
  · realized_pnl 来自交易所（通常【不含】commission）⇒ 净 = Σrp − Σcommission
  · 【区分】策略账户（demo_strategy_*）与测试账户（manual/smoke/fee_capture/second_level）
  · 【区分】时间窗（可传 --since）—— 事故/修复期的数据应排除
只读。
"""
import argparse, collections, datetime as dt, json, os
import psycopg

TEST_PREFIXES = ('manual_', 'second_level_', 'fee_capture_', 'smoke_')


def classify(vk: str) -> str:
    return 'test' if vk.startswith(TEST_PREFIXES) else ('strategy' if vk.startswith('demo_strategy_') else 'other')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', help='ISO8601 UTC，只统计此后成交')
    ap.add_argument('--by-account', action='store_true')
    args = ap.parse_args()

    since_ms = None
    if args.since:
        s = args.since.replace('Z', '+00:00')
        since_ms = int(dt.datetime.fromisoformat(s).timestamp() * 1000)

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        q = """select virtual_account_key, pair, price, quantity, commission, is_maker,
                      realized_pnl, timestamp_ms
                 from quant.demo_fills"""
        params = []
        if since_ms is not None:
            q += ' where timestamp_ms >= %s'
            params.append(since_ms)
        q += ' order by timestamp_ms'
        rows = c.execute(q, params).fetchall()

    print(f'  ═══ Demo P&L 跟踪 ═══')
    print(f'  窗口: {"全部" if since_ms is None else args.since}')
    print(f'  成交 {len(rows)} 笔')
    if not rows:
        return

    grp = collections.defaultdict(lambda: {'n': 0, 'fee': 0.0, 'rp': 0.0, 'notional': 0.0,
                                           'maker': 0, 'taker': 0})
    for vk, pair, px, qty, com, mk, rp, ts in rows:
        g = grp[classify(vk)]
        g['n'] += 1
        g['fee'] += float(com or 0)
        g['rp'] += float(rp or 0)
        g['notional'] += float(px) * float(qty)
        g['maker' if mk else 'taker'] += 1

    print()
    print(f'  {"类别":<10}{"成交":>6}{"名义":>12}{"费":>10}{"realized":>11}{"净(rp−费)":>12}{"费/名义":>10}{"maker/taker"}')
    for k in ('strategy', 'test', 'other'):
        g = grp.get(k)
        if not g:
            continue
        net = g['rp'] - g['fee']
        bps = g['fee'] / g['notional'] * 10000 if g['notional'] else 0
        print(f'  {k:<10}{g["n"]:>6}{g["notional"]:>12.2f}{g["fee"]:>10.6f}{g["rp"]:>+11.4f}{net:>+12.4f}{bps:>10.4f}'
              f'  {g["maker"]}/{g["taker"]}')

    tot_net = sum(g['rp'] - g['fee'] for g in grp.values())
    strat = grp.get('strategy', {'rp': 0, 'fee': 0, 'n': 0})
    print()
    print(f'  ⇒ 全体净 = {tot_net:+.4f} USDT')
    if strat['n']:
        print(f'  ⇒ 【策略账户净 = {strat["rp"] - strat["fee"]:+.4f} USDT】（{strat["n"]} 笔成交）')

    if args.by_account:
        print()
        print(f'  ═══ 按账户 ═══')
        print(f'  {"账户":<52}{"类别":<9}{"成交":>5}{"费":>10}{"realized":>11}{"净":>11}')
        acc = collections.defaultdict(lambda: {'n': 0, 'fee': 0.0, 'rp': 0.0})
        for vk, pair, px, qty, com, mk, rp, ts in rows:
            a = acc[vk]
            a['n'] += 1
            a['fee'] += float(com or 0)
            a['rp'] += float(rp or 0)
        for vk, a in sorted(acc.items(), key=lambda kv: -(kv[1]['rp'] - kv[1]['fee'])):
            net = a['rp'] - a['fee']
            print(f'  {vk[:50]:<52}{classify(vk):<9}{a["n"]:>5}{a["fee"]:>10.6f}{a["rp"]:>+11.4f}{net:>+11.4f}')


if __name__ == '__main__':
    main()
