"""把【毛边缘】与【成本】分开，判断"成本是否吃掉了小正边缘"。只读。

口径:
  · lab 的 payload: net_pnl = gross_pnl − fees（本会话已验证到 0.000）
  · 毛 bp = gross_pnl / notional · 费 bp = fees / notional · 净 bp = net / notional
  · 对比 Demo 的实际成本（6 bp/往返: 入场 maker 2 + 出场 taker 4）
"""
import collections
import json
import os
import random
import statistics

import psycopg

random.seed(20261016)
LEG = ('_100x', '_2x2_', '_taker')
TARGETS = ['s07', 's08', 's16', 's20', 's35', 's02', 's05', 's10']


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def ci(recs, key, iters=2000):
    cl = collections.defaultdict(list)
    for r in recs:
        cl[r['close'] // 60000].append(r[key])
    keys = list(cl)
    boot = []
    for _ in range(iters):
        s = []
        for _ in range(len(keys)):
            s.extend(cl[random.choice(keys)])
        boot.append(statistics.median(s))
    boot.sort()
    return boot[int(.025 * len(boot))], boot[int(.975 * len(boot))]


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                        where event_type='trade_closed' order by observed_at_ms""").fetchall()

fam = collections.defaultdict(list)
for sid, pl, ob in rows:
    p = J(pl)
    n = float(p.get('notional') or 0)
    if not n:
        continue
    if any(k in sid for k in LEG) and '_100x_maker' not in sid:
        continue
    gross = p.get('gross_pnl')
    fees = p.get('fees')
    net = p.get('net_pnl')
    if gross is None or fees is None or net is None:
        continue
    fam[sid.split('_')[0]].append({
        'close': int(ob),
        'gross': float(gross) / n * 10000,
        'fee': float(fees) / n * 10000,
        'net': float(net) / n * 10000})

print('  ═══ 毛 vs 费 vs 净（中位数 · 聚类自助 CI）═══')
print(f'  {"族":<6}{"笔":>5}{"毛中位":>9}{"费中位":>8}{"净中位":>9}   {"毛 CI":>18}   {"判定":>18}')
for f in TARGETS:
    recs = fam.get(f)
    if not recs or len(recs) < 10:
        continue
    g = statistics.median([r['gross'] for r in recs])
    fe = statistics.median([r['fee'] for r in recs])
    ne = statistics.median([r['net'] for r in recs])
    lo, hi = ci(recs, 'gross')
    verdict = ('✅ 毛显著为正' if lo > 0 else '— 毛含 0')
    print(f'  {f:<6}{len(recs):>5}{g:>+9.2f}{fe:>8.2f}{ne:>+9.2f}   [{lo:>+7.2f},{hi:>+7.2f}]{verdict:>20}')

print()
print('  ═══ 关键问题: 若【成本降到 Demo 可实现的最低】，谁能转正？═══')
print('  （Demo 实测: 入场 maker 2 bp + 出场 taker 4 bp = 6 bp；若出场也 maker ⇒ 4 bp）')
print(f'  {"族":<6}{"毛中位":>9}{"净@6bp":>10}{"净@4bp":>10}{"净@2bp":>10}{"结论":>22}')
for f in TARGETS:
    recs = fam.get(f)
    if not recs or len(recs) < 10:
        continue
    g = statistics.median([r['gross'] for r in recs])
    n6, n4, n2 = g - 6, g - 4, g - 2
    best = []
    if n6 > 0:
        best.append('@6bp 已正')
    elif n4 > 0:
        best.append('需出场改 maker')
    elif n2 > 0:
        best.append('需双边 maker')
    else:
        best.append('❌ 2bp 成本下仍负')
    print(f'  {f:<6}{g:>+9.2f}{n6:>+10.2f}{n4:>+10.2f}{n2:>+10.2f}{best[0]:>24}')
