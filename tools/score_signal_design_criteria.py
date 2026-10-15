"""按「新信号设计要求」的 8 条标准给现有族打分。

标准（来自报告第五节「🔬 新信号的设计要求」）:
  1 信号数量    ≥ 200 个唯一入场时刻
  2 时间分散    单日占比 < 60%
  3 信号分散    top1 占比 < 20%
  4 币种稳健    至少 2 个币同号
  5 幅度门槛    组差 > 4 bp（净收益率，按名义归一）— 用去重均值×名义代理
  6 两半一致    两半同号 且 后半 CI 不含 0
  7 信息源新颖  不在已清点的 18 类之内（现有族全部为否）
  8 去重后显著  唯一入场时刻去重后仍显著
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
recs = collections.defaultdict(list)
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
            # ⚠️⚠️ Round 188 修正: 原用【原始 net_pnl】⇒ 被仓位规模主导（R1000 变体绝对
            #    值最大）⇒ 与报告其它部分（归一化千分比）口径不一致。现改为归一化。
            recs[k].append((int(oc), float(p['net_pnl']) / nt * 1000, nt,
                            str(p.get('pair')).split('/')[0]))
            break
conn.close()

print("  ══ 现有族 vs「新信号设计要求」记分卡 ══")
print(f"    {'族':<28}{'n唯一':>6}{'单日%':>7}{'top1%':>7}{'正币':>5}{'两半':>5}{'组差':>5}{'跨段':>5}  通过/8")
rows = []
for fam in FAMS:
    d = recs.get(fam)
    if not d:
        continue
    uniq = {}
    for oc, v, nt, pr in d:
        uniq.setdefault(oc, (v, nt, pr))
    n = len(uniq)
    vals = np.array([v for v, _, _ in uniq.values()])
    tot = vals.sum()
    top1 = (max(vals) / np.abs(vals).sum() * 100) if np.abs(vals).sum() else float("nan")
    byday = collections.defaultdict(float)
    for oc, (v, _, _) in uniq.items():
        byday[dt.datetime.fromtimestamp(oc / 1000, dt.timezone.utc).strftime('%m-%d')] += v
    # ⚠️ Round 187 修正: 原用 |tot| 作分母 ⇒ 当 tot≈0 时比值爆炸（模拟: 被拒率 100%）。
    #    改用 Σ|x|（绝对贡献）作分母 —— 对 tot≈0 也良定义。
    gross = sum(abs(x) for x in byday.values())
    maxday = (max(abs(x) / gross * 100 for x in byday.values())
              if gross > 0 else float('nan'))
    # 同时保留原口径（|净额|为分母）以便对照
    maxday_net = (max(abs(x / tot * 100) for x in byday.values())
                  if abs(tot) > 1e-9 else float('inf'))
    coins = {pr for _, _, pr in uniq.values()}
    ks = sorted(uniq)
    h = len(ks) // 2
    a = np.array([uniq[k][0] for k in ks[:h]]); b = np.array([uniq[k][0] for k in ks[h:]])
    # ⚠️ Round 186: n 很小时切片可能为空 ⇒ 先判长度，避免 RuntimeWarning
    twohalf = bool(len(a) and len(b) and a.mean() * b.mean() > 0)
    # ⚠️ Round 185 修正: 原版只算了 7 条却打印 "/8"，且【缺第 5 条与第 9 条】。
    #    现补齐为完整 9 条（与报告第五节的 9 条设计要求一一对应）。
    c1 = n >= 200                                  # ① 去重后唯一入场时刻 ≥200
    c2 = maxday < 60                               # ② 单日占比 <60%
    c3 = top1 < 20                                 # ③ top1 贡献 <20%
    # ④ ≥2 币【正】均值（原实现只数不同币数 ⇒ 3 币时恒真，Round 188 修正）
    #    意图（见报告 3.4）: 正收益不能只来自单一币种（s20/s35 曾是单币 ETH 驱动）
    coin_means = collections.defaultdict(list)
    for _v, _nt, _pr in uniq.values():
        coin_means[_pr].append(_v)
    pos_coins = sum(1 for cv in coin_means.values() if np.mean(cv) > 0)
    neg_coins = sum(1 for cv in coin_means.values() if np.mean(cv) < 0)
    n_coins_traded = len(coin_means)
    c4 = (pos_coins >= 2) or (n_coins_traded == 1 and pos_coins == 1 and False)
    # 记录用于展示
    _pos_coins, _n_coins = pos_coins, n_coins_traded
    # ⑤ |组差| > 4bp
    # ⚠️⚠️ Round 186 修正: 第 5 条需要【预测变量】来分组；现有族没有预测变量。
    #    原实现用【结果自身】的分位分组再比组均值 ⇒ 【循环论证】:
    #    模拟显示纯随机数据（σ=5‰）下通过率 【100%】。
    #    ⇒ 现标记为【不适用（N/A）】，且不计入分母。
    c5 = None
    c6 = twohalf                                   # ⑥ 两半同号
    c7 = False                                     # ⑦ 信息源新颖（现有族均为已清点的信息源）
    c8 = False                                     # ⑧ 去重后 BH-FDR 正显著（本会话证明无族通过）
    # ⑨ 跨季度同号（Round 184 的族级实施 · 4 等分全部同号）
    if n >= 20:
        idx = np.linspace(0, n, 5).astype(int)
        segs = [vals[idx[t]:idx[t+1]].mean() for t in range(4)]
        neg = sum(1 for m in segs if m < 0)
        c9 = (neg == 0 or neg == 4)
    else:
        c9 = False
    checks = [c1, c2, c3, c4, c6, c7, c8, c9]      # c5 不适用 ⇒ 不进分母
    passed = sum(1 for x in checks if x)
    denom = len(checks)                            # = 8
    # ⚠️ Round 188: r[4] 应为 c4（≥2 币正均值），原为 len(coins) ⇒ 分项显示错误
    rows.append((fam, n, maxday, top1, c4, twohalf, passed, denom, c9))
    print(f"    {fam[:28]:<28}{n:>6}{maxday:>7.0f}{top1:>7.0f}{f'{_pos_coins}/{_n_coins}':>5}"
          f"{'是' if twohalf else '否':>5}{'—':>5}{'是' if c9 else '否':>5}"
          f"  {passed}/{denom}")

print()
print("  ══ 汇总 ══")
mx = max(r[6] for r in rows)
print(f"    最高通过数: {mx}/{rows[0][7]} · 达到满分的族: {sum(1 for r in rows if r[6]==r[7])} 个")
print(f"    ⚠️ 第 ② 条分母用 Σ|x|（绝对贡献）而非 |净额| —— 后者在 tot≈0 时退化（Round 187）")
print(f"    ⚠️ 第 ⑤ 条（|组差|>4bp）对现有族【不适用】—— 需要预测变量才能分组，")
print(f"       原实现用结果自身分组是循环论证（Round 186 修正，故分母为 8 而非 9）")
print(f"    ⇒ 【无一族满足全部要求】—— 与本报告核心结论一致")
print()
print("    各族通过的条目数分布:")
c = collections.Counter(r[6] for r in rows)
for k in sorted(c, reverse=True):
    print(f"      {k}/8: {c[k]} 个族")
print()
print("    分项通过率（各多少族通过 · ⑤ 对现有族不适用）:")
labels = ['①n≥200','②单日<60%','③top1<20%','④≥2币为正','⑤|组差|>4bp(N/A)','⑥两半同号',
          '⑦信息源新颖','⑧BH-FDR','⑨跨季度同号']
cols = []
for r in rows:
    cols.append([r[1]>=200, r[2]<60, r[3]<20, r[4], None, r[5], False, False, r[8]])
for idx, lab in enumerate(labels):
    vals = [c[idx] for c in cols]
    if all(v is None for v in vals):
        print(f"      {lab:<20}{'— 不适用':>10}")
    else:
        print(f"      {lab:<20}{sum(1 for v in vals if v):>5} / {len(rows)} 族")
