# Demo 30 天验收读取服务

`DemoAcceptanceService` 只从已提交的 `DemoEvidenceStore` 证据链读取材料，
重建固定的 30 个北京时间日与 31 个午夜边界，再调用现有验收裁定器。服务没有
调用方 evidence、bundle ref、approval 或执行参数，也不写归档；所有返回结果
固定 `acceptance_eligible=False`、`production_wired=False`。

## 内部配置合同

构造时冻结完整 scope（环境、账户、阶段、策略版本、运行）、五项身份哈希、
轮次 ID、北京时间起始日、可信 bundle selector、可信语义校验器和时钟。selector
只接收服务生成的 binding，并返回恰好包含 `bundle_ref` 与 `manifest_ref` 的内部
映射。selector 不能接受 HTTP 参数或返回 caller-selected evidence。

Bundle 必须为 `evidence_bundle_recorded`，并链接恰好五种 typed source：
`account_snapshot`、`normal_orders`、`conditional_orders`、`fills`、`cashflows`。
所选 manifest 是独立的早期 `account_snapshot` 记录；它的
`projection_source_refs` 必须逐项等于 bundle 的五个 source refs。manifest 另列出
31 个 boundary refs、30 个 daily refs、一个 risk ref、完成历史 refs，以及每条
投影记录的 `record_sha256`。链顺序、scope、kind、引用 hash 与 manifest 顺序都要
通过归档链验证。

boundary、daily、risk 和 cost 投影使用 `demo-acceptance-projection-v1` schema。
每个投影的 `source_refs` 必须精确绑定上述五种 typed source；daily 另外引用相邻
的开闭午夜边界。投影来源必须比投影更早提交，boundary 也必须出现在 manifest
列出的边界集合中。语义校验器收到归档行副本、来源行副本、purpose 和冻结
binding；服务仅接受逐字段匹配的回执，包括 scope、身份哈希、记录 hash、来源
记录 hash 和固定窗口。校验器缺失、抛错或返回布尔值均产生证据不足。回执形状
本身不是真实性证明，生产组合根仍需提供依据原始来源审查语义的实现。

完成交易必须由归档完成索引精确覆盖；待更正历史、重复交易身份、缺交易、
不完整费用或不能重新运行 `reconcile_round_trip_costs` 的成本来源都会阻止通过。
归档行和完成索引先在账户锁下读取成同一快照，之后释放数据库连接，再执行纯
投影与语义处理。链损坏和存储错误以固定原因码返回，不传播底层异常文本。

## 范围与验证

本服务不产生午夜估值、账户持仓、订单完整性、风险覆盖或成本完整性的事实；
这些都必须由归档投影及可信语义校验器证明。未通过只能表示证据不足或已确认
失败，不会开启执行资格。测试使用实际 `DemoEvidenceStore` Python 实现和内存
SQL 边界，不连接 PostgreSQL、交易所或服务。
