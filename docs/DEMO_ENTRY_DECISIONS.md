# EntryDecision 与风险定仓提案

`demo_entry_decision.py` 提供没有 I/O 的 `propose_entry_decision(mapping)`，用于在
最终 maker 报价和交易规则已冻结后，计算不可变的入场提案。它不会发订单、写
数据库、申请批准或预占预算。`eligible_proposal` 表示输入及纯计算通过；最终
权威裁定仍需由唯一交易 writer 在账户事务内重新核对并提交。

## 调用与不可变输出

```python
from demo_entry_decision import propose_entry_decision

decision = propose_entry_decision(frozen_inputs)
record = decision.to_dict()
# decision.verdict: rejected / eligible_proposal
# decision.reason_codes: tuple，保留全部可独立判断的拒绝码
# decision.canonical_json: 已冻结、排序键、无多余空白的 JSON
# decision.sha256: canonical_json UTF-8 的 SHA-256
```

`EntryDecision` 为 frozen、slots dataclass；内部只保留字符串、元组等不可变
值，`to_dict()` 每次返回独立对象。调用后修改原始 dict、内部列表或返回值都
不会改写提案。金额、数量、价格、费率等使用规范化十进制字符串；未知或非法
数值保留 null 和拒绝码。任何提案均固定 `reservation_committed=false`、
`acceptance_eligible=false`。相同规范化输入产生相同 hash；数字文本尾零及
BTCUSDT 等已识别的持仓币种别名不改变规范化身份。

## 必需输入

| 领域 | 字段与要求 |
| --- | --- |
| 环境与身份 | environment 固定 binance_demo；account_id、strategy_version_id、run_id、stage_id、signal_id、decision_id 必须是非空安全标识 |
| 不可变 hash | input_fingerprint、strategy_hash、risk_policy_hash、cost_model_hash、account_snapshot_hash、market_snapshot_hash、exchange_rules_hash、exit_params_hash；均为 64 位小写 SHA-256 文本 |
| 来源与水位 | account_snapshot_ref、market_snapshot_ref、exchange_rules_ref、exposure_watermark，必须明确非空 |
| 时效 | signal_at_ms、decision_at_ms、quote_at_ms、account_at_ms，以及 max_signal_age_ms、max_quote_age_ms、max_account_age_ms；均为非负整数；未来事实或严格超过冻结上限会拒绝 |
| 明确完整事实 | strategy_approved、indicators_ready、data_complete、inputs_closed、time_aligned、costs_complete、funding_complete、account_reconciled、risk_entries_enabled、native_protection_verified、exposure_complete、margin_evidence_complete，均必须为布尔 True |
| 交易所配置 | actual_leverage 为整数 1–10，margin_type=ISOLATED，is_auto_add_margin=False，hedged=False |
| 账户预算 | stage_initial_usdt、net_equity_usdt 为正；used_risk_usdt、used_margin_usdt 为非负；active_symbols 必须包含同账户全部持仓和未决入口的币种并集，BTC/ETH/TRX 之外的未知占用拒绝 |
| 最终方向与价格 | pair 为 BTC/ETH/TRX 的标准 USDT 永续 pair，side=buy/sell；entry_price、stop_price、target_price、atr14、price_tick 均为正 |
| 交易规则 | quantity_step、min_quantity、max_quantity 为正；min_notional_usdt 为已知非负；min_quantity 不得大于 max_quantity |
| 冻结限制 | frozen_max_loss_usdt、frozen_max_notional_usdt 必须为正；不以系统上限放大较小的策略上限 |
| 保证金估计 | initial_margin_per_unit_usdt 为可信上游对该报价/实际杠杆/档位的保守逐单位估计，且不得低于 entry_price/actual_leverage |
| 完整成本 | entry_maker_fee_rate、stop_taker_fee_rate、target_exit_fee_rate、stop_slippage_per_unit_usdt、target_slippage_per_unit_usdt、funding_expense_per_unit_usdt、other_cost_per_unit_usdt 均明确已知非负；显式 0 允许，缺失不视为 0 |

