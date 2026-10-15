"""逐笔成交订单流失衡（OFI）的预测力检验。

背景: ticks 表有 2,124 万行逐笔成交（含 buyer_maker ⇒ 主动方），从未被用作细粒度信号。
      lab 的 s07 用 15m 粒度的 taker_buy/volume；本测试检验【分钟级】是否有额外信息。

方法:
  ① 按分钟聚合 futures 逐笔: buy_vol（buyer_maker=False）/ sell_vol（True）
  ② OFI = (buy − sell) / (buy + sell)
  ③ 用 5m K 线算 15m/1h/4h 前瞻收益；市场中性 = 该币 − BTC 同期
  ④ 检验: Pearson / Spearman 秩相关 · 前后两半同号 · 逐日单日占比 · 组差 vs 往返费率 4bp
"""
import os, json, collections, bisect
import numpy as np
import datetime as dt
import psycopg

PAIRS = ('ETH/USDT:USDT', 'TRX/USDT:USDT')
HOR = {'15m': 3, '1h': 12, '4h': 48}      # 5m 根数
DAYS = float(os.environ.get('TK_DAYS', '7'))


def spearman(x, y):
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

# 5m K 线
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
print(f"  ══ 逐笔订单流失衡（OFI）· 窗口 {DAYS:g} 天 ══")
print(f"    {'币种':<6}{'尺度':>5}{'分钟数':>8}{'n':>7}{'Pearson':>10}{'Spearman':>10}"
      f"{'前半ρ':>9}{'后半ρ':>9}{'高组bp':>9}{'低组bp':>9}{'差bp':>8}  判定")

for pair in PAIRS:
    if pair not in kld:
        continue
    # 逐笔聚合到分钟
    rows = conn.execute("""select (timestamp/60000)*60000 as bt,
                                  sum(case when buyer_maker then 0 else quantity end) as bv,
                                  sum(case when buyer_maker then quantity else 0 end) as sv,
                                  count(*) as n
            from quant.ticks
            where exchange='binance' and market='futures' and pair=%s and timestamp > %s
            group by 1 order by 1""", (pair, since)).fetchall()
    ofi = {}
    for bt, bv, sv, n in rows:
        bv = float(bv or 0); sv = float(sv or 0)
        if bv + sv > 0:
            ofi[int(bt)] = (bv - sv) / (bv + sv)
    bt_arr = np.array(sorted(ofi), dtype=np.int64)
    tsp, opp, clp = kld[pair]
    print(f"    {pair}: 分钟桶 {len(bt_arr)} 个（有成交的分钟）")
    for hn, bars in HOR.items():
        X, Y, D = [], [], []
        for bt in bt_arr:
            if bt < tsp[0] or bt < bts[0]:
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
            if abs(rr) > 30 or abs(br) > 30:
                continue
            X.append(ofi[int(bt)]); Y.append(rr - br)
            D.append(dt.datetime.fromtimestamp(bt / 1000, dt.timezone.utc).strftime('%m-%d'))
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
        print(f"    {pair[:6]:<6}{hn:>5}{len(bt_arr):>8}{len(X):>7}{pc:>+10.4f}{sc:>+10.4f}"
              f"{s1:>+9.3f}{s2:>+9.3f}{yh:>+9.2f}{yl:>+9.2f}{yh-yl:>+8.2f}"
              f"  单日{mx:.0f}%  {'✅' if ok else '❌'}")
conn.close()
print()
print("  ══ 判定: 两半同号 · |Spearman|>0.03 · |组差|>4bp · 单日<60% ══")
