"""s07 边缘的【前向】检验: R436 之后是否有新数据？边缘是否恢复/继续衰减？只读。

输出:
  ① s07 全部平仓的时间序列（按小时聚合毛/净中位）
  ② R436 之后（新数据）的独立切片
  ③ 加载器修复（07:09Z）之后
  ④ 与 R436 结论的对比
"""
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

# R436 分析截止（10-16 当天的数据已在 R436 中用过 ⇒ 以 07:40Z 为界更安全）
R436_END = int(dt.datetime(2026, 10, 16, 7, 40, tzinfo=dt.timezone.utc).timestamp() * 1000)
FIX_MS = int(dt.datetime(2026, 10, 16, 7, 9, tzinfo=dt.timezone.utc).timestamp() * 1000)


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                        where event_type='trade_closed' and strategy_id like 's07%'
                        order by observed_at_ms""").fetchall()

recs = []
for sid, pl, ob in rows:
    p = J(pl)
    nn = float(p.get('notional') or 0)
    if not nn:
        continue
    recs.append({'close': int(ob),
                 'gross': float(p.get('gross_pnl') or 0) / nn * 10000,
                 'net': float(p.get('net_pnl') or 0) / nn * 10000})

print(f'  s07 平仓总计 {len(recs)} 笔')
if not recs:
    raise SystemExit

print()
print('  ═══ ① 按小时聚合（毛/净中位）═══')
byh = collections.defaultdict(list)
for r in recs:
    byh[dt.datetime.fromtimestamp(r['close'] / 1000, dt.timezone.utc).strftime('%m-%d %HZ')].append(r)
print(f'  {"小时":<12}{"笔":>5}{"毛中位":>9}{"净中位":>9}{"胜率":>7}')
for h in sorted(byh):
    rs = byh[h]
    g = statistics.median([x['gross'] for x in rs])
    n = statistics.median([x['net'] for x in rs])
    w = sum(1 for x in rs if x['net'] > 0) / len(rs) * 100
    print(f'  {h:<12}{len(rs):>5}{g:>+9.2f}{n:>+9.2f}{w:>6.1f}%')

print()
print('  ═══ ② R436 之后（真正的新数据）═══')
new = [r for r in recs if r['close'] >= R436_END]
if new:
    g = statistics.median([x['gross'] for x in new])
    n = statistics.median([x['net'] for x in new])
    w = sum(1 for x in new if x['net'] > 0) / len(new) * 100
    print(f'    {len(new)} 笔 · 毛中位 {g:+.2f} · 净中位 {n:+.2f} · 胜率 {w:.1f}%')
    print(f'    时间范围: '
          f'{dt.datetime.fromtimestamp(min(x["close"] for x in new) / 1000, dt.timezone.utc):%m-%d %H:%M} → '
          f'{dt.datetime.fromtimestamp(max(x["close"] for x in new) / 1000, dt.timezone.utc):%m-%d %H:%M}')
else:
    print('    （尚无新平仓 —— R436 之后无新数据）')

print()
print('  ═══ ③ 加载器修复（07:09Z）之后 ═══')
post = [r for r in recs if r['close'] >= FIX_MS]
if post:
    g = statistics.median([x['gross'] for x in post])
    n = statistics.median([x['net'] for x in post])
    w = sum(1 for x in post if x['net'] > 0) / len(post) * 100
    print(f'    {len(post)} 笔 · 毛中位 {g:+.2f} · 净中位 {n:+.2f} · 胜率 {w:.1f}%')
else:
    print('    （尚无）')

print()
print('  ═══ ④ 三段对比（R436 的三段结论是否延续）═══')
segs = [('早期 ≤10-13', None, int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp() * 1000)),
        ('中期 10-14~10-15',
         int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp() * 1000),
         int(dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc).timestamp() * 1000)),
        ('最新 10-16',
         int(dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc).timestamp() * 1000), None)]
for name, lo, hi in segs:
    sub = [r for r in recs if (lo is None or r['close'] >= lo) and (hi is None or r['close'] < hi)]
    if not sub:
        print(f'    {name}: 无样本')
        continue
    g = statistics.median([x['gross'] for x in sub])
    n = statistics.median([x['net'] for x in sub])
    w = sum(1 for x in sub if x['net'] > 0) / len(sub) * 100
    print(f'    {name:<18}{len(sub):>5} 笔 · 毛中位 {g:>+8.2f} · 净中位 {n:>+8.2f} · 胜率 {w:>5.1f}%')
