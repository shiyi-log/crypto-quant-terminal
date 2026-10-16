#!/usr/bin/env python3
"""盘口深度【分档位】的 OBI 与 d_obi —— 检验「用全部 20 档是否稀释了信号」。

动机
----
实测该盘口第 1 档深度是第 3–20 档的 100–300 倍（0.315 vs 0.001–0.003）
⇒ 把 20 档【全部相加】(R344/R347 的做法) 会严重稀释第 1 档的信息。
微观结构文献亦指出【近档】深度最具信息量。

设计
----
对 k ∈ {1, 3, 5, 10, 20} 分别计算:
  obi_k  = (Σ_{i<k} bid_size − Σ_{i<k} ask_size) / (Σ + Σ)      （水平）
  d_obi_k= obi_k(分钟末) − obi_k(分钟初)                          （变化）
目标: 未来 1 / 5 分钟收益。置换校准用 max-statistic 覆盖【全部 (k, 特征, 持有期)】单元
（即把「选 k」这一搜索本身计入多重检验）。

只读。数据: quant.orderbooks + quant.candles
"""
import os
import time
import numpy as np
import psycopg
from scipy import stats as st

PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')
KS = (1, 3, 5, 10, 20)
HS = (1, 5)
DAYS = 7
NPERM = 200
COST_BP = 6.0
RNG = np.random.default_rng(323)

SUMS = ',\n         '.join(
    f"(select sum((e->>1)::numeric) from jsonb_array_elements(bids) with ordinality o(e, i) where i <= {k}) as bd{k},\n"
    f"         (select sum((e->>1)::numeric) from jsonb_array_elements(asks) with ordinality o(e, i) where i <= {k}) as ad{k}"
    for k in KS)

SQL = f"""
with snap as (
  select (timestamp/60000)*60000 as minute, timestamp,
         {SUMS}
  from quant.orderbooks
  where pair = %s and timestamp >= %s
),
f as (select distinct on (minute) * from snap order by minute, timestamp),
l as (select distinct on (minute) * from snap order by minute, timestamp desc)
select f.minute,
       {', '.join(f'f.bd{k}, f.ad{k}, l.bd{k}, l.ad{k}' for k in KS)}
from f join l using (minute)
order by f.minute
"""


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        mx = conn.execute("select max(timestamp) from quant.orderbooks").fetchone()[0]
        since = int(mx) - DAYS * 86400000
        data = {}
        for pr in PAIRS:
            t0 = time.time()
            rows = conn.execute(SQL, (pr, since)).fetchall()
            d = dict(ts=np.array([int(r[0]) for r in rows], dtype=np.int64))
            col = 1
            saf = lambda a: np.where(np.abs(a) < 1e-12, 1e-12, a)
            for k in KS:
                bf = np.array([float(r[col]) for r in rows]); col += 1
                af = np.array([float(r[col]) for r in rows]); col += 1
                bl = np.array([float(r[col]) for r in rows]); col += 1
                al = np.array([float(r[col]) for r in rows]); col += 1
                d[f'obi{k}'] = (bf - af) / saf(bf + af)
                d[f'd_obi{k}'] = (bl - al) / saf(bl + al) - d[f'obi{k}']
            d['px'] = {int(r[0]): float(r[1]) for r in conn.execute(
                """select timestamp, close from quant.candles
                   where exchange='binance' and candle_type='futures' and timeframe='1m'
                     and pair=%s order by timestamp""", (pr,))}
            data[pr] = d
            print(f'  {pr}: {len(d["ts"]):,} 分钟 · {time.time()-t0:.1f}s')

    FEATS = tuple([f'obi{k}' for k in KS] + [f'd_obi{k}' for k in KS])

    def fwd(pr, H):
        d = data[pr]
        return np.array([(d['px'].get(t + H * 60000, np.nan) - d['px'].get(t, np.nan)) / d['px'].get(t, np.nan)
                         if d['px'].get(t) else np.nan for t in d['ts']])

    chk = [np.nanmean(fwd(pr, H)[~np.isnan(fwd(pr, H))]) * 1e4 for pr in PAIRS for H in HS]
    print(f'  【自检】随机锚点毛收益均值 {np.mean(chk):+.3f} bp')
    assert abs(np.mean(chk)) < 3.0, '自检失败'
    print('  ✅ 自检通过')
    print()

    print('  ═══ 逐档位（只列 |ρ| 最大的若干）═══')
    print(f'    {"pair":<18}{"feat":<11}{"H":>4}{"n":>8}{"ρ":>11}{"毛效应‰":>10}{"效应/成本":>10}')
    recs = []
    for pr in PAIRS:
        for f in FEATS:
            for H in HS:
                y = fwd(pr, H)
                m = ~np.isnan(y)
                x, yy = data[pr][f][m], y[m]
                if len(x) < 200:
                    continue
                r = st.spearmanr(x, yy)[0]
                g = float(np.nanmean(np.sign(x - np.median(x)) * yy) * 1000)
                recs.append((abs(r), pr, f, H, len(x), r, g))
    recs.sort(reverse=True)
    for a, pr, f, H, n, r, g in recs[:14]:
        print(f'    {pr:<18}{f:<11}{H:>4}{n:>8,}{r:>+11.5f}{g:>+10.5f}{g/(COST_BP/10):>9.3f}×')

    def max_absrho(shift=None):
        best = 0.0; arg = None
        for pr in PAIRS:
            for f in FEATS:
                for H in HS:
                    y = fwd(pr, H)
                    m = ~np.isnan(y)
                    x, yy = data[pr][f][m], y[m]
                    if len(x) < 200:
                        continue
                    if shift is not None:
                        yy = np.roll(yy, -shift)
                    r = abs(st.spearmanr(x, yy)[0])
                    if r > best:
                        best, arg = r, (pr, f, H)
        return best, arg

    obs, arg = max_absrho()
    print()
    print(f'  ═══ 观察最大 |ρ| = 【{obs:.5f}】（{arg[0]} · {arg[1]} · H={arg[2]}m）═══')
    nulls = np.array([max_absrho(shift=int(RNG.integers(10, 2000)))[0] for _ in range(NPERM)])
    p = (np.sum(nulls >= obs) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p = {p:.4f}】（已覆盖 20 个单元与 k 的选择）')
    print()
    print('  ═══ 各 k 的最优 d_obi（对比 R347 的 k=20）═══')
    for k in KS:
        best = max((r for r in recs if r[2] == f'd_obi{k}'), default=None,
                   key=lambda x: x[0]) if any(r[2] == f'd_obi{k}' for r in recs) else None
        if best:
            print(f'    k={k:<3} 最优 |ρ| = {best[0]:.5f} ({best[1]} H={best[3]}m) · 毛效应 {best[6]:+.5f}‰')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
