#!/usr/bin/env python3
"""条件扫描: flow_imbal 的预测关系是否在某【子集】里显著变大？

背景（R337）: 无条件关系统计真实（置换 p=0.005）但极小（ρ≈0.032）且符号为负，
              量级差成本门槛 13–45 倍。⇒ 追问: 是否条件化后（极端值/时段/波动率）变大？

严格性
------
- 每个单元报告【效应量】与【组的 |ρ|】，不是只看 p 值（R302 的经济显著性要求）
- 零分布: 用【循环时移前向收益】做 300 次，**每次在所有单元上取最大 |统计量|**
  ⇒ 得到【family-wise 校正后】的临界值（避免"扫很多格子挑最大"的伪影）
- 成本门槛: 0.6‰（6bp 往返）；报告 效应 / 成本 比值

只读。数据: orderflow 5m · BTC+ETH
"""
import os
import json
import numpy as np
import psycopg
from scipy import stats as st

H = 1                     # 5m 前向
COST_BP = 6.0             # 往返成本（bp）
NPERM = 300
RNG = np.random.default_rng(312)


def fnum(e, k):
    try:
        return float(e.get(k))
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
    ok = np.ones(len(ts), bool)
    ok[1:] = np.diff(ts) == 300000
    fi = np.array([fnum(e, 'flow_imbal') for e in ex])
    fwd = np.full(len(cl), np.nan)
    fwd[:-H] = (cl[H:] - cl[:-H]) / cl[:-H]
    # 用于条件分组的辅助量
    vol = np.full(len(cl), np.nan)
    ret = np.full(len(cl), np.nan)
    ret[1:] = np.diff(np.log(cl))
    for i in range(50, len(cl)):
        vol[i] = np.nanstd(ret[i - 50:i])
    hour = (ts // 3600000) % 24
    m = ok & ~np.isnan(fwd) & ~np.isnan(fi) & ~np.isnan(vol)
    return dict(ts=ts[m], fi=fi[m], fwd=fwd[m], vol=vol[m], hour=hour[m])


def cells(d):
    """生成 (单元名, 布尔掩码) 列表"""
    out = []
    a = np.abs(d['fi'])
    q = np.percentile(a, [50, 80, 90, 95, 99])
    out.append(('全部', np.ones(len(a), bool)))
    out.append(('|fi|>p50', a > q[0]))
    out.append(('|fi|>p80', a > q[1]))
    out.append(('|fi|>p90', a > q[2]))
    out.append(('|fi|>p95', a > q[3]))
    out.append(('|fi|>p99', a > q[4]))
    v = d['vol']
    vq = np.percentile(v, [20, 50, 80, 95])
    out.append(('低波动 Q1', v <= vq[0]))
    out.append(('高波动 Q5', v > vq[3]))
    for lo, hi, lab in ((0, 8, '时段 00-08Z'), (8, 16, '时段 08-16Z'), (16, 24, '时段 16-24Z')):
        out.append((lab, (d['hour'] >= lo) & (d['hour'] < hi)))
    return out


def stat(x, y):
    """单元统计量: |Spearman ρ|"""
    good = ~(np.isnan(x) | np.isnan(y))
    if good.sum() < 500:
        return 0.0, np.nan
    r = st.spearmanr(x[good], y[good])[0]
    return abs(r), r


def main():
    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as conn:
        D = {pr: load(conn, pr) for pr in ('BTC', 'ETH')}
    for pr, d in D.items():
        print(f'  {pr}: {len(d["fi"]):,} 有效样本')
    assert all(len(d['fi']) > 10000 for d in D.values()), '样本过少（自检）'
    print()

    names = [n for n, _ in cells(D['BTC'])]
    print(f'  ═══ 观察: {len(names)} 个单元 × 2 币 ═══')
    print(f'    {"单元":<16}{"币":<6}{"n":>9}{"ρ":>10}{"毛效应‰":>11}{"效应/成本":>11}')
    obs = {}
    for nm, m in cells(D['BTC']):
        pass
    for pr, d in D.items():
        for nm, m in cells(d):
            x = d['fi'][m]
            y = d['fwd'][m]
            a, r = stat(x, y)
            obs[(pr, nm)] = (a, r, int(m.sum()))
            gross = float(np.nanmean(np.sign(x) * y) * 1000)   # 跟随信号方向的毛效应
            print(f'    {nm:<16}{pr:<6}{int(m.sum()):>9,}{r:>+10.4f}{gross:>+11.4f}{gross/(COST_BP/10):>10.2f}×')
    print()
    obs_max = max(v[0] for v in obs.values())
    print(f'  ═══ 观察最大 |ρ| = {obs_max:.5f} ═══')
    print()
    print(f'  ═══ 置换零分布（{NPERM} 次 · 循环时移 · 每次在【所有单元】上取最大 |ρ|）═══')
    nulls = []
    for _ in range(NPERM):
        mx = 0.0
        for pr, d in D.items():
            off = int(RNG.integers(20, len(d['fwd']) // 3))
            y2 = np.roll(d['fwd'], -off)
            for nm, m in cells(d):
                a, _ = stat(d['fi'][m], y2[m])
                if a > mx:
                    mx = a
        nulls.append(mx)
    nulls = np.array(nulls)
    p = (np.sum(nulls >= obs_max) + 1) / (NPERM + 1)
    print(f'    零分布: 均值 {nulls.mean():.5f} · 中位 {np.median(nulls):.5f} · '
          f'95% {np.percentile(nulls,95):.5f} · 最大 {nulls.max():.5f}')
    print(f'    ⇒ 【family-wise p（观察 ≥ 零分布）= {p:.4f}】')
    print()
    print('  ═══ 判定 ═══')
    if p <= 0.05:
        best = max(obs.items(), key=lambda kv: kv[1][0])
        print(f'    ⚠️ 最强单元 {best[0][1]}({best[0][0]}) |ρ|={best[1][0]:.5f} 超出零分布')
        print(f'    ⇒ 但需同时看【经济量级】: 毛效应须 ≥ {COST_BP/10:.2f}‰ 才可交易')
    else:
        print(f'    🔴 所有条件单元的最大 |ρ| = {obs_max:.5f} 落在零分布内')
        print(f'    ⇒ 【条件化【没有】产生超出噪声的更强关系】')
        print(f'    ⇒ 【⇒ 关系是真实的但均匀地小；不存在"某个子集里很大"的情形】')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
