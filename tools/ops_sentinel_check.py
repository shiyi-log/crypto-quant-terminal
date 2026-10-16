"""运维哨兵（只读）: 托管采集器 + 新鲜度 + 覆盖率 + 摘要。"""
import datetime as dt
import json
import os
import time
import psycopg

NOW = int(time.time() * 1000)

for f, tag in (('database/runtime/market_stream_status.json', 'runtime'),
               ('database/runtime/trx/market_stream_status.json', 'trx')):
    if os.path.exists(f):
        d = json.load(open(f))
        print(f'  {tag}: intervals {d.get("kline_intervals")} · db_fail {d.get("database_failures")} · '
              f'write_fail {d.get("write_failure_batches")} · spool {d.get("spool_batches")} · '
              f'err {str(d.get("last_error"))[:30]}')

with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    for ct, tf, ms, label in (('futures', '15m', 900000, 'futures 15m'),
                              ('futures', '1h', 3600000, 'futures 1h'),
                              ('futures', '1d', 86400000, 'futures 1d'),
                              ('spot', '5m', 300000, 'spot 5m')):
        r = c.execute("""select max(timestamp) from quant.candles
                         where exchange='binance' and candle_type=%s and timeframe=%s
                           and split_part(pair,'/',1)='BTC'""", (ct, tf)).fetchone()[0]
        if r:
            print(f'  {label}: 滞后 {(NOW - (int(r) + ms)) / 60000:.1f} 分')
    mx = c.execute("select max(observed_at_ms) from quant.strategy_sim_events where event_type='decision'").fetchone()[0]
    print(f'  最近决策: {dt.datetime.fromtimestamp(int(mx) / 1000, dt.timezone.utc):%H:%M:%S}Z'
          f'（{(NOW - int(mx)) / 60000:.1f} 分前）')
    n = c.execute("select count(*) from quant.demo_orders where virtual_account_key like 'demo_strategy_%'").fetchone()[0]
    print(f'  Demo 订单: {n} 笔')

spool = 'database/runtime/spot/market_spool'
if os.path.isdir(spool):
    print(f'  spot spool: {len([x for x in os.listdir(spool) if x.endswith(".json")])} 个')

d = json.load(open('database/runtime/strategy_lab_status.json'))
print(f'  实验室: pid {d.get("pid")} · running {d.get("running")} · accounts {d.get("account_count")} · '
      f'err {str(d.get("last_error"))[:24]}')
for k, v in (d.get('native_timeframes') or {}).items():
    ts = dt.datetime.fromtimestamp(int(v.get('latest_closed_at')) / 1000, dt.timezone.utc).strftime('%H:%M')
    print(f'    {k}: latest_closed={ts}Z · {v.get("status")}')
