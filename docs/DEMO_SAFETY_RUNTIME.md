# Demo 执行安全运行时

demo_safety_runtime.SafetyRuntime 把既有执行器连接到原子风险预占、原生保护
协调器及完整往返成本事件。它是服务端内部组合接口。生产控制器只要配置
risk_account_scope、验收轮次或安全运行时工厂，就必须使用此路径；工厂缺失、
返回错误或内部证据不完整时，在旧纸面定仓、普通订单预占和 POST 之前拒绝
新增风险。直接创建的旧研究会话保留历史冻结行为，状态明确为
legacy_research_ineligible，所有会话 acceptance_eligible=False。

当前真实适配器 protection_capabilities().acceptance_eligible 仍为 False。
因此这些接线不会放行真实新增风险，也没有完成真实账户验收。离线测试的
可信内部 stub 明确提供 True，只用于验证预占后单次提交的顺序。

## 组合根与账户锁

控制器接受可选 safety_runtime_factory，仅由服务端代码传入。工厂关键字参数
包括 account_scope、ledger、adapter、account_lock、strategy_id 和
acceptance_round，返回绑定该账户与 ledger namespace 的 SafetyRuntime。
构造 DemoRiskReservationStore 时使用 ledger.store、account_scope 和
ledger.virtual_account_key，再注入下述四个可信 provider。

运行时绑定检查账户范围、策略 namespace 及同一个 account_lock 对象。控制器
从创建第一会话起一直使用 _account_cycle_lock，共享账户协调器也沿用此锁。
锁顺序为账户锁 → 执行服务锁。跨进程唯一 writer 仍由既有 controller 文件锁
与部署约束保证，不能以 Python 线程锁声称跨进程独占。运行时不能证明 provider
的事实来源；组合根必须只能读取已经校验、提交的内部归档。

普通订单账本和 DemoRiskReservationStore 必须使用同一 DataStore、环境和
namespace。条件账本默认构造 DemoConditionalStore，也可注入同 namespace
账本供离线测试；条件账本的创建不会执行迁移或 DDL。运行期迁移仍须显式完成。

## 内部 provider 协议

四个 provider 均为同步 callable，接受 account_scope、strategy_id、now_ms。
返回已提交归档信封，必须包含 committed=True、非空 archive_ref、匹配的
account_scope，以及下表内容。

committed 只是内部存储提交回执的投影。HTTP 布尔值、方法是否存在、可修改
的状态缓存或未经校验的原始响应都不能作为 provider。信封校验并不自动证明
引用真实存在；持久归档读取和真实性核验由组合根拥有。错误只记录类型和中文
固定原因，不将可能含凭据的异常文本写入状态或日志。

| Provider | 附加参数与返回内容 |
| --- | --- |
| context_provider | 返回 context，为 install_context 的完整权威上下文，含阶段、当前净权益、全账户外部占用、批准与快照水位 |
| entry_source_provider | 另接收信号副本 event、上下文副本 context；返回 source，为完整不可变 EntryDecision 输入，最终 maker 报价、交易规则、信号 ATR14、止损、严格 2R 目标和每项明确成本必须在归档中冻结 |
| protection_facts_provider | 另接收本轮 positions；返回 items 列表，每项含 plan、position、可选 market，遵循原生协调器合同；无自有仓位时允许已提交空列表 |
| completion_provider | 返回 events 列表，每项包含稳定 event_id、trade_id、timestamp_ms、精确 net_pnl_usdt、complete=True、funding_complete=True；仅能来自已提交完整成本报告中的新增 durable 完成事件 |

完整成本报告仍应由 reconcile_round_trip_costs 及持久 previous completion
状态生成。运行时不会从 derive_round_trips 的未含资金费展示数据生成风险
事件。稳定 ID 在持久风险事件中去重；整个候选批次先在隔离内存策略中校验，
再一次调用风险事件存储提交，成功后才替换当前内存策略。失败保持入口暂停，
不部分消费事件；下轮可按同一稳定 ID 重放，不自动恢复人工暂停。

