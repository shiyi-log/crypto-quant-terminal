#!/usr/bin/env python3
"""maker 挂单的【成交率】与【逆向选择】——直接从 K 线路径测量。

为什么需要
----------
决策 4（maker 出场实验）的阻塞点是「maker 成交率未知」。本工具用 K 线路径回答两个问题：

  ① 成交率: 限价买挂在价格 P，若后续 N 根内 low ≤ P 则成交（卖单反之用 high ≥ P）
  ② 逆向选择: 【条件于成交】的后续收益 —— 即"挂单等来的成交"是否系统性吃亏

方法
----
对每一根 K 线 t（作为信号时刻），在 P = close[t] 挂单，观察 t+1..t+TTL：
- 买单成交条件: low ≤ P；卖单成交条件: high ≥ P
- 首次成交后，测量从 P 到 t+TTL_end 的收益（成交后持有到窗口末）
- 对照: 【立即以 taker 市价成交】（价格用 close[t] + 半个价差估计，或用 close[t] 作为下界）

⚠️ 口径声明
- 用 close 作为挂单价（真实中会是 bid/ask，会有半个价差的差异）
- 成交判定用 candle 的 low/high ⇒ 【乐观】: 只要触价即算成交（未考虑排队）
- 因此报告的是【成交率的上界】
- 成本: taker 4bp/边；maker 2bp/边

只读。数据: quant.candles futures 5m/15m
"""
import os
import numpy as np
import psycopg

TTLS = (1, 2, 3, 6)          # TTL in bars
COST_MAKER_BP = 2.0          # 单边
COST_TAKER_BP = 4.0          # 单边
TF = '5m'
MS = 300000
PAIRS = ('BTC', 'ETH', 'TRX')


def load(conn, pr):
    rows = list(conn.execute(
        """select timestamp, open, high, low, close from quant.candles
           where exchange='binance' and candle_type='futures' and timeframe=%s
             and split_part(pair,'/',1)=%s order by timestamp""", (TF, pr)))
    ts = np.array([int(r[0]) for r in rows], dtype=np.int64)
    o = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    lo = np.array([float(r[3]) for r in rows])
    cl = np.array([float(r[4]) for r in rows])
    ok = np.ones(len(ts), bool)
    ok[1:] = np.diff(ts) == MS
    return ts[ok], o[ok], h[ok], lo[ok], cl[ok]


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        D = {pr: load(conn, pr) for pr in PAIRS}
    for pr, (ts, *_rest) in D.items():
        print(f'  {pr}: {len(ts):,} 根 {TF}')
    assert all(len(v[0]) > 10000 for v in D.values()), '样本过少（自检）'
    print()

    print(f'  ═══ ① maker 挂单成交率（挂单价 = 信号根收盘价）· {TF} ═══')
    print(f'    {"TTL":<6}{"样本":>10}{"买单成交率":>12}{"卖单成交率":>12}{"平均":>10}')
    rates = {}
    for ttl in TTLS:
        fb = fs = tot = 0
        for pr, (ts, o, h, lo, cl) in D.items():
            n = len(cl) - ttl - 1
            P = cl[:n]                                   # 挂单价
            # 买: 后续 ttl 根内 low <= P
            buy = np.zeros(n, bool)
            sell = np.zeros(n, bool)
            for k in range(1, ttl + 1):
                buy |= lo[k:k + n] <= P
                sell |= h[k:k + n] >= P
            fb += buy.sum(); fs += sell.sum(); tot += n
        rates[ttl] = ((fb + fs) / (2 * tot))
        print(f'    {ttl:<6}{tot:>10,}{fb/tot*100:>11.1f}%{fs/tot*100:>11.1f}%{(fb+fs)/(2*tot)*100:>9.1f}%')
    print()

    print(f'  ═══ ② 逆向选择：条件于成交的后续收益（毛 ‰，正 = 有利）═══')
    print(f'    {"TTL":<6}{"成交后持有到窗口末":>20}{"立即 taker":>14}{"maker−taker":>14}')
    for ttl in TTLS:
        mk = []
        tk = []
        for pr, (ts, o, h, lo, cl) in D.items():
            n = len(cl) - ttl - 1
            P = cl[:n]
            for side, d in (('buy', 1.0), ('sell', -1.0)):
                if d > 0:
                    hit = np.zeros(n, bool)
                    for k in range(1, ttl + 1):
                        hit |= lo[k:k + n] <= P
                else:
                    hit = np.zeros(n, bool)
                    for k in range(1, ttl + 1):
                        hit |= h[k:k + n] >= P
                endpx = cl[ttl:ttl + n]
                r_mk = (endpx - P) / P * d * 1000 - 2 * COST_MAKER_BP / 10
                r_tk = (endpx - P) / P * d * 1000 - 2 * COST_TAKER_BP / 10
                mk.append(r_mk[hit]); tk.append(r_tk[hit])
        a = np.concatenate(mk); b = np.concatenate(tk)
        print(f'    {ttl:<6}{a.mean():>+20.4f}{b.mean():>+14.4f}{a.mean()-b.mean():>+14.4f}')
    print()
    print('  ═══ 判读 ═══')
    print('    ⇒ 成交率给出 maker 可得性的【上界】（触价即算成交，未考虑排队）')
    print('    ⇒ "成交后持有到窗口末"为负 ⇒ 存在【逆向选择】（挂单等来的成交吃亏）')
    print('    ⇒ maker−taker 的差 = 仅手续费的差（4bp）+ 相同的路径收益 ⇒ 应恒为 +4bp')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
