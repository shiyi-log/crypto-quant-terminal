# 已封存账户观察与精确权益一致性

`DemoEquityObservation` 从真实 `DemoEvidenceStore` 读取已经封存的采集清单，
离线重放原文并计算账户响应内部的金额一致性。它不采集、不加载凭据、
不发命令、不提供策略批准，也不证明某一历史时点的账户状态。

## 输入和调用

构造必须显式提供真实归档实例、`FrozenCollectionBinding` 和
`FrozenObservationPolicy`。完整五字段运行身份及五项 hash 由内部组合根
固定；观察政策另绑定该摘要。政策没有隐式默认值：

```python
from demo_equity_observation import DemoEquityObservation, FrozenObservationPolicy

policy = FrozenObservationPolicy(binding, {
    'schema': 'demo-equity-observation-policy-v1',
    'account_api': 'fapiPrivateV3', 'account_path': 'account',
    'asset': 'USDT', 'multi_assets_margin': False,
    'equation': 'wallet_plus_unrealized_v1',
    'money_scale': 24, 'max_integer_digits': 18,
    'max_assets': 64, 'max_positions': 2048,
})
producer = DemoEquityObservation(archive, binding, policy)
observation = producer.calculate(completed_capture_manifest_ref)
receipt = producer.persist(completed_capture_manifest_ref)
```

`calculate` 只接内部已封存 account capture 的 end manifest ref；没有外部
value、resolver、verified 或 semantic_validator 参数。income 清单不能代替
账户采集，身份、计划或归档链错误返回固定 `ObservationError`。

`DemoCollectionRun.replay_completed()` 是公开只读入口。它先检查真实唯一
start/end、冻结身份、预算和操作参数，再重用离线重放；不存在的 ID 或未
完成清单直接拒绝，不执行 `_prepare/_execute`，不创建 start，不访问原
适配器或当前时钟。已封存失败可作为失败事实原样重放；观察生产器将其
标记为 `observation_capture_incomplete`。

## 精确计算与未知值

金额只接普通十进制字符串；不接 float、bool、NaN、指数形式、前导零、
超过政策精度的数。最多18位整数和24位小数，按冻结 scale 无损转换为
Python整数后运算，不使用默认28位 Decimal context。结果按普通十进制
字符串返回，去掉无意义末尾零，不改变数值。较小精度和资源上限可以
显式冻结为新政策；不会静默四舍五入。

检查以下实际响应关系：

- account：`totalMarginBalance = totalWalletBalance + totalUnrealizedProfit`。
- 唯一 USDT asset：`marginBalance = walletBalance + unrealizedProfit`，且与
  account 三项相等。
- 全部原文 position 的 `unrealizedProfit` 之和与 account 浮盈亏相等。

同资产、同(symbol, positionSide)重复、缺金额/列表、非USDT资产任一非零
金额、不支持的多资产模式、矛盾等式或非USDT合约都拒绝计算成功。策略
白名单以外的 USDT symbol 保留，不能过滤后才核对。一个响应混合 BOTH
和 LONG/SHORT 模式也拒绝，multiAssetsMargin 不能充当 hedgeMode 证明。

字段白名单固定为守卫适配器当前保留的 V3 account/asset/position 合同；
可选金额若存在也检查精确格式。未知字段不能将 `verified` 等扩展真值
透传。缺失金额不填零，缺失仓位不当空仓。V3省略零行只涉及该响应内容，
因此即使仓位列表为空，产物也不签发 `flat`。

`isolatedWallet/isolatedMargin` 是已经分配的抵押，不能再加进账户钱包；
availableBalance、crossWalletBalance、notional 不是净权益。真实手续费与
资金费已反映在钱包中，不再次扣减；浮盈亏不乘杠杆。这里只核对账户
返回的浮盈亏，未从 Demo mark 重建仓位估值。

## 输出、归档与日志

产物 schema 为 `demo-equity-observation-v1`，保存冻结政策/身份摘要、真实
start/end/组件引用及 record hashes、明确的金额结果、原始资产和仓位
副本、模式观察值、server clock bracket、本地 observed interval 与各个
HTTP started/received interval。这些区间分开保留，不命名为 state_at。

响应内等式成立时可返回 `calculation_status=computed` 与
`response_consistent=True`；这只证明按固定规则复算一致。失败返回
`not_estimable`、固定原因码和 `amounts=None`，不复制未验证正文中的任意
文字；原文仍留在归档，可按引用审计。

以下字段始终为 None 或 False：

```text
as_of=None, state_at_ms=None, data_complete=False, reconciled=False,
funding_finality_verified=False, boundary_verified=False,
acceptance_eligible=False, production_authorized=False
```

`persist` 重新复算后只追加 equity_reconciliation 独立观察 schema，稳定键
绑定整个结果摘要；真实提交并重新读回身份/正文后才返回回执。相同输入
重启重算、重复提交复用原记录；不覆盖或修改来源。产物使用
`source_collection_id`，避免污染旧采集运行按 `collection_id` 读取的清单。

此观察 schema 不属于 safety provider 或 acceptance projection，不能直接
注入风险运行时。日志使用中文固定状态、数量和原因码；不记录金额、账户
标识、原文或底层异常文本。

## 验证范围与后续边界

离线测试使用真实 DemoEvidenceStore Python 归档链，SQL边界为 MemorySQL；
验证实际已封存清单、合法链错误组装、篡改、重启、精确金额、显式零与
缺失、额外字段、资源限制、提交失败与回执、不触发采集和日志脱敏。
本切片不包含真实 PostgreSQL 测试或生产组合根。

REST 请求时间、serverTime、局部 updateTime、短页流水、钱包差额吻合，
都不能独立证明午夜状态或资金费最终性。没有全局序号的用户流与普通
公共行情也不能自行补齐这些证明。正式公共 Futures mark 不能当作 Demo
mark；mark 的 E 是事件时点、T 是下次资金费时间。

后续还需私有事件和 Demo mark 的真实归档、会话/缺口与各域最终性合同、
可信起点、订单/成交/现金流交叉对账、冻结边界估值规则及修订政策。
原有 `build_risk_evidence` 只接受 direct_exchange_asof，不将重放改名为
交易所原始快照。观察计算、代码发布或归档存在均不构成午夜、Demo预检、
30天验收或真实交易授权。
