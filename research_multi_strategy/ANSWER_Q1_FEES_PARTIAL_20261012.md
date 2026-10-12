# Q1 部分答案：Demo 实际费率（账户报价，非成交回报）

> 2026-10-12 · DSH · 目标第 65 轮 · **Q1 前半完成；后半需真实成交**

## 一、已拿到的硬数据

**来源**：币安官方 Demo 签名账户查询（`/fapi/v1/commissionRate`），
落盘于 `quant.demo_account.payload.commissionRates`。

| 币对 | maker | taker |
|---|---|---|
| BTCUSDT | 0.0200% | **0.0400%** |
| ETHUSDT | 0.0200% | **0.0400%** |
| TRXUSDT | 0.0200% | **0.0400%** |

**往返成本**：maker **4.0 bp** · taker **8.0 bp**

## 二、与项目假设的对比

| 项 | 项目假设 | Demo 实测 | 差异 |
|---|---|---|---|
| maker | 0.0200%/边 | 0.0200%/边 | ✅ 一致 |
| **taker** | **0.0500%/边** | **0.0400%/边** | ❌ **项目高估 25%** |
| taker 往返 | 10.0 bp | **8.0 bp** | −2 bp |

**⚠️ 影响评估**：taker 门槛从 10 bp 降到 8 bp（降 20%），
但**最佳毛边际仅 1.401 bp**（见 ANSWER_Q2）→ **「秒级不可行」的结论不变**。

## 三、⚠️ 关键限制：这是「报价」不是「成交回报」

```
commissionRateSource: "demo_signed_account_commission_rate_quote_not_fill"
```

**Q1 的要求原文是「从实际成交回报读（commission / commissionAsset / is_maker）」** ——
本答案**只满足前半**：

| Q1 子项 | 状态 | 数据源 |
|---|---|---|
| 账户费率报价 | ✅ 已得 | `demo_account.payload.commissionRates` |
| **实际成交的 commission** | ❌ 尚未 | 需 `demo_fills` 有数据（**当前 0 行**） |
| **commissionAsset**（是否用 BNB 抵扣） | ❌ 尚未 | 同上 |
| **is_maker**（实际是 maker 还是 taker 成交） | ❌ 尚未 | 同上 |

**⇒ Q1 的完整答案必须等真实下单成交后才成立。**

## 四、附带确认的账户状态

```
wallet_balance:    5000.00000000 USDT
available_balance: 5000.00000000 USDT
margin_balance:    5000.00000000 USDT
assets: USDT 5000 · USDC 5000 · BTC 0.01 · 其余 0
positions: []（无持仓）
demoFingerprint: 3f723a881743091b
endpoint_host: demo-fapi.binance.com  ✅
```

→ **Demo 账户有资金、无持仓、连接正常 ⇒ 具备下单条件。**

## 五、当前唯一阻塞

```
✅ 凭据已配置（updated_by=admin · 2026-10-12 03:31:48）
✅ 连接正常（账户/仓位快照已落盘）
✅ 账户有资金（5000 USDT）
✅ 五表已建（demo_orders/fills/positions/account/signal_comparisons）
❌ demo_orders = 0 行 → 【runner 尚未启动】

⇒ 下一步 = 人工启动闸门: 选一个已冻结的单腿账户 → 点击「开始」
   （DemoRunnerController.start(account_id) 是设计上的人工闸门）
```

## 六、我的更正记录

**第 47 处自查**：我曾怀疑「资产名被截断（USDT→U）」与「余额全 0（解析 bug）」。
核对后：`'U'` 是币安真实返回的资产（USDT 也在列表中）；余额并非全 0
（USDT 5000 / USDC 5000 / BTC 0.01）。**适配层没有篡改响应。**
→ 又一次「先怀疑、后核对」，且核对得很快。
