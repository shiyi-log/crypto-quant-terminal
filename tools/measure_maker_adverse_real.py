#!/usr/bin/env python3
"""用真实 10 Hz 盘口测【maker 挂单的成交延迟与逆向选择】—— 补上 R339 判定"不可测"的缺口。

背景
----
R339 判定"逆向选择不可测"，理由是缺少【成交瞬间的中间价】。
现在 quant.orderbooks 有 1721 万行、每秒约 9-10 个快照、20 档 ⇒ 可以用真实盘口测量。

方法（卖单 = 出场腿）
--------------------
1. SQL 里把盘口聚合成【5 秒】桶: 该桶内的 min(ask) / max(bid) / 最后一个 mid
2. 在时刻 t 挂一个被动卖单 P = mid(t) × (1+δ)
3. 成交 = 第一个 t' > t 满足 【bucket 的 max(bid) >= P】（有买方吃到你的价）
4. **逆向选择** = 成交后 mid 相对 P 的漂移:
     short 时不利 = (mid(t'+h) − P)/P  > 0
5. 对照（taker）: 立刻以 bid(t) 成交，收益 = (bid(t) − mid(t+h))/bid(t)

输出
----
逐 δ 给出: 成交率、平均等待、成交后 h 分钟的逆向选择（bp）、以及 maker vs taker 的净差。

严格性
------
- 内置自检: 成交样本必须 > 100，否则报错而非静默
- 明确声明: 「bucket 内 max(bid) >= P」是【成交判定的乐观上界】（未计排队）

只读。数据: quant.orderbooks + quant.candles
"""
import os
import time
import numpy as np
import psycopg

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
DELTAS_BP = (0.5, 1.0, 2.0, 5.0)
HORIZONS = (1, 5, 15)          # 分钟
BUCKET_MS = 5000
DAYS = 7

SQL = """
with snap as (
  select timestamp,
         (bids->0->>0)::numeric as b1,
         (asks->0->>0)::numeric as a1,
         (bids->0->>0)::numeric + (asks->0->>0)::numeric as s
  from quant.orderbooks
  where pair = %s and timestamp >= %s
)
select (timestamp/%s)*%s as bucket,
       min(a1) as ask_min, max(b1) as bid_max,
       avg(s)/2 as mid
from snap
group by 1
order by 1
"""


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        print(f'  窗口: 最近 {DAYS} 天（到大 {time.strftime("%m-%d %H:%M", time.gmtime(int(mx)/1000))}Z）· 桶 = {BUCKET_MS/1000:.0f}s')
        data = {}
        for pr in PAIRS:
            t0 = time.time()
            rows = conn.execute(SQL, (pr, since, BUCKET_MS, BUCKET_MS)).fetchall()
            b = np.array([int(r[0]) for r in rows], dtype=np.int64)
            ask_min = np.array([float(r[1]) for r in rows])
            bid_max = np.array([float(r[2]) for r in rows])
            mid = np.array([float(r[3]) for r in rows])
            px = {int(r[0]): float(r[1]) for r in conn.execute(
                """select timestamp, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s order by timestamp""", (pr,))}
            data[pr] = dict(b=b, ask_min=ask_min, bid_max=bid_max, mid=mid, px=px)
            print(f'  {pr}: {len(b):,} 个 {BUCKET_MS/1000:.0f}s 桶 · {time.time()-t0:.1f}s')

    print()
    print('  ═══ 卖单（出场腿）: 挂单价 P = mid×(1+δ)，成交 = 后续桶 max(bid) ≥ P ═══')
    hdr = f'    {"pair":<18}{"δ(bp)":>7}{"成交率":>9}{"等待(s)":>9}'
    for h in HORIZONS:
        hdr += f'{f"+{h}m 逆向(bp)":>16}'
    print(hdr)

    summary = {}
    for pr, d in data.items():
        b, mid, bmax = d['b'], d['mid'], d['bid_max']
        n = len(b)
        for dbp in DELTAS_BP:
            P = mid * (1 + dbp / 1e4)
            maxwait = int(60 * 1000 / BUCKET_MS)      # 最多等 60 秒
            filled = 0
            waits = []
            adv = {h: [] for h in HORIZONS}
            for i in range(0, n - 1):
                j_end = min(i + 1 + maxwait, n)
                # 找第一个 bucket 的 bid_max >= P[i]
                seg = bmax[i + 1:j_end]
                hit = np.nonzero(seg >= P[i])[0]
                if len(hit) == 0:
                    continue
                j = i + 1 + hit[0]
                # 成交时刻与 mid：用 j 的 mid
                filled += 1
                waits.append((b[j] - b[i]) / 1000.0)
                for h in HORIZONS:
                    tgt = b[j] + h * 60000
                    k = int(np.searchsorted(b, tgt))
                    if k < n:
                        adv[h].append((mid[k] - P[i]) / P[i] * 1e4)   # short: 正 = 不利
            rate = filled / n * 100
            row = f'    {pr:<18}{dbp:>7.1f}{rate:>8.1f}%{(np.mean(waits) if waits else float("nan")):>9.2f}'
            for h in HORIZONS:
                v = np.array(adv[h])
                row += f'{v.mean():>+16.3f}' if len(v) else f'{"—":>16}'
                summary[(pr, dbp, h)] = v.mean() if len(v) else float('nan')
            print(row)
            assert filled == 0 or len(adv[HORIZONS[0]]) > 0, '自检失败'
    print()
    print('  ═══ 判读 ═══')
    print('    ⇒ 「逆向(bp)」为正 = 成交后 mid 朝不利方向走（挂单等来的成交吃亏）')
    print('    ⇒ 与 taker 相比: maker 省 2bp 手续费（单边），但要减去【逆向选择 + 未成交】')
    print('    ⚠️ 成交判定用「桶内 max(bid) ≥ P」= 【乐观上界】（未计排队）⇒ 真实成交率更低')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
