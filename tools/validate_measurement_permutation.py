"""元要求 A: 随机对照 —— 验证「funding 预测力」的测量管线本身。

做法: 把 funding 值【打乱】（置换），保持时间戳与分布不变，
      重跑同一管线。若管线无偏，置换后的相关系数应≈0 且两半不同号。

同时做 20 次置换，给出 corr 的经验分布，用以判断真实 corr 是否超出噪声范围。
"""
import os, bisect, collections
import numpy as np
import datetime as dt
import psycopg

PAIRS = ('ETH/USDT:USDT', 'TRX/USDT:USDT')
HOR = {'8h': 8, '24h': 24, '72h': 72}
SRC = 'funding_rate'
NPERM = int(os.environ.get('NPERM', '20'))


def spearman(x, y):
    if len(x) < 30:
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
    if rows:
        kld[pair] = (np.array([int(r[0]) for r in rows], dtype=np.int64),
                     np.array([float(r[1]) for r in rows]),
                     np.array([float(r[2]) for r in rows]))
bts, bop, bcl = kld['BTC/USDT:USDT']
rng = np.random.default_rng(11100)

print("  ══ 随机对照（置换检验）· 检验测量管线是否有偏 ══")
print(f"    {'币种':<6}{'尺度':>5}{'n':>6}{'真实ρ':>9}{'置换均值':>10}{'置换标准差':>11}"
      f"{'|真实|/σ':>10}  判定")
for pair in PAIRS:
    rows = conn.execute("""select timestamp, value from quant.series_points
            where metric=%s and asset=%s order by timestamp""", (SRC, pair)).fetchall()
    fund = {int(t): float(v) for t, v in rows if v is not None}
    tsp, opp, clp = kld[pair]
    ft = np.array(sorted(fund), dtype=np.int64)
    for hn, bars in HOR.items():
        X, Y = [], []
        for t in ft:
            if t < tsp[0] or t < bts[0] or t > tsp[-1]:
                continue
            ni = bisect.bisect_right(tsp, t); bni = bisect.bisect_right(bts, t)
            if ni + bars >= len(tsp) or bni + bars >= len(bts):
                continue
            e, be = opp[ni], bop[bni]
            if e <= 0 or be <= 0:
                continue
            rr = (clp[ni + bars] / e - 1) * 100
            br = (bcl[bni + bars] / be - 1) * 100
            if abs(rr) > 60 or abs(br) > 60:
                continue
            X.append(fund[t]); Y.append(rr - br)
        if len(X) < 200:
            continue
        X = np.array(X); Y = np.array(Y)
        real = spearman(X, Y)
        perm = []
        for _ in range(NPERM):
            perm.append(spearman(rng.permutation(X), Y))
        perm = np.array([p for p in perm if not np.isnan(p)])
        mu = perm.mean(); sd = perm.std()
        z = abs(real - mu) / sd if sd > 0 else float('nan')
        verdict = "✅ 超出噪声" if z > 3 else ("⚠️ 接近噪声" if z > 2 else "❌ 与噪声不可分")
        print(f"    {pair[:6]:<6}{hn:>5}{len(X):>6}{real:>+9.4f}{mu:>+10.4f}{sd:>11.4f}{z:>10.2f}  {verdict}")
        # 两半一致性（置换后应不同号）
        k = len(X) // 2
        s1r = spearman(X[:k], Y[:k]); s2r = spearman(X[k:], Y[k:])
        s1p = []; s2p = []
        for _ in range(NPERM):
            xp = rng.permutation(X)
            s1p.append(spearman(xp[:k], Y[:k])); s2p.append(spearman(xp[k:], Y[k:]))
        s1p = np.array([x for x in s1p if not np.isnan(x)]); s2p = np.array([x for x in s2p if not np.isnan(x)])
        print(f"      {'':<11}真实两半 {s1r:+.3f}/{s2r:+.3f}"
              f" · 置换两半均值 {s1p.mean():+.3f}/{s2p.mean():+.3f}")
conn.close()
print()
print("  ══ 判读 ══")
print("    置换均值应≈0 ⇒ 管线无偏；置换两半均值应≈0 且不系统同号")
print("    真实 ρ 只有在 |z|>3 且两半同号时才值得进一步检验")
