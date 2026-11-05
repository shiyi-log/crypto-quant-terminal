# 量化策略联合研究：负结果总报告（2026-10-24）

角色：Codex 实现，DSH 找方向+独立审核。所有结论均基于历史模拟/paper 证据，未部署、未接真实下单。  
原则：不因“预测算术”否决；否决必须有模拟证据（n、成本、funding、强平、CI/统计）。

## 结论

**至今没有任何方向通过“可盈利 + 真实模拟交易证据”门槛。** 已系统测试 9 个方向，全部 rejected 或数据不足。

## 方向总表

| 方向 | 关键证据 | 判决 |
|---|---|---|
| S36 资金费/基差 carry（15m） | 0/0.5/1bp 情景 net ≈ -17bp，CI 全负（2bp n=76 not_estimable） | **rejected** |
| S37 配对交易（15m） | n=1,399 net -9.12bp；DSH 独立复现 18/18 变体 gross<0、net 全负 | **rejected** |
| S38 1d 横截面动量 | 191 期；q20 gross +30.05bp/周、net +24.4bp，CI [-43.9,+98.9] p=0.50；q30 p=0.64；全样本不显著 | **rejected** |
| S39 quarter-hour 特征（4/8/12h） | 36 变体 0 正；最好 -5.32bp（CI 跨 0）；毛边缘 5–7bp < 16bp 成本 | **rejected** |
| S40 funding carry 轮换（8h） | 6 变体全负；k2_h1 -10.17bp、k3_h1 -9.82bp 显著负；强平已结算 | **rejected** |
| R1 maker 成交证据 | fill_ts/queue 修正后；1–5s markout -0.3~-1.1bp（逆向选择） | 不支持 maker carry |
| R7 清算冲击 | 无 forceOrder 历史，仅 55 个代理事件；CI 全跨 0 | 数据不足（not_estimable） |
| R9 现货-永续 carry（7/14/28d） | 28d net +10.53bp CI [-2.2,+24.3] p=0.11；2025 -7.6bp、2026 -22.7bp；条件规则 OOS 负 | **rejected** |
| R10 跨所 funding 价差（Binance/Bybit） | spread 0.465bp/8h；net -15.6/-15.3/-15.0bp（1/3/7 窗），fee 主导 | **rejected** |

## 共同教训

1. **成本是主杀手**：8bp/腿 taker + 高换手使 15m/8h 策略净收益系统性为负；毛边缘 1–7bp 不足以覆盖 16bp 往返。
2. **funding 本质是 carry**：S40/R9/R10 均验证 funding 收入无法在 2025–2026 regime 覆盖成本；2023–2024 的历史正收益不可外推。
3. **数据缺口会制造假信号**：S39 曾因 tick=0.01 误用使低币价品种每笔固定亏，修复后从 -350bp 回到 -5~-15bp；R9 曾因强平用当日 close 产生 lookahead；R10 fixture 数据产生常数假结果。
4. **统计口径要保守**：重叠仓位必须用 ≥持有期的 block bootstrap；MDE 必须与 δ_econ=12bp 比较；coverage<90% 需披露偏差。
5. **可实现的正 edge 很薄**：动量 gross +20–30bp/周但方差极大；carry 在低费率/长持有下才可能转正，但当前 taker 费率下不成立。

## 如果继续：只剩高复杂度方向

- 期权波动率风险溢价（Deribit DVOL/期权，需新数据与期权定价模拟）；
- on-chain/多 venue 执行套利（需新数据源、延迟/容量假设）；
- maker/低费率制度（需要真实 fill 概率与队列证据，R1 已显示短 horizon 逆向选择）。

建议：在投入这些方向前，先确认数据可得性与成本假设，否则大概率重复“成本吃掉边缘”的结论。

## 证据索引

`S36_VERDICT_20261024.md`、`S37_DSH_INDEPENDENT_REPLICATION_20261024.md`、`S38_VERDICT_20261024.md`、`S39_VERDICT_20261024.md`、`S40_VERDICT_20261024.md`、`R1_MAKER_V3_AUDIT_20261024.md`、`R9_DSH_AUDIT_20261024.md`、`R10_DSH_AUDIT_20261024.md`；artifacts 下对应 run 目录。

## 增补：期权波动率溢价（R11 预研，2026-10-24）

- Deribit DVOL 历史可取（BTC/ETH，1h，2024–2026）。
- VRP 预检（IV²−RV²，30d）：BTC 均值 **+186.8bp 方差**（2025 +481、2026 -255）；ETH 均值 **-520.8bp**（2025 -647、2026 -331）→ 仅 BTC 部分年份有正溢价，regime 不稳。
- **历史期权成交不可得**：`get_last_trades_by_currency_and_time` 对 2024-12 窗口返回空，只有近期成交；过期 instrument 的 trade history 也取不到。因此无法用真实期权成交/盘口做模拟，只能 DVOL+BS 合成（execution_evidence=unknown），不满足“真实模拟交易证据”要求。
- 判决：**数据受限，暂不立项**；若将来取得历史期权成交/IV 曲面，再评估 BTC 短 vol 的窄范围 paper。

## 增补：funding 变化（拥挤度变化）信号（DSH 预检，2026-10-25）

17 币、1d 价格 + official funding；每 24h 计算 funding 变化 Δf（近 24h 减前 24h），按 Δf 排序，long 最低 20%、short 最高 20%，持有 1/3/7d，8bp/单位 gross：
- hold 1d：n=1,344，gross ≈ -5.4bp、net -13.4bp，CI [-21.3,-5.4]
- hold 3d：gross ≈ -14.2bp、net -22.2bp，CI [-35.7,-8.3]
- hold 7d：gross ≈ -20.1bp、net -28.1bp，CI [-48.8,-7.7]
- 反向后毛收益仅 +5~+20bp，扣 16bp 成本后仍无稳定正 net。
结论：funding 变化方向不构成可交易 edge，归档不作为候选。

