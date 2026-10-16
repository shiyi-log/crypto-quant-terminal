"""综合状态核查（只读）: 退役生效 · 检验点进度 · 全链路健康。"""
import collections
import datetime as dt
import json
import os
import time

import psycopg

NOW = int(time.time() * 1000)
FAMS = ('s12', 's18', 's21', 's14', 's34', 's09', 's03', 's17')
CURRENT = 'demo_strategy_f6cb33743f6146c385dddf3bd061892c'


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def t(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime('%H:%M:%S')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    # ── ① 退役生效 ──
    rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
    retired = {sid for sid, exe in rows if sid.split('_')[0] in FAMS}
    off = sum(1 for sid, exe in rows if sid.split('_')[0] in FAMS
              and J(exe).get('execution_enabled') is False)
    print(f'  ① 退役: {len(retired)} 个账户 · execution_enabled=false 【{off}/{len(retired)}】')

    for label, cut in (('s12/s18/s21 退役后（07:19:34Z）',
                        int(dt.datetime(2026, 10, 16, 7, 19, 34, tzinfo=dt.timezone.utc).timestamp() * 1000)),
                       ('全 8 族退役后（07:09Z）',
                        int(dt.datetime(2026, 10, 16, 7, 9, 3, tzinfo=dt.timezone.utc).timestamp() * 1000))):
        ev = c.execute('select strategy_id, observed_at_ms from quant.strategy_sim_events where observed_at_ms>=%s',
                       (cut,)).fetchall()
        bad = [e for e in ev if e[0] in retired]
        print(f'     {label}: 事件 {len(ev)} · 【退役族事件 {len(bad)}】' + (' ✅' if not bad else ' ❌'))

    # ── ② 检验点进度 ──
    cols = [d[0] for d in c.execute('select * from quant.demo_fills limit 0').description]
    fl = [dict(zip(cols, r)) for r in c.execute(
        'select * from quant.demo_fills where virtual_account_key=%s order by timestamp_ms',
        (CURRENT,)).fetchall()]
    ro = {}
    for oid, r in c.execute("""select exchange_order_id::text, reduce_only from quant.demo_orders
                               where exchange_order_id is not null"""):
        ro[oid] = bool(r)
    exits = [f for f in fl if ro.get(str(f.get('exchange_order_id') or ''), False)]
    print()
    print(f'  ② 检验点进度（PREREG 目标 n≥100）')
    print(f'     当前账户: 成交 {len(fl)} · 【出场（=往返）{len(exits)}】')
    if fl:
        span_h = (int(fl[-1]['timestamp_ms']) - int(fl[0]['timestamp_ms'])) / 3600000
        rate = len(exits) / span_h if span_h else 0
        print(f'     跨度 {span_h:.1f} h ⇒ 速率 {rate:.3f} 往返/h')
        if rate:
            print(f'     ⇒ 距 100 还需 {max(0, 100 - len(exits))} 笔 ⇒ 约 【{max(0, 100 - len(exits)) / rate / 24:.1f} 天】')

    # ── ③ 全链路健康 ──
    print()
    print('  ③ 全链路健康')
    for ct, tf, ms, label in (('futures', '15m', 900000, 'futures 15m'),
                              ('futures', '1h', 3600000, 'futures 1h'),
                              ('spot', '5m', 300000, 'spot 5m')):
        r = c.execute("""select max(timestamp) from quant.candles
                         where exchange='binance' and candle_type=%s and timeframe=%s
                           and split_part(pair,'/',1)='BTC'""", (ct, tf)).fetchone()[0]
        if r:
            print(f'     {label}: 滞后 {(NOW - (int(r) + ms)) / 60000:.1f} 分')
    mx = c.execute("select max(observed_at_ms) from quant.strategy_sim_events where event_type='decision'").fetchone()[0]
    print(f'     最近决策: {t(mx)}Z（{(NOW - int(mx)) / 60000:.1f} 分前）')
    n = c.execute("select count(*) from quant.demo_orders where virtual_account_key like 'demo_strategy_%'").fetchone()[0]
    print(f'     Demo 订单: {n} 笔')

d = json.load(open('database/runtime/strategy_lab_status.json'))
print(f'     实验室: pid {d.get("pid")} · running {d.get("running")} · accounts {d.get("account_count")} · '
      f'err {str(d.get("last_error"))[:24]}')
for k, v in (d.get('native_timeframes') or {}).items():
    print(f'       {k}: latest_closed={t(v.get("latest_closed_at"))}Z · {v.get("status")}')
