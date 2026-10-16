"""TTL 延长的【盈亏】回放: 新增的成交是盈利还是亏损？

对每笔被 TTL 取消的入场单:
  ① 入场: 在触价的那一分钟按限价成交（TTL 窗口内首次触及）
  ② 出场: 按 s07 的实际持有期（1 根 15m 或 2 根 15m = 15/30 分钟）用收盘价出场
  ③ 净 bp = 方向 × (exit/entry − 1) × 10000 − 成本
     成本: 入场 maker 2 bp + 出场 taker 4 bp = 6 bp（R404 实测）

对比 TTL=30s 与 TTL=60s 两组的净 bp。
只读。
"""
import os, json, datetime as dt
import psycopg

COST_BP = 6.0
HOLD_BARS = (1, 2)   # 1 根或 2 根 15m


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select pair, side, status, price, created_ms, intent_payload
                          from quant.demo_orders
                         where virtual_account_key like 'demo_strategy_%'
                           and status in ('canceled','rejected','abandoned_after_absent_lookup')
                           and order_type='limit'
                         order by created_ms""").fetchall()

    print(f'  未成交入场单: {len(rows)}')
    print()
    print(f'  {"时间":<10}{"pair":<16}{"dir":<6}{"limit":>11}{"TTL30":>7}{"TTL60":>7}'
          f'{"fill@":>7}{"hold":>6}{"net_bp":>9}{"cost":>7}')
    res = {'30': [], '60': []}
    for pair, side, st, price, cm, ip in rows:
        P = float(price)
        p = J(ip)
        sig = (p.get('paper_signal') or {})
        direction = sig.get('side')  # 'long' / 'short'
        if direction is None:
            direction = 'long' if side == 'buy' else 'short'

        # 1m K 线（用于找触价时刻）
        ks = c.execute("""select timestamp, low, high from quant.candles
                            where exchange='binance' and candle_type='futures'
                              and timeframe='1m' and pair=%s
                              and timestamp >= %s and timestamp <= %s
                            order by timestamp""", (pair, cm, cm + 360_000)).fetchall()

        def first_touch(win_ms):
            for ts, l, h in ks:
                if ts > cm + win_ms:
                    break
                if side == 'buy' and float(l) <= P:
                    return ts
                if side == 'sell' and float(h) >= P:
                    return ts
            return None

        t30, t60 = first_touch(30_000), first_touch(60_000)
        # 只评估 TTL60 才成交的（即 TTL 延长新增的）
        if t60 is None:
            continue
        fill_ms = t60 + 60_000  # 该分钟收盘时成交（保守）
        # 15m 收盘价序列
        closes = c.execute("""select timestamp, close from quant.candles
                                where exchange='binance' and candle_type='futures'
                                  and timeframe='15m' and pair=%s
                                  and timestamp >= %s and timestamp <= %s
                                order by timestamp""", (pair, cm, cm + 3 * 3600_000)).fetchall()
        # 找到 fill 之后第 1/2 根 15m 的收盘
        after = [(ts, float(cl)) for ts, cl in closes if ts > fill_ms]
        if len(after) < max(HOLD_BARS):
            continue
        for hb in HOLD_BARS:
            exit_px = after[hb - 1][1]
            gross_bp = (exit_px / P - 1) * 10000
            if direction == 'short':
                gross_bp = -gross_bp
            net = gross_bp - COST_BP
            res['60'].append(net)
            if t30 is not None:
                res['30'].append(net)
            if hb == HOLD_BARS[0]:
                print(f'  {dt.datetime.fromtimestamp(int(cm)/1000, dt.timezone.utc):%m-%d %H:%M:%S}Z '
                      f'{pair:<16}{direction:<6}{P:>11.4f}'
                      f'{("✓" if t30 else "·"):>7}{"✓":>7}'
                      f'{dt.datetime.fromtimestamp(int(fill_ms)/1000, dt.timezone.utc):%H:%M:%S}'
                      f'{hb*15:>5}m{net:>+9.2f}{COST_BP:>7.1f}')

    print()
    print('  ═══ 汇总（TTL=60s 会成交的那批 · 持有 1 或 2 根 15m）═══')
    for k in ('30', '60'):
        v = res[k]
        if not v:
            continue
        mean = sum(v) / len(v)
        print(f'    TTL {k}s 组: n={len(v)} · 净均值 {mean:+.2f} bp · '
              f'胜率 {sum(1 for x in v if x > 0)/len(v)*100:.1f}%')
    v60 = res['60']
    if v60:
        mean = sum(v60) / len(v60)
        print()
        print(f'  ⇒ 【TTL 延长新增的成交: 净均值 {mean:+.2f} bp（成本后）】')
        print(f'  ⇒ ⇒ {"✅ 新增成交为正 ⇒ TTL 延长有价值" if mean > 0 else "🔴 新增成交为负 ⇒ TTL 延长【只会增加亏损笔数】"}')
