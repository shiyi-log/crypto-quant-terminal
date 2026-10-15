"""族级「分时段稳定性」检验（报告第五节第 9 条判据的正确实施）。

背景（Round 184）：
  预注册计划 §3 步骤 ④ 原调用 `check_signal_period_stability.py`，
  但该工具读的是 `quant.candles` + `quant.series_points`（**单序列**），
  【不支持按族分析】⇒ 无法用于族级的第 9 条判据。

本脚本做正确的事：对每个族，检查其【去重后】的归一化收益
在【各时段】内的符号一致性（第 9 条 = 全部时段同号）。

⚠️ 前视声明：本检验是【描述性】的 —— 它衡量同一族在不同时段的表现符号是否一致，
   不含"选族"步骤，因此不存在切分方向的前视问题。
"""
import os
import json
import collections
import numpy as np
import psycopg

FAMS = ('s01_orb_breakout', 's02_dual_thrust', 's03_tsmom_vol_target', 's04_btc_eth_pairs',
        's05_bollinger_fade', 's06_volume_climax_reversal', 's07_surge_continuation',
        's08_wick_rejection', 's09_trend_pullback', 's10_lead_lag_btc_eth',
        's11_tsmom_bollinger_pullback', 's12_surge_expansion_breakout',
        's14_vol_compression_release', 's16_weekly_seasonality', 's17_overnight_range_fade',
        's18_drawdown_recovery', 's20_intraday_range_position', 's33_basis_momentum',
        's34_vwap_anchor_reversion', 's35_cascade_continuation')
LEGACY = ('_100x', '_2x2_', '_taker')
NSEG = int(os.environ.get('NSEG', '4'))
MIN_PER_SEG = int(os.environ.get('MIN_PER_SEG', '5'))


def J(v):
    return v if isinstance(v, dict) else json.loads(v)


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])
ded = {}
for sid, pl in conn.execute("""select strategy_id, payload from quant.strategy_sim_events
                               where event_type='trade_closed'"""):
    p = J(pl)
    if p.get('net_pnl') is None or not p.get('notional'):
        continue
    if any(k in sid for k in LEGACY) and '_100x_maker' not in sid:
        continue
    fam = next((f for f in FAMS if sid.startswith(f)), None)
    if not fam:
        continue
    oc = p.get('opened_candle_ms')
    nt = float(p['notional'])
    if not oc or nt <= 0:
        continue
    ded.setdefault((fam, int(oc)), float(p['net_pnl']) / nt * 1000)
conn.close()

byfam = collections.defaultdict(list)
for (fam, oc), v in ded.items():
    byfam[fam].append((oc, v))
for f in byfam:
    byfam[f].sort()

print(f"  ══ 族级分时段稳定性（第 9 条判据 · 去重后 · {NSEG} 等分）══")
print(f"    共 {len(byfam)} 个族 · 去重后 {len(ded)} 条观测")
print()
print(f"    {'族':<34}{'n':>5}  " + "".join(f"{'段'+str(i+1):>10}" for i in range(NSEG))
      + f"{'同号?':>8}")
npass = 0
for fam in sorted(byfam):
    recs = byfam[fam]
    if len(recs) < NSEG * MIN_PER_SEG:
        continue
    # 按时序等分
    arr = np.array([v for _, v in recs])
    idx = np.linspace(0, len(arr), NSEG + 1).astype(int)
    means = [arr[idx[i]:idx[i + 1]].mean() for i in range(NSEG)]
    neg = sum(1 for m in means if m < 0)
    same = (neg == 0 or neg == NSEG)
    if same:
        npass += 1
    print(f"    {fam[:34]:<34}{len(arr):>5}  "
          + "".join(f"{m:>+10.3f}" for m in means)
          + f"{('✅' if same else '❌'):>8}")

print()
print(f"  ══ 结果 ══")
print(f"    通过第 9 条（全部 {NSEG} 段同号）的族: {npass} 个")
print(f"    ⚠️ 零效应下偶然全部同号的概率 = {2 * 0.5**NSEG * 100:.2f}%（{NSEG} 段）")
print()
print(f"  ══ 与单序列工具的区别（重要）══")
print(f"    check_signal_period_stability.py: 单序列（candles + series_points）⇒ funding/dvol 用")
print(f"    本脚本: 【族级】（strategy_sim_events 按族去重）⇒ 第 9 条判据的正确实施")
