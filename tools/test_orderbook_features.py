#!/usr/bin/env python3
"""盘口【微观价格】与【价差】是否预测未来收益？—— R317/R344 之后的第二个新方向。

为什么与 OBI 不同
----------------
R344 测的是【静态深度失衡】 OBI = (Σbid_depth − Σask_depth)/(Σbid_depth + Σask_depth) ⇒ NULL。
本工具测的是微观结构里【最强】的两个量：
  ① 微观价格 micro = (bid × ask1_size + ask × bid1_size)/(bid1_size + ask1_size)
     dev_micro = (micro − mid)/mid  （bp）—— 深度加权的公允价值偏移
  ② 价差 spread = (ask − bid)/mid   （bp）
  ③ 深度不平衡 OBI（作为对照，应与 R344 一致）

严格性
------
- 每个特征 × 每个持有期都报告 ρ 与【经济量级】（毛效应‰ / 成本门槛）
- 置换校准: 循环时移未来收益 ⇒ max|ρ| 的零分布（家庭错误率校正）
- 内置自检: 随机锚点毛收益须 ≈ 0

只读。数据: quant.orderbooks + quant.candles
"""
import os
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
HS = (1, 3, 5, 15)
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(318)

SQL = """
with snap as (
  select timestamp,
         (select sum((b->>1)::numeric) from jsonb_array_elements(bids) b) as bd,
         (select sum((a->>1)::numeric) from jsonb_array_elements(asks) a) as ad,
         (bids->0->>0)::numeric as b1, (asks->0->>0)::numeric as a1,
         (bids->0->>1)::numeric as b1s, (asks->0->>1)::numeric as a1s
  from quant.orderbooks
  where pair = %s and timestamp >= %s
)
select (timestamp/60000)*60000 as minute,
       count(*) as n,
       avg((bd-ad)/(bd+ad)) as obi,
       avg((b1*a1s + a1*b1s)/(b1s+a1s)) as micro,
       avg((b1+a1)/2) as mid,
       avg((a1-b1)/((a1+b1)/2)) as spread
from snap
group by 1
having count(*) >= 20
order by 1
"""


def load(conn, pair, since):
    t0 = time.time()
    rows = conn.execute(SQL, (pair, since)).fetchall()
    return dict(
        ts=np.array([int(r[0]) for r in rows], dtype=np.int64),
        n=np.array([int(r[1]) for r in rows]),
        obi=np.array([float(r[2]) for r in rows]),
        micro=np.array([float(r[3]) for r in rows]),
        mid=np.array([float(r[4]) for r in rows]),
        spread=np.array([float(r[5]) for r in rows]),
        secs=time.time() - t0)


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        data = {}
        for pr in PAIRS:
            d = load(conn, pr, since)
            px = {int(r[0]): float(r[1]) for r in conn.execute(
                """select timestamp, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s order by timestamp""", (pr,))}
            d['px'] = px
            # 派生特征（bp）
            d['dev_micro'] = (d['micro'] - d['mid']) / d['mid'] * 1e4
            d['spread_bp'] = d['spread'] * 1e4
            data[pr] = d
            print(f'  {pr}: {len(d["ts"]):,} 分钟 · {d["secs"]:.1f}s · 快照 {d["n"].mean():.0f}/分')
            print(f'      OBI {d["obi"].std():.4f} · dev_micro sd {d["dev_micro"].std():.3f} bp '
                  f'· spread 中位 {np.median(d["spread_bp"]):.3f} bp')

    def fwd_ret(d, H):
        return np.array([(d['px'].get(t + H * 60000, np.nan) - d['px'].get(t, np.nan)) / d['px'].get(t, np.nan)
                         if d['px'].get(t) else np.nan for t in d['ts']])

    # 自检
    chk = [np.nanmean(fwd_ret(d, H)[~np.isnan(fwd_ret(d, H))]) * 1e4
           for d in data.values() for H in HS]
    print(f'  【自检】随机锚点毛收益均值 {np.mean(chk):+.3f} bp')
    assert abs(np.mean(chk)) < 3.0, '自检失败'
    print('  ✅ 自检通过')
    print()

    FEATS = ('dev_micro', 'obi', 'spread_bp')
    print('  ═══ 逐特征 × 逐持有期 ═══')
    print(f'    {"pair":<18}{"feat":<12}{"H":>4}{"n":>7}{"ρ":>10}{"毛效应‰":>10}{"效应/成本":>10}')
    for pr, d in data.items():
        for f in FEATS:
            x0 = d[f]
            for H in HS:
                y = fwd_ret(d, H)
                m = ~np.isnan(y)
                x, yy = x0[m], y[m]
                if len(x) < 200:
                    continue
                r = st.spearmanr(x, yy)[0]
                g = float(np.nanmean(np.sign(x - np.median(x)) * yy) * 1000)
                print(f'    {pr:<18}{f:<12}{H:>4}{len(x):>7,}{r:>+10.5f}{g:>+10.5f}{g/(COST_BP/10):>9.2f}×')
    print()

    def max_absrho(shift=None):
        best = 0.0; arg = None
        for pr, d in data.items():
            for f in FEATS:
                for H in HS:
                    y = fwd_ret(d, H)
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

    obs, arg = max_absrho()
    print(f'  ═══ 观察最大 |ρ| = 【{obs:.5f}】（{arg[0]} · {arg[1]} · H={arg[2]}m）═══')
    nulls = []
    for _ in range(NPERM):
        b, _ = max_absrho(shift=int(RNG.integers(10, 2000)))
        nulls.append(b)
    nulls = np.array(nulls)
    p = (np.sum(nulls >= obs) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p = {p:.4f}】')
    print()
    print('  ═══ 判定 ═══')
    if p > 0.05:
        print(f'    🔴 观察 max|ρ| = {obs:.5f} 落在零分布内')
        print(f'    ⇒ 【微观价格 / 价差 / OBI 均无超出噪声的预测力】')
    else:
        print(f'    ⚠️ 超出零分布 ⇒ 存在统计真实的关系 ⇒ 但须看经济量级（成本门槛 {COST_BP/10:.2f}‰）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
