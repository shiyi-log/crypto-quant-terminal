"""全族退役候选评估: 聚类校正中位数 + 自助 CI + 加载/启用状态。只读。

判据（R380 纪律）: 聚类校正后【显著为负】（CI 上界 < 0）⇒ 退役候选。
聚类单位 = 【平仓分钟】（同分钟内的多账户/多变体视为一个事件簇），
以避免"同族多变体共享同一市场事件"造成的伪独立。
"""
import collections
import datetime as dt
import json
import os
import random
import statistics
import sys

import psycopg

sys.path.insert(0, '.')
from strategy_lab_service import (load_specs, load_cost_adjusted_specs, load_2x2_specs,
                                  normalize_account_spec)
from strategy_factorial import load_factorial_specs

random.seed(20261016)
LEG = ('_100x', '_2x2_', '_taker')


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


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
        continue  # 排除 legacy
    f = sid.split('_')[0]
    fam[f].append({'bp': npn / n * 10000, 'close': int(ob),
                   'liq': 'liquidation' in str(p.get('exit_reason') or p.get('reason') or '').lower()})

req = (load_specs() + load_cost_adjusted_specs() + load_2x2_specs(activate=True)
       + load_factorial_specs(normalizer=normalize_account_spec))
loaded = {s['strategy_id'] for s in req}

print(f'  ═══ 全族退役候选评估（聚类单位 = 平仓分钟）═══')
print(f'  {"族":<7}{"笔":>5}{"簇":>5}{"中位bp":>9}{"均值bp":>9}{"爆仓%":>7}{"[95% CI]":>20}{"被加载":>8}{"判定":>14}')
cands = []
for f in sorted(fam):
    recs = fam[f]
    if len(recs) < 10:
        continue
    b = [r['bp'] for r in recs]
    cl = collections.defaultdict(list)
    for r in recs:
        cl[r['close'] // 60000].append(r['bp'])
    keys = list(cl)
    boot = []
    for _ in range(2000):
        s = []
        for _ in range(len(keys)):
            s.extend(cl[random.choice(keys)])
        boot.append(statistics.median(s))
    boot.sort()
    lo, hi = boot[int(.025 * len(boot))], boot[int(.975 * len(boot))]
    liq = sum(1 for r in recs if r['liq']) / len(recs) * 100
    is_loaded = any(sid.startswith(f + '_') for sid in loaded)
    verdict = '🔴 显著为负' if hi < 0 else ('✅ 显著为正' if lo > 0 else '— 含 0')
    if hi < 0:
        cands.append((f, len(recs), len(keys), statistics.median(b), lo, hi, liq, is_loaded))
    print(f'  {f:<7}{len(recs):>5}{len(keys):>5}{statistics.median(b):>+9.1f}'
          f'{sum(b) / len(b):>+9.1f}{liq:>6.0f}%   [{lo:>+7.1f},{hi:>+7.1f}]{("✅" if is_loaded else "❌"):>8}{verdict:>14}')

print()
print(f'  ═══ 退役候选汇总（显著为负）: {len(cands)} 个 ═══')
print(f'  {"族":<7}{"笔":>5}{"簇":>5}{"中位bp":>9}{"CI 上界":>9}{"爆仓%":>7}{"被加载":>8}')
for f, n, k, med, lo, hi, liq, ld in sorted(cands, key=lambda x: x[3]):
    print(f'  {f:<7}{n:>5}{k:>5}{med:>+9.1f}{hi:>+9.1f}{liq:>6.0f}%{("✅" if ld else "❌"):>8}')
