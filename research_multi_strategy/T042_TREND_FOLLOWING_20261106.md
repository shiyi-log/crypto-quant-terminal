# T042 TrendFollowing 20-pair 1d 回测评估（2026-11-06）

- 策略：`bot/user_data/strategies/TrendFollowing.py`
  - 20 日收盘价通道突破，t+1 开盘成交，趋势状态持续持有至反向突破；
  - 横向按突破强度选前 top_n=8，target_exposure=0.30，max_open_trades=20；
  - 无固定 ROI/止损（仅 -60% 极端兜底）；channel 使用 `.shift(1)`，无前视。
- 数据：20 对 USDT 永续 1d，2022-12 → 2026-10（Binance Vision + DB）
- 成本：taker **8bp/边**（`--fee 0.0008`），未额外建模滑点
- 区间：2023-04-10 → 2026-10-23（startup 130d）

## 结果（auto gate）

| 指标 | 值 |
|---|---|
| status | **pass（仅回测门槛）** |
| trades | 691 |
| total profit | **+33.44%** |
| CAGR | **8.49%** |
| maxDD | 11.85% |
| Sharpe | 0.722 |
| Sortino | 2.98 |
| Calmar | 4.17 |
| profit factor | 1.410 |
| win rate | 38.9% |

分年 PnL（USDT，10k 起始）：2023 **-433.84**；2024 +2200.13；2025 +844.77；2026 +733.43。

## 判决

- 按 5.7% 年化 + Sharpe>0.5 + PF>1.05 + DD<25% 的门槛，**历史回测通过**；
- 但 2023 为负、收益集中在 2024–2026；未建模滑点/资金费；20 币池存在选择偏差；
- **尚未 forward**：不能称已确认有效，只能作为 paper forward 候选。

## 下一步

- 用 dry-run/paper 账户跑 TrendFollowing forward（需 Binance 网络恢复）；
- 记录每日权益、交易、滑点；至少 20–30 个交易日后按门槛复核。

证据：`artifacts/t042_trend_following_20261106/backtest_result.zip`
