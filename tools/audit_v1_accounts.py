"""检查 v1 模型账户是否有止损 / 是否交易过。只读。"""
import datetime as dt
import json
import os
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
    v1 = [(sid, J(exe)) for sid, exe in rows if 'next-close-open-v1' in str(J(exe).get('version') or '')]
    print(f'  v1-base 账户 {len(v1)} 个:')
    for sid, e in v1:
        print(f'    {sid[:56]:<58} enabled={e.get("execution_enabled")}')
    print()
    print('  ══ 它们的开仓记录（全历史）与是否有止损 ══')
    for sid, e in v1:
        r = c.execute(
            """select payload, observed_at_ms from quant.strategy_sim_events
               where strategy_id=%s and event_type='position_opened'
               order by observed_at_ms desc limit 1""", (sid,)).fetchone()
        n = c.execute(
            "select count(*) from quant.strategy_sim_events where strategy_id=%s and event_type='position_opened'",
            (sid,)).fetchone()[0]
        if r:
            p = J(r[0])
            ts = dt.datetime.fromtimestamp(int(r[1]) / 1000, dt.timezone.utc).strftime('%m-%d %H:%M')
            print(f'    {sid[:52]:<54} 开仓 {n:>3} 次 · 最后 {ts}Z · '
                  f'stop_level={p.get("stop_level")} · margin={p.get("margin")} · notional={p.get("notional")}')
        else:
            print(f'    {sid[:52]:<54} 开仓 {n:>3} 次 · 【从未开仓】')
    print()
    print('  ══ 这 10 个账户是否有 trade_closed（是否产生过盈亏）══')
    tot = 0.0
    for sid, e in v1:
        rows2 = c.execute(
            """select payload from quant.strategy_sim_events
               where strategy_id=%s and event_type='trade_closed'""", (sid,)).fetchall()
        net = sum(float(J(x[0]).get('net_pnl') or 0) for x in rows2)
        tot += net
        if rows2:
            print(f'    {sid[:52]:<54} {len(rows2):>3} 笔 · 净 {net:+.2f}')
    if not tot:
        print('    （全部无平仓）')
