# 原生条件保护协调器

`demo_native_protection.NativeProtectionCoordinator` 是同步、可独立测试的保护协调器，配合
`DemoConditionalStore` 和 `BinanceDemoAdapter` 使用。模块尚未接入执行服务，没有真实
Demo 账户验收，也不会授予开仓资格；所有结果的 `acceptance_eligible`、
`execution_complete` 和 `entries_allowed` 均为 `False`。`protected` 只表示本次离线
契约下，当前自有数量的止损和目标已被独立 GET 精确确认为 `NEW`。

## 调用边界

构造时注入 `ledger`、`adapter`、安全的 `account_id`、跨所有策略共享的账户写锁
`account_lock`，以及三个回调：

```python
coordinator = NativeProtectionCoordinator(
    conditional_store, adapter,
    account_id=account_id, account_lock=account_writer_lock,
    pause_entries=pause_entries,
    cancel_remaining_entry=cancel_remaining_entry,
    reduce_owned=request_owned_reduce_only,
)
result = coordinator.protect(frozen_plan, owned_position, now_ms=now_ms, market=market)
```

调用者必须保证当前进程与其他进程之间只有一个账户写者。传入的锁仅是同步上下文
协议，模块不能证明它是跨进程锁。没有后台线程，也没有自动重试 POST。调用者负责
持续提交最新、完整、同账户同策略同轮次的事实。不能把 HTTP 客户端传入的布尔值当作
可信对账证明。这里的字段验证不构成来源真实性、数据库 EntryDecision 接受证明、
交易所能力验证或策略冻结合约的完整验证。

回调协议：

| 回调 | 参数及职责 |
| --- | --- |
| `pause_entries(reason, evidence)` | 持久暂停此账户新风险。不得因进程重启自动解除。 |
| `cancel_remaining_entry(parent_client_id, reason)` | 通过普通订单账本确认所有权，幂等撤销剩余入场；撤单受理也要继续查询与成交对账。 |
| `reduce_owned(evidence, reason, idempotency_key)` | 接收兜底请求，必须先重新取得同身份自有仓位、成交和活动订单，再通过普通订单持久意图与单次领取执行 reduce-only。不能直接按旧快照数量发单。 |

兜底回调的 `evidence.protection_request` 包含完成时的 `now_ms`、
`requires_fresh_owned_reconciliation=True`、`snapshot_stale`、`reduce_only=True` 和
`account_id`。回调被接受只代表 `fallback_requested=True`，不代表减仓已经提交或成交。
回调必须把幂等键持久化；相同账户、轮次、决策、父入场、实际数量和成交水位重试得到
相同 SHA-256 键，新增实际数量/成交水位得到另一键。每次实际减仓仍须复核最新所有权，
且不得用新水位绕过任何尚未解析的普通减仓单。

## 冻结计划与事实输入

`frozen_plan` 的必需字段：

| 类别 | 字段 |
| --- | --- |
| 身份 | `environment=binance_demo`、`account_id`、`strategy_id`、`run_id`、`entry_decision_id`、`parent_entry_client_id`、`pair`、`position_side=long/short` |
| 初始退出参数 | `entry_price`、`initial_stop_price`、`target_price`、`entry_atr`、`break_even_stop_price`、`break_even_costs_verified=True`、`price_tick`、64 位小写十六进制 `exit_params_hash` |
| 条件语义 | `working_type=MARK_PRICE/CONTRACT_PRICE`、布尔 `price_protect` |
| 时间边界 | 正整数 `max_snapshot_age_ms`、`max_market_age_ms`、`protection_target_ms`（不得超过 2000） |

金额采用有限精确 Decimal 字符串，数量不得由浮点数传入。允许 BTC/ETH/TRX USDT
永续，父入场和条件单采用稳定 `qd-` 身份。计划验证初始止损方向、严格 2R 目标和价格
刻度，并要求声明已验证覆盖费用的保本价。费用证明的来源和 EntryDecision 接受状态
仍由上游负责。完整规范化计划的哈希参与每代条件单 ID，因此重启后也能检出初始 R、
ATR、目标、工作价格或退出参数漂移。先按父入场寻找已有意图，再验证账户命名空间内的
交易对、策略、轮次和决策，不能通过改变这些字段绕过未知提交。

`owned_position` 必须同上述账户、策略、轮次、父入场和交易对；包括：

- `signed_quantity`：多头正、空头负、空仓零。
- `observed_ms`、`exposure_started_ms`、安全的 `source_ref`、`fill_watermark`。
- 明确为 `True` 的 `facts_complete`、`owned_position`、`fills_reconciled`、
  `normal_orders_complete`、`conditional_orders_complete`。
- `normal_open_orders` 列表、空的 `ownership_conflicts` 列表。
- 存在触发子订单时，空仓清理还要求 `reconciled_child_order_ids` 包含每个已对账子订单 ID。

