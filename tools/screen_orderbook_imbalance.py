"""订单簿失衡 · 最终干净检验（秩相关 + 离群防护 + 完整诊断）。

修正前两次的 bug:
  ① 订单簿 8 天 vs 1m K 线仅 2 天 ⇒ 早期分钟产生常数样本（已加区间守卫）
  ② Y 的 BTC 超额项出现极端值 ⇒ 用【秩相关】(Spearman) 免疫离群点

严格口径: 每分钟最后一条快照 → 下一根 1m K 线开盘入场；超额 = 该币 − BTC 同期。
"""
import os, json, bisect
import numpy as np
import datetime as dt
import psycopg

PAIRS = ('ETH/USDT:USDT', 'TRX/USDT:USDT')
LEVELS = (5, 20)
HOR = {'1h': 12, '4h': 48}   # 5m 根数
DAYS = float(os.environ.get('OB_DAYS', '8'))
TOP = 20


def J(v):
    return v if isinstance(v, list) else json.loads(v)


def spearman(x, y):
    """秩相关（对离群点免疫）。"""
    if len(x) < 30:
        return float('nan')
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return float('nan')
    return float(np.corrcoef(rx, ry)[0, 1])


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])
N = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
since = N - int(DAYS * 86400000)

kld = {}
for pair in ('BTC/USDT:USDT',) + PAIRS:
    rows = conn.execute("""select timestamp, open, close from quant.candles
            where exchange='binance' and candle_type='futures' and timeframe='5m'
              and pair=%s and timestamp > %s order by timestamp""", (pair, since)).fetchall()
    if rows:
        kld[pair] = (np.array([int(r[0]) for r in rows], dtype=np.int64),
                     np.array([float(r[1]) for r in rows]),
                     np.array([float(r[2]) for r in rows]))
bts, bop, bcl = kld['BTC/USDT:USDT']
print(f"  ══ 最终干净检验 · 窗口 {DAYS:g} 天 ══")
print(f"  K 线实际覆盖: {dt.datetime.fromtimestamp(bts[0]/1000,dt.timezone.utc):%m-%d %H:%M}"
      f" → {dt.datetime.fromtimestamp(bts[-1]/1000,dt.timezone.utc):%m-%d %H:%M}（{len(bts)} 根 1m）")
print(f"    {'币种':<6}{'档':>4}{'尺度':>5}{'n':>6}{'Pearson':>10}{'Spearman':>10}"
      f"{'前半ρ':>9}{'后半ρ':>9}{'高组超额%':>11}{'低组超额%':>11}{'差bp':>8}  判定")
for pair in PAIRS:
    if pair not in kld:
        continue
    rows = conn.execute("""
        select distinct on ((timestamp/60000)*60000)
               (timestamp/60000)*60000 as bt, bids, asks
        from quant.orderbooks
        where market='futures' and pair=%s and timestamp > %s
        order by (timestamp/60000)*60000, timestamp desc
    """, (pair, since)).fetchall()
    buckets = {}
    for bt, b, a in rows:
        bb, aa = J(b)[:TOP], J(a)[:TOP]
        rec = {}
        for L in LEVELS:
            bq = sum(float(x[1]) for x in bb[:L]); aq = sum(float(x[1]) for x in aa[:L])
            rec[L] = (bq - aq) / (bq + aq) if bq + aq > 0 else None
        buckets[int(bt)] = rec
    tsp, opp, clp = kld[pair]
    for L in LEVELS:
        for hn, bars in HOR.items():
            X, Y = [], []
            for bt in sorted(buckets):
                if bt < tsp[0] or bt < bts[0]:
                    continue
                imb = buckets[bt].get(L)
                if imb is None:
                    continue
                ni = bisect.bisect_right(tsp, bt)
                bni = bisect.bisect_right(bts, bt)
                if ni + bars >= len(tsp) or bni + bars >= len(bts):
                    continue
                e, be = opp[ni], bop[bni]
                if e <= 0 or be <= 0:
                    continue
                rr = (clp[ni + bars] / e - 1) * 100
                br = (bcl[bni + bars] / be - 1) * 100
                if abs(rr) > 30 or abs(br) > 30:      # 离群防护
                    continue
                X.append(imb); Y.append(rr - br)
            if len(X) < 200:
                continue
            X = np.array(X); Y = np.array(Y)
            pc = float(np.corrcoef(X, Y)[0, 1])
            sc = spearman(X, Y)
            k = len(X) // 2
            s1 = spearman(X[:k], Y[:k]); s2 = spearman(X[k:], Y[k:])
            hi = X >= np.quantile(X, 0.8); lo = X <= np.quantile(X, 0.2)
            yh = Y[hi].mean() * 100; yl = Y[lo].mean() * 100
            d = yh - yl
            ok = (s1 * s2 > 0) and abs(d) > 4 and abs(sc) > 0.03
            print(f"    {pair[:6]:<6}{L:>4}{hn:>5}{len(X):>6}{pc:>+10.4f}{sc:>+10.4f}"
                  f"{s1:>+9.3f}{s2:>+9.3f}{yh:>+11.2f}{yl:>+11.2f}{d:>+8.2f}  {'✅' if ok else '❌'}")
conn.close()
print()
print("  ══ 判定: 两半秩相关同号 · |Spearman|>0.03 · |组差|>4bp（往返费率）══")
print("  ⇒ 若全部 ❌ ⇒ 订单簿失衡在 1h/4h 尺度上无可用预测力")
