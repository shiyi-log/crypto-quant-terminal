#!/usr/bin/env python3
"""验证「信号搜索穷尽」本身：观察到的【最大 |ρ|】是否超出【多重检验零分布】？

设计
----
- 特征: orderflow 5m 的 7 个独立量（去掉与 flow_imbal 完全冗余的 _q/_base/buy_ratio 中的重复）
- 目标: 前向收益 H ∈ {1,3,6} 根（5m/15m/30m）
- 统计量: 所有 (币 × 持有期 × 特征) 上的【最大 |Spearman ρ|】
- 零假设: 信号与前向收益无关。用【循环时移前向收益】构造零分布
  （保持收益的自相关与波动聚集，只破坏与信号的对应关系）
- 判定: 若观察值落在零分布内 ⇒ 「最强信号」无法与多重检验的噪声最大值区分
        ⇒ 「搜索穷尽」在【方法论层面】得到加固

只读。数据: quant.candles candle_type='orderflow' timeframe='5m'
"""
import os, json, sys
import numpy as np
import psycopg
from scipy import stats as st

FEATS = ['flow_imbal', 'buy_ratio', 'avg_trade_sz', 'num_trades',
         'quote_volume', 'sell_volume', 'taker_buy_quote']
HS = (1, 3, 6)
SUB = 2          # 子采样步长（控时）
NPERM = 200
RNG = np.random.default_rng(311)


def fnum(e, k):
    v = e.get(k)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def load(conn, pr):
    rows = list(conn.execute(
        """select timestamp, close, extras from quant.candles
           where exchange='binance' and candle_type='orderflow' and timeframe='5m'
             and split_part(pair,'/',1)=%s order by timestamp""", (pr,)))
    ts = np.array([int(r[0]) for r in rows])
    cl = np.array([float(r[1]) for r in rows])
    ex = [r[2] if isinstance(r[2], dict) else json.loads(r[2]) for r in rows]
    F = {f: np.array([fnum(e, f) for e in ex]) for f in FEATS}
    # 只保留时间戳连续的段
    ok = np.ones(len(ts), bool)
    ok[1:] = np.diff(ts) == 300000
    return ts[ok], cl[ok], {f: F[f][ok] for f in FEATS}


def max_absrho(ts, cl, F):
    """在 (持有期 × 特征) 上取最大 |ρ|"""
    t = ts[::SUB]
    c = cl[::SUB]
    Fs = {f: F[f][::SUB] for f in FEATS}
    best = 0.0
    for H in HS:
        fwd = np.full(len(c), np.nan)
        for i in range(len(c) - H):
            if t[i + H] - t[i] == H * SUB * 300000:
                fwd[i] = (c[i + H] - c[i]) / c[i]
        m = ~np.isnan(fwd)
        for f in FEATS:
            x = Fs[f][m]
            y = fwd[m]
            good = ~(np.isnan(x) | np.isnan(y))
            if good.sum() < 1000:
                continue
            r = abs(st.spearmanr(x[good], y[good])[0])
            if r > best:
                best = r
    return best


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        panels = {}
        for pr in ('BTC', 'ETH'):
            ts, cl, F = load(conn, pr)
            panels[pr] = (ts, cl, F)
            print(f'  {pr}: 连续段 {len(ts):,} 行（子采样后 {len(ts[::SUB]):,}）')
    print()

    # 逐 (币,持有期,特征) 的观察值
    obs = {}
    for pr, (ts, cl, F) in panels.items():
        t = ts[::SUB]
        c = cl[::SUB]
        Fs = {f: F[f][::SUB] for f in FEATS}
        for H in HS:
            fwd = np.full(len(c), np.nan)
            for i in range(len(c) - H):
                if t[i + H] - t[i] == H * SUB * 300000:
                    fwd[i] = (c[i + H] - c[i]) / c[i]
            m = ~np.isnan(fwd)
            for f in FEATS:
                x = Fs[f][m]
                y = fwd[m]
                good = ~(np.isnan(x) | np.isnan(y))
                if good.sum() < 1000:
                    continue
                obs[(pr, H, f)] = abs(st.spearmanr(x[good], y[good])[0])

    if not obs:
        print('  !! 无有效观察值（自检失败）'); return 1
    assert len(obs) >= 20, f'观察单元过少: {len(obs)}'
    obs_max = max(obs.values())
    arg = max(obs, key=obs.get)
    print('  ═══ 观察值（orderflow 7 特征 × 3 持有期 × 2 币）═══')
    print(f'    最大 |ρ| = 【{obs_max:.5f}】（{arg[0]} · H={arg[1]} · {arg[2]}）')
    for k, v in sorted(obs.items(), key=lambda x: -x[1])[:5]:
        print(f'      {v:.5f}  {k[0]} H={k[1]} {k[2]}')
    print()

    print(f'  ═══ 置换零分布（{NPERM} 次 · 循环时移前向收益 · 每次在【同一批单元】上取最大）═══')
    nulls = []
    for _ in range(NPERM):
        mx = 0.0
        for pr, (ts, cl, F) in panels.items():
            t = ts[::SUB]
            off = int(RNG.integers(50, len(cl) // 3))
            c = np.roll(cl, -off)[::SUB]
            Fs = {f: F[f][::SUB] for f in FEATS}
            for H in HS:
                fwd = np.full(len(c), np.nan)
                for i in range(len(c) - H):
                    if t[i + H] - t[i] == H * SUB * 300000:
                        fwd[i] = (c[i + H] - c[i]) / c[i]
                m = ~np.isnan(fwd)
                for f in FEATS:
                    x = Fs[f][m]
                    y = fwd[m]
                    good = ~(np.isnan(x) | np.isnan(y))
                    if good.sum() < 1000:
                        continue
                    r = abs(st.spearmanr(x[good], y[good])[0])
                    if r > mx:
                        mx = r
        nulls.append(mx)
    nulls = np.array(nulls)
    p = (np.sum(nulls >= obs_max) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls, 95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【p（观察 ≥ 零分布）= {p:.4f}】')
    print()
    print('  ═══ 判定 ═══')
    if p > 0.05:
        print(f'    🔴 观察到的最大 |ρ| = {obs_max:.5f}【落在零分布内】')
        print(f'    ⇒ 「最强信号」【无法与多重检验的噪声最大值区分】')
        print(f'    ⇒ 「信号搜索穷尽」的结论【在方法论层面得到加固】')
    else:
        print(f'    ⚠️ 观察值超出零分布 ⇒ 可能存在真实关系，需进一步核查')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
