# Demo 冻结运行与持久采集清单

`demo_collection_run.py` 提供内部只读编排。它不启动线程或服务，不读取凭据，
没有 HTTP 路由，也不连接生产 writer。组合根必须显式提供受守卫适配器、
真实 `DemoEvidenceStore` 和 `FrozenCollectionBinding`。

## 冻结身份

`FrozenCollectionBinding(scope, identity_hashes)` 必须一次给齐下列字段，不从
归档、适配器、进程环境或旧记录推断缺失项：

- scope：`environment`、`account_id`、`stage_id`、`strategy_version_id`、`run_id`。
- identity_hashes：`code_hash`、`strategy_hash`、`execution_hash`、
  `risk_policy_hash`、`cost_model_hash`。

环境只接受 `binance_demo`；身份拒绝连接 URL 和 userinfo，哈希必须为小写
64 位十六进制。绑定底层存不可变元组，属性返回副本；绑定摘要覆盖完整
scope 与全部五项 hash。`DemoCollectionRun` 构造后还固定绑定、采集 ID、
适配器、归档、时钟和请求预算。该类型只表达内部冻结配置，并不认证交易所
账户归属、账户独占、策略批准或凭据真实性。

```python
binding = FrozenCollectionBinding(server_scope, frozen_identity_hashes)
run = DemoCollectionRun(adapter, archive, binding, collection_id='capture-20261117-001')
account = run.capture_account()
# 资金采集是另一份冻结计划，必须另用 collection_id。
income_run = DemoCollectionRun(adapter, archive, binding, collection_id='income-20261117-001')
income = income_run.collect_income(since_ms=window_start, until_ms=window_end)
```

调用者不能从 HTTP 字段传入 `verified`、`reconciled` 或其他真实性声明。当前
代码没有将上述示例接入 API。普通 `DemoAccountCollector` 仍保留三字段账户级
合同；历史记录保持原样，不补写策略、运行或 hash。

## 持久清单

不改变现有数据库迁移和 kind 白名单。清单归档使用 `equity_reconciliation`
这个既有容器，明确指定 `schema=demo-collection-manifest-v1`；它不是权益
对账证明，不使用安全 provider 的 schema 或完成/验收 API。

每个 `collection_id` 在同一账户、阶段内只对应一个计划，持久保存：

1. `start`：完整绑定、绑定 hash、操作、参数、请求预算、预期账户组件、
   本机开始时间。这个时间不代表交易所状态时点。
2. 原文及 `component`：原文携带完整 scope、五 hash、采集 ID、绑定 hash、
   组件序号；后续事件固定该原文的完整提交回执，包括 ref、正文 hash、
   record hash、账户序列、kind 和 scope。
3. `failure`：只保存白名单固定原因码和顺序，不保存上游异常文本。
4. `end`：固定 start ref/hash、按实际提交顺序排列的原文回执、组件事件
   回执、失败事件回执及结果。`completed` 仅表示这一采集计划结构完成；
   存在失败时为 `failed`，其余原文依然保留。

原文提交与组件事件提交是两个独立归档事务。后者失败不会撤销原文；原文
仍可用采集 ID 定位。任何清单提交失败均抛出固定
`collection_manifest_commit_failed`，不产生完成返回值。失败事件提交失败
同样保留 start 与已提交事实。事务内部没有网络请求。

## 重启与重放

相同采集 ID 的完整 end 只有在以下核验后才能重放：真实归档链、五字段
scope、五 hash、冻结计划、start/end、全部回执与 hash、原文和组件事件
严格递增顺序，以及派生仓位的账户 ref/hash/sequence 和同一次 positions。
账户链可以夹有其他采集事件，因此不要求账户序列连续。

重放只使用归档资料的离线适配器，执行同一结构归约并比较规范 JSON。这会
再次核验账户组件对应的 kind/path/api/全账户空 params，重建扁平账户身份和
时钟括号；资金采集从归档 cashflows 重建冻结窗口、满页拆分、叶扫描与去重
结果。缺少实际资金原文、收窄请求范围、错误投影或被替换的回执均不能
获得完成结论。固定 false/null 旗标按 JSON 类型比较，整数 0 不能代替 false。

完整成功或失败清单重放均不调用原适配器，也不追加记录。只有 start 而没有
有效 end 时，返回 `collection_incomplete_requires_new_id`；不得对旧 ID
自动重发请求或拼接新旧事实。重新采集必须使用新 ID。相同 ID 修改绑定、
操作、窗口、limit 或请求预算时，返回 `collection_identity_conflict`。

## 传输与语义界线

账户采集的六个实际请求均使用 `with_evidence=True`；验证 GET、endpoint、
API、全账户 params、摘要基础、请求/接收时间与 metadata 白名单，并成功
提交后，才可 `transport_evidence_complete=true`。第七项 positions 明确
派生账户原文，无独立 REST 证据。规范化订单 data 与原始 HTTP bytes 的
摘要基础不同，不把规范化 JSON 重新计算成原始字节摘要。

所有结果和清单始终保持 `as_of=None`、`as_of_ms=None`、`reconciled=false`、
`boundary_verified=false`、`acceptance_eligible=false`。资金窗口即使结构
完整，`settlement_finality_verified` 仍为 false。提交与摘要不证明外部来源
真实、延迟入账齐全、原子账户状态、精确午夜或可交易资格。

## 验证与未接入内容

离线测试使用真实 `DemoEvidenceStore`，仅将 SQL 边界替换为内存；验证冻结、
身份隔离、完整/失败重放、清单事务失败、合法链上的错误组装及来源投影
核验，不访问数据库或交易所。另有 `test_demo_collection_persistence.py` 六项
真实 PostgreSQL 集成测试，仅使用独立 quant_test 随机 schema。2026-11-17
实际 6 项通过（28.67 秒），覆盖新连接池重启、资金满页拆分、身份冲突、
清单提交阶段的真实延迟约束失败、已提交原文保留、未完成旧 ID 零网络，
以及 UPDATE/DELETE/联合 TRUNCATE 的 SQLSTATE 55000。测试后独立确认本组
随机 schema 残留为零；该结果不等于全仓或生产验证。

仍缺账户唯一 writer、生产 API 组合根、可信运行选择器、账户归属/独占
证明、午夜事件重放、完整费用与风险语义验证器，以及实际 Demo 30 天
验收。此模块不会填补这些语义缺口，也不会开放下单权限。
