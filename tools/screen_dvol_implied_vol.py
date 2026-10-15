"""Deribit dvol（隐含波动率指数）的预测力检验。

数据: quant.series_points metric='dvol' · BTC/ETH 各 1,000 个日度观测
      覆盖 2024-01-13 → 2026-10-08（约 2.7 年）—— 本会话历史最长的数据源。

与已否证的"波动率过滤器"的区别: 那是【已实现】波动率，这里 dvol 是【隐含】波动率。

三个假设:
  ① 水平: 高 dvol（高不确定性）⇒ 后续收益方向/幅度
  ② 变化: Δdvol ⇒ 后续收益
  ③ 方差风险溢价（VRP = dvol − 已实现波动率）⇒ 后续收益
     （经典预测因子: 期权贵 ⇒ 风险溢价高 ⇒ 后续收益？）
"""
import os, bisect
import numpy as np
import datetime as dt
import psycopg


def spearman(x, y):
    if len(x) < 20:
        return float('nan')
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return float('nan')
    return float(np.corrcoef(rx, ry)[0, 1])


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])
# 1h K 线（算已实现波动率与前瞻收益）
kld = {}
for pair in ('BTC/USDT:USDT', 'ETH/USDT:USDT'):
    rows = conn.execute("""select timestamp, close from quant.candles
            where exchange='binance' and candle_type='futures' and timeframe='1h'
              and pair=%s order by timestamp""", (pair,)).fetchall()
    kld[pair] = (np.array([int(r[0]) for r in rows], dtype=np.int64),
                 np.array([float(r[1]) for r in rows]))
dvol = {}
for a, ts, v in conn.execute("""select asset, timestamp, value from quant.series_points
        where metric='dvol' order by timestamp"""):
    dvol.setdefault(a, []).append((int(ts), float(v)))
conn.close()

print("  ══ dvol 预测力检验（日度 · 假设 ①②③）══")
print(f"    {'币':<5}{'假设':<26}{'n':>6}{'Spearman':>10}{'前半ρ':>9}{'后半ρ':>9}"
      f"{'高分位bp':>10}{'低分位bp':>10}{'差bp':>9}{'单日%':>7}  判定")

for asset, pair in (('BTC', 'BTC/USDT:USDT'), ('ETH', 'ETH/USDT:USDT')):
    dv = sorted(dvol.get(asset, []))
    if len(dv) < 200:
        continue
    ts, cl = kld[pair]
    # 1h 对数收益
    lr = np.diff(np.log(cl))
    rts = ts[1:]
    # 日度已实现波动率（过去 24 根 1h 的标准差）
    rv = np.array([lr[max(0, i - 23):i + 1].std() for i in range(len(lr))])
    recs = []
    for i, (t, v) in enumerate(dv):
        ni = bisect.bisect_right(rts, t)
        if ni < 25 or ni + 24 >= len(rts):
            continue
        fwd = (cl[bisect.bisect_right(ts, t) + 24] / cl[bisect.bisect_right(ts, t)] - 1) * 100 \
            if bisect.bisect_right(ts, t) + 24 < len(cl) else None
        if fwd is None or abs(fwd) > 60:
            continue
        j = bisect.bisect_right(rts, t) - 1
        realized = rv[j] * np.sqrt(24) * 100     # 日度已实现波动率（%）
        prev = dv[i - 1][1] if i > 0 else v
        recs.append((v, v - prev, v - realized, fwd,
                     dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%m-%d')))
    if len(recs) < 200:
        continue
    for label, idx in (('① 水平 dvol', 0), ('② Δdvol', 1), ('③ 方差风险溢价', 2)):
        X = np.array([r[idx] for r in recs]); Y = np.array([r[3] for r in recs])
        if X.std() == 0:
            continue
        sc = spearman(X, Y)
        k = len(X) // 2
        s1 = spearman(X[:k], Y[:k]); s2 = spearman(X[k:], Y[k:])
        hi = X >= np.quantile(X, 0.8); lo = X <= np.quantile(X, 0.2)
        yh = Y[hi].mean() * 100; yl = Y[lo].mean() * 100
        days = [r[4] for r in recs]
        import collections
        byday = collections.defaultdict(float)
        for i2 in np.where(hi)[0]:
            byday[days[i2]] += Y[i2]
        tot = sum(byday.values())
        mx = max(abs(v / tot * 100) for v in byday.values()) if tot else float('nan')
        ok = (s1 * s2 > 0) and abs(sc) > 0.05 and abs(yh - yl) > 4 and mx < 60
        print(f"    {asset:<5}{label:<26}{len(X):>6}{sc:>+10.4f}{s1:>+9.3f}{s2:>+9.3f}"
              f"{yh:>+10.2f}{yl:>+10.2f}{yh-yl:>+9.2f}{mx:>6.0f}%  {'✅' if ok else '❌'}")
print()
print("  ══ 判据: 两半同号 · |ρ|>0.05 · |差|>4bp · 单日<60% ══")
