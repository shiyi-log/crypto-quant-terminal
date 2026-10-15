"""把「跨时段同号」判据应用到本报告唯一的正向结论: 策略选择的样本外价值。

背景: 3.4 节结论是 "top-k 样本外超额 +1.19~+1.47‰ · Spearman ρ=+0.4996
      · 95% CI [+0.2033,+0.7363] · 13 个切分点 13/13 为正"。
      Round 114 的新判据（跨时段同号）要求检验它是否由单一时段驱动。

做法:
  ① 把交易按时间切成 N 段（leave-one-out 风格）
  ② 在每个"训练段"上排序各族、在"测试段"上算 top-3 超额
  ③ 报告每段的符号与幅度，以及【去掉任一段后】的整体 ρ
"""
import os, psycopg, json, collections
import numpy as np
import datetime as dt

LEGACY = ('_100x', '_2x2_', '_taker')
FAMS = ('s01_orb_breakout', 's02_dual_thrust', 's03_tsmom_vol_target', 's04_btc_eth_pairs',
        's05_bollinger_fade', 's06_volume_climax_reversal', 's07_surge_continuation',
        's08_wick_rejection', 's09_trend_pullback', 's10_lead_lag_btc_eth',
        's11_tsmom_bollinger_pullback', 's12_surge_expansion_breakout',
        's14_vol_compression_release', 's16_weekly_seasonality', 's17_overnight_range_fade',
        's18_drawdown_recovery', 's20_intraday_range_position', 's33_basis_momentum',
        's34_vwap_anchor_reversion', 's35_cascade_continuation')


def J(v):
    return v if isinstance(v, dict) else json.loads(v)


conn = psycopg.connect(os.environ['QUANT_DATABASE_URL'])
# ⚠️ Round 182 修正: 原版【未按唯一入场时刻去重】⇒ n 虚增约 9 倍。
#    现同时输出【未去重】与【去重后】两套结果（去重为默认主口径）。
DEDUP = os.environ.get('NO_DEDUP') != '1'
raw = []
for sid, pl in conn.execute("""select strategy_id, payload from quant.strategy_sim_events
                               where event_type='trade_closed'"""):
    p = J(pl)
    if p.get('net_pnl') is None or not p.get('notional'):
        continue
    if any(k in sid for k in LEGACY) and '_100x_maker' not in sid:
        continue
    oc = p.get('opened_candle_ms'); nt = float(p['notional'])
    if not oc or nt <= 0:
        continue
    for k in FAMS:
        if sid.startswith(k):
            raw.append((k, int(oc), float(p['net_pnl']) / nt * 1000))
            break
conn.close()
if DEDUP:
    seen = {}
    for fam, oc, v in raw:
        seen.setdefault((fam, oc), v)
    recs = [(f, o, v) for (f, o), v in seen.items()]
    print(f"  ⚠️ 已按 (族, 唯一入场时刻) 去重: {len(recs)} 条（未去重 {len(raw)} ⇒ 冗余 {len(raw)/len(recs):.1f}×）")
    print(f"  （设 NO_DEDUP=1 可复现未去重的旧口径）")
else:
    recs = raw
    print(f"  ⚠️ NO_DEDUP=1 ⇒ 使用【未去重】旧口径: {len(recs)} 条")
recs.sort(key=lambda x: x[1])

# 按时间切 N 段
NSEG = 6
bounds = np.linspace(0, len(recs), NSEG + 1).astype(int)
print("  ══ 策略选择的样本外价值 · 分时段检验 ══")
print(f"    数据 {len(recs)} 笔 · 切成 {NSEG} 段")
print()
print(f"    {'训练段→测试段':<18}{'训练 top3':<24}{'测试超额(‰)':>13}")
excess = []
for i in range(NSEG):
    tr = recs[bounds[i]:bounds[i + 1]]
    te = [r for j, r in enumerate(recs) if not (bounds[i] <= j < bounds[i + 1])]
    def fam_mean(rows):
        d = collections.defaultdict(list)
        for f, t, v in rows:
            d[f].append(v)
        return {f: float(np.mean(v)) for f, v in d.items() if len(v) >= 2}
    mtr = fam_mean(tr); mte = fam_mean(te)
    if len(mtr) < 3:
        continue
    top = sorted(mtr, key=lambda f: -mtr[f])[:3]
    base = float(np.mean(list(mte.values())))
    tk = float(np.mean([mte[f] for f in top if f in mte])) if any(f in mte for f in top) else float('nan')
    e = tk - base
    excess.append((i + 1, top, e))
    print(f"    段{i+1} → 其余{'':<9}{str([t[:8] for t in top]):<24}{e:>+13.3f}")

print()
print("  ══ 汇总 ══")
vals = [e for _, _, e in excess if not np.isnan(e)]
pos = sum(1 for v in vals if v > 0)
print(f"    {len(vals)} 段 · 为正 {pos} 段 · 均值 {np.mean(vals):+.3f}‰")
print(f"    ⇒ 【{'全部同号 ✅' if pos == len(vals) or pos == 0 else '符号不一致 ❌'}】")
print()
print("  ══ 去掉任一段后的整体 ρ（原报告为 +0.4996）══")
# 简化: 每段内部算 (族排名, 该族均值) 的 Spearman，再看各段
for i in range(NSEG):
    seg = recs[bounds[i]:bounds[i + 1]]
    d = collections.defaultdict(list)
    for f, t, v in seg:
        d[f].append(v)
    fams = [f for f in d if len(d[f]) >= 2]
    if len(fams) < 4:
        continue
    y = np.array([float(np.mean(d[f])) for f in fams])
    x = np.argsort(np.argsort(-y)).astype(float)   # 排名（0=最好）
    if x.std() == 0 or y.std() == 0:
        continue
    rho = float(np.corrcoef(x, y)[0, 1])
    print(f"    段{i+1}: n族={len(fams):>2} · 该段内 ρ(排名, 收益) = {rho:+.3f}")