所有这些字段应由服务端事实聚合器生成。`exposure_started_ms` 应持久化为暴露出现时间，
后续快照不得推迟它。协调器同时以最早持久条件意图创建时间保守约束未知保护计时，
避免重启或快照刷新重置已存在的 2 秒窗口。

可选 `market` 包括正的 `price`、`observed_ms`、安全 `source_ref` 和
`data_complete=True`。行情过期阻止移动止损，但仍会查询持久条件单和请求取消入场。
可信行情已穿过止损时，继续对账并按超时规则请求减仓，不会把它归为无效仓位证据。
仓位本身过期/身份冲突则暂停新风险，不能创建或撤销条件单，也不能用此未验证证据
发出减仓请求。

## 持久提交与替换顺序

每类条件单使用独立递增 revision。首先持久化不可变意图，再完成单次提交 claim，
事务返回后才能 POST。提交成功响应不作为保护证明，必须使用同一 client ID 独立 GET。
只有精确匹配身份、交易对、方向、数量、触发价、BOTH、reduce-only、工作价格和保护
参数的 `NEW` 才计入覆盖。止盈不计作止损；重叠多代数量取单代最大保护量，不相加。

已 reserve 但从未 claim 的原意图在重启后可领取一次；created_ms 沿用持久事实。
一旦 claim，无论超时、明确拒绝、GET 缺失或进程重启，都仅查询原 ID。未知或已触发
这一代不会通过新 revision 逃避。查询失败、被账本拒绝的回退水位、撤单 ACK 均不能
刷新覆盖证据。POST/GET/DELETE 异常日志仅记类型名，不记录原始消息或签名 URL。

实际部分成交增加后，新止损须覆盖完整当前自有仓位；先提交新代并独立 GET 确认，
才重新查询旧止损并尝试撤销。新单拒绝/未知/不匹配保留旧单。DELETE ACK 仅表示命令
受理，之后继续 GET；仍为 NEW 或触发竞态进入 `protected_cleanup_pending`。初始 R
和 ATR 固定，多头止损只升、空头只降；达到 +1R 后先覆盖已验证的费用价，并用冻结
的 1 ATR 跟踪。价格按 tick 向不提前触发方向取整。

`monotonic_ms` 是可选注入时钟，默认 `time.monotonic_ns()`；以调用的 `now_ms` 为锚，
每次 I/O 完成后刷新观察时间，检查仓位和条件证据是否因耗时而过期。2 秒是工程目标，
不是后台定时保证：同步网络调用、调用频率和回调响应都会影响实际延迟。保护不完整
时暂停新风险并取消剩余入场；时间到达后请求可信拥有仓位的 reduce-only 兜底。

## 触发与空仓清理

`TRIGGERING`、`TRIGGERED`、`FINISHED` 均保留原始条件状态，另外查询实际子订单。
它们不等于普通订单 FILLED，更不等于仓位为零。`execution_complete` 始终为 False。

空仓清理必须有本次新鲜零自有数量、成交对账、完整普通/条件订单快照；先读取条件
开放列表，存在任何外部身份就返回 `blocked_external_orders`，绝不自动撤销外部单。
仅撤销本轮本父入场的已知 NEW 条件单，随后重新查询和读取开放列表。所有自有条件
单必须有当前精确终态事实且开放列表为空，普通开放订单列表为空；条件终态时间不得
晚于零仓位事实。FINISHED/带实际子订单的条件还必须查询到同 ID 普通子订单终态，
其时间被零仓位事实覆盖，且 ID 已纳入 `reconciled_child_order_ids`。未知查无结果、
活动/缺失子订单、查询冲突、撤单仅受理和过期零仓位都不能完成清理。

结果状态包括 `protected`、`protected_cleanup_pending`、`protection_incomplete`、
`blocked_evidence`、`blocked_external_orders`、`flat_reconciled`，并提供
`stop_covered_quantity`、`desired_stop_price`、`fallback_requested` 和
`cleanup_complete`。上游必须保留暂停状态，独立决定何时完成完整账户对账或重新允许
风险；该模块的状态不能用来直接授予真实交易资格。

## 离线验证

`tests/test_demo_native_protection.py` 使用内存持久边界、真实条件账本意图验证和事件
重放，复用生产时间一致性规则，交易所为 stub。覆盖重启未知、精确保护、部分成交、
替换拒绝/未知、撤单受理/触发竞态、子订单清理、外部条件单、冻结计划、精确 Decimal、
回退查询水位和注入时钟推进。执行：

```sh
./.venv/bin/python -m pytest tests/test_demo_native_protection.py -q
```

这些测试不涉及真实交易所、账户凭证、生产数据库或服务重启，不能替代实际 Demo
能力验收、账户级唯一写者集成、EntryDecision/RiskReservation 接线和普通订单兜底
幂等账本集成。
