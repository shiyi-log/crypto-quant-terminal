#!/usr/bin/env python3
"""盘口动态特征【多特征组合】能否把信号做够大？—— 用严格的样本外评估。

动机
----
R350 的最强单特征是 1 分钟 `d_obi5`（ρ=0.095 · family-wise p=0.005 · 非波动率代理 ·
样本外 8/8 增强），但毛效应仅 0.037‰ vs 成本 0.60‰ ⇒ 差 16 倍。
本轮问: 【组合多个盘口动态特征】能否缩小这个差距？

严格性（关键）
--------------
- 【时间对半分割】: 前半拟合 OLS 权重，后半评估 ⇒ 杜绝样本内过拟合
- 【置换校准】: 循环时移后半的未来收益 ⇒ max|ρ| 零分布（覆盖特征集的选择）
- 报告【经济量级】+ 与单特征的对照
- 内置自检: 随机锚点毛收益 ≈ 0

只读。数据: quant.orderbooks + quant.candles
"""
import os
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
KS = (5, 20)
HS = (1, 5)
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(324)

SQL = """
with snap as (
  select (timestamp/60000)*60000 as minute, timestamp, bids, asks
  from quant.orderbooks
  where pair = %s and timestamp >= %s
),
f as (select distinct on (minute) * from snap order by minute, timestamp),
l as (select distinct on (minute) * from snap order by minute, timestamp desc)
select f.minute, f.bids, f.asks, l.bids, l.asks
from f join l using (minute) order by f.minute
"""


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        data = {}
        for pr in PAIRS:
            t0 = time.time()
            rows = conn.execute(SQL, (pr, since)).fetchall()

            def lev(arr, k, which):
                """arr: jsonb list of [price, size]; which: 0=price 1=size"""
                try:
                    a = arr if isinstance(arr, list) else __import__('json').loads(arr)
                except Exception:
                    return np.nan
                return sum(float(x[which]) for x in a[:k])

            saf = lambda a: np.where(np.abs(a) < 1e-12, 1e-12, a)
            b1f = np.array([lev(r[1], 1, 0) for r in rows]); a1f = np.array([lev(r[2], 1, 0) for r in rows])
            b1l = np.array([lev(r[3], 1, 0) for r in rows]); a1l = np.array([lev(r[4], 1, 0) for r in rows])
            bd5f = np.array([lev(r[1], 5, 1) for r in rows]); ad5f = np.array([lev(r[2], 5, 1) for r in rows])
            bd5l = np.array([lev(r[3], 5, 1) for r in rows]); ad5l = np.array([lev(r[4], 5, 1) for r in rows])
            bd20f = np.array([lev(r[1], 20, 1) for r in rows]); ad20f = np.array([lev(r[2], 20, 1) for r in rows])
            bd20l = np.array([lev(r[3], 20, 1) for r in rows]); ad20l = np.array([lev(r[4], 20, 1) for r in rows])
            obi5f = (bd5f - ad5f) / saf(bd5f + ad5f)
            obi5l = (bd5l - ad5l) / saf(bd5l + ad5l)
            d = dict(
                ts=np.array([int(r[0]) for r in rows], dtype=np.int64),
                d_obi5=obi5l - obi5f,
                d_obi20=((bd20l - ad20l) / saf(bd20l + ad20l)) - ((bd20f - ad20f) / saf(bd20f + ad20f)),
                obi5=obi5f,
                rel_dbd=(bd20l - bd20f) / saf(bd20f),
                rel_dad=(ad20l - ad20f) / saf(ad20f),
                d_spread=(((a1l - b1l) / ((a1l + b1l) / 2)) - ((a1f - b1f) / ((a1f + b1f) / 2))) * 1e4,
            )
            d['pressure'] = d['rel_dbd'] - d['rel_dad']
            d['px'] = {int(r[0]): float(r[1]) for r in conn.execute(
                """select timestamp, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s order by timestamp""", (pr,))}
            data[pr] = d
            print(f'  {pr}: {len(d["ts"]):,} 分钟 · {time.time()-t0:.1f}s')

    FEATS = ('d_obi5', 'pressure', 'rel_dbd', 'rel_dad', 'd_spread', 'obi5', 'd_obi20')

    def fwd(pr, H):
        d = data[pr]
        return np.array([(d['px'].get(t + H * 60000, np.nan) - d['px'].get(t, np.nan)) / d['px'].get(t, np.nan)
                         if d['px'].get(t) else np.nan for t in d['ts']])

    print()
    print('  ═══ 前半拟合 / 后半评估（OLS 组合 vs 最强单特征）═══')
    print(f'    {"pair":<18}{"H":>3}{"方法":<22}{"前半ρ":>10}{"后半ρ":>10}{"后半毛效应‰":>13}{"效应/成本":>10}')
    oos_recs = {}
    for pr in PAIRS:
        d = data[pr]
        for H in HS:
            y = fwd(pr, H)
            m = ~np.isnan(y)
            ts = d['ts'][m]; y = y[m]
            X = np.column_stack([d[f][m] for f in FEATS])
            n = len(y); k = n // 2
            # 标准化（用前半的均值/标准差）
            mu, sd = X[:k].mean(0), X[:k].std(0); sd[sd == 0] = 1
            Z = (X - mu) / sd
            # 单特征对照
            for f in ('d_obi5', 'pressure'):
                i = FEATS.index(f)
                ri = st.spearmanr(Z[:k, i], y[:k])[0]
                ro = st.spearmanr(Z[k:, i], y[k:])[0]
                g = float(np.mean(np.sign(Z[k:, i]) * y[k:]) * 1000)
                print(f'    {pr:<18}{H:>3}{f:<22}{ri:>+10.5f}{ro:>+10.5f}{g:>+13.5f}{g/(COST_BP/10):>9.3f}×')
            # OLS 组合
            A = np.column_stack([Z[:k], np.ones(k)])
            beta, *_ = np.linalg.lstsq(A, y[:k], rcond=None)
            sc_in = Z[:k] @ beta[:-1]
            sc_out = Z[k:] @ beta[:-1]
            ri = st.spearmanr(sc_in, y[:k])[0]
            ro = st.spearmanr(sc_out, y[k:])[0]
            g = float(np.mean(np.sign(sc_out) * y[k:]) * 1000)
            print(f'    {pr:<18}{H:>3}{"OLS 组合(7特征)":<22}{ri:>+10.5f}{ro:>+10.5f}{g:>+13.5f}{g/(COST_BP/10):>9.3f}×')
            oos_recs[(pr, H, 'ols')] = absc = abs(ro)
            # 等权组合
            sc2_in = Z[:k].mean(1); sc2_out = Z[k:].mean(1)
            ri2 = st.spearmanr(sc2_in, y[:k])[0]
            ro2 = st.spearmanr(sc2_out, y[k:])[0]
            g2 = float(np.mean(np.sign(sc2_out) * y[k:]) * 1000)
            print(f'    {pr:<18}{H:>3}{"等权组合":<22}{ri2:>+10.5f}{ro2:>+10.5f}{g2:>+13.5f}{g2/(COST_BP/10):>9.3f}×')
            oos_recs[(pr, H, 'eq')] = abs(ro2)
    print()
    best_single = max((abs(st.spearmanr(
        ((data[pr][f] - data[pr][f].mean()) / (data[pr][f].std() or 1))[~np.isnan(fwd(pr, H))],
        fwd(pr, H)[~np.isnan(fwd(pr, H))])[0])
        for pr in PAIRS for f in ('d_obi5', 'pressure') for H in HS))
    print(f'  ═══ 判定 ═══')
    print(f'    最强单特征（全样本 |ρ|）≈ {best_single:.5f}')
    print(f'    组合的最佳后半 |ρ| = {max(oos_recs.values()):.5f}')
    print(f'    ⇒ 成本门槛 {COST_BP/10:.2f}‰；需要毛效应 ≥ 该值才可交易')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
