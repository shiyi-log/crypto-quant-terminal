"""策略选择价值的【严格 walk-forward】检验。

背景（Round 181/182）：
  原 3.4 节的复核有两种切法，但【都不是严格 walk-forward】：
    · 切法 A（选在"其余"· 测在本段）："其余"含未来数据 ⇒ 前视偏差
    · 切法 B（选在本段 · 测在"其余"）：评估集同样含未来数据
  且工具原本还【未去重】（冗余 9.1×）。

本脚本做正确的那一种：
  **单向扩展窗口 walk-forward** —— 在 t 时刻仅用【t 之前】的数据选族，
  在【t 之后】的一段上评估。无重叠、无未来信息。
"""
import os
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
MIN_TRAIN_FAMS = 3
MIN_TEST_FAMS = 3


def J(v):
    return v if isinstance(v, dict) else __import__('json').loads(v)


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
recs = sorted([(k[1], k[0], v) for k, v in ded.items()])
print(f"  ══ 严格 walk-forward（单向扩展窗口）══")
print(f"  去重后样本 {len(recs)} 条（按 族 + 唯一入场时刻）")
print()

# ⚠️ Round 195: 切分点数与范围可配置（默认 5 · 可用 N_SPLITS 环境变量增加）
N_SPLITS = int(os.environ.get('N_SPLITS', '5'))
LO = float(os.environ.get('WF_LO', '0.30'))
HI = float(os.environ.get('WF_HI', '0.85'))
cuts = np.linspace(LO, HI, N_SPLITS)
def run_wf(banned=frozenset(), selectable_only=None):
    """banned: 完全移出宇宙；selectable_only: 仅禁止选中、但仍计入基准。"""
    vals, sel = [], collections.Counter()
    for frac in cuts:
        k = int(len(recs) * frac)
        tr, te = recs[:k], recs[k:]
        if len(tr) < 50 or len(te) < 30:
            continue

        def fm(rows):
            d = collections.defaultdict(list)
            for t, f, v in rows:
                if f in banned:
                    continue
                d[f].append(v)
            return {f: float(np.mean(v)) for f, v in d.items() if len(v) >= 2}

        mtr, mte = fm(tr), fm(te)
        if len(mtr) < MIN_TRAIN_FAMS or len(mte) < MIN_TEST_FAMS:
            continue
        pool = [f for f in mtr if selectable_only is None or f not in selectable_only]
        if len(pool) < 3:
            continue
        top = sorted(pool, key=lambda f: -mtr[f])[:3]
        base = float(np.mean(list(mte.values())))
        hit = [mte[f] for f in top if f in mte]
        if not hit:
            continue
        vals.append((frac, len(tr), len(te), top, float(np.mean(hit)) - base))
        for f in top:
            sel[f] += 1
    return vals, sel


def summarize(vals, label):
    a = np.array([v[4] for v in vals])
    pos = int((a > 0).sum())
    rng = np.random.default_rng(19500)
    bs = a[rng.integers(0, len(a), size=(20000, len(a)))].mean(axis=1)
    ci = np.percentile(bs, [2.5, 97.5])
    print(f"    {label:<32}n={len(a):>3} · 为正 {pos}/{len(a)} · 均值 {a.mean():+.3f}‰ · "
          f"CI [{ci[0]:+.3f},{ci[1]:+.3f}] {'✅不含0' if ci[0] * ci[1] > 0 else '❌含0'}")
    return a


print(f"  {'训练截止':<12}{'训练n':>7}{'测试n':>7}{'训练top3':<30}{'测试超额(‰)':>13}")
vals, sel = run_wf()
for frac, ntr, nte, top, e in vals:
    print(f"  前 {frac*100:>3.0f}%{'':<6}{ntr:>7}{nte:>7}{str([t[:12] for t in top]):<30}{e:>+13.3f}")

print()
if vals:
    a = summarize(vals, "① 全部族（原口径）")
    pos = int((a > 0).sum())
    print()
    # ⚠️ Round 261 教训：选族价值可能完全依赖少数族 —— 默认就做稳健性检验
    print(f"  ══ 稳健性检验（Round 261 内建 · 防止「少数族撑起结论」）═══")
    print(f"    被选中次数最多的族: " +
          ", ".join(f"{f[:26]}({c}/{len(vals)})" for f, c in sel.most_common(4)))
    top2 = [f for f, _ in sel.most_common(2)]
    if len(top2) == 2:
        v2, _ = run_wf(banned=frozenset(top2))
        if v2:
            summarize(v2, "② 把这两个族移出宇宙")
        v3, _ = run_wf(selectable_only=frozenset(top2))
        if v3:
            summarize(v3, "③ 含二者但不许选中")
        print()
        print("    ⇒ ⚠️ 若 ②/③ 的符号与 ① 相反 ⇒ 【① 的价值来自被移出的族，"
              "不是选族方法本身】⇒ 【不得据此宣称「选族有价值」】")
    print()
    print(f"  ══ 与其它切法的对照 ══")
    print(f"    严格 walk-forward（本脚本 · 单向）    : {pos}/{len(vals)} · 均值 {np.mean([v[4] for v in vals]):+.3f}‰")
    print(f"    切法 A（选在其余 · 测在本段 · 含前视）: 4/6 · 均值 +1.791‰")
    print(f"    切法 B（选在本段 · 测在其余 · 含前视）: 5/6 · 均值 +1.267‰")
    print(f"    （原报告 · 未去重 · 切法 B）          : 5/6 · 均值 +1.542‰")
    print()
    print("  ⚠️ 另见 Round 262：任何「正向」结论都必须同时报出 MDE"
          "（80% 功效下的最小可检测效应）；本报告各族 MDE 均 > 6bp ⇒ 正向结论无一有功效支撑。")
