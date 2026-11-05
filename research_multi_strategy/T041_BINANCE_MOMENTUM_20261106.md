# T041 BinanceMomentum 模板策略评估（2026-11-06）

- 策略：`bot/user_data/strategies/BinanceMomentum.py`（Freqtrade 模板：RSI/Stoch/ADX，5m 模板强制 1d 回测）
- 数据：20 对 1d，2022-12 → 2026-10；taker 8bp
- 区间：2022-12-31 → 2026-10-23，max_open_trades=20

## 结果

| 指标 | 值 |
|---|---|
| trades | 175 |
| total profit | +4.16% |
| CAGR（约 3.8 年） | ~1.1% |
| maxDD | 2.50% |
| Sharpe | 0.29 |
| profit factor | 1.393 |
| win rate | 86.9% |

分年 PnL（USDT，初始 10k）：2023 +85.7；2024 +14.3；2025 **-8.65**；2026 +324.7。

## 判决

- 年化约 **1.1%**，远低于 5.7% 经济门槛；
- 收益高度集中于 2026，2025 为负；模板策略、long-only，存在 regime 依赖；
- **不通过**，不作为候选；仅记录为负/不足证据。

证据：`artifacts/t041_binance_momentum_20261106/`（结果摘要、配置）。
