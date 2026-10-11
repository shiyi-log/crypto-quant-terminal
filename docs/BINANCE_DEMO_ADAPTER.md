# Binance Demo 专用适配器

`binance_demo_adapter.py` 提供 USDⓈ-M 永续 Demo 传输边界，当前合同版本
`binance-demo-usdm-v1`，CCXT 版本 `4.5.85`。默认只读；实现及测试不需要现有实盘
密钥，不读取 `.env`、Freqtrade 实盘配置或任何旧账户凭据。

用户选择的密钥入口是前端配置与数据库加密保存。凭据提供方将独立 Demo API key/secret
作为显式构造参数注入适配器；适配器不创建 API key。`from_demo_env` 仅是可选入口，
只识别 `QUANT_BINANCE_DEMO_API_KEY` / `QUANT_BINANCE_DEMO_API_SECRET`，不加载文件。
切勿将 key、secret、签名 URL 或请求头写入代码、日志、交易原始记录或 Git。

```python
from binance_demo_adapter import BinanceDemoAdapter

# demo_key / demo_secret 由独立加密凭据提供方注入；示例不包含实际凭据。
adapter = BinanceDemoAdapter(api_key=demo_key, api_secret=demo_secret)
# 默认只读。开启交易由 Demo runner 的人工配置闸门单独控制。
```

## 环境与传输边界

- 使用 `enable_demo_trading(True)`，不同时开启 sandbox。
- 唯一允许网络主机：HTTPS `demo-fapi.binance.com`；按 API 家族固定 `/fapi/v1`
  或 `/fapi/v3`。API 版本、sandbox、地址、日志或传输配置漂移时拒绝操作。
- 使用直接 FAPI 请求，不调用 CCXT `load_markets`，因此不隐式访问 Spot/SAPI。
- 构造后移除 CCXT 的 live/testnet URL 备份；签名阶段和最后 HTTP 阶段都检查地址。
- HTTP 重定向、代理环境继承及重试均禁用；CCXT 端点级重试选项也无法重新开启重试。
- 禁止输出 CCXT 调试日志；不保留最后签名请求、签名 URL、请求头或请求体。
- CCXT 升级需要复核合同；安装版本不等于已复核版本时拒绝构造生产客户端。
- 不提供调杠杆、调保证金模式、切换持仓模式或实盘环境的接口。

仅 BTC/ETH/TRX 的 `*/USDT:USDT` 永续可下单。全账户仓位及完整 `openOrders`
只读快照会保留外部币种，让 runner 能发现归属冲突；这些快照不会授权其交易。

直接 FAPI 调用以已复核的 CCXT 端点限速成本为基础，显式配置并作保守调整，而非所有请求默认 cost=1：
无币种 openOrders 为 40、有币种为 1；positionSide/dual 为 30；symbolConfig、
allOrders、userTrades 为 5；order 查询/提交/撤单为 1/4/1。V3 account/positionRisk
在 CCXT 源码标 1，本次无法独立访问官方权重文档，采用保守 5，不能称为实测权重。
未指定币种的 premiumIndex 同样从 CCXT 的 1 保守调整为 10。
不可通过传入 config 把成本降到零。exchangeInfo 返回的 rateLimits 原样保留在
`declared_rate_limits`；缺失保持未知，不替账户推断限额。runner 应低频缓存元数据，
在入场前重新确认；风险保护不应依赖高成本的全账户未结订单轮询。

## Runner 接口

`BinanceDemoAdapter(..., allow_trading=False, owned_client_ids=())` 的主要接口：

| 方法 | 返回/约束 |
| --- | --- |
| `fetch_exchange_info()` | 原始 Binance 交易规则，缓存三个允许币种 |
| `prepare_limit_order(pair, side, quantity, price)` | 数量向下取整，买价向下/卖价向上按 tick 取整，再检查最小量/名义；返回 `quantity`、`price` 十进制字符串 |
| `validate_order(pair, quantity, price, market=False)` | 拒绝非法量价，不自动舍入 |
| `fetch_orders(pair, since_ms=None, limit=1000)` | 按币种历史订单；不可代替完整未结订单查询 |
| `fetch_open_orders(pair=None)` | 完整未结订单；无币种参数时包括外部符号供归属检查 |
| `fetch_order_by_client_id(pair, client_order_id)` | 仅 `OrderNotFound` 返回 `None`；网络失败仍抛错 |
| `fetch_trades(pair, since_ms=None, end_ms=None, order_id=None, from_id=None, limit=1000)` | 实际 userTrades；显式时间区间必须少于七天；`from_id` 按成交 ID 分页，禁止和时间/订单 ID 同发 |
| `fetch_positions()` | 全账户只读快照，含 `signed_qty`、`position_side`、`allowed_pair`、原始字段 |
| `fetch_position_mode()` | GET `positionSide/dual`，即使无仓位也返回 `hedged`；缺字段拒绝 |
| `fetch_commission_rate(pair)` | GET `commissionRate`，权重 20；返回原始 `symbol` / `makerCommissionRate` / `takerCommissionRate` 字符串，缺失或非法值拒绝 |
| `fetch_symbol_config(pair=None)` | GET `symbolConfig`，记录实际杠杆、保证金模式和原始字段，不修改配置 |
| `fetch_balance()` | 只读余额/权益与原始 account 字段 |
| `test_limit_order(pair, side, quantity, price, position_side='BOTH')` | 仅 POST `order/test`（成本 1），可在只读模式检查签名测试下单请求；不调用真实 `order`，不创建本地订单或成交 |
| `submit_limit_order(pair, side, quantity, price, client_order_id, post_only=True, reduce_only=False)` | 默认 GTX；仅已启用交易且有显式 Demo 凭据时提交 |
| `submit_market_order(pair, side, quantity, client_order_id, reduce_only=True)` | 只允许 reduce-only 保护退出，不舍入实际持仓数量 |
| `authorize_owned_client_id(client_order_id)` | 只应从已绑定的持久订单账本恢复归属 |
| `cancel_order(pair, order_id=None, client_order_id=...)` | 要求归属注册，先核对服务器 ID，再撤订单；不撤外部订单 |