完整交易按经济完成时间消费，不能按归档到达顺序修改连续亏损。每批新事件先
按 timestamp_ms 排序，持久风险日志中的已消费完整交易形成经济时间水位。
早于或等于此水位的新交易直接关闭入口；不同交易在同毫秒没有顺序证明时
同样拒绝。旧稳定 ID 仅允许精确相同事实重放，不重复消费或延长冷却。重启
从持久风险日志恢复该水位；既有日志已乱序时必须人工补证，运行时不会静默
重排旧事实、解除已有冷却或把晚到盈利插入历史清零连亏。

## 新增风险提交顺序

1. 既有信号方向、时效、持仓和人工门禁通过后，生产立即转安全路径。缺少
   provider 的情况不会读取旧纸面止损风险或执行旧数量公式。
2. 检查适配器明确的保护验收能力；真实适配器 False 保持拒绝。再获取已提交
   保护事实、完整成本事件、账户上下文和冻结入场 source。
3. 核对账户、策略、hash、信号、方向、上下文引用和时间、当前事实年龄、阶段
   本金、信号 ATR 与冻结 ATR 倍数、最终止损和 2R、冻结风险及名义额上限。
   未知成本由 EntryDecision 拒绝，不把缺失值当作零。
4. 纯提案决定候选最终数量；reserve_entry 在数据库同一连接事务内再次按
   durable 账户占用计算并原子提交 Decision、风险预占及普通订单意图。
5. 执行器只使用 claim_reserved_entry，再次核对当前账户上下文、批准和
   时效。普通 claim_submission 不得用于此入场。事务返回后才 POST。
6. POST 未知调用 mark_reserved_unknown，保留全部预占；后续仅按原身份
   查询。暂停、TTL、拒绝、查询 absent 或进程重启均不在此运行时释放风险。

Provider 获取发生在数据库事务外；事务方法内无网络。执行器保留已有运行
门禁和人工启动、恢复顺序。新的安全接线不触发真实策略、资本阶段或验收轮次。

## 已有持仓、保护与退出

每个完整轮询继续普通订单和成交对账。原生运行时把已归档保护事实与当前本地
自有数量及交易所实际数量核对，并核对持久接受的 EntryDecision、父入场、
实际成交价与冻结 ATR 和 2R。事实缺失或不匹配时暂停入口、撤销剩余入场，
随后仍执行旧自有仓位保护和策略退出。审计失败不会中断减仓流程，状态单独
暴露 safety_runtime_audit_error。

缺少运行时分支的额外入口撤销读取失败也单独记录 safety_runtime_cancel_error，
保持入口暂停后继续已有保护及策略退出。账户范围在任何绑定与日志前拒绝
URL、userinfo、控制字符和不安全文本，避免配置错误将凭据当作账户标识记录。

原生兜底回调携带稳定幂等键；回调先重新对账，再重新读取仓位与普通开放
订单、核对当前所有权，才通过既有 _exit 生成 reduce-only 退出。存在未决
减仓时沿用现有幂等规则。POST ACK、取消 ACK 或回调完成均不表示成交完成；
结果保持 execution_complete=False 和 acceptance_eligible=False。

## 验证与当前限制

新离线行为测试覆盖缺失、未提交、跨账户 provider，过期或漂移 source，旧
定仓旁路，原子预占后的 reserved claim，未知保留，完整成本批次和重放，
原生实际持仓、重新核对后的稳定减仓，以及真实 cycle 中保护失败仍执行退出。

本子节提供组合根接口，并不声称全账户事实采集器、不可变归档存储、报价
归档与实际保护能力已在生产接通。默认无 provider 时生产新增风险保持阻断。
阶段 100、300 必须先恢复与上下文一致的风险政策；当前不自动更改阶段本金。
