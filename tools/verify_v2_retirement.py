"""验证 v2 账户退役是否生效（重启后应 0 事件）。只读。"""
import datetime as dt
import json
import os
import collections
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


CUT = int(dt.datetime(2026, 10, 16, 7, 9, 3, tzinfo=dt.timezone.utc).timestamp() * 1000)
RETIRED_FIVE = ("s14", "s34", "s09", "s03", "s17")

with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
    v2 = {sid for sid, exe in rows if 'liquidation-v2' in str(J(exe).get('version') or '')}
    print(f'  v2 账户总数: {len(v2)}')
    n_off = 0
    for sid in v2:
        r = c.execute("select execution->>'execution_enabled' from quant.strategy_sim_accounts where strategy_id=%s", (sid,)).fetchone()
        if r and r[0] == 'false':
            n_off += 1
    print(f'  ⇒ 其中 execution_enabled=false: 【{n_off}/{len(v2)}】')

    ev = c.execute('select strategy_id, event_type, observed_at_ms from quant.strategy_sim_events where observed_at_ms>=%s', (CUT,)).fetchall()
    print(f'  重启后（07:09:03Z 起）事件: {len(ev)}')
    byv2 = [e for e in ev if e[0] in v2]
    print(f'  ⇒ 【来自 v2 账户的事件: {len(byv2)}】' + (' ✅' if not byv2 else ' ❌'))

    b = collections.Counter(int(e[2]) for e in ev)
    for ob, n in sorted(b.items())[:5]:
        print(f'    {dt.datetime.fromtimestamp(ob/1000, dt.timezone.utc):%H:%M:%S}Z: {n} 条')

    q = "select count(*) from quant.strategy_sim_events where strategy_id ~ %s and observed_at_ms>=%s"
    r5 = c.execute(q, ('^s(14|34|09|03|17)_', CUT)).fetchone()[0]
    print(f'  ⇒ 【前次退役 5 族: {r5}】')

    for f in ('s16', 's18', 's32', 's33', 's36', 's07', 's20', 's35', 's02'):
        n = c.execute("select count(*) from quant.strategy_sim_events where strategy_id like %s and observed_at_ms>=%s",
                      (f + '_%', CUT)).fetchone()[0]
        print(f'    {f}: {n}')