所有数字只接受 Decimal、十进制字符串和整数，拒绝 bool、float、NaN、Infinity、
负成本。输入至多 96 位有效数，十进制指数与数量级有固定限制，超限以字段
`*_invalid` 拒绝。计算使用独立 1024 位 Decimal 上下文，不依赖调用方全局
precision 或 rounding。未知字段不回显；输入标识不接受容器、换行或 URL
查询串，避免错误结果夹带任意上游内容。

这些字段的 True、hash 与 ref 都是调用方的声明。纯函数不能证明其对应真实
归档、批准、独占账户、原生保护或完整资金流水；接线层必须读取权威事实，
不能允许 HTTP 调用方自行提供声明来绕过门禁。

## 价格、成本与定仓

本模块不修改价格。entry、stop、target 必须已在 price_tick 网格上，多头
stop<entry<target，空头 target<entry<stop，并满足最终 target=entry+2×
(entry−stop)。这将 R 定义为最终入场价到初始保护触发价的价格距离。ATR14
与 exit_params_hash 被冻结，但止损 ATR 倍数是否符合该参数合同仍由上游核验。

设阶段本金 C，当前可信净权益 E，则 B=min(C,E)，单笔上限=1%×B，账户
风险上限=2%×B，保证金上限=50%×B。盈利不扩大 B；亏损降低新单预算。
历史 used 风险和保证金始终按原金额保留，B 降低后若超预算就拒绝新增，不能
裁剪 used 数字以制造可用额度。最多两个币种的限制也计入未成交入口预占。

止损和目标的逐单位滑点字段表示不利方向的价格支出。多头 SELL 退出价格为
trigger−slippage，空头 BUY 退出价格为 trigger+slippage；不正的执行价拒绝。
退出手续费基数保守采用 max(trigger, adverse_execution)，避免空头买回
价格上涨时少算费用，同时多头也不低于触发价收费。

逐单位计划损失等于：

```text
abs(entry − stop)
+ entry × entry_maker_fee_rate
+ max(stop, adverse_stop_execution) × stop_taker_fee_rate
+ stop_slippage_per_unit_usdt
+ funding_expense_per_unit_usdt
+ other_cost_per_unit_usdt
```

逐单位净目标收益等于 2R 价格距离，减去入场手续费、目标退出手续费、目标
不利滑点、资金费支出和其它成本。资金费收入不会降低保守计划风险。净收益
风险比门禁采用精确交叉乘法 `net_gain×2 >= planned_loss×3`；展示 RR 的
相除结果不参与阈值判断。止损触发价与该成本模型仍不能保证真实成交损失上限。

数量从以下上限的最小值按 quantity_step 向下取整：

- 单笔 1% B、冻结单笔上限、账户 2% B 剩余风险，除以逐单位计划损失。
- 账户 50% B 剩余保证金，除以 initial_margin_per_unit_usdt。
- 冻结名义额上限除以最终 entry_price。
- max_quantity。

最终重新核算单笔/合计风险、保证金、数量和名义额。低于最小数量、最小
名义额或数量为零直接拒绝，不提高数量凑交易。拒绝提案可以保留有效的候选
数量用于审计，但不得提交订单；依赖输入无效无法计算时金额保持 null。

## 后续事务接线要求

- `used_*` 必须来自同一账户的全部策略、持仓、剩余入口和未知提交；以
  exposure_watermark 防止快照与本地预占重叠或遗漏。纯函数不提供原子性。
- EntryDecision、风险预占和订单意图需在同一连接事务提交，并在账户锁内
  重新检查阶段、批准、风险、快照时效和最新占用。不能串联三个各自提交
  的公开存储方法冒充原子事务。
- durable submission claim 之后未知结果保持全部保守预占，只查询原稳定
  身份，不因超时、30 秒 TTL、刷新、重启或查询 absent 自动释放。
- 完整成交后风险转为持仓占用；撤销仅释放已确认未成交余量。保护和
  reduce-only 退出不应因新增风险预算不足而失去执行能力。
- 生产尚未调用此模块。它不取代真实账户对账、原生条件单覆盖/安全替换、
  人工批准、运行日志、不可变数据库存储和完整研究/验收关口。
