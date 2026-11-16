# Demo 原生条件单窄接口

2026-11-16。此小节只交付离线合同明确的适配器，不授予正式验收资格，
也不代表真实 Demo 已验证部分成交保护或重叠替换。执行器尚未调用新方法。

接口固定为 `demo-fapi.binance.com/fapi/v1`：POST/GET/DELETE `algoOrder`、
GET `openAlgoOrders`、GET `allAlgoOrders`。禁止批量 DELETE `algoOpenOrders`，
禁止账户配置写入，保持无自动重试、TLS、最终 URL 与敏感信息守卫。限速成本
来自锁定的 CCXT 4.5.85 声明，不代表账户/IP 实际剩余配额。

提交仅允许 STOP_MARKET/TAKE_PROFIT_MARKET、BOTH 单向、明确数量与
reduceOnly=true。`working_type` 和 `price_protect` 必须由调用方显式传入，
不能由适配器猜测冻结策略。触发价使用 triggerPrice，稳定身份使用 clientAlgoId；
数量不取整，按市价数量过滤器和触发价 tick 校验，保护退出不因入口最小名义额
而上调数量。closePosition、条件入口与原生百分比 trailing 未开放。

调用方必须先持久化不可变意图并成功 claim，再调用提交。适配器内存还会永久
拒绝相同稳定 ID 的第二次尝试；超时、损坏回执、身份/语义不符及重复 ID 错误
保持未知，只能查询。此内存防护不能替代跨重启的数据库账本。确定拒单也不
重用 ID。新接口记录中文提交结果日志，只包含安全身份、数量、价格和错误类型。

算法单独立保存 algoId、clientAlgoId、algoType、orderType、algoStatus、
reduceOnly、closePosition、workingType、quantity、triggerPrice 和 actualOrderId。
TRIGGERING/TRIGGERED/FINISHED 都不证明真实退出；execution_complete 始终 false。
自有算法单可以按 actualOrderId 查询真实普通子单，保留子单自身的 clientOrderId；
不假定它等于算法单客户端 ID，不把算法状态映射成普通单 FILLED。

撤销前核对 durable 所有权及实际算法单 ID。官方精确字符串 code="200" 与
兼容整数 200 只表示撤销命令已接受。损坏 DELETE 回执使用
conditional_cancel_unknown，不冒充“未发生写入”的本地拒绝；仍需查询算法单、
子单、成交和持仓。查询不存在不证明可以重发，也不证明无残留风险。

全账户条件单快照保留外来币种和外来客户端身份，便于后续阻断归属冲突；
不会自动取得它们的撤销权。历史查询支持小于七天的显式窗口和 inclusive algoId
游标；单页不是完整归档，调用方必须完成分页、重叠去重和留存期限处理。

官方依据为 [USDⓈ-M REST Trade](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
及 [User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams)。
公开合同不能证明当前独占 Demo 账户实际支持安全重叠替换。后续仍需条件单
账本、唯一 writer 保护协调器、逐次 partial fill 覆盖、先确认新保护再撤旧保护、
触发/撤销竞态和受控 Demo 证据。资格字段继续为 false。

验证：先观察35项缺失能力失败，再实现；独立复核补充10项回执/ID/类型失败、
中文日志失败和子单/历史查询、非法类型失败后修正。最终两文件回归147项通过、218子测试
通过，Ruff与差异检查通过。并行新迁移在一次集成进程已初始化后出现，导致
10项集成被“待迁移”兼容检查阻断；该次结果不计通过，迁移稳定后重新运行。
