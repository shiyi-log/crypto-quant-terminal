# Binance Demo s34 Switch Verification

操作时间：2026-10-13 23:46 CST 左右；核对时间：2026-10-13 23:54 CST

本次操作的目的为验证 Demo 下单链路和前向记录，不构成盈利、上线或可用性结论。

## 目标与边界

- 目标账户：`s34_vwap_anchor_reversion_15m_Fnew_Demo_R1000_k1.0_100x_maker`
- 策略：成交量加权锚定回归（15m）
- 研究币集：BTC/USDT:USDT、ETH/USDT:USDT、TRX/USDT:USDT
- 旧主会话：`s02_dual_thrust_B15m_R1000_k1.0_100x_maker`
- 旧 namespace：`demo_strategy_dc0fc0b01eff44c2bf4b5d6e2f50ce60`
- 新 namespace：`demo_strategy_42630ff44cd9434890feaeff48a2bdc1`
- Demo 端点：`demo-fapi.binance.com`

切换前确认旧主会话无归属持仓、无挂单，订单/成交计数为 `0 / 0`。另外，因主策略切换门控要求所有并行会话先暂停，已暂停以下三个并行会话；它们均核对为空仓且订单/成交为 `0 / 0`：

- `demo_strategy_aa4055ddab834c1c8b7bf769f5e4d080`（price burst）
- `demo_strategy_76d1f115e280412ea1f0d4cbe99e04e2`（s07）
- `demo_strategy_f4c7f945f41c41b092db0943c2fa4052`（s35）

## 操作结果

| 操作 | 结果 | 证据 |
|---|---|---|
| 暂停并行会话 | 完成 | 三个会话持久化为 `paused_flat`，`entriesEnabled=false` |
| `/switch` 目标 s34 | 后端状态已完成 | 前端等待超过 20 秒后显示 `timeout of 20000ms exceeded`；随后读取数据库持久化状态确认主会话已变更为 s34，入口仍关闭。原始 HTTP 响应未在客户端保留，不能把超时客户端错误写成 HTTP 失败。 |
| `/start` s34 | 完成 | 确认弹窗后页面显示“正在 Demo 运行”，主会话 `entriesEnabled=true`；数据库 runner 状态为 `observing`。 |

最终 runner 状态：

- `selectedAccountId=s34_vwap_anchor_reversion_15m_Fnew_Demo_R1000_k1.0_100x_maker`
- `virtualAccountKey=demo_strategy_42630ff44cd9434890feaeff48a2bdc1`
- `entriesEnabled=true`
- `status=observing`
- `owned_positions={}`
- `last_error=null`
- `forwardBoundaryMs=1791906407239`

旧 s02 namespace 仍保留在共享会话账本中，未覆盖其历史记录。

## 身份与执行指纹

- s34 `rule_hash`：`aa54a59a24dc168bc46d9526133d98e3f434ebb817af2ccb8f94ed30c92e86ad`
- s34 `execution_hash`：`387356c245fc17a014fd342e99fee61c39db0481ad1e86c1baafebc71fd4626a`
- Demo `demo_policy_hash`：`4bef1c4166d5b9a500107c64adaf050d554eff966b3991bced1d5c7d862ea8ef`
- Demo policy：`paper_atr_risk_proxy_capped_by_demo_notional`
- 入口类型：`post_only_limit_at_signal_close`
- 保护退出：`reduce_only_market`

## 名义金额口径

纸面 s34 账户的冻结参数是：初始权益 10,000 USDT、目标敞口 30%、100×、单笔风险预算 1,000 USDT，纸面名义按

`R / (k * entry_atr / entry_price + 2 * fee_rate_per_side)`

计算；该值随每个信号的 ATR 和价格变化，不能预先写成一个固定金额。

Demo runner 另有 `maxNotionalUsdt=1000` 上限。实际提交名义应以量化后的订单数量乘委托价核对，实际成交名义应以成交数量乘成交价核对，并受交易所精度、最小名义和未成交状态影响。

截至本核对时，新 namespace 的 Demo 账本为：订单 `0`、成交 `0`、持仓 `0`。因此：

- 实际提交名义：未知（尚无订单）；
- 实际成交名义：未知（尚无成交）；
- 纸面信号名义：按上述 ATR 风险公式逐信号计算，尚无可对应的 s34 新会话成交记录；
- `1000 USDT` 是 Demo cap，不是已下单或已成交名义。

## 限制

本记录只说明会话切换、入口闸门和账本边界。Demo 订单簿与执行可能不同于实盘；手续费以实际成交回报为准；当前没有成交样本，不能评价成本、收益或策略表现。新会话只接收启动边界之后的事件，不复制旧纸面持仓。
