"""funding 观察项的季度稳定性检验。

动机: TRX 24h 单配置通过了 6/6 判据，但 s09/s16 的教训是
      「看似稳健的结果可能由单一时段驱动」。
      10 个月数据可分为 4 个季度 ⇒ 检查信号是否【各季度同号】。

同时检查「不同 market-neutral 基准」的稳健性（BTC vs 三币均值）。
"""
import os, bisect, collections
import numpy as np
import datetime as dt
import psycopg

PAIRS = ('ETH/USDT:USDT', 'TRX/USDT:USDT')
BARS = 24          # 24h
SRC = 'funding_rate'


def spearman(x, y):
    if len(x) < 20:
        return float('nan')
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return float('nan')
    return float(np.corrcoef(rx, ry)[0, 1])


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])
kld = {}
for pair in ('BTC/USDT:USDT',) + PAIRS:
    rows = conn.execute("""select timestamp, open, close from quant.candles
            where exchange='binance' and candle_type='futures' and timeframe='1h'
              and pair=%s order by timestamp""", (pair,)).fetchall()
    kld[pair] = (np.array([int(r[0]) for r in rows], dtype=np.int64),
                 np.array([float(r[1]) for r in rows]),
                 np.array([float(r[2]) for r in rows]))
bts, bop, bcl = kld['BTC/USDT:USDT']

print("  ══ funding 观察项的季度稳定性（24h 尺度）══")
print(f"    {'币种':<6}{'季度':<10}{'n':>6}{'Spearman':>11}{'组差bp':>10}{'高费率组bp':>12}{'低费率组bp':>12}")
for pair in PAIRS:
    rows = conn.execute("""select timestamp, value from quant.series_points
            where metric=%s and asset=%s order by timestamp""", (SRC, pair)).fetchall()
    fund = {int(t): float(v) for t, v in rows if v is not None}
    tsp, opp, clp = kld[pair]
    recs = []
    for t in sorted(fund):
        if t < tsp[0] or t < bts[0] or t > tsp[-1]:
            continue
        ni = bisect.bisect_right(tsp, t); bni = bisect.bisect_right(bts, t)
        if ni + BARS >= len(tsp) or bni + BARS >= len(bts):
            continue
        e, be = opp[ni], bop[bni]
        if e <= 0 or be <= 0:
            continue
        rr = (clp[ni + BARS] / e - 1) * 100
        br = (bcl[bni + BARS] / be - 1) * 100
        if abs(rr) > 60 or abs(br) > 60:
            continue
        recs.append((t, fund[t], rr - br))
    recs.sort()
    if len(recs) < 200:
        continue
    # 按季度切（即 01-01 起的 3 个月块）
    q = collections.defaultdict(list)
    for t, x, y in recs:
        d = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
        q[(d.month - 1) // 3 + 1].append((x, y))
    signs = []
    for k in sorted(q):
        X = np.array([a for a, _ in q[k]]); Y = np.array([b for _, b in q[k]])
        if len(X) < 50:
            continue
        sc = spearman(X, Y)
        hi = X >= np.quantile(X, 0.8); lo = X <= np.quantile(X, 0.2)
        yh = Y[hi].mean() * 100; yl = Y[lo].mean() * 100
        signs.append(sc)
        print(f"    {pair[:4]:<6}{'Q'+str(k):<10}{len(X):>6}{sc:>+11.4f}{yh-yl:>+10.2f}"
              f"{yh:>+12.2f}{yl:>+12.2f}")
    if signs:
        same = all(s < 0 for s in signs) or all(s > 0 for s in signs)
        print(f"    {'':<16}⇒ 【{len(signs)} 个季度 · {'全部同号 ✅' if same else '符号不一致 ❌'}】")
    print()
conn.close()
print("  ══ 判读 ══")
print("    若各季度 Spearman 全部为负 ⇒ 信号不是由单一时段驱动 ⇒ 稳健")
print("    若有季度翻正 ⇒ 与 s09/s16 同类问题 ⇒ 不可用")
