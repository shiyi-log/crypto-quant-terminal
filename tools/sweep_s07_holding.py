"""固定持有期 H 扫描（反事实重放）: 用 s07 的【入场信号】，但改为固定持有 H 根 15m。

⚠️ 关键: 只用【入场】信号（opened_candle_ms + entry_price + side），忽略原出场，
  然后按固定 H 在【下一根开盘】平仓 ⇒ 完全消除"按出场原因分组"的选择偏差。

成本情景:
  · 入场 maker 2 bp
  · 出场 taker 4 bp（基线）/ maker 2 bp（乐观）

⚠️ 探索性（同一数据扫描 H）⇒ 最优 H 需样本外确认。
"""
import argparse
import collections
import json
import os
import statistics

import psycopg

BAR = 900_000


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hs', default='1,2,3,4,6,8,12')
    ap.add_argument('--entry-fee-bp', type=float, default=2.0)
    args = ap.parse_args()
    hs = [int(x) for x in args.hs.split(',')]

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        rows = c.execute("""select strategy_id, payload from quant.strategy_sim_events
                            where event_type='position_opened' and strategy_id like 's07%'
                              and strategy_id like '%B15m%'""").fetchall()
        ents = []
        for sid, pl in rows:
            p = J(pl)
            o = p.get('opened_candle_ms')
            nn = float(p.get('notional') or 0)
            if not o or not nn:
                continue
            ents.append({'sid': sid, 'pair': p.get('pair'), 'side': p.get('side'),
                         'entry': float(p.get('entry_price') or 0), 'notional': nn,
                         'open_ms': int(o)})
        # 去重（同族多变体共享入场）
        seen = set()
        uniq = []
        for e in ents:
            k = (e['pair'], e['side'], e['open_ms'], e['entry'])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(e)
        print(f'  s07 B15m 入场: {len(ents)} 条 ⇒ 去重后 【{len(uniq)}】 条独立信号')

        pairs = collections.defaultdict(list)
        for e in uniq:
            pairs[e['pair']].append(e)

        maxh = max(hs)
        kmaps = {}
        for pair, items in pairs.items():
            lo = min(i['open_ms'] for i in items) - BAR
            hi = max(i['open_ms'] for i in items) + (maxh + 3) * BAR
            ks = c.execute("""select timestamp, open from quant.candles
                              where exchange='binance' and candle_type='futures' and timeframe='15m'
                                and pair=%s and timestamp >= %s and timestamp <= %s
                              order by timestamp""", (pair, lo, hi)).fetchall()
            kmaps[pair] = {int(a): float(b) for a, b in ks}

        print()
        print(f'  ═══ 固定持有期 H 扫描（去重信号 · 入场费 {args.entry_fee_bp} bp）═══')
        print(f'  {"H(根)":>6}{"H(分)":>7}{"样本":>6}{"毛中位":>9}{"毛均值":>9}'
              f'{"净@taker4":>11}{"净@maker2":>11}{"胜率":>7}')
        best = None
        for H in hs:
            out = []
            for pair, items in pairs.items():
                km = kmaps[pair]
                for e in items:
                    fill = km.get(e['open_ms'] + (H + 1) * BAR)
                    if fill is None:
                        continue
                    g = ((fill - e['entry']) if e['side'] == 'long'
                         else (e['entry'] - fill)) * (e['notional'] / e['entry'])
                    out.append(g / e['notional'] * 10000)
            if not out:
                continue
            med = statistics.median(out)
            mean = sum(out) / len(out)
            win = sum(1 for x in out if x > 0) / len(out) * 100
            n_taker = med - args.entry_fee_bp - 4.0
            n_maker = med - args.entry_fee_bp - 2.0
            print(f'  {H:>6}{H * 15:>7}{len(out):>6}{med:>+9.2f}{mean:>+9.2f}'
                  f'{n_taker:>+11.2f}{n_maker:>+11.2f}{win:>6.1f}%')
            if best is None or med > best[1]:
                best = (H, med, n_taker, n_maker)
        if best:
            print()
            print(f'  ⇒ 最优 H = 【{best[0]} 根（{best[0] * 15} 分）】· 毛中位 {best[1]:+.2f} bp'
                  f' · 净@taker {best[2]:+.2f} bp · 净@maker {best[3]:+.2f} bp')


if __name__ == '__main__':
    main()
