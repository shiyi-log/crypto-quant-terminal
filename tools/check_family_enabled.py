"""按族检查 execution_enabled 状态（决定谁是真正的未退役候选）。只读。"""
import collections
import json
import os
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


CANDS = ['s21', 's18', 's14', 's12', 's17', 's34', 's03', 's09']

with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
    by = collections.defaultdict(lambda: collections.Counter())
    for sid, exe in rows:
        fam = sid.split('_')[0]
        if fam not in CANDS:
            continue
        e = J(exe)
        en = e.get('execution_enabled')
        ver = 'v2' if 'liquidation-v2' in str(e.get('version') or '') else 'v3'
        by[fam][(str(en), ver)] += 1

print('  ═══ 显著为负的 8 个族的账户状态 ═══')
print(f'  {"族":<6}{"账户":>5}   分布 (execution_enabled, 模型)')
for fam in CANDS:
    d = by.get(fam)
    if not d:
        print(f'  {fam:<6}{0:>5}   （无账户）')
        continue
    total = sum(d.values())
    active = sum(n for (en, v), n in d.items() if en != 'False')
    print(f'  {fam:<6}{total:>5}   {dict(d)}  ⇒ 【启用中 {active}】')

print()
print('  ═══ 真正【未退役】（execution_enabled != False）的族 ═══')
for fam in CANDS:
    d = by.get(fam) or {}
    active = sum(n for (en, v), n in d.items() if en != 'False')
    if active:
        print(f'    🔴 {fam}: 启用 {active} 个账户')
    else:
        print(f'    ✅ {fam}: 全部已退役')