量价准备之后 runner 仍须按实际舍入后的量价复核风险预算、名义上限和余额。GTX
只是提交意图，不能代替实际成交 `maker` 字段，也不能保证成交。

测试下单只接受空对象，或当前 Demo 实测的严格空订单模板：`orderId/updateTime=0`、
所有订单文本字段为空、所有标志为 false。未知字段、非零订单号或非空订单信息均拒绝，
不回退真实下单。模板保留 Binance 原生字段，但它属于诊断证据，不能写入实际订单/成交账。
成功只证明此签名测试请求被接受；普通下单的保证金、风控和实际撮合仍需前向订单验证。

适配器不会替 runner 实现真实仓位归属、信号选择、风控闸门、持久化或统计裁定。
这些都应由 runner/账本接线。禁止把直接调用上述方法视为自动上线授权。

userTrades 返回满 1000 条时不能推断完整。runner 应先保留已验证页，再按 `最大成交 ID + 1`
继续查询并过滤自己的 `orderId`，同时检查 `time` 的观察截止边界和执行数量是否对账；
无法证明完整时保持告警。CCXT 源码明确 `fromId` 不能与时间区间同发；本次官方 API
文档匿名访问返回 HTTP 202 空体，未验证 `fromId + orderId` 组合，因此适配器保守拒绝
该组合。双向持仓模式及实际杠杆应通过上述只读接口预检，不能只从非零仓位推断。

## 不确定提交与幂等

`stable_client_order_id(namespace, signal_id, pair, side, role)` 生成稳定 `qd-` ID。
runner **必须在网络调用前原子持久化提交 claim**；重复回合和进程重启也不能重发。
适配器自身还在校验后用锁原子 claim，阻断同实例并发重复 POST。

明确的交易所拒单抛 `DemoExchangeRejected`，`error_kind=exchange_rejected`，只保留
安全的异常类别和数字错误码；该 ID 仍不能复用。重复 ID 类错误保持不确定，因为服务器
上可能已有之前的订单。超时、网络错误、错误或身份不匹配的订单响应都抛
`DemoSubmissionUnknown`；先按同一 client ID 查询，查询暂时不存在也不证明没有下单。

`DemoSafetyError.error_kind=safety` 表示本地护栏/校验错误。
`DemoRequestError` 不含交易所原始异常字符串，防止签名和凭据进入日志。
`last_call` 只保留路径、API 家族、方法、毫秒延迟、状态和安全错误类别/数字码。

## 费用、原始记录与结论边界

订单保留 `orderId`、`clientOrderId`、`origQty`、`executedQty`、`avgPrice`、`status`
等 Binance 原始 camelCase 字段；成交保留 `id`、`orderId`、`commission`、
`commissionAsset`、`maker`、`realizedPnl`、`time`。标准化投影供账本使用，`raw` 供
Binance 格式核对；只移除凭据、请求头、签名等认证材料。

费用仅来源于实际 `userTrades`。缺 `commission` 或资产时标为不完整；`maker` 缺失
保持未知。BNB/USDT 等混合费用按资产单独汇总，没有同一时点换算证据就不能给出完整
USDT 净利润。不会把配置费率替代为“实测”，不会把缺失费用填成零。

`fetch_commission_rate` 可在只读检查时获取当前 Demo 账户的币种费率快照，且不回退到
建模费率。两项费率必须是有限、非负的十进制字符串，返回原始字符格式；请求失败、字段
缺失或币种不匹配即失败。该快照不能替代每笔实际成交的 commission，也不证明 Demo
与实盘费率相同。

订单部分成交与未成交不从信号推断；runner 应保存提交时点、实际成交与撤单状态，
据此计算未成交率、延迟、滑点，并将同一规则/输入哈希的内部模拟与 Demo 并排比较。
该适配器没有预先认定任何秒级策略盈利或亏损，15m 边际不能外推所有秒级策略。

Binance 官方说明 Demo 的图表、订单簿定价和执行可能与实际交易不同，Demo 收益不是
未来实盘收益的可靠指标；其 FAQ 也不能证明本账户实际费率与实盘相同。费用、成交角色、
未成交和净收益必须在真实 Demo 成交后核对。Demo 结果只提供上线证据之一，实盘上线
仍需人工闸门。

来源：

- [Binance Demo FAQ](https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd)
- [Demo API key 入口](https://demo.binance.com/en/my/settings/api-management)
- 本机 CCXT 4.5.85 `binance.enable_demo_trading`、`sign`、`fetch2`、`fetch` 源码；
  本次离线复核其 Demo URL、sandbox 互斥、端点重试和请求留存行为。

## 当前验证状态

51 项离线测试及独立只读审查通过，手续费率查询已补环境、凭据、权重和非法响应回归。没有读取实际凭据、发签名网络请求、提交 Demo
订单或修改交易所账户配置。覆盖真正 CCXT 客户端的无网络构造、重定向、单端点重试与
错误环境拒绝，以及并发 claim、外部撤单、价格/数量精度、部分成交、真实费用和脱敏。
真实 Demo 手续费、未成交率、秒级净利润及可上线性尚无成交证据。
