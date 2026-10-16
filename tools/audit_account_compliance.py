"""全账户合规审计: 按执行模型 / 是否有止损 / 最大可能损失 分类。只读。

合规标准（用户明确约束）: 单笔最大损失 ≤ 50 USDT。
  · 有 ATR 止损 ⇒ 最大损失 ≈ 名义 × (stop_pct + 费) ⇒ 由 risk_sizing 保证 ≤ max_loss
  · 无止损 ⇒ 最大损失 = 全部保证金（爆仓）⇒ 保证金 > 50 即违规
"""
import collections
import json
import os
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    accts = c.execute('select strategy_id, definition, execution from quant.strategy_sim_accounts').fetchall()
    # 从事件里取每个账户的最近一次开仓，判断是否带止损 + 保证金
    opens = c.execute(
        """select strategy_id, payload from quant.strategy_sim_events
           where event_type='position_opened' and observed_at_ms > 1791916800000"""
    ).fetchall()

by_acct = {}
for sid, pl in opens:
    p = J(pl)
    by_acct[sid] = p

rows = []
for sid, dfn, exe in accts:
    e = J(exe)
    ver = str(e.get('version') or '')
    en = e.get('execution_enabled', J(dfn).get('execution_enabled'))
    p = by_acct.get(sid)
    has_stop = None
    margin = None
    if p:
        has_stop = p.get('stop_level') is not None
        margin = float(p.get('margin') or 0) or None
    model = ('v2-no-atr' if 'liquidation-v2' in ver else
             'v3-entry-atr' if 'entry-atr' in ver else
             'v1-base' if 'next-close-open-v1' in ver else 'other')
    rows.append({'sid': sid, 'ver': ver[:52], 'model': model, 'enabled': en,
                 'has_stop': has_stop, 'margin': margin})

print('  ═══ 按【执行模型 × 是否启用】分类 ═══')
g = collections.Counter((r['model'], str(r['enabled'])) for r in rows)
for (m, en), n in sorted(g.items()):
    print(f'    {m:<14} enabled={en:<6} ⇒ {n:>4}')

print()
print('  ═══ 按【是否有止损】分类（仅有开仓记录的账户）══')
g2 = collections.Counter((r['model'], str(r['has_stop'])) for r in rows if r['has_stop'] is not None)
for (m, hs), n in sorted(g2.items()):
    print(f'    {m:<14} 有止损={hs:<6} ⇒ {n:>4}')

print()
print('  ═══ ⚠️ 合规风险: 【无止损且启用】的账户 ═══')
risky = [r for r in rows if r['has_stop'] is False and r['enabled'] is not False]
print(f'    数量: 【{len(risky)}】')
mgs = sorted(r['margin'] for r in risky if r['margin'])
if mgs:
    print(f'    保证金: min {mgs[0]:.2f} · 中位 {mgs[len(mgs)//2]:.2f} · max {mgs[-1]:.2f}')
    over = sum(1 for m in mgs if m > 50)
    print(f'    ⇒ 保证金 > 50 USDT 的: {over}/{len(mgs)}')
if risky:
    print('    明细（前 20）:')
    for r in risky[:20]:
        print(f'      {r["sid"][:56]:<58} model={r["model"]:<13} margin={r["margin"]}')

print()
print('  ═══ 按模型汇总（全部 382）══')
g3 = collections.Counter(r['model'] for r in rows)
for m, n in sorted(g3.items()):
    print(f'    {m:<14} {n:>4}')
