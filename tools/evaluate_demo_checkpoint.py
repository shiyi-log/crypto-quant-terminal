"""Demo 100 往返检验点的【机械判读器】—— 严格实现 PREREG_demo_100_roundtrips.md。

只读。输出 PREREG §4 的决策 + §7 的否决项 + §3 的主要终点。

用法:
  QUANT_DATABASE_URL=... python3 tools/evaluate_demo_checkpoint.py
  ... --since 2026-10-15T16:00:00Z --account demo_strategy_f6cb33743f6146c385dddf3bd061892c
"""
import argparse
import collections
import datetime as dt
import json
import os
import random
import statistics

import psycopg

random.seed(20261016)
BAR_MIN_MS = 60_000


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def cluster_median_ci(values_by_minute, iters=2000):
    keys = list(values_by_minute)
    boot = []
    for _ in range(iters):
        s = []
        for _ in range(len(keys)):
            s.extend(values_by_minute[random.choice(keys)])
        boot.append(statistics.median(s))
    boot.sort()
    return boot[int(.025 * len(boot))], boot[int(.975 * len(boot))]


def build_trips(c, account, since_ms):
    cols = [d[0] for d in c.execute('select * from quant.demo_fills limit 0').description]
    q = 'select * from quant.demo_fills order by timestamp_ms'
    fills = [dict(zip(cols, r)) for r in c.execute(q).fetchall()]
    ro = {}
    for oid, r in c.execute("""select exchange_order_id::text, reduce_only
                                 from quant.demo_orders where exchange_order_id is not null"""):
        ro[oid] = bool(r)

    open_legs = collections.defaultdict(list)
    trips = collections.OrderedDict()
    for f in fills:
        vk = f['virtual_account_key']
        if vk != account:
            # 仍需推进其它账户的配对状态？不需要（不同账户互不影响）
            continue
        oid = str(f.get('exchange_order_id') or '')
        is_exit = ro.get(oid, False)
        px, qty = float(f['price']), float(f['quantity'])
        notional, fee = px * qty, float(f['commission'] or 0)
        k = (vk, f['pair'])
        if not is_exit:
            open_legs[k].append([int(f['timestamp_ms']), qty, notional, fee])
            continue
        close_ms = int(f['timestamp_ms'])
        if close_ms < since_ms:
            continue
        t = trips.setdefault((vk, f['pair'], close_ms),
                             {'pair': f['pair'], 'close_ms': close_ms, 'open_ms': None,
                              'qty': 0.0, 'gross': 0.0, 'fee': 0.0, 'notional': 0.0})
        remaining = qty
        while remaining > 1e-12 and open_legs[k]:
            leg = open_legs[k][0]
            take = min(remaining, leg[1])
            frac = take / leg[1]
            ec, ef = leg[2] * frac, leg[3] * frac
            t['qty'] += take
            t['gross'] += px * take - ec
            t['fee'] += ef + fee * (take / qty)
            t['notional'] += ec
            if t['open_ms'] is None or leg[0] < t['open_ms']:
                t['open_ms'] = leg[0]
            leg[1] -= take
            leg[2] -= ec
            leg[3] -= ef
            remaining -= take
            if leg[1] <= 1e-12:
                open_legs[k].pop(0)
    return [t for t in trips.values() if t['notional'] > 0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-10-15T16:00:00Z')
    ap.add_argument('--account', default='demo_strategy_f6cb33743f6146c385dddf3bd061892c')
    args = ap.parse_args()
    since_ms = int(dt.datetime.fromisoformat(args.since.replace('Z', '+00:00')).timestamp() * 1000)

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        trips = build_trips(c, args.account, since_ms)

    print('  ══════ PREREG_demo_100_roundtrips.md · 机械判读 ══════')
    print(f'  账户: {args.account[:46]}')
    print(f'  窗口: since {args.since}（修复后窗口）')
    print(f'  ★ 往返数 n = 【{len(trips)}】（目标 ≥100 · 按唯一出场计数）')
    if not trips:
        print('  ⇒ 无样本')
        return

    for t in trips:
        t['net'] = t['gross'] - t['fee']
        t['gross_bp'] = t['gross'] / t['notional'] * 10000
        t['fee_bp'] = t['fee'] / t['notional'] * 10000
        t['net_bp'] = t['net'] / t['notional'] * 10000

    nets = [t['net_bp'] for t in trips]
    gs = [t['gross_bp'] for t in trips]
    fs = [t['fee_bp'] for t in trips]

    print()
    print('  §3 主要终点（净 bp 中位数 · 聚类自助 95% CI）')
    bymin = collections.defaultdict(list)
    for t in trips:
        bymin[t['close_ms'] // BAR_MIN_MS].append(t['net_bp'])
    lo, hi = cluster_median_ci(bymin)
    med = statistics.median(nets)
    print(f'    净 bp: 中位 {med:+.2f} · 均值 {sum(nets) / len(nets):+.2f} · CI [{lo:+.2f}, {hi:+.2f}]'
          f' · 聚类数 {len(bymin)}')
    print(f'    毛 bp 中位 {statistics.median(gs):+.2f} · 费 bp 中位 {statistics.median(fs):.2f}')
    win = sum(1 for x in nets if x > 0) / len(nets) * 100
    print(f'    胜率 {win:.1f}%')

    print()
    print('  §7 否决项检查')
    v1 = med <= 0
    print(f'    [1] 净 bp 中位数 ≤ 0 ? {"❌ 触发" if v1 else "✅ 未触发"}（中位 {med:+.2f}）')
    s = sorted(trips, key=lambda t: t['close_ms'])
    mid = len(s) // 2
    halves = []
    for name, sub in (('前半', s[:mid]), ('后半', s[mid:])):
        if not sub:
            continue
        m = statistics.median([t['net_bp'] for t in sub])
        halves.append((name, len(sub), m))
        print(f'    [2] {name}净中位 {m:+.2f}（n={len(sub)}）')
    v2 = bool(halves and halves[-1][2] <= 0)
    print(f'        ⇒ 后半 ≤ 0 ? {"❌ 触发（边缘可能已衰减）" if v2 else "✅ 未触发"}')
    top = sorted(trips, key=lambda t: -t['net_bp'])[10:]
    m_wo = statistics.median([t['net_bp'] for t in top]) if top else float('nan')
    v3 = m_wo <= 0
    print(f'    [3] 删去最大 10 笔盈利后净中位 {m_wo:+.2f} ⇒ {"❌ 触发（尾部依赖）" if v3 else "✅ 未触发"}')
    fee_med = statistics.median(fs)
    v4 = fee_med > 6.5
    print(f'    [4] 费/往返中位 {fee_med:.3f} bp ⇒ {"❌ 触发（成本失控）" if v4 else "✅ 未触发"}')
    v5 = len(trips) < 100
    print(f'    [5] n < 100 ? {"⚠️ 触发（功效不足）" if v5 else "✅ 未触发"}')

    print()
    print('  §4 决策')
    if v5:
        print(f'    🟡 【功效不足】—— n={len(trips)} < 100，按 PREREG 不下结论')
    elif lo > 0:
        print(f'    🟢 【支持 H1】—— 中位净 bp {med:+.2f}，CI [{lo:+.2f}, {hi:+.2f}] 下界 > 0（n={len(trips)}）')
    elif med > 0:
        print(f'    🟡 【方向为正但未达显著】—— 中位 {med:+.2f}，CI [{lo:+.2f}, {hi:+.2f}] 含 0'
              f' ⇒ 按 PREREG【禁止】表述为"已实现盈利"')
    else:
        print(f'    🔴 【不支持 H1】—— 中位净 bp {med:+.2f}（n={len(trips)}）')
    print()
    print(f'  触发的否决项: {[i for i, v in enumerate([v1, v2, v3, v4], 1) if v]}'
          + (' + [5 功效不足]' if v5 else ''))


if __name__ == '__main__':
    main()
