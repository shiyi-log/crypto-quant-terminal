# T039 MomentumRegimeResearch 评估（2026-11-06）

- 策略：`bot/user_data/strategies/MomentumRegimeResearch.py`
- 数据：BTC 1d/1h 2022-12→2026-10（data.binance.vision + DB）；funding 2022-12→2026-10（Binance 归档 + DB）
- 训练：FreqAI LightGBMRegressor，walk-forward 365d train / 90d backtest
- 回测区间：2025-06-01 → 2026-10-23（满足 865 天 startup）
- 成本：taker 8bp（freqtrade fee 0.0005/边）

## 结果（auto-iteration gate）

| 指标 | 值 |
|---|---|
| status | **rejected** |
| trades | 145 |
| profit | -19.84% |
| maxDD | 19.84% |
| profit factor | 0.205 |
| Sharpe | -3.275 |
| min trades gate | 100（通过） |
| profit/PF/Sharpe gate | 全部失败 |

## 结论

- 该动量+波动率+funding 过滤策略在 2025-06→2026-10 上**显著为负**，按 gate 拒绝；
- 不部署、不进入 forward；
- 证据：`artifacts/t039_momentum_regime_20261106/`（STATUS.json、策略、配置）。

## 备注

- Freqtrade 的 funding 1h 重采样会制造 >12h 假缺口，策略改为直接读取本地 funding JSON；
- 工作区在 11-06 00:12 被重置，本结果基于 iCloud 备份恢复后的代码/数据。
