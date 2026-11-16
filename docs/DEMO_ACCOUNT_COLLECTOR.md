# Demo 账户与全类型资金流采集

`demo_account_collector.py` 是只读编排模块，没有线程、定时服务、数据库连接或
交易命令。调用者注入受守卫的 Demo 适配器和不可变归档接口。归档方法返回
确认提交的回执后，采集结果才能引用该事实；归档失败不当作空响应。

## 资金流窗口

`DemoAccountCollector(adapter, archive, scope).collect_income(since_ms=…, until_ms=…)`
使用 `fetch_income_window(..., with_evidence=True)`，不发送币种或类型筛选。
范围含起止毫秒，先拆成差值严格小于七天的连续块。满页响应先归档，再把
窗口二分成 `[left, middle]`、`[middle+1, right]`，直到每个叶响应严格短于
limit。单毫秒仍满页、请求预算耗尽或父响应中的事实在所有叶窗口消失时，
返回 `complete=false`。没有已审查的 page 游标，也不会伪造一个空终页。

父响应不参加最终求和，完整叶窗口转换为既有 reducer 的 `income_scans`。
每叶只有 `page=1`，这是本地文件结构序号，不是远端分页声明。交易身份采用
`incomeType+tranId`；相同内容重复和不同内容冲突都会阻止完整结论。未知
资金类型、非 USDT 资产和支持交易名单外的币种均保留归档；未知类型及非
USDT 资产阻止当前资金协议通过。金额不经 float 或默认精度归一化。

这里的 `complete` 表示已捕获窗口的结构覆盖和当前类型协议符合性，
`settlement_finality_verified` 始终为 false。它不证明交易所没有延迟补记
事件；钱包差额、费用、成交、阶段和日初仍须独立对账。

## 账户观察

`capture_account()` 顺序归档服务端时间、账户原文、账户响应派生的仓位、
多资产模式、普通挂单、条件挂单，再次服务端时间。每部分成功独立保留
source_ref；读取失败不变成空列表。服务端时钟倒退标记失败。
真实非空归一化订单的 `request_latency_ms` 是适配器本机计时 float，采集
时仅将这一受控元数据转换为十进制文本；不修改适配器对象，也不放宽任何
价格、数量或费用 float 的归档拒绝规则。

账户、模式和时钟使用原子请求证据封套，避免并发借用 mutable last_call。
当前普通与条件挂单旧接口没有该封套，保存观察并明确
`transport_evidence_complete=false`；派生仓位引用账户原文，不冒称另一次
独立 REST 查询。多次请求的收集完成不代表原子账户快照。返回值明确保留
`as_of_ms=null`、`reconciled=false`、`boundary_verified=false`。

原始 HTTP 字节摘要与 sanitized JSON 摘要基础由适配器分别声明。保存
sanitized 原文和字节摘要不等于保存原始字节，也不独立证明凭据归属或账户
独占。账户 scope 必须由服务端持久绑定，不能来自调用方 HTTP 布尔声明。

## 午夜槽位和风险投影

`DailyBoundarySlot(scope, midnight_ms)` 仅接受精确北京时间午夜。边界到达前
为 pending，缺证据为 missing，冲突后为 conflict；缺失和冲突阻止新增风险。
`fulfill(ref, resolver=internal_resolver, proof_validator=internal_validator)`
读取已提交 boundary，再逐个解析 `source_refs`，核对真实存在、提交、类型、
账户/阶段及正文固定的 `source_record_hashes`。解析器只能证明存储和链完整，
必须另注入内部语义验证器核验精确时点、完整覆盖和估值。验证器返回绑定
proof_record_sha256、来源record哈希、scope、method、state_at及估值hash的
结构化结果；单一 True、coverage_complete声明或单独 archive.read 均不够。
它接受 direct_exchange_asof 或明确命名的
replayed_boundary_projection；同证明幂等，改变证明不覆盖旧值。

`entries_allowed` 只表示边界这一项门禁已满足，`boundary_only=true`；
它不代替批准、风险预算、原生保护、资金对账和完整执行门禁。槽位本身是
纯内存状态机，调用者必须把候选/冲突写入持久归档并在重启时恢复。

`build_risk_evidence(..., proof_validator=internal_validator)` 仅从解析并通过
同一语义验证合同的归档引用组装现有净权益 reducer 输入，并逐一核对流水
叶原文。内容寻址正文保存底层来源引用及hash；读取验证后才向 reducer 的
独立副本注入当前投影的真实 receipt.ref，避免不可构造的正文自引用，且不
修改历史归档。现 reducer 仅接受
exchange_account_snapshot；因此本方法只接受 direct_exchange_asof，不能
把重放边界改名为原始快照。普通 capture_account 返回值不会被接为可信
权益。真正重放来源合同需另行版本化和评审。

REST 请求时间、响应接收时间、serverTime、资产或仓位 updateTime 均不能
独立证明午夜整个账户权益。调度器准点发请求不构成 verified。缺少真正
远端时点或完整、已冻结估值规则的事件重放时，继续保持不可评估。日中启动
阶段也不能伪造午夜以满足 stage_start<=day_opening；正式30自然日轮次的
首日启动合同仍须明确。

## 尚未接入

- launchd 常驻调度、账户用户流/标记价完整性水位与实际午夜重放。
- 账户独占/凭据轮换归属、已归档真实资金及原生接口的受控合同证明。
- 生产 writer 的可信证据 provider、权威完整费用与30天验收汇总。

所有模块输出固定 `acceptance_eligible=false`。没有读取真实凭据或发出
真实交易请求作为开发验证。日志记录账户、阶段、窗口、组件、数量、原因码
及异常类型，不记录上游异常消息、签名 URL、密钥或响应正文。
账户和阶段标识在任何请求或日志前拒绝 `://` 和 `@`，避免把连接串或
userinfo 当作可记录身份。

官方协议核对入口：
[USDⓈ-M REST Account](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)。
2026-11-16 文档未列 income page 游标；incomeType 不传返回全部类型，
limit 最大1000，历史保留三个月，tranId 在同类型内唯一。七天分块是本地
冻结采集协议，不声称是该接口已证实的最大区间。
