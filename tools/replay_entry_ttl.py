"""TTL 回放: 被 TTL 取消的入场单，在【延长后的窗口】内是否会成交？

规则: BUY limit @P 成交 ⟺ 1m K 线 low ≤ P；SELL limit @P 成交 ⟺ high ≥ P
窗口: 从 created_ms 起 30s / 60s / 120s / 300s
只读。
"""
import os, json, datetime as dt
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select pair, side, status, price, created_ms, intent_payload, signal_ms
                          from quant.demo_orders
                         where virtual_account_key like 'demo_strategy_%'
                           and status in ('canceled','rejected','abandoned_after_absent_lookup')
                           and order_type='limit'
                         order by created_ms""").fetchall()
    print(f'  待评估的未成交入场单: {len(rows)}')
    print()
    hdr = f'  {"时间":<10}{"pair":<16}{"side":<5}{"limit":>11}{"ref":>11}'
    for w in ('30s', '60s', '120s', '300s'):
        hdr += f'{w:>7}'
    print(hdr)
    stats = {30: 0, 60: 0, 120: 0, 300: 0}
    for pair, side, st, price, cm, ip, sms in rows:
        P = float(price)
        ref = J(ip).get('reference_price')
        ks = c.execute("""select timestamp, low, high from quant.candles
                            where exchange='binance' and candle_type='futures'
                              and timeframe='1m' and pair=%s
                              and timestamp >= %s and timestamp <= %s
                            order by timestamp""", (pair, cm, cm + 360_000)).fetchall()

        def touch(win_ms):
            for ts, l, h in ks:
                if ts > cm + win_ms:
                    break
                if side == 'buy' and float(l) <= P:
                    return True
                if side == 'sell' and float(h) >= P:
                    return True
            return False

        res = {w: touch(w * 1000) for w in (30, 60, 120, 300)}
        for w in stats:
            if res[w]:
                stats[w] += 1
        line = (f'  {dt.datetime.fromtimestamp(int(cm)/1000, dt.timezone.utc):%m-%d %H:%M:%S}Z '
                f'{pair:<16}{side:<5}{P:>11.4f}{str(ref):>11}')
        for w in (30, 60, 120, 300):
            line += f'{("✓" if res[w] else "·"):>7}'
        print(line)

    n = len(rows)
    print()
    print(f'  ═══ 汇总（n={n}）═══')
    for w in (30, 60, 120, 300):
        pct = stats[w] / n * 100 if n else 0
        print(f'    TTL {w:>3}s ⇒ 会成交 {stats[w]:>2}/{n} = 【{pct:.1f}%】')
