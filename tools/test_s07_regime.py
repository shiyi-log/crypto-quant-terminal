"""假设检验: s07 的边缘是否【状态依赖】（按入场时波动率分层）。只读。

方法:
  · 对每笔 s07 交易，取其【入场时刻】的 15m 已收盘 K 线，计算:
      - 近 20 根真实波幅均值 / 收盘价（ATR%）
      - 近 20 根收益率标准差（realized vol）
  · 按 ATR% 分位分 3 层，看毛/净中位数与胜率
  · ⚠️ 同时做时间对半，检验分层结论是否【跨期一致】（防过拟合）
"""
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

BAR = 900_000


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, payload from quant.strategy_sim_events
                        where event_type='position_opened' and strategy_id like 's07%'
                          and strategy_id like '%B15m%'""").fetchall()
    uniq, seen = [], set()
    for sid, pl in rows:
        p = J(pl)
        o = p.get('opened_candle_ms')
        nn = float(p.get('notional') or 0)
        if not o or not nn:
            continue
        k = (p.get('pair'), p.get('side'), int(o), float(p.get('entry_price') or 0))
        if k in seen:
            continue
        seen.add(k)
        uniq.append({'pair': p.get('pair'), 'side': p.get('side'),
                     'entry': float(p.get('entry_price') or 0), 'notional': nn,
                     'open_ms': int(o)})

    # 该信号的毛边缘：用 s07 实际的 net_pnl（按 (pair,side,open_ms) 匹配 trade_closed）
    trades = c.execute("""select payload from quant.strategy_sim_events
                          where event_type='trade_closed' and strategy_id like 's07%'
                            and strategy_id like '%B15m%'""").fetchall()
    tmap = {}
    for (pl,) in trades:
        p = J(pl)
        nn = float(p.get('notional') or 0)
        if not nn:
            continue
        key = (p.get('pair'), p.get('side'), int(p.get('opened_candle_ms') or 0))
        tmap[key] = float(p.get('gross_pnl') or 0) / nn * 10000

    for e in uniq:
        e['gross'] = tmap.get((e['pair'], e['side'], e['open_ms']))

    pairs = collections.defaultdict(list)
    for e in uniq:
        pairs[e['pair']].append(e)
    kmaps = {}
    for pair, items in pairs.items():
        lo = min(i['open_ms'] for i in items) - 30 * BAR
        hi = max(i['open_ms'] for i in items) + 2 * BAR
        ks = c.execute("""select timestamp, high, low, close from quant.candles
                          where exchange='binance' and candle_type='futures' and timeframe='15m'
                            and pair=%s and timestamp >= %s and timestamp <= %s
                          order by timestamp""", (pair, lo, hi)).fetchall()
        kmaps[pair] = [(int(a), float(b), float(cc), float(d)) for a, b, cc, d in ks]

    out = []
    for e in uniq:
        if e['gross'] is None:
            continue
        ks = kmaps[e['pair']]
        idx = None
        for i, k in enumerate(ks):
            if k[0] == e['open_ms']:
                idx = i
                break
        if idx is None or idx < 21:
            continue
        win = ks[idx - 20:idx]
        trs = [k[1] - k[2] for k in win]
        close = ks[idx - 1][3]
        atr_pct = (sum(trs) / len(trs)) / close * 100 if close else 0
        rets = [(win[i][3] / win[i - 1][3] - 1) for i in range(1, len(win))]
        rv = statistics.pstdev(rets) * 100 if len(rets) > 1 else 0
        e2 = dict(e)
        e2['atr_pct'] = atr_pct
        e2['rv'] = rv
        out.append(e2)

    print(f'  样本（含波动率）: 【{len(out)}】笔')
    out.sort(key=lambda x: x['open_ms'])
    mid = len(out) // 2


    def layer(sub, label):
        if len(sub) < 6:
            print(f'    {label}: 样本不足（{len(sub)}）')
            return
        sub = sorted(sub, key=lambda x: x['atr_pct'])
        n = len(sub)
        for name, part in (('低波动', sub[:n // 3]), ('中波动', sub[n // 3:2 * n // 3]),
                           ('高波动', sub[2 * n // 3:])):
            if not part:
                continue
            g = [x['gross'] for x in part]
            med = statistics.median(g)
            win = sum(1 for x in g if x > 0) / len(g) * 100
            print(f'    {label} {name}: {len(part):>3} 笔 · 毛中位 {med:>+8.2f} · '
                  f'净@taker {med - 6:>+8.2f} · 胜率 {win:>5.1f}% · '
                  f'ATR% [{part[0]["atr_pct"]:.2f}, {part[-1]["atr_pct"]:.2f}]')

    print()
    print('  ═══ 全样本按入场 ATR% 分层 ═══')
    layer(out, '全部')
    print()
    print('  ═══ 时间对半: 前半 ═══')
    layer(out[:mid], '前半')
    print()
    print('  ═══ 时间对半: 后半 ═══')
    layer(out[mid:], '后半')

    print()
    print('  ═══ 相关性（入场 ATR% vs 毛 bp）═══')
    xs = [x['atr_pct'] for x in out]
    ys = [x['gross'] for x in out]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    print(f'    Pearson r = {num / den:+.3f}' if den else '    （无方差）')
