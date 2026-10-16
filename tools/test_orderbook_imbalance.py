#!/usr/bin/env python3
"""盘口深度不平衡（OBI）是否预测未来收益？—— 全新数据源，从未测过。

为什么是新的
------------
实验室用的 `flow_imbal` 是【成交】失衡（5m 棒的主动买/卖量）。
本工具测的是【挂单深度】失衡（20 档盘口的 bid size vs ask size），
来自 `quant.orderbooks`（1721 万行 · 每秒约 9-10 个快照 · 实时）。
这是【不同的量】，本仓库此前从未测试。

方法
----
1. SQL 里把 20 档盘口展开，逐快照算 OBI = (Σbid_size − Σask_size)/(Σbid_size + Σask_size)
2. 聚合成【每分钟】的均值
3. 对每个持有期 H ∈ {1,3,5,15} 分钟，测 OBI 与未来收益的 Spearman ρ
4. 【置换校准】: 循环时移未来收益 ⇒ 得到 max|ρ| 的零分布（家庭错误率校正）
   ⇒ 判断观察到的 max|ρ| 是否超出噪声

严格性
------
- 报告【效应量】+ 置换 family-wise p，不只看 p 值
- 成本门槛 6 bp（往返，实测）⇒ 报告 效应/成本 比值
- 内置自检: 随机锚点的毛收益须 ≈ 0

只读。数据: quant.orderbooks + quant.candles
"""
import os
import sys
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
HS = (1, 3, 5, 15)          # 分钟
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(317)

SQL = """
with snap as (
  select pair, timestamp,
         (select sum((b->>1)::numeric) from jsonb_array_elements(bids) b) as bd,
         (select sum((a->>1)::numeric) from jsonb_array_elements(asks) a) as ad,
         (bids->0->>0)::numeric as b1,
         (asks->0->>0)::numeric as a1
  from quant.orderbooks
  where pair = %s and timestamp >= %s
)
select (timestamp/60000)*60000 as minute,
       count(*) as n,
       avg((bd-ad)/(bd+ad)) as obi,
       avg((b1+a1)/2) as mid
from snap
group by 1
having count(*) >= 20
order by 1
"""


def load_obi(conn, pair, since):
    t0 = time.time()
    rows = conn.execute(SQL, (pair, since)).fetchall()
    return (np.array([int(r[0]) for r in rows], dtype=np.int64),
            np.array([float(r[2]) for r in rows]),
            np.array([float(r[3]) for r in rows]),
            np.array([int(r[1]) for r in rows]),
            time.time() - t0)


def load_px(conn, pair):
    rows = conn.execute(
        """select timestamp, close from quant.candles
           where exchange='binance' and candle_type='futures' and timeframe='1m'
             and pair=%s order by timestamp""", (pair,)).fetchall()
    return {int(r[0]): float(r[1]) for r in rows}


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        print(f'  窗口: 最近 {DAYS} 天（到 {time.strftime("%m-%d %H:%M", time.gmtime(int(mx)/1000))}Z）')
        data = {}
        for pr in PAIRS:
            ts, obi, mid, n, dt = load_obi(conn, pr, since)
            px = load_px(conn, pr)
            data[pr] = (ts, obi, mid, n, px)
            print(f'  {pr}: {len(ts):,} 分钟 · {dt:.1f}s · 每分钟快照 {n.mean():.0f} · OBI sd {obi.std():.4f}')

    # ── 自检 ──
    chk = []
    for pr, (ts, obi, mid, n, px) in data.items():
        for H in HS:
            fwd = np.array([(px.get(t + H * 60000, np.nan) - px.get(t, np.nan)) / px.get(t, np.nan)
                            if px.get(t) else np.nan for t in ts])
            m = ~np.isnan(fwd)
            if m.sum() > 100:
                chk.append(np.nanmean(fwd[m]) * 10000)
    print(f'  【自检】随机锚点毛收益均值 {np.mean(chk):+.3f} bp（应 ≈0）')
    assert abs(np.mean(chk)) < 3.0, '自检失败'
    print('  ✅ 自检通过')
    print()

    def max_absrho(shift=None):
        best = 0.0
        arg = None
        for pr, (ts, obi, mid, n, px) in data.items():
            for H in HS:
                fwd = np.array([(px.get(t + H * 60000, np.nan) - px.get(t, np.nan)) / px.get(t, np.nan)
                                if px.get(t) else np.nan for t in ts])
                m = ~np.isnan(fwd)
                x, y = obi[m], fwd[m]
                if shift is not None and len(y) > shift:
                    y = np.roll(y, -shift)
                if len(x) < 200:
                    continue
                r = abs(st.spearmanr(x, y)[0])
                if r > best:
                    best, arg = r, (pr, H)
        return best, arg

    obs, arg = max_absrho()
    print(f'  ═══ 观察最大 |ρ| = 【{obs:.5f}】（{arg[0]} · H={arg[1]}m）═══')
    # 逐单元详表
    print(f'    {"pair":<18}{"H":>4}{"n":>8}{"ρ":>11}{"毛效应‰":>11}{"效应/成本":>11}')
    for pr, (ts, obi, mid, n, px) in data.items():
        for H in HS:
            fwd = np.array([(px.get(t + H * 60000, np.nan) - px.get(t, np.nan)) / px.get(t, np.nan)
                            if px.get(t) else np.nan for t in ts])
            m = ~np.isnan(fwd)
            x, y = obi[m], fwd[m]
            if len(x) < 200:
                continue
            r = st.spearmanr(x, y)[0]
            g = float(np.nanmean(np.sign(x) * y) * 1000)
            print(f'    {pr:<18}{H:>4}{len(x):>8,}{r:>+11.5f}{g:>+11.5f}{g/(COST_BP/10):>10.2f}×')
    print()
    print(f'  ═══ 置换零分布（{NPERM} 次 · 循环时移 · 每次在所有单元取最大 |ρ|）═══')
    nulls = []
    for _ in range(NPERM):
        off = int(RNG.integers(10, 2000))
        b, _ = max_absrho(shift=off)
        nulls.append(b)
    nulls = np.array(nulls)
    p = (np.sum(nulls >= obs) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p = {p:.4f}】')
    print()
    print('  ═══ 判定 ═══')
    if p > 0.05:
        print(f'    🔴 观察 max|ρ| = {obs:.5f} 落在零分布内 ⇒ 【盘口深度不平衡【无】可检出预测力】')
    else:
        print(f'    ⚠️ 观察 max|ρ| = {obs:.5f} 超出零分布 ⇒ 【存在统计真实的关系】')
        print(f'    ⇒ 但仍须看【经济量级】: 毛效应须 ≥ {COST_BP/10:.2f}‰ 才可交易')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
