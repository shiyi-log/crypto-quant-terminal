#!/usr/bin/env python3
"""逐笔订单流失衡（30 秒级）是否预测未来收益？—— 此前最细只到 1 分钟。

为什么是新的
------------
- 实验室的 `flow_imbal` 是【5 分钟】棒上的主动买/卖量
- R344/R347 的盘口特征最细是【1 分钟】
- 本轮用【原始 ticks】（3080 万行）构造【30 秒】级 OFI ⇒ 比此前细 2–10 倍

符号约定（关键）
----------------
Binance 的 `buyer_maker = True` 表示【买方是挂单方(maker)】⇒ 【主动方是卖方】⇒ sign = −1
  ofi = Σ quantity × (−1 if buyer_maker else +1)
✅ 自检: 把 ofi 聚合到 5 分钟，与 quant.candles 的 orderflow `flow_imbal` 对照，
   符号约定正确时两者应【强正相关】。

方法
----
- 30 秒桶: ofi · 总量 · 笔数 · 桶末价
- 目标: 未来 30s / 1m / 5m 的收益（用桶末价）
- 【置换校准】: 循环时移未来收益 ⇒ max|ρ| 零分布（家庭错误率校正）
- 内置自检: 随机锚点毛收益 ≈ 0

只读。数据: quant.ticks (+ quant.candles 做符号自检)
"""
import os
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
BUCKET = 30000            # 30 秒
HS = (1, 2, 10)           # 以桶为单位 ⇒ 30s / 60s / 300s
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(322)

SQL = """
with tb as (
  select (timestamp/%s)*%s as bucket, timestamp, price, quantity, buyer_maker
  from quant.ticks
  where pair = %s and timestamp >= %s
),
agg as (
  select bucket,
         sum(case when buyer_maker then -quantity else quantity end) as ofi,
         sum(quantity) as vol,
         count(*) as n
  from tb group by bucket
),
lastp as (
  select distinct on (bucket) bucket, price
  from tb order by bucket, timestamp desc
)
select a.bucket, a.ofi, a.vol, a.n, l.price
from agg a join lastp l on l.bucket = a.bucket
order by a.bucket
"""


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.ticks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        data = {}
        for pr in PAIRS:
            t0 = time.time()
            rows = conn.execute(SQL, (BUCKET, BUCKET, pr, since)).fetchall()
            d = dict(
                ts=np.array([int(r[0]) for r in rows], dtype=np.int64),
                ofi=np.array([float(r[1]) for r in rows]),
                vol=np.array([float(r[2]) for r in rows]),
                n=np.array([int(r[3]) for r in rows]),
                px=np.array([float(r[4]) for r in rows]),
            )
            d['ofi_n'] = d['ofi'] / np.where(d['vol'] == 0, 1, d['vol'])   # 归一化失衡
            data[pr] = d
            print(f'  {pr}: {len(d["ts"]):,} 个 {BUCKET/1000:.0f}s 桶 · {time.time()-t0:.1f}s '
                  f'· {d["n"].mean():.0f} tick/桶')

        # ── 符号自检: ofi 聚合到 5 分钟 vs orderflow flow_imbal ──
        print()
        print('  ═══ 符号约定自检（ofi vs 实验室 flow_imbal，5 分钟）═══')
        for pr in PAIRS:
            d = data[pr]
            m5 = (d['ts'] // 300000) * 300000
            agg = {}
            for t, o, v in zip(m5, d['ofi'], d['vol']):
                a = agg.setdefault(int(t), [0.0, 0.0])
                a[0] += o; a[1] += v
            of = {k: (v[0] / v[1] if v[1] else 0.0) for k, v in agg.items()}
            lab = {}
            for t, ex in conn.execute(
                """select timestamp, extras from quant.candles
                   where candle_type='orderflow' and timeframe='5m' and pair=%s""", (pr,)):
                e = ex if isinstance(ex, dict) else __import__('json').loads(ex)
                try:
                    lab[int(t)] = float(e.get('flow_imbal'))
                except Exception:
                    pass
            ks = sorted(set(of) & set(lab))
            if len(ks) > 100:
                x = np.array([of[k] for k in ks]); y = np.array([lab[k] for k in ks])
                r = st.spearmanr(x, y)[0]
                print(f'    {pr}: n={len(ks)} · ρ(我的 ofi, flow_imbal) = {r:+.4f} '
                      f'⇒ {"✅ 符号约定正确" if r > 0.5 else "🔴 符号可能反了"}')
            else:
                print(f'    {pr}: 重叠样本不足（{len(ks)}）')

    def fwd(pr, H):
        d = data[pr]
        n = len(d['px'])
        out = np.full(n, np.nan)
        for i in range(n - H):
            if d['px'][i]:
                out[i] = (d['px'][i + H] - d['px'][i]) / d['px'][i]
        return out

    FEATS = ('ofi_n', 'ofi')
    chk = [np.nanmean(fwd(pr, H)[~np.isnan(fwd(pr, H))]) * 1e4 for pr in PAIRS for H in HS]
    print()
    print(f'  【自检】随机锚点毛收益均值 {np.mean(chk):+.3f} bp')
    assert abs(np.mean(chk)) < 3.0, '自检失败'
    print('  ✅ 自检通过')
    print()

    def max_absrho(shift=None):
        best = 0.0; arg = None
        for pr in PAIRS:
            for f in FEATS:
                for H in HS:
                    y = fwd(pr, H)
                    m = ~np.isnan(y)
                    x, yy = data[pr][f][m], y[m]
                    if len(x) < 500:
                        continue
                    if shift is not None:
                        yy = np.roll(yy, -shift)
                    r = abs(st.spearmanr(x, yy)[0])
                    if r > best:
                        best, arg = r, (pr, f, H * BUCKET / 1000)
        return best, arg

    print('  ═══ 逐特征 × 逐持有期 ═══')
    print(f'    {"pair":<18}{"feat":<8}{"持有":>7}{"n":>9}{"ρ":>10}{"毛效应‰":>10}{"效应/成本":>10}')
    for pr in PAIRS:
        for f in FEATS:
            for H in HS:
                y = fwd(pr, H)
                m = ~np.isnan(y)
                x, yy = data[pr][f][m], y[m]
                if len(x) < 500:
                    continue
                r = st.spearmanr(x, yy)[0]
                g = float(np.nanmean(np.sign(x - np.median(x)) * yy) * 1000)
                print(f'    {pr:<18}{f:<8}{int(H*BUCKET/1000):>6}s{len(x):>9,}{r:>+10.5f}{g:>+10.5f}{g/(COST_BP/10):>9.3f}×')
    obs, arg = max_absrho()
    print()
    print(f'  ═══ 观察最大 |ρ| = 【{obs:.5f}】（{arg[0]} · {arg[1]} · {arg[2]}s）═══')
    nulls = np.array([max_absrho(shift=int(RNG.integers(5, 3000)))[0] for _ in range(NPERM)])
    p = (np.sum(nulls >= obs) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p = {p:.4f}】')
    print()
    if p > 0.05:
        print(f'    🔴 落在零分布内 ⇒ 【30 秒级逐笔 OFI 无超出噪声的预测力】')
    else:
        print(f'    ⚠️ 超出零分布 ⇒ 统计真实 ⇒ 但仍须看经济量级（门槛 {COST_BP/10:.2f}‰）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
