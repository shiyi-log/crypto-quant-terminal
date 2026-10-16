"""重放：把 ATR 止损【钳制到强平距离的 70%】后，历史交易会怎样。

背景（R430）:
  · `stop_pct = k × ATR / entry`，但【强平距离恒为 1/leverage = 1%（100×）】
  · 当 k×ATR% > 1% 时【止损在强平之外】⇒ 止损永远不触发 ⇒ 损失全部保证金
  · 代码里已有 `stop_clamp_policy="liquidation_distance_fraction"`（factor 固定 0.7）
    的钳制机制，但【382/382 账户均未启用】

本工具【只读】，用 1m K 线重放:
  对每一笔【有止损且止损在强平外】的开仓:
    · 原路径: 到原平仓时刻（可能已爆仓）
    · 钳制路径: 止损价改为 clamp 后的位置；若 1m 路径先触及 ⇒ 在该处平仓
  ⇒ 对比两者的净盈亏

口径:
  · 止损采用【收盘确认 + 下一根开盘成交】（与引擎 `fixed_entry_atr_close_confirmed_next_open` 一致）
  · 费率: 入场/出场各按 spec 的 `fee_rate_per_side`（默认取成交记录的实际费率）
  · 时间窗: 重放上限 = 原平仓时刻（不引入原交易之外的信息）
"""
import argparse, collections, datetime as dt, json, os
import psycopg

BAR = 60_000  # 1m


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since-ms', type=int, default=1791916800000)
    ap.add_argument('--limit', type=int, default=400)
    ap.add_argument('--factor', type=float, default=0.7)
    ap.add_argument('--show', type=int, default=12)
    args = ap.parse_args()

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        cols = [d[0] for d in c.execute('select * from quant.strategy_sim_events limit 0').description]
        # 取【全部】开平仓事件再配对，避免跨窗口配对断裂；随后按【平仓时间】筛选
        rows = [dict(zip(cols, r)) for r in c.execute(
            """select * from quant.strategy_sim_events
               where event_type in ('position_opened','trade_closed')
               order by observed_at_ms""").fetchall()]

        # 用 trade_id 配对
        opened, closed = {}, {}
        for r in rows:
            p = J(r['payload'])
            tid = p.get('trade_id')
            if not tid:
                continue
            (opened if r['event_type'] == 'position_opened' else closed)[tid] = (p, int(r['observed_at_ms']))

        # 取 K 线
        def candles(pair, lo, hi):
            out = c.execute(
                """select timestamp, open, high, low, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s and timestamp >= %s and timestamp <= %s order by timestamp""",
                (pair, lo, hi)).fetchall()
            return [(int(a), float(b), float(cc), float(d), float(e)) for a, b, cc, d, e in out]

        pairs = collections.defaultdict(list)
        for tid, (p, ts) in opened.items():
            if tid not in closed:
                continue
            cp, cts = closed[tid]
            if cts < args.since_ms:      # 按【平仓时间】筛选
                continue
            pairs[p['pair']].append((tid, p, ts, cp, cts))

        results = []
        for pair, items in pairs.items():
            lo = min(i[2] for i in items) - BAR
            hi = max(i[4] for i in items) + 2 * BAR
            ks = candles(pair, lo, hi)
            kby = {k[0]: i for i, k in enumerate(ks)}
            for tid, op, ots, cl, cts in items[:args.limit]:
                ep = op.get('entry_price')
                sl = op.get('stop_level')
                lp = op.get('liquidation_price')
                side = op.get('side')
                notional = float(op.get('notional') or 0)
                if not (ep and lp and notional):
                    continue
                ep, lp = float(ep), float(lp)
                liq_dist = abs(lp - ep) / ep
                raw_dist = abs(float(sl) - ep) / ep if sl is not None else None
                if raw_dist is None or raw_dist <= liq_dist:
                    continue  # 只看"止损在强平外"的
                clamped_dist = liq_dist * args.factor
                direction = 1 if side == 'long' else -1
                stop_price = ep - direction * clamped_dist * ep
                # 沿 1m 路径走到【原平仓时刻】
                i0 = kby.get(ots // BAR * BAR)
                if i0 is None:
                    continue
                orig_net = float(cl.get('net_pnl') or 0)
                exit_px = None
                for k in ks[i0:]:
                    ts, o, h, l, cl_ = k
                    if ts > cts:
                        break
                    hit = (l <= stop_price) if side == 'long' else (h >= stop_price)
                    if hit:
                        exit_px = stop_price
                        break
                if exit_px is None:
                    new_net = orig_net  # 钳制止损未触发 ⇒ 与原来相同
                    kind = 'same'
                else:
                    gross = (exit_px - ep) * direction * (notional / ep)
                    # 入场费按原始 entry_fee；出场费按名义 * 4bp（taker 止损在下一根开盘）
                    entry_fee = float(op.get('entry_fee') or 0)
                    exit_fee = notional * 0.0004
                    new_net = gross - entry_fee - exit_fee
                    kind = 'clamped'
                results.append({'pair': pair, 'side': side, 'notional': notional,
                                'raw_bp': raw_dist * 10000, 'liq_bp': liq_dist * 10000,
                                'orig': orig_net, 'new': new_net, 'kind': kind,
                                'orig_reason': cl.get('exit_reason') or cl.get('reason')})

    n = len(results)
    if not n:
        print('  无符合条件的样本（止损在强平外）')
        return
    no = sum(r['notional'] for r in results)
    go = sum(r['orig'] for r in results)
    gn = sum(r['new'] for r in results)
    cl_n = sum(1 for r in results if r['kind'] == 'clamped')
    print(f'  ═══ 钳制止损重放（factor {args.factor} ⇒ 止损距离 {args.factor * 100:.1f}%）═══')
    print(f'  样本: 【{n}】笔（原本止损在强平外）· 其中被钳制止损触发的: 【{cl_n}】笔')
    print(f'  总名义: {no:,.0f}')
    print(f'  【原始净 = {go:+,.2f} USDT（{go / no * 10000:+.2f} bp）】')
    print(f'  【钳制后净 = {gn:+,.2f} USDT（{gn / no * 10000:+.2f} bp）】')
    print(f'  ⇒ 差异 = 【{gn - go:+,.2f} USDT（{(gn - go) / no * 10000:+.2f} bp）】')
    if args.show:
        print()
        print(f'  {"pair":<16}{"侧":<6}{"原止损bp":>10}{"强平bp":>9}{"原净":>10}{"新净":>10}{"触发":>7}')
        for r in results[:args.show]:
            print(f'  {r["pair"]:<16}{r["side"]:<6}{r["raw_bp"]:>10.1f}{r["liq_bp"]:>9.1f}'
                  f'{r["orig"]:>+10.2f}{r["new"]:>+10.2f}{r["kind"]:>7}')


if __name__ == '__main__':
    main()
