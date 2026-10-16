#!/usr/bin/env python3
"""盘口【动态】特征（挂单压力）是否预测未来收益？—— R344/R345 之后的第三个新方向。

与已测的区别
------------
- R344: 【静态】OBI（某时刻的深度失衡）⇒ NULL
- R345: dev_micro（微观价格偏移）⇒ NULL；spread ⇒ 真实但主要是波动率代理
- 本轮: 每分钟内深度的【变化】⇒ 捕捉【限价单提交/撤销压力】
    d_obi        = OBI(分钟末) − OBI(分钟初)
    rel_dbd      = (bid_depth_末 − bid_depth_初)/bid_depth_初
    rel_dad      = (ask_depth_末 − ask_depth_初)/ask_depth_初
    pressure     = rel_dbd − rel_dad          （正 = 买盘加深 ⇒ 看涨压力）
    d_spread     = spread(末) − spread(初)      （bp）

方法
----
- PG17 的 min_by/max_by 取每分钟的【首/末】快照
- 每个特征 × 每个持有期（1/5/15 分钟）测 Spearman ρ 与符号化毛效应
- 【置换校准】: 循环时移未来收益 ⇒ max|ρ| 的零分布（家庭错误率校正）
- 内置自检: 随机锚点毛收益须 ≈ 0

只读。数据: quant.orderbooks + quant.candles
"""
import os
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
HS = (1, 5, 15)
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(320)

SQL = """
with snap as (
  select (timestamp/60000)*60000 as minute, timestamp,
         ((bids->0->>0)::numeric + (asks->0->>0)::numeric)/2 as mid,
         (asks->0->>0)::numeric - (bids->0->>0)::numeric as spr,
         (select sum((b->>1)::numeric) from jsonb_array_elements(bids) b) as bd,
         (select sum((a->>1)::numeric) from jsonb_array_elements(asks) a) as ad
  from quant.orderbooks
  where pair = %s and timestamp >= %s
),
agg as (
  select minute, count(*) as n, avg((bd-ad)/(bd+ad)) as obi_avg
  from snap group by 1
),
f as (
  select distinct on (minute) minute, mid, spr, bd, ad
  from snap order by minute, timestamp
),
l as (
  select distinct on (minute) minute, mid, spr, bd, ad
  from snap order by minute, timestamp desc
)
select a.minute, a.n, a.obi_avg,
       f.mid, l.mid, f.spr, l.spr, f.bd, l.bd, f.ad, l.ad
from agg a
join f on f.minute = a.minute
join l on l.minute = a.minute
order by a.minute
"""


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        data = {}
        for pr in PAIRS:
            t0 = time.time()
            rows = conn.execute(SQL, (pr, since)).fetchall()
            d = dict(
                ts=np.array([int(r[0]) for r in rows], dtype=np.int64),
                n=np.array([int(r[1]) for r in rows]),
            )
            obi_avg = np.array([float(r[2]) for r in rows])
            mid_f = np.array([float(r[3]) for r in rows]); mid_l = np.array([float(r[4]) for r in rows])
            spr_f = np.array([float(r[5]) for r in rows]); spr_l = np.array([float(r[6]) for r in rows])
            bd_f = np.array([float(r[7]) for r in rows]); bd_l = np.array([float(r[8]) for r in rows])
            ad_f = np.array([float(r[9]) for r in rows]); ad_l = np.array([float(r[10]) for r in rows])
            obi_f = (bd_f - ad_f) / (bd_f + ad_f)
            obi_l = (bd_l - ad_l) / (bd_l + ad_l)
            safe = lambda a: np.where(np.abs(a) < 1e-12, 1e-12, a)
            d.update(dict(
                ts=d['ts'], n=d['n'],
                d_obi=obi_l - obi_f,
                rel_dbd=(bd_l - bd_f) / safe(bd_f),
                rel_dad=(ad_l - ad_f) / safe(ad_f),
                d_spread=(spr_l - spr_f) / ((mid_f + mid_l) / 2) * 1e4,
                obi=obi_avg,
            ))
            d['pressure'] = d['rel_dbd'] - d['rel_dad']
            d['px'] = {int(r[0]): float(r[1]) for r in conn.execute(
                """select timestamp, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s order by timestamp""", (pr,))}
            data[pr] = d
            print(f'  {pr}: {len(d["ts"]):,} 分钟 · {time.time()-t0:.1f}s · 快照 {d["n"].mean():.0f}/分')
            print(f'      d_obi sd {d["d_obi"].std():.4f} · pressure sd {d["pressure"].std():.3f} '
                  f'· d_spread sd {d["d_spread"].std():.3f} bp')

    def fwd(pr, H):
        d = data[pr]
        return np.array([(d['px'].get(t + H * 60000, np.nan) - d['px'].get(t, np.nan)) / d['px'].get(t, np.nan)
                         if d['px'].get(t) else np.nan for t in d['ts']])

    FEATS = ('d_obi', 'pressure', 'd_spread', 'obi')
    chk = []
    for pr in PAIRS:
        for H in HS:
            y = fwd(pr, H); chk.append(np.nanmean(y[~np.isnan(y)]) * 1e4)
    print(f'  【自检】随机锚点毛收益均值 {np.mean(chk):+.3f} bp')
    assert abs(np.mean(chk)) < 3.0, '自检失败'
    print('  ✅ 自检通过')
    print()

    def max_absrho(shift=None):
        best = 0.0; arg = None
        for pr in PAIRS:
            d = data[pr]
            for f in FEATS:
                for H in HS:
                    y = fwd(pr, H)
                    m = ~np.isnan(y)
                    x, yy = d[f][m], y[m]
                    if len(x) < 200:
                        continue
                    if shift is not None:
                        yy = np.roll(yy, -shift)
                    r = abs(st.spearmanr(x, yy)[0])
                    if r > best:
                        best, arg = r, (pr, f, H)
        return best, arg

    print('  ═══ 逐特征 × 逐持有期（含对照 obi = R344 静态量）═══')
    print(f'    {"pair":<18}{"feat":<11}{"H":>4}{"n":>7}{"ρ":>10}{"毛效应‰":>10}{"效应/成本":>10}')
    for pr in PAIRS:
        d = data[pr]
        for f in FEATS:
            for H in HS:
                y = fwd(pr, H)
                m = ~np.isnan(y)
                x, yy = d[f][m], y[m]
                if len(x) < 200:
                    continue
                r = st.spearmanr(x, yy)[0]
                g = float(np.nanmean(np.sign(x - np.median(x)) * yy) * 1000)
                print(f'    {pr:<18}{f:<11}{H:>4}{len(x):>7,}{r:>+10.5f}{g:>+10.5f}{g/(COST_BP/10):>9.2f}×')
    obs, arg = max_absrho()
    print()
    print(f'  ═══ 观察最大 |ρ| = 【{obs:.5f}】（{arg[0]} · {arg[1]} · H={arg[2]}m）═══')
    nulls = [max_absrho(shift=int(RNG.integers(10, 2000)))[0] for _ in range(NPERM)]
    nulls = np.array(nulls)
    p = (np.sum(nulls >= obs) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p = {p:.4f}】')
    print()
    print('  ═══ 判定 ═══')
    if p > 0.05:
        print(f'    🔴 落在零分布内 ⇒ 【盘口动态特征【无】可检出预测力】')
    else:
        print(f'    ⚠️ 超出零分布 ⇒ 存在统计真实的关系 ⇒ 须看经济量级（门槛 {COST_BP/10:.2f}‰）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
