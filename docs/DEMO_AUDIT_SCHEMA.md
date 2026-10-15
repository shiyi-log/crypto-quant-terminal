# 币安 Demo Trading 独立审计账

`DemoStore` 只接受 `environment=binance_demo`、`endpoint_host=demo-fapi.binance.com`。
构造对象不连接数据库、不读取环境或凭据。主入口显式调用 `ensure_schema()` 安装
`database/demo_schema.sql`，连接与事务复用现有 `DataStore._connection()`。
这些表不写既有模拟账户、实盘交易或事件表。

## 表与身份

| 表 | 内容与约束 |
| --- | --- |
| `demo_orders` | 先落盘的冻结订单意图、唯一 client id、交易所订单状态投影与追加审计观察 |
| `demo_fills` | 交易所实际成交；订单/成交/币对唯一身份，数量与成交价不可改写 |
| `demo_positions` | 币安仓位查询的不可变观察；空快照不代表程序替用户平仓 |
| `demo_account` | 币安余额/权益查询的不可变观察 |
| `demo_signal_comparisons` | **额外第五表**：相同冻结信号的内部价格代理、Demo 事实、runner checkpoint 与会话绑定 |

每笔订单同时记录研究虚拟账户、策略、信号、意图、规则 hash、执行 hash 与输入指纹。
`virtual_account_key` 是研究会话标识，不是 API key，也不代表币安实际拥有一个独立子账户。
订单和成交的币对统一储存为 CCXT 形式，`BTCUSDT` 与 `BTC/USDT:USDT` 无法绕过唯一身份。
允许研究币种仅 BTC/ETH/TRX 的 USDT 永续。

`intent_payload` 永久保留冻结 ATR、止损、TTL、内部信号与政策 hash 等风险上下文。
`raw` 则只保存允许列表中的币安响应字段；更新响应不会覆盖 `intent_payload`。
任何 API key、secret、请求、签名、请求头、Cookie 均不得作为账本身份或响应证据。
响应原字段允许列表与比较元数据脱敏分别处理，带查询参数的 URL 与字符串中的凭据也会脱敏。
单次快照、比较或响应最多 256 KiB；账本不自动删除过去观察。

## 提交和重启

```python
ledger = DemoStore(data_store, virtual_account_key="demo_default")
ledger.ensure_schema()  # 主入口明确执行；普通对象构造没有 DDL
row, created = ledger.reserve_order(frozen_intent)
if row["status"] == "intent" and ledger.claim_submission(row["client_id"]):
    # reserve 与 claim 都已提交；此后才允许向官方 Demo 发请求。
    response = adapter.submit_limit_order(...)
    ledger.record_order_observation(row["client_id"], response)
```

`reserve_order` 在事务提交后才返回；相同冻结意图重试返回原行，不同事实发生身份碰撞则拒绝。
`claim_submission` 用数据库条件更新选出唯一提交者，`submission_attempts` 最大为 1。
提交期间断线或进程退出后，`submitting/unknown` 状态只应按原 client id 查询对账，不能再提交。
`mark_submission_unknown` 记录不确定状态；`append_reconciliation` 只追加查询事实而不改变成交。

`abandoned_after_absent_lookup` 是 runner 的本地审计终态，不是交易所的取消或拒绝状态。
只有同一 client ID 连续至少三次查无订单、提交已超过五分钟、无本地成交、无交易所挂单且
对应交易对仓位为零时才允许迁移；迁移会保留每次查无结果及最终快照的审计记录。
`abandon_unclaimed_order` 只让从未 claim 的本地意图因暂停或 TTL 到期变为内部 `expired`，
不会假造交易所状态或 order id，也不会把它计入交易所未成交率；已提交的未知状态仍须对账。

交易所订单观察校验 client id、币对、方向、订单类型、数量和已知 order id。
投影要求交易所版本与累积成交量不倒退；终结状态不会被迟来的开放状态重开。
旧观察仍保留，并标明 `projection_accepted=false`。
`EXPIRED_IN_MATCH` 在内部属于 `expired`，导出的原始状态仍是 `EXPIRED_IN_MATCH`。

