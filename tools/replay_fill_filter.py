"""成交过滤重放（R442 的检验）: 对 s07 的 lab 信号施加 Demo 的 maker 限价 + 短 TTL 成交条件。

⚠️ 口径标注:
  · 用 1m K 线判断"是否触及限价"（覆盖 60 秒），而 Demo 的 TTL 是 30 秒
    ⇒ 本重放的成交率会【偏高】（更宽松），结论需按此打折
  · 限价偏差用【实测值】2.490202 bps（来自 Demo 订单 32）
  · 出场沿用 lab 的原出场（时间与原因），出场费按 lab 的费率

输出: 全信号 vs 成交子集 的毛/费/净 bp
"""
import argparse
import collections
import datetime as dt
import json
import os
import statistics

import psycopg

BAR = 60_000


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dev-bps', type=float, default=2.490202)
    ap.add_argument('--maker-fee-bp', type=float, default=2.0)
    ap.add_argument('--taker-fee-bp', type=float, default=4.0)
    args = ap.parse_args()
    dev = args.dev_bps / 10000.0

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        rows = c.execute("""select strategy_id, payload, observed_at_ms from quant.strategy_sim_events
                            where event_type='trade_closed' and strategy_id like %s""",
                         ('s07%B15m%',)).fetchall()
        recs = []
        for sid, pl, ob in rows:
            p = J(pl)
            nn = float(p.get('notional') or 0)
            e = p.get('entry_price')
            x = p.get('exit_price')
            if not nn or not e or not x:
                continue
            recs.append({'pair': p.get('pair'), 'side': p.get('side'),
                         'entry': float(e), 'exit': float(x), 'notional': nn,
                         'open_ms': int(p.get('opened_candle_ms') or 0),
                         'orig_gross': float(p.get('gross_pnl') or 0),
                         'orig_fee': float(p.get('fees') or 0)})
        print(f'  s07 B15m 信号: {len(recs)} 条')

        pairs = collections.defaultdict(list)
        for r in recs:
            if r['open_ms']:
                pairs[r['pair']].append(r)
        kmaps = {}
        for pair, items in pairs.items():
            lo = min(i['open_ms'] for i in items) - BAR
            hi = max(i['open_ms'] for i in items) + 2 * BAR
            ks = c.execute("""select timestamp, open, high, low, close from quant.candles
                              where exchange='binance' and candle_type='futures' and timeframe='1m'
                                and pair=%s and timestamp >= %s and timestamp <= %s
                              order by timestamp""", (pair, lo, hi)).fetchall()
            kmaps[pair] = {int(a): (float(cc), float(d)) for a, b, cc, d, e in ks}

        filled, unfilled, unknown = [], [], 0
        for r in recs:
            if not r['open_ms']:
                unknown += 1
                continue
            km = kmaps.get(r['pair'], {})
            bar = km.get(r['open_ms'])
            if bar is None:
                # 试前后一根
                bar = km.get(r['open_ms'] - BAR) or km.get(r['open_ms'] + BAR)
            if bar is None:
                unknown += 1
                continue
            hi, lo = bar
            if r['side'] == 'long':
                limit = r['entry'] * (1 - dev)
                touched = lo <= limit
            else:
                limit = r['entry'] * (1 + dev)
                touched = hi >= limit
            if not touched:
                unfilled.append(r)
                continue
            # 成交: 入场价为限价（更优）；出场沿用 lab 的原出场
            direction = 1 if r['side'] == 'long' else -1
            qty = r['notional'] / r['entry']
            new_gross = (r['exit'] - limit) * direction * qty
            # 入场费: maker 2 bp；出场费: 沿用原费用比例（taker 4 bp）
            new_entry_fee = limit * qty * args.maker_fee_bp / 10000
            new_exit_fee = r['exit'] * qty * args.taker_fee_bp / 10000
            r2 = dict(r)
            r2['limit'] = limit
            r2['new_gross_bp'] = new_gross / r['notional'] * 10000
            r2['new_fee_bp'] = (new_entry_fee + new_exit_fee) / r['notional'] * 10000
            r2['new_net_bp'] = r2['new_gross_bp'] - r2['new_fee_bp']
            r2['orig_gross_bp'] = r['orig_gross'] / r['notional'] * 10000
            r2['orig_net_bp'] = (r['orig_gross'] - r['orig_fee']) / r['notional'] * 10000
            filled.append(r2)

        n = len(filled) + len(unfilled)
        print(f'  可判定: {n} 条（数据缺失 {unknown}）')
        print(f'  【成交率 = {len(filled)}/{n} = {len(filled) / n * 100:.1f}%】'
              f'（Demo 实测 38.9% ⇒ 【本重放偏宽松】）')
        print()
        if not filled:
            print('  无成交样本')
            return

        tot = sum(r['notional'] for r in filled)
        ng = sum(r['new_gross_bp'] * r['notional'] / 10000 for r in filled)
        nf = sum(r['new_fee_bp'] * r['notional'] / 10000 for r in filled)
        # 对照: 同样本的原路径
        og = sum(r['orig_gross'] for r in filled)
        of = sum(r['orig_fee'] for r in filled)

        print('  ═══ 成交子集 vs 该子集的原路径（同信号同出场）═══')
        print(f'    名义合计 {tot:,.0f}')
        print(f'    原路径:      毛 {og / tot * 10000:+.2f} bp · 费 {of / tot * 10000:.2f} bp · '
              f'净 {(og - of) / tot * 10000:+.2f} bp')
        print(f'    限价成交后:  毛 {ng / tot * 10000:+.2f} bp · 费 {nf / tot * 10000:.2f} bp · '
              f'【净 {(ng - nf) / tot * 10000:+.2f} bp】')
        print()
        print('  ═══ 中位数视角 ═══')
        print(f'    原路径净中位 {statistics.median([r["orig_net_bp"] for r in filled]):+.2f} bp')
        print(f'    成交后净中位 {statistics.median([r["new_net_bp"] for r in filled]):+.2f} bp')
        w = sum(1 for r in filled if r['new_net_bp'] > 0) / len(filled) * 100
        print(f'    成交后胜率 {w:.1f}%')
        print()
        print('  ═══ 未成交子集的表现（若它们被"假设成交"会怎样）═══')
        if unfilled:
            t2 = sum(r['notional'] for r in unfilled)
            ug = sum(r['orig_gross'] for r in unfilled)
            ufe = sum(r['orig_fee'] for r in unfilled)
            print(f'    未成交 {len(unfilled)} 条 · 毛 {ug / t2 * 10000:+.2f} bp · '
                  f'净 {(ug - ufe) / t2 * 10000:+.2f} bp')
        print()
        print('  ═══ 中位数口径（防肥尾误导，与 R435/R440 一致）═══')
        print(f'    成交子集:  原路径毛中位 {statistics.median([r["orig_gross_bp"] for r in filled]):+.2f}'
              f' · 成交后毛中位 {statistics.median([r["new_gross_bp"] for r in filled]):+.2f}'
              f' · 成交后净中位 {statistics.median([r["new_net_bp"] for r in filled]):+.2f}')
        if unfilled:
            print(f'    未成交子集: 毛中位 {statistics.median([r["orig_gross"] / r["notional"] * 10000 for r in unfilled]):+.2f}'
                  f' · 净中位 {statistics.median([(r["orig_gross"] - r["orig_fee"]) / r["notional"] * 10000 for r in unfilled]):+.2f}')
        print(f'    全信号:    毛中位 {statistics.median([r["orig_gross"] / r["notional"] * 10000 for r in recs if r["notional"]]):+.2f}'
              f' · 净中位 {statistics.median([(r["orig_gross"] - r["orig_fee"]) / r["notional"] * 10000 for r in recs if r["notional"]]):+.2f}')
        print()
        print('  ═══ 全信号（对照，= lab 口径 · 求和）═══')
        ta = sum(r['notional'] for r in recs if r['notional'])
        ag = sum(r['orig_gross'] for r in recs)
        af = sum(r['orig_fee'] for r in recs)
        print(f'    {len(recs)} 条 · 毛 {ag / ta * 10000:+.2f} bp · 净 {(ag - af) / ta * 10000:+.2f} bp')


if __name__ == '__main__':
    main()
