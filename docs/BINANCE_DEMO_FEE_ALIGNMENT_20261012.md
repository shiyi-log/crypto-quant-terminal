# Binance Demo 手续费对齐（2026-10-12）

## 当前费率表

币安 Demo 账户的签名 `/fapi/v1/commissionRate` 查询记录显示，BTCUSDT、ETHUSDT、TRXUSDT 均为 maker `0.0002`、taker `0.0004`/边。十笔实际 Demo 成交均为 taker，按 `commission / (price × quantity)` 验算为 `0.0400%`/边；`commissionAsset` 均为 USDT。maker 目前仍只有账户报价，没有实际 maker 成交验证。

| 执行角色 | 每边费率 | 往返费率 | 证据状态 |
|---|---:|---:|---|
| Maker | 0.0002（0.0200%） | 4 bp | Demo 账户报价；待 maker 成交核验 |
| Taker | 0.0004（0.0400%） | 8 bp | Demo 账户报价及十笔实际成交回报 |

费率证据来自 `quant.demo_account.payload.commissionRates` 与 `quant.demo_fills` 的冒烟/秒级测试成交记录。实际账本费用仍只读取交易所成交回报，不用费率表补写缺失的 commission、资产或 maker 身份。

## 冻结身份兼容

当前费率版本为 `binance-demo-commission-20261012-v1`。新定义显式携带该版本，并使用 maker `0.0002`、taker `0.0004`。未携带版本标记的旧定义继续使用其原冻结执行模型；其中历史 taker 费率 `0.0005` 不自动改写。更新全局默认值不会迁移既有账户、成交、执行哈希或历史报告。

这一区分只管费率表与身份兼容。成交路径决定每笔实际适用的费率：next-open 代理既不证明 maker 成交，也不证明限价单一定成交；maker 费用报价不能替代成交回报。
