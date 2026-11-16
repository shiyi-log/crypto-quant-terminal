# Demo 账户证据只读接口

本小节实现账户归档采集所需的固定 Demo 只读原语，不是账户对账器，也不产生
精确午夜余额、完整资金覆盖或正式验收资格。没有为本次开发调用真实交易接口。

## 调用合同

- fetch_income_window(since_ms, until_ms, limit=1000) 固定使用 GET /fapi/v1/income，
  请求仅包含 startTime/endTime/limit。不按币种或流水类型过滤；保留白名单外
  币种、未知收入类型和非 USDT 资产，供后续协议明确判定。金额保持交易所原始
  十进制文本，tranId、incomeType、time、symbol 和 asset 保留；缺身份、非法
  金额或越界时间使响应无效。
- 窗口两端都包含；允许单一毫秒窗口，跨度严格小于七天，limit 为 1–1000
  的整数。七天限制是本地冻结采集协议。当前官方合同没有 page 参数，此接口
  不生成分页或完整结论。满页应继续递归分窗，单毫秒仍满页必须标记证据不足。
  tranId 的去重作用域由流水类型共同定义。
- fetch_multi_assets_mode() 读取 GET /fapi/v1/multiAssetsMargin，只接受明确
  的布尔值；缺失、数字零、字符串 false 均不能推断成关闭。没有配置写操作。
- fetch_server_time() 读取公开 GET /fapi/v1/time，只接受非负整数 serverTime。
  它是时钟对照，不是账户净值的历史时点。
- fetch_account_snapshot() 读取 GET /fapi/v3/account，返回原始 Binance 字段
  及金额文本，保留 totalWalletBalance、totalUnrealizedProfit、
  totalMarginBalance、assets、positions。不生成 as_of 或已对账声明；顶层
  只接受已列出的官方账户字段，不透传替身注入的资金完整、日初、已对账声明。
  原 fetch_balance 兼容行为保持原样。

私有读取需要显式 Demo 凭据；服务端时间无需凭据。最终 URL、固定 Demo host、
TLS、禁止重试和重定向、敏感字段清理沿用原有守卫。新增 income 和
multiAssetsMargin 仅允许 GET，POST/PUT/DELETE 均被拒绝。

## 请求证据与并发

四个方法均可指定 with_evidence=True，返回 data 和 request_evidence。data
仍为上述列表、布尔、整数或账户字典；证据包含 path/api/method、request_params、
started_at_utc、received_at_utc、latency_ms、response_sha256、
response_hash_basis，以及安全的状态、版本与成本元数据。

结果验证、摘要读取和证据复制在同一请求锁内完成。采集器应使用这个封装，
避免在并发请求后从可变 last_call 猜测哪个摘要属于当前响应。旧响应摘要在
新请求前清空；只有当前守卫传输捕获了响应字节，基础才为
raw_http_response_bytes。注入的离线替身缺少这些字节时，明确使用
canonical_json，摘要针对完整脱敏原始响应；不能冒充交易所 HTTP 证据。

真实 HTTP 摘要可能包含已被脱敏或未列为账户输出字段的内容，因此单凭输出
对象不能复算实际字节摘要。持久归档还需可靠保存来源与内容、原子提交回执、
稳定账户身份和审计链。本接口不提供归档持久化。

## 证据边界

- asset/position.updateTime 是最后变化时间；签名、本地接收、服务端时间都
  不能替代精确午夜账户净值。没有可信边界证据时继续阻止新增风险。
- 原生保护资格仍为 false，余额和资金读取不解除任何批准、风险或保护门禁。
- 请求成本来自已安装 CCXT 4.5.85 元数据：income 30、multiAssetsMargin 30、
  time 1；V3 account 沿用原有保守成本 5（CCXT 当前元数据为 1）。这些只是
  本地限速配置，不证明实际账户或 IP 配额。
- 官方历史流水有保留期；接口成功或空列表不能证明轮次全历史完整。

## 本小节验证

新增 58 项测试先观察到预期失败，再实现固定只读合同；独立复核新增 5 项边界
也先观察失败后修正。2026-11-16 本机账户读取、原生条件单及既有适配器
三文件合计 210 项通过，218 个子测试通过；
Ruff、Python 编译和差异检查通过。验证全部采用离线响应，没有读取真实凭据。

官方合同参考：[USD-M REST Account](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)。
