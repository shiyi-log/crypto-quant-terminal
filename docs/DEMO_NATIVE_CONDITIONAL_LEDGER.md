# Binance Demo 原生条件单账本

本小节对应 2026-11-16 重构计划的原生保护持久化基础。它只提供意图、单次提交
claim、追加式事实与可重建投影，不发送交易请求，不证明交易所原生能力可用，
不授予正式 Demo 验收资格。

## 显式安装与兼容检查

DemoConditionalStore 继承 DemoStore 的固定 Demo 环境、账户 namespace、
显式 ensure_schema() 和只读 check_schema()。新增迁移为
0003_native_conditional_orders.sql，不修改已发布 0001/0002 的内容或 checksum。
普通服务连接/交易启动只调用兼容检查；需要迁移时停止新增风险并由显式运维流程
实施备份、升级和恢复验证。此代码变更未在生产库执行迁移。

迁移创建三个独立模型：

- demo_conditional_intents 保存不可变意图和 SHA-256，包括策略、轮次、
  EntryDecision 引用、父入口、币种、方向、止损/止盈类型、量价、触发依据、
  revision 和被替换身份。
- demo_conditional_events 按持久化 sequence 追加 reservation、claim、未知、
  查询缺失、交易所观察与撤销回执。意图和事件均以 ALWAYS trigger 禁止 UPDATE、
  DELETE 和 TRUNCATE。
- demo_conditional_current 是可重建投影。读取和 claim 从意图及完整事件流校验、
  重放，不能通过篡改投影的 attempts/status 恢复发送资格。

金额用 Decimal 和无固定小数位的 NUMERIC，精确标准化不调用会受上下文舍入的
Decimal.normalize()。输入对位数/指数作界限检查，避免无界字符串展开；数值
字符串小数尾零等价，超精度不会被数据库悄悄舍入。所有时间为 UTC 毫秒事实或
处理时间，数据库事件 sequence 决定重放顺序。

## 提交与观察合同

reserve_conditional(intent) 在同一事务写入意图、reserved 事件与投影，完全
重复返回已有结果；相同 client 或父入口/type/revision 的不同事实拒绝。
claim_conditional_submission(client_algo_id, claimed_ms) 在账户 namespace
事务锁下最多成功一次；claim 事件先于网络调用持久化。调用方只有在方法返回
True 且事务提交后才可使用窄适配器发送一次。进程在 claim 后退出视为未知，
重启只查询同一身份，不能重复 POST，也不能换 ID 规避未知。

mark_conditional_unknown() 只接受安全的错误类型名，不保存原始上游异常。
record_conditional_observation(..., None, ...) 记录查无结果；缺查不证明未提交、
被拒绝或已撤销，不释放 claim。每个观察都要求相同 client、币种、方向、类型、
数量、触发价、BOTH、reduce-only、非 closePosition 和冻结触发参数。

算法单身份、实际触发子订单身份和普通订单/成交身份独立保存。原始
NEW/TRIGGERING/TRIGGERED/FINISHED/REJECTED/EXPIRED/CANCELED 不映射为普通
FILLED；任何算法状态的 execution_complete 均为 False。真实退出完成必须
另由普通子订单、fills、仓位和剩余普通/条件单对账证明。

同一事件 ID 重复内容幂等，不同内容冲突并回滚。同一交易所水位的矛盾事实、
算法 ID/子订单 ID 改变或状态倒退标记 reconciliation_conflict，保留较早已知
事实。旧水位事实仍追加但不覆盖较新投影。子订单字段缺失不抹掉已知 ID。
条件单 raw 使用独立严格白名单，排除凭据、签名、请求头和任意非合同字段。

撤销 ACK 校验 receipt 的 algo ID、client ID 和精确整数200或字符串"200"成功 code，
只追加命令事件。
它不修改算法状态、水位或仓位，不等于取消完成；后续必须查询确认并处理触发
竞态。投影以 cancel_pending 和 last_cancel_ack_ms 暴露取消仍待确认，ACK/缺查/
迟到观察不会刷新 last_exchange_observed_ms 代表的有效交易所事实新鲜度。
CANCELED 也只表示条件单取消，不表示账户已平。创建、更新时间和观测时间必须
有序，未来事实不能固化为长期覆盖真实撤销的最新事实。

## 当前限制和后续接线

- strategy/run/decision/parent 字段是不可变引用，尚不是批准、独占账户、资本
  阶段或真实成交存在的证明。namespace 不是经交易所核验的实际账户 ID。
- 尚未实现原生保护协调器、部分成交数量覆盖、新确认后撤旧的替换流程、2 秒
  指标、用户流与 REST 勾稽、退出互斥、残留清理及执行器/API/UI 接线。
- supersedes 只核对同 namespace、同策略/轮次/父入口/类型/方向和更早 revision；
  保留旧保护与新保护是否真实生效仍由后续协调器及受控 Demo 合同核验。
- protection_capabilities().acceptance_eligible 必须继续为 False，直到上述路径、
  重启恢复及真实受控 Demo 证据完整。此模块不能替代人工上线/异常恢复批准。
- 尚未完成生产备份恢复演练；代码发布不构成数据库已升级或正式验收已开始。

## 本节验证证据

先运行缺实现测试并观察失败，再实现迁移与账本。后续高精度、撤销回执和状态
回退边界先观察失败再修复。验证仅连接独立 quant_test，每轮随机 schema，结束
删除该测试 schema；不使用生产 DSN，不访问交易所，不变更运行服务。
最终数量与完整仓库回归结果以开发台账和 CI 的实际执行记录为准。
