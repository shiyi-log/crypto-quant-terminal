"""细查候选族的边缘稳健性: s08 / s10 / s07 / s16 / s20 / s35。只读。

纪律（本会话已建立）:
  ① 聚类单位 = 平仓分钟（同族多变体共享同一市场事件）
  ② 中位数基准（100× 下分布极度肥尾）
  ③ 自助 CI（2000 次，聚类重采样）
  ④ 报告【效应量 ÷ 成本门槛】（成本 = 4 bp lab / 6 bp demo）
  ⑤ 样本外: 时间对半分（并要求后半独立显著）
"""
import collections
import datetime as dt
import json
import os
import random
import statistics

import psycopg

random.seed(20261016)
LEG = ('_100x', '_2x2_', '_taker')
TARGETS = ['s07', 's08', 's10', 's16', 's20', 's35', 's02', 's05']


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def cluster_ci(recs, iters=2000):
    cl = collections.defaultdict(list)
    for r in recs:
        cl[r['close'] // 60000].append(r['bp'])
    keys = list(cl)
    boot = []
    for _ in range(iters):
        s = []
        for _ in range(len(keys)):
            s.extend(cl[random.choice(keys)])
        boot.append(statistics.median(s))
    boot.sort()
    return boot[int(.025 * len(boot))], boot[int(.975 * len(boot))], len(keys)


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                        where event_type='trade_closed' order by observed_at_ms""").fetchall()

fam = collections.defaultdict(list)
for sid, pl, ob in rows:
    p = J(pl)
    n = float(p.get('notional') or 0)
    npn = float(p.get('net_pnl') or 0)
    if not n:
        continue
    if any(k in sid for k in LEG) and '_100x_maker' not in sid:
        continue
    fam[sid.split('_')[0]].append({
        'bp': npn / n * 10000, 'close': int(ob),
        'liq': 'liquidation' in str(p.get('exit_reason') or p.get('reason') or '').lower()})

COST = 4.0  # lab 4 bp
print(f'  ═══ 候选族边缘稳健性（成本门槛 {COST} bp）═══')
print(f'  {"族":<6}{"笔":>5}{"簇":>4}{"中位":>8}{"均值":>8}{"[95% CI]":>20}{"爆仓%":>7}{"效应/成本":>10}{"判定":>14}')
for f in TARGETS:
    recs = fam.get(f)
    if not recs:
        continue
    b = [r['bp'] for r in recs]
    lo, hi, nk = cluster_ci(recs)
    liq = sum(1 for r in recs if r['liq']) / len(recs) * 100
    med = statistics.median(b)
    ratio = med / COST
    verdict = '✅ 正' if lo > 0 else ('🔴 负' if hi < 0 else '— 含 0')
    print(f'  {f:<6}{len(recs):>5}{nk:>4}{med:>+8.1f}{sum(b) / len(b):>+8.1f}'
          f'   [{lo:>+7.1f},{hi:>+7.1f}]{liq:>6.0f}%{ratio:>+10.2f}{verdict:>14}')

print()
print('  ═══ 样本外检验（时间对半 · 要求后半独立显著）═══')
for f in TARGETS:
    recs = fam.get(f)
    if not recs or len(recs) < 20:
        continue
    recs = sorted(recs, key=lambda r: r['close'])
    mid = len(recs) // 2
    outs = []
    for name, sub in (('前半', recs[:mid]), ('后半', recs[mid:])):
        b = [r['bp'] for r in sub]
        lo, hi, nk = cluster_ci(sub, iters=1200)
        outs.append((name, len(sub), statistics.median(b), lo, hi))
    h1, h2 = outs
    ok = h2[3] > 0
    print(f'  {f:<6} 前半 {h1[1]:>4}笔 中位 {h1[2]:>+7.1f} CI[{h1[3]:>+7.1f},{h1[4]:>+7.1f}] · '
          f'后半 {h2[1]:>4}笔 中位 {h2[2]:>+7.1f} CI[{h2[3]:>+7.1f},{h2[4]:>+7.1f}] '
          f'⇒ {"✅ 后半独立为正" if ok else "❌ 后半不显著为正"}')
