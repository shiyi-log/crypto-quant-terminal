# 内部归档安全 Provider 桥接

ArchiveProviderBridge 只读 DemoEvidenceStore 的已提交证据链与完整交易索引，
为 SafetyRuntime 提供四个内部 provider。构造时冻结 environment、account_id、
stage_id、strategy_version_id、run_id，以及 code_hash、strategy_hash、
execution_hash、risk_policy_hash、cost_model_hash 五项 SHA-256。

这不是采集器、交易开关或验收裁定器。记录存在只证明已存字节和引用关系，
不能证明精确午夜、全部流水、账户独占、报价可执行或保护覆盖。没有可信内部
语义校验器时，provider 返回 committed=False、固定 reason_codes、
acceptance_eligible=False。真实适配器原生保护资格仍为 False。

## 组合根配置

构造参数 archive 必须为 DemoEvidenceStore；scope 必须完整匹配该归档账户、
阶段与 Demo 环境。selectors 是服务端内部 callable 字典，分别包含 context、
entry、protection、completion。每个 selector 接收冻结 scope、identity_hashes、
now_ms；entry 另接 event/context，protection 另接 positions。selector 返回内部
归档 ref。不能把 HTTP ref、document 可变快照或客户端布尔值变成 selector。

max_age_ms 必须明确为上述四类指定非负整数年龄上限，不使用隐含默认值。
semantic_validator 为内部可信函数，必须验证语义与底层来源的真实性；不能是
从 payload 复制 verified=True 的函数。测试中的受控 stub 只验证接口与链绑定，
不会作为生产真实性实现。

archive_runtime_factory(bridge) 返回现有控制器 safety_runtime_factory 兼容
工厂。工厂核对 account_scope、strategy_id、DataStore 实例、环境及已有验收
身份，创建同 namespace 的 DemoRiskReservationStore，再注入四个 provider。传入
非空 acceptance_round 时必须包含 controller 当前使用的九项绑定：round_id、
environment、account_scope、strategy_id、rule_hash、execution_hash、status、
approval_id、approved_by。文本须非空、首尾无空白、不含控制字符或 URL，长度
不超过 512；环境、账户、策略、两个哈希以及 active 状态必须与桥接器冻结
身份一致。round_id 标识批准轮次，不要求等于归档 run_id；归档运行身份由
桥接 scope 固定，入口启用前由 Controller 重新验证当前批准。
它不安装表、不修改账户模式、不采集、不开始交易。API 组合根由调用方另行
接线；缺配置的默认生产运行时保持关闭新增风险。

## 类型化投影归档

四类投影均为 equity_reconciliation，完整运行 scope 不可省略；正文包括：

- schema 固定 demo-safety-provider-v1，provider 为 context/entry/protection/completion。
- identity_hashes 为上述五项完整哈希，as_of_ms 为非负整数。
- value 为冻结投影：context 对象、完整 EntryDecision source 对象、protection items
  数组，或 completion 覆盖对象。
- source_refs 为下表角色到真实归档 ref 的映射；source_record_hashes 固定每个
  ref 对应的 record_sha256。引用必须比当前投影更早提交。

| 投影 | 必需来源角色与归档类型 |
| --- | --- |
| context | account→account_snapshot、positions、normal→normal_orders、conditional→conditional_orders、fills、income→cashflows、clock、stage_opening→boundary、day_opening→boundary |
| entry | market→equity_reconciliation、exchange_rules→equity_reconciliation、clock |
| protection | positions、normal→normal_orders、conditional→conditional_orders、fills、clock |
| completion | account→account_snapshot、normal→normal_orders、conditional→conditional_orders、fills、income→cashflows |

同一真实边界可同时承担阶段起点和日初角色，但不能用错误类型替代。每条来源
都通过 archive.read 重新校验整个账户链，核对完整运行 scope、五项 hash、记录
hash 和提交顺序。普通只带账户 scope 的采集记录不足以直接作为该运行权威。
行情和交易规则目前没有专用原始归档 kind，必须先有独立审查的内部派生投影，
不能以记录标题或空 items 推断内容完整。

semantic_validator 接收投影记录副本、sources(ref→完整来源记录)副本，以及
purpose 和 binding。仅接受完全匹配的回执：verified=True、purpose、scope、
identity_hashes、projection_record_sha256、source_record_hashes。校验器必须
依据原始事实验证覆盖、时间、估值、所有权及费用，而不是将此回执形状当证明。
修改副本不能改写已存归档或桥接器配置。

## Provider 输出与失败

调用四个 provider 时仅允许 account_scope、strategy_id、now_ms，以及各自
明确的内部附加参数。任意 HTTP 风格 ref/verified 参数触发固定拒绝码。
成功信封包含 committed=True、archive_ref、account_scope 和 context/source/
items/events；失败包含 committed=False 与固定 reason_codes，不包含底层异常
文本。所有输出 acceptance_eligible=False。

缺 selector、缺年龄限制、损坏链、错误 kind/scope/hash、未来或过期投影、缺失
引用、hash 不匹配、未验证语义和费用更正分别返回明确原因。Provider 只读，
没有网络和写事务；每次 archive.read 自行完成存储读取后再执行语义核验器。
组合根也不得把网络或长任务塞入数据库读取事务。

## 完整成本与重放

completion value 包括 completion_refs 列表和 scan_through_ms。覆盖投影须新鲜、
经过相同语义核验，并精确列出当前真实 completion_records 索引的全部 ref。
空索引本身不能证明没有遗漏完成交易；没有已验证覆盖投影时仍不可用。

桥接器逐条读取真实完成正文及 source_refs，再从已归档 cost_evidence 重新
运行 reconcile_round_trip_costs，核对 scope、五项 hash、完整交易、资金费及
风险事件。任何 correction_required 都关闭本次完成事件 provider，不能静默
保留先前盈利或删除历史。完成时间不得晚于已验证 scan_through_ms。

输出使用完整 durable 索引，按经济 timestamp_ms 排序并保留稳定 event_id/
trade_id。精确重复读取返回同一事件；桥接器不以进程内 seen 集合丢弃记录，
风险账本负责持久幂等消费。SafetyRuntime 另外执行持久经济时间水位规则：
跨批次晚到的更早新事实或无顺序证明的同刻交易关闭入口，不因排序当前索引
就声称可安全改写已消费连亏历史。completion_sequence 和 completion_refs 可供
内部审计，但不是放行或释放预占的许可。

## 当前验证与限制

离线测试通过真实 DemoEvidenceStore Python 实现的链和完成索引 API，底层 SQL
边界替换为内存，不访问 PostgreSQL 或交易所。它验证跨组件合同，不替代真实
数据库隔离、生产来源真实性、不可变存储的外部锚点和账户验收。

生产仍需真实归档投影生产者、内部语义验证器、可信引用选择策略和明确的
API 组合根。当前不生成午夜值、完整成本、原生保护或批准事实，不依据 HTTP
声明放行，也不宣称 P1 全部完成或 30 天验收通过。
