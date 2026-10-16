"""s07 毛边缘的【定论性】稳定性分析: 合并 CI + 分段 CI + 趋势检验 + 自助。只读。

目的: 在 R435（毛显著为正）与 R436（边缘衰减）之间给出【一次定论】，
      避免依据"逐日中位数"（受离群值影响）来回摇摆。
"""
import collections
import datetime as dt
import json
import os
import random
import statistics

import psycopg

random.seed(20261016)
DAY = 86_400_000


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def cluster_median_ci(recs, iters=3000):
    cl = collections.defaultdict(list)
    for r in recs:
        cl[r['close'] // 60000].append(r['gross'])
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
                        where event_type='trade_closed' and strategy_id like 's07%'
                        order by observed_at_ms""").fetchall()
recs = []
for sid, pl, ob in rows:
    p = J(pl)
    nn = float(p.get('notional') or 0)
    if not nn:
        continue
    # 只取 B15m 系列（当前设计），排除旧 100x 变体以保持口径一致
    if 'B15m' not in sid:
        continue
    recs.append({'close': int(ob),
                 'gross': float(p.get('gross_pnl') or 0) / nn * 10000,
                 'net': float(p.get('net_pnl') or 0) / nn * 10000})

print(f'  s07（B15m 系列）平仓 {len(recs)} 笔')
print()
lo, hi, nk = cluster_median_ci(recs)
g = [r['gross'] for r in recs]
n = [r['net'] for r in recs]
print('  ═══ ① 合并（全样本）═══')
print(f'    毛: 中位 {statistics.median(g):+.2f} · 均值 {sum(g) / len(g):+.2f} · '
      f'聚类自助 95% CI 【[{lo:+.2f}, {hi:+.2f}]】· 聚类数 {nk}')
print(f'    净: 中位 {statistics.median(n):+.2f} · 均值 {sum(n) / len(n):+.2f}')
print(f'    ⇒ {"✅ 毛显著为正" if lo > 0 else "— 毛 CI 含 0"}')

print()
print('  ═══ ② 分段（按自然日 · 每段给 CI）═══')
byday = collections.defaultdict(list)
for r in recs:
    byday[dt.datetime.fromtimestamp(r['close'] / 1000, dt.timezone.utc).strftime('%m-%d')].append(r)
print(f'  {"日期":<8}{"笔":>5}{"毛中位":>9}{"CI":>20}{"净中位":>9}{"判定":>16}')
for d in sorted(byday):
    sub = byday[d]
    if len(sub) < 5:
        continue
    a, b, k = cluster_median_ci(sub, iters=1200)
    mg = statistics.median([x['gross'] for x in sub])
    mn = statistics.median([x['net'] for x in sub])
    verdict = '✅ 毛正显著' if a > 0 else ('🔴 毛负显著' if b < 0 else '— 含 0')
    print(f'  {d:<8}{len(sub):>5}{mg:>+9.2f}   [{a:>+7.2f},{b:>+7.2f}]{mn:>+9.2f}{verdict:>18}')

print()
print('  ═══ ③ 趋势检验（时间 vs 毛 bp · Spearman 秩相关）═══')
xs = [r['close'] for r in recs]
ys = [r['gross'] for r in recs]
order = sorted(range(len(xs)), key=lambda i: xs[i])
rx = [0] * len(xs)
for rank, i in enumerate(order):
    rx[i] = rank
# y 的秩（处理并列）
yorder = sorted(range(len(ys)), key=lambda i: ys[i])
ry = [0] * len(ys)
for rank, i in enumerate(yorder):
    ry[i] = rank
mx = sum(rx) / len(rx)
my = sum(ry) / len(ry)
num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
rho = num / den if den else 0
# 置换检验
obs = abs(rho)
cnt = 0
for _ in range(2000):
    perm = ry[:]
    random.shuffle(perm)
    mx2, my2 = mx, sum(perm) / len(perm)
    nu = sum((a - mx2) * (b - my2) for a, b in zip(rx, perm))
    de = (sum((a - mx2) ** 2 for a in rx) * sum((b - my2) ** 2 for b in perm)) ** 0.5
    if de and abs(nu / de) >= obs:
        cnt += 1
print(f'    Spearman ρ = {rho:+.4f} · 置换检验 p ≈ {cnt / 2000:.4f}'
      f' ⇒ {"✅ 无显著时间趋势（边缘稳定）" if cnt / 2000 > 0.05 else "⚠️ 存在显著时间趋势（衰减/改善）"}')

print()
print('  ═══ ④ 首末 1/3 对比（非参数）═══')
s = sorted(recs, key=lambda r: r['close'])
k = len(s) // 3
for name, sub in (('首 1/3', s[:k]), ('中 1/3', s[k:2 * k]), ('末 1/3', s[2 * k:])):
    a, b, kk = cluster_median_ci(sub, iters=1200)
    print(f'    {name}: {len(sub):>4} 笔 · 毛中位 {statistics.median([x["gross"] for x in sub]):>+8.2f}'
          f' · CI [{a:>+7.2f},{b:>+7.2f}] ⇒ {"✅ 正" if a > 0 else ("🔴 负" if b < 0 else "— 含 0")}')
