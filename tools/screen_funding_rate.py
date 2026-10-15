"""资金费率（funding_rate）的预测力检验。

背景: quant.series_points 有 funding_rate 236,558 条，每币 7,400+ 条、
      8 小时一条、覆盖 01-01 → 10-08（10 个月）。该数据【未接入任何策略】。

假设: 高资金费率 = 多头拥挤 ⇒ 后续相对跑输（对持仓的均值回归）。

方法:
  ① 取 BTC/ETH/TRX 的 funding_rate
  ② 用 1h K 线算 forward 8h / 24h / 72h 收益（1h 覆盖 01-01 → 10-15）
  ③ 市场中性 = 该币收益 − BTC 同期
  ④ 检验: Pearson / Spearman · 前后两半 · 逐日单日占比 · 组差 vs 往返费率 4bp
"""
import os, json, bisect, collections
import numpy as np
import datetime as dt
import psycopg

PAIRS = ('ETH/USDT:USDT', 'TRX/USDT:USDT')
HOR = {'8h': 8, '24h': 24, '72h': 72}      # 1h 根数
SRC = os.environ.get('FUND_METRIC', 'funding_rate')


def spearman(x, y):
    if len(x) < 30:
        return float('nan')
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return float('nan')
    return float(np.corrcoef(rx, ry)[0, 1])


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])

# 1h K 线
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
print(f"  ══ 资金费率预测力 · metric={SRC} ══")
print(f"  1h K 线覆盖: {dt.datetime.fromtimestamp(bts[0]/1000,dt.timezone.utc):%m-%d} → "
      f"{dt.datetime.fromtimestamp(bts[-1]/1000,dt.timezone.utc):%m-%d}（{len(bts)} 根）")
print(f"    {'币种':<6}{'尺度':>5}{'n':>7}{'Pearson':>10}{'Spearman':>10}"
      f"{'前半ρ':>9}{'后半ρ':>9}{'高费率组bp':>12}{'低费率组bp':>12}{'差bp':>9}{'单日%':>7}  判定")

for pair in PAIRS:
    if pair not in kld:
        continue
    rows = conn.execute("""select timestamp, value from quant.series_points
            where metric=%s and asset=%s order by timestamp""", (SRC, pair)).fetchall()
    fund = {int(t): float(v) for t, v in rows if v is not None}
    if len(fund) < 200:
        print(f"    {pair}: funding 样本不足（{len(fund)}）")
        continue
    tsp, opp, clp = kld[pair]
    ft = np.array(sorted(fund), dtype=np.int64)
    print(f"    {pair}: funding {len(ft)} 条 · "
          f"{dt.datetime.fromtimestamp(ft[0]/1000,dt.timezone.utc):%m-%d} → "
          f"{dt.datetime.fromtimestamp(ft[-1]/1000,dt.timezone.utc):%m-%d}")
    for hn, bars in HOR.items():
        X, Y, D = [], [], []
        for t in ft:
            if t < tsp[0] or t < bts[0] or t > tsp[-1]:
                continue
            ni = bisect.bisect_right(tsp, t)
            bni = bisect.bisect_right(bts, t)
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
            D.append(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%m-%d'))
        if len(X) < 200:
            continue
        X = np.array(X); Y = np.array(Y)
        if X.std() == 0:
            continue
        pc = float(np.corrcoef(X, Y)[0, 1]); sc = spearman(X, Y)
        k = len(X) // 2
        s1 = spearman(X[:k], Y[:k]); s2 = spearman(X[k:], Y[k:])
        hi = X >= np.quantile(X, 0.8); lo = X <= np.quantile(X, 0.2)
        yh = Y[hi].mean() * 100; yl = Y[lo].mean() * 100
        byday = collections.defaultdict(float)
        for i in np.where(hi)[0]:
            byday[D[i]] += Y[i]
        tot = sum(byday.values())
        mx = max(abs(v / tot * 100) for v in byday.values()) if tot else float('nan')
        ok = (s1 * s2 > 0) and mx < 60 and abs(sc) > 0.03 and abs(yh - yl) > 4
        print(f"    {pair[:6]:<6}{hn:>5}{len(X):>7}{pc:>+10.4f}{sc:>+10.4f}"
              f"{s1:>+9.3f}{s2:>+9.3f}{yh:>+12.2f}{yl:>+12.2f}{yh-yl:>+9.2f}{mx:>6.0f}%"
              f"  {'✅' if ok else '❌'}")
conn.close()
print()
print("  ══ 判定: 两半同号 · |Spearman|>0.03 · |组差|>4bp · 单日<60% ══")
