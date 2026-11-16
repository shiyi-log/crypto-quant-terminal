# Demo 账户证据不可变归档

`DemoEvidenceStore` 把清洗后的账户事实原文、请求证据和引用关系固定在 PostgreSQL。
迁移为 `database/demo_migrations/0005_account_evidence_archive.sql`，不改写 0001–0004。
普通初始化不隐式安装结构；运维先显式执行全局迁移，再执行 DemoStore 的
`ensure_schema()`，运行时使用 `check_schema()` 核对迁移身份。

## 采集接口

```python
from demo_evidence_store import DemoEvidenceStore

archive = DemoEvidenceStore(data_store, account_id, stage_id)
scope = {"environment": "binance_demo", "account_id": account_id, "stage_id": stage_id}
capture = adapter.fetch_income_window(since_ms=start, until_ms=end, with_evidence=True)
receipt = archive.append(
    "cashflows", scope, capture["data"], capture["request_evidence"],
    stable_key=capture_identity,
)
fact = archive.read(receipt["ref"])
```

网络请求在 `append()` 之前完成。请求结果及元数据必须来自适配器同次调用的
`with_evidence=True` 信封，不能从共享可变 `last_call` 拼接。内部派生/离线事实
传入空 `{}`，明确表示没有传输证据。归档不自行补写来源真实性或覆盖完成标志。

允许类型为 account_snapshot、positions、normal_orders、conditional_orders、fills、
cashflows、clock、boundary、equity_reconciliation、completion、acceptance。后两类
只能使用专用 API，不能通过普通 `append()` 绕过引用核验。非空请求证据只接受已
审查 GET 路径、参数和元数据，且路径必须与归档类型匹配。原始账户、模式和持仓
方向读取可归入 account_snapshot；positionRisk 归入 positions；income 归入 cashflows。

金额字符串原样保存，Decimal 用有界、无上下文舍入的精确十进制字符串保存。
payload 中 float、NaN、Infinity 和任意对象拒绝；适配器唯一允许的 float 延迟
元数据转为有界十进制字符串。递归移除密钥字段，清除字符串 URL 查询和认证信息；
连接串、URL 或认证语法不能藏在 JSON 键、账户身份、阶段身份或稳定 key 中。
每条规范化正文上限 8 MiB。清洗后的 JSONB 不等于 HTTP 原始字节。

## 收据、幂等与完整性

收据字段：`ref`、正文 `sha256`、链记录 `record_sha256`、账户 `sequence`、`kind`、
`scope`、`committed=True`。只有数据库上下文成功提交后才返回收据；提交失败
直接抛错。同一阶段的显式 `stable_key` 和完全相同规范事实返回原收据，任何事实
或请求证据变化均报 immutable conflict。省略 key 时按正文内容寻址，因此不同
请求时间产生新采集；需要重试幂等的采集者应在网络请求前冻结本次 capture key。

一个 Demo 账户跨所有阶段共用事务 advisory lock、连续序号和前项哈希。阶段
读取拒绝其他账户或阶段的 ref。`read()` 和 `records()` 校验该账户整条可见链的
连续序号、scope、kind、ref、正文及链哈希，再返回独立副本；旧 ref 的读取也会
发现后续损坏。`records(all_stages=True)` 可供内部审计回放所有阶段。

归档表和完成索引表均有 ALWAYS UPDATE/DELETE/TRUNCATE 防改触发器。数据库
所有者仍可禁用触发器或改写整库；哈希链可发现普通破坏，但没有数据库外部签名
锚点，不能证明有权重写者没有删除整个尾段并重建链。部署权限、外部备份和可信
高水位由运维负责，不能把本收据当成账户独占或交易所签名。

## 完整往返完成事件

```python
previous = archive.previous_completions()
# reconciliation_ref 指向已归档 equity_reconciliation，其 payload 包含 cost_evidence。
result = archive.append_completion(scope, proposed_trip, [reconciliation_ref])
history = archive.completion_records()
```

专用 API 从已校验的不可变 reconciliation 事实重新运行往返成本纯函数，并
严格比较调用者提案。不能自报 complete、净收益、proof 或 risk_event。支持纯
函数首次结果以及传入 `previous_completions` 后得到的真实幂等/更正结果。归档
来源必须与完成 scope 相同，不能引用可变 documents 或错误类型事实。

首次完整 trade_id 与 proof_sha256 在一个事务内追加 completion 正文和索引，
返回 `new_event=True` 与唯一风险事件。重复提交相同经济 proof 返回原 ref，
`new_event=False,risk_event=None`；扩大扫描但经济 proof 不变时保留首次来源。
同 trade_id 的不同 proof 追加 `correction_required=True` 审计记录，永不替换
首次记录或产生第二个连亏事件；相同更正重试同样幂等。`previous_completions()`
只返回首次证明映射。调用者必须检查更正历史，并保持后续验收不可裁定。

完成读取先取得同账户事务锁，再读取 archive 和索引，避免 READ COMMITTED
下两次查询跨并发提交而伪报损坏。索引每一项必须与归档 completion 完全一致，
同时核对归档没有丢失索引。`completion_records()` 返回按账户 sequence 排序的
完整本阶段历史及 ref；调用者可持久化 sequence 高水位后自行过滤新增记录。
风险事件消费方仍需要自己的持久幂等事务；归档返回成功不等于下游已消费。

## 验收输入包

`append_acceptance_bundle(scope, identity_hashes, typed_source_refs, stable_key=...)`
要求 scope 同时包含 environment/account_id/stage_id/strategy_version_id/run_id，
并要求全部五项 code_hash/strategy_hash/execution_hash/risk_policy_hash/cost_model_hash。
来源必须分别为 account_snapshot、normal_orders、conditional_orders、fills、cashflows
五种已存在归档，完整 scope 和 payload.identity_hashes 逐项相同。payload 中若
另有 scope 身份也不得冲突。账户级采集若尚未绑定策略/运行，不能直接签发该包，
应由内部可信绑定过程另行形成有明确运行 scope 的证据。

输出只有 `status=evidence_bundle_recorded`。所有完成与验收派生正文固定
`production_wired=False,acceptance_eligible=False`。归档层不裁定账户独占、实际
费用完整性、精确北京时间午夜、边界覆盖、风控释放、原生保护资格或验收通过，
也未连接生产执行器、调度器或 HTTP 批准入口。

## 验证

`./.venv/bin/python -m pytest tests/test_demo_evidence_store.py -q`

数据库用例必须通过 QUANT_TEST_DATABASE_URL 指向 `quant_test`，并使用随机 schema；
不使用生产库。覆盖精确金额、秘密清洗、提交失败、typed 请求、跨阶段链、并发
幂等、ALWAYS 防改、链损坏、同快照完成读取、成本重算、更正及完整运行绑定。
