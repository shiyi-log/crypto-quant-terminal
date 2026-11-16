# 账户级风险预占事务

本模块是重构计划的内部存储切片，尚未接入生产执行服务、HTTP 接口或真实账户采集器，不能作为 Demo 实盘验收证据。所有结果的 acceptance_eligible 均为 False。它只访问明确安装迁移的 PostgreSQL，不访问交易所。

## 身份与可信输入

DemoRiskReservationStore(data_store, account_scope, virtual_account_key) 在同一 schema 保存账户预算与策略 namespace 的普通订单。account_scope 是服务端稳定的真实账户标识，不是凭据；同一 namespace 不能迁往另一账户。不同 namespace 共用 demo-risk-reserve:binance_demo:<account_scope> 事务锁。

install_context(context) 仅供可信采集与批准服务调用。上下文显式冻结账户、阶段本金、净权益、账户快照引用与 SHA-256、外部暴露水位、完整性状态，以及策略/run/策略和风险及成本哈希批准表。模块校验身份和不可变性，不能自行证明这些外部事实真实或完整。禁止把 HTTP 用户传来的 approved 或 complete 直接送入此 API。

首个上下文以序号 1 安装，并明确 exposure_scope=external_only_excludes_local_reservations。外部风险、保证金及币种仅表示未由本地预占覆盖的存量，避免重复计数。后续序号连续；阶段与阶段本金固定，外部风险和保证金不能减少，外部币种不能消失。初版没有外部存量减少或资本阶段切换协议，因此会保守阻断一些本可恢复的交易。刷新水位不能把存量归零。

## 原子裁定与重试

reserve_entry(source, intent, context_id=...) 先按冻结请求身份查精确重试，然后在账户事务锁内从不可变预占事件重放当前占用，替换调用者的旧 used_risk / used_margin / active symbols，再调用纯 propose_entry_decision。策略批准与账户状态来自已安装最新上下文。

接受时，Decision、预占投影、reserved 事件和普通 demo_orders 意图使用同一连接原子提交；任一失败全部回滚。拒绝时保存完整 Decision 原因，不创建普通订单。原样重试返回原结果，改变输入、上下文、namespace、同账户 client ID 或已使用 Decision 身份均拒绝。规范 JSON 拒绝不支持的对象类型；Decimal 不使用浮点和默认 28 位精度。

普通意图严格匹配 Decision 的最终数量、价格、币种、方向、策略、signal、策略和退出哈希、input fingerprint、limit/GTX/reduce_only=False。冲突的 type/order_type 别名被拒绝。普通订单的 NUMERIC(38,18) 校验继续阻止数据库静默舍入；不兼容精度导致整笔事务回滚。数据库触发器冻结风险绑定后的普通订单身份。

同账户同币预占互斥。多个 namespace 可竞争剩余额度，账户锁保证后一个使用已提交占用；不同账户的预算互不占用。现有未绑定旧普通订单仍沿用旧执行流程，部署接线前必须把它们计入可信外部暴露，不能混用旧路径来创建未登记的新风险。

## 领取和未知

claim_reserved_entry(client_id, now_ms=..., context_id=...) 在账户锁内重新核对当前批准、明确完整性状态、原 signal/quote 及当前账户事实年龄、订单 expiry、当前 B=min(stage_initial,net_equity) 下的单笔 1%、累计 2%、保证金 50% 和最多两币。净权益下降不缩减旧预占；不再满足资格的旧 intent 不能发送。领取一次提交 claimed 事件和普通订单 submitting/attempts=1，之后崩溃只能按稳定 client ID 查询。

旧 DemoStore.claim_submission 对新绑定订单返回 False；数据库还要求绑定订单提升 attempts 时已存在同一 client/namespace 的 claimed 事件和投影。触发器不是对具有任意 SQL 写权限者的权限沙箱，应用数据库角色仍应按部署方案隔离。

mark_reserved_unknown 追加不可变事件；精确重复幂等。未知、提交中或事实缺失会阻断账户其他新增风险和领取。UNKNOWN 不会因超时、absent、重启、部分成交或撤单而释放。初版对任何非 released 预占始终保留最初全额风险和保证金。

## 完整空仓对账释放

local_fact_hashes() 提供账户已绑定 namespace 的普通订单、逐笔成交和条件订单事件水位哈希。可信归档采集器应先采集同一账户、阶段、观察时间与 exposure watermark 的五类完整资料：positions、orders、conditional_orders、fills、cashflows，并在相应资料中写入当前 local ledger hash。每份资料必须声明正确 kind，普通和条件未结订单及持仓列表必须为空。

record_flat_reconciliation(fact) 要求五类持久化 documents 引用及哈希，校验账户/阶段/时间/类型/完整性及本地事实水位，再把五类原文复制到不可变对账表。随后 documents 被覆盖不会改写已归档证据。fact 包含 fact_id/environment/account_id/stage_id/client_id/observed_ms/exposure_watermark/source_refs/local_ledger_hashes。

release_with_reconciliation(client_id,fact_id) 仅接受上述不可变事实，要求观察时间覆盖风险事件、普通订单及成交时间，且本地事实哈希从归档到释放未变。普通订单仍 submitting/unknown/open/partially_filled 时拒绝。未领取 intent 在释放事务内改为 expired，之后无法提交。释放精确重试返回 False。释放保存该 client 的订单、成交和条件事件最终哈希；若以后出现迟到或更正事实，读取预算即失败关闭，要求重新对账。

空仓释放在账户锁之后、任何本地事实读取之前，以 EXCLUSIVE MODE NOWAIT 申请普通订单、成交、条件意图和条件事件四表的事务锁，保持到提交。它允许只读查询，但短时阻止这些表的所有账户写入和行锁读取，防止旧写入路径在检查与释放之间插入新事实。任一锁冲突抛出 psycopg.errors.LockNotAvailable 并使整个事务回滚，保留全部预占；调用者只能在事务退出后明确重试。NOWAIT 避免条件事件外键与反向写入顺序形成死锁，不能捕获异常后继续使用已失败事务。仅在稀少的完整空仓清理使用此保守锁，事务内没有网络请求。

本地哈希和表锁只证明这四张本地事实表的状态一致，不能证明交易所资料完整，也不代替真实成交/现金流校验。当前归档输入是可信内部协议；尚缺正式采集器、现金流不可变仓库及跨所有事实写入者的统一账户锁接线。普通预占及领取仍依赖唯一账户写者服务协议；本模块尚未改造继承的普通观察写入路径。因此此切片不得独立启用自动交易。

## 迁移与验证

显式安装 0004_entry_risk_reservations.sql，不得改写已应用旧迁移。context、Decision、预占事件和完整对账原文均有 ENABLE ALWAYS UPDATE/DELETE/TRUNCATE 拒绝触发器。demo_risk_reservations 是可重建投影，预算通过不可变事件重放得到，不能通过改小投影释放额度。

测试使用独立 quant_test 的随机 schema，覆盖并发最后额度、跨 namespace、事务注入回滚、身份冲突、低权益精确边界、过期和撤销批准、未知重启、部分撤单、不可变表、旧领取路径阻断、有效归档释放及陈旧/迟到事实。测试没有真实交易所或生产数据访问。
