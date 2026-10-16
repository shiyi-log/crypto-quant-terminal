"""规则消融（探索性）: s07 若【不因 orderflow 反转离场】而【持有到期】，结果如何？

设计:
  · 取 s07 中 reason='orderflow_direction_reversed' 的平仓
  · 消融路径: 从 opened_candle_ms 起持有 H 根 15m（H 由"持有到期"组的中位持有期推出），
    在【该根收盘确认后的下一根开盘】成交（与引擎口径一致）
  · 用 15m K 线取价；若数据缺失则跳过并记录
  · 成本: 出场按 maker 2 bp 与 taker 4 bp 两种情景分别算

⚠️ 探索性: 同一数据上的规则消融 ⇒ 需样本外确认才能作为结论。
"""
import argparse
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

BAR = 900_000  # 15m


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--h-bars', type=int, default=4, help='持有期（15m 根数）')
    ap.add_argument('--entry-fee-bp', type=float, default=2.0)
    ap.add_argument('--close-fee-bp', type=float, default=4.0)
    ap.add_argument('--maker-close-fee-bp', type=float, default=2.0)
    args = ap.parse_args()

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        rows = c.execute("""select strategy_id, payload from quant.strategy_sim_events
                            where event_type='trade_closed' and strategy_id like 's07%'
                              and strategy_id like '%B15m%'""").fetchall()
        recs = []
        for sid, pl in rows:
            p = J(pl)
            r = str(p.get('exit_reason') or p.get('reason') or '')
            if r != 'orderflow_direction_reversed':
                continue
            o = p.get('opened_candle_ms')
            nn = float(p.get('notional') or 0)
            if not o or not nn:
                continue
            recs.append({'sid': sid, 'pair': p.get('pair'), 'side': p.get('side'),
                         'entry': float(p.get('entry_price') or 0), 'notional': nn,
                         'open_ms': int(o), 'orig_exit': float(p.get('exit_price') or 0),
                         'orig_gross': float(p.get('gross_pnl') or 0),
                         'orig_fee': float(p.get('fees') or 0)})
        print(f'  消融样本（orderflow 反转离场）: 【{len(recs)}】笔')

        # 目标出场时间 = 开仓根 + H 根 ⇒ 在该根收盘后下一根开盘成交
        target_close = {r['sid'] + str(r['open_ms']): r['open_ms'] + args.h_bars * BAR for r in recs}
        pairs = collections.defaultdict(list)
        for r in recs:
            pairs[r['pair']].append(r)

        ok, skipped = [], 0
        for pair, items in pairs.items():
            lo = min(i['open_ms'] for i in items)
            hi = max(i['open_ms'] for i in items) + (args.h_bars + 3) * BAR
            ks = c.execute("""select timestamp, open from quant.candles
                              where exchange='binance' and candle_type='futures' and timeframe='15m'
                                and pair=%s and timestamp >= %s and timestamp <= %s
                              order by timestamp""", (pair, lo - BAR, hi)).fetchall()
            kmap = {int(a): float(b) for a, b in ks}
            for r in items:
                tgt = r['open_ms'] + args.h_bars * BAR          # 该根的【开始】时刻
                fill = kmap.get(tgt + BAR)                       # 下一根的开盘价
                if fill is None:
                    skipped += 1
                    continue
                r2 = dict(r)
                r2['abl_exit'] = fill
                r2['abl_gross'] = ((fill - r['entry']) if r['side'] == 'long'
                                   else (r['entry'] - fill)) * (r['notional'] / r['entry'])
                ok.append(r2)

        print(f'  可重放: 【{len(ok)}】笔 · 数据缺失跳过: {skipped}')
        if not ok:
            return

        tot = sum(r['notional'] for r in ok)
        og = sum(r['orig_gross'] for r in ok)
        ag = sum(r['abl_gross'] for r in ok)
        of = sum(r['orig_fee'] for r in ok)
        # 消融: 入场费保持；出场费分两种情景
        abl_fee_taker = tot * (args.entry_fee_bp + args.close_fee_bp) / 10000
        abl_fee_maker = tot * (args.entry_fee_bp + args.maker_close_fee_bp) / 10000

        print()
        print(f'  ═══ 结果（总名义 {tot:,.0f}）═══')
        print(f'  原路径（反转离场）: 毛 {og / tot * 10000:+.2f} bp · 费 {of / tot * 10000:.2f} bp · '
              f'净 {(og - of) / tot * 10000:+.2f} bp')
        print(f'  消融（持有到期）  : 毛 {ag / tot * 10000:+.2f} bp')
        print(f'    ⇒ 出场 taker {args.close_fee_bp} bp ⇒ 净 {(ag - abl_fee_taker) / tot * 10000:+.2f} bp')
        print(f'    ⇒ 出场 maker {args.maker_close_fee_bp} bp ⇒ 净 {(ag - abl_fee_maker) / tot * 10000:+.2f} bp')
        print()
        print('  ═══ 中位数视角（100× 肥尾下更稳健）═══')
        ob = statistics.median([r['orig_gross'] / r['notional'] * 10000 for r in ok])
        ab = statistics.median([r['abl_gross'] / r['notional'] * 10000 for r in ok])
        print(f'    原路径毛中位 {ob:+.2f} bp · 消融毛中位 {ab:+.2f} bp')
        wins_o = sum(1 for r in ok if r['orig_gross'] > 0) / len(ok) * 100
        wins_a = sum(1 for r in ok if r['abl_gross'] > 0) / len(ok) * 100
        print(f'    原路径胜率 {wins_o:.1f}% · 消融胜率 {wins_a:.1f}%')


if __name__ == '__main__':
    main()