成交身份为环境/虚拟账户/币对/order id/trade id；重复成交不会重复计数。
实际成交价、数量和时间必须一致，累计成交不能超过冻结订单数量。
手续费、资产和 maker 身份缺失时均保留 `null`，**不使用配置费率补零或补假设值**。
手续费金额与资产可以分别已知或未知，原始成交仍完整入库；只有两者都已知才算费用完整。
`demo_fee_fields_independent_v2` 迁移只移除旧 Demo 成交表的费用字段耦合 CHECK，保留其他约束。
更新版本可修订实际手续费；同版本只补此前未知字段，不以冲突值覆盖已知字段。
全部费用观察保留。不同资产手续费分别汇总，不自动把 BNB 等资产折算成 USDT。
若较新回报只提供变更币种但未给金额，或只变更金额却没有币种，
旧完整费用组合暂作为历史事实保留，新 partial 回报单列在 raw 与 audit，
`commission_conflict=true`。不能把两个版本的字段拼成从未报告过的金额/币种组合；
该状态下费用总额与净利都属于未知，导出只呈现新 partial 回报。
新的完整实际费用回报才能解除冲突。

`bind_account` 冻结非秘密会话身份；保留 stage `binding` 不允许普通比较 API 写入，
数据库部分唯一索引与事务锁防止改绑。新的策略、执行政策或会话应使用新的研究虚拟账户。

## 查询与币安格式

`list_orders/list_fills/list_comparisons` 默认最多 1000 条，供界面与导出使用。
风险对账应使用 `runner_orders()`、`runner_fills()` 的全会话记录，或
`net_fill_positions()` 的全历史 SQL 净数量聚合，不能用界面窗口估算仓位。
`get_latest_account_snapshot/get_latest_position_snapshot` 返回最近不可变观察。

`binance_order(row)` 与 `binance_fill(row)` 为模块函数，保留币安 camelCase 名称：
`orderId/clientOrderId/symbol/side/type/status/origQty/executedQty/avgPrice/price/time/updateTime`，
以及 `id/orderId/qty/quoteQty/commission/commissionAsset/realizedPnl/buyer/maker/time`。
价格、数量、费用以十进制字符串返回。订单导出优先交易所原字段；原字段缺失才使用
审计投影，并在 `demoAudit.fallbackFields` 列出回退字段；本机创建时点单列
`demoAudit.localCreatedMs`，不会覆盖交易所 `time`。
没有交易所时间或状态事实时相应字段保留 `null`，不会把本地 TTL 过期伪装成币安过期回报。

`metrics()` 提供：订单状态计数、实际成交数量、按资产费用与未知 maker 计数、
实际订单观察的 API 延迟中位数（没有测量则 `null`）、未决订单数量和账本净仓暴露。
费用指标单列 `unknown_commission_count` 与 `unknown_commission_asset_count`。
每个资产分组只有全部费用金额已知才给出完整 `commission`；否则为 `null`，
已知部分另列 `known_commission_subtotal`。资产未知时不把不同货币金额相加。
费用版本冲突另列 `commission_conflict_count`，不计入完整总额或已知小计。
未成交率的分母只包含已经提交或有交易所 order id、且终结的非 `reduce_only` 入口订单；
部分成交后撤单不计为零成交。
没有终结入口订单时比例是 `null`，不能展示为零。
这些指标不含未知的资金费和非 USDT 费用换算，也不能据此声称实盘收益已得到验证。

## 验证

`tests/test_demo_store.py` 共 29 项，包括并发提交 claim、重启去重、实际部分成交、
费用版本更新、冻结绑定、输入身份校验、币对别名防绕过、秘密脱敏、不可变快照、
真实 adapter 输出桥接和币安字段导出。
数据库测试显式要求独立 `quant_test`，再使用随机 schema；拒绝连接生产位置。
本模块交付阶段仅运行隔离测试，不安装生产表、不发外部订单、不读取 API 凭据。
