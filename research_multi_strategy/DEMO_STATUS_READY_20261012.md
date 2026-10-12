# 币安 Demo 状态：已完全就绪，只差人工「开始」

> 2026-10-12 03:37 UTC+8 · DSH · 目标第 66 轮 · 全部为**核实结果**（非据文档）

## 一、就绪清单

| 项 | 状态 | 证据 |
|---|---|---|
| 凭据配置 | ✅ | `binance_demo_credentials` 1 条 · masked_key=••••LUtF · updated_by=admin |
| 环境锁定 | ✅ | `endpoint_host = demo-fapi.binance.com`（DB CHECK 强制） |
| 连接检查 | ✅ | `binance_demo_connection_check.json` @03:37:17 `connected=true, readOnly=true` |
| 账户资金 | ✅ | wallet/available/margin = **5000 USDT**；USDC 5000；BTC 0.01 |
| 持仓 | ✅ | `positions: []`（无持仓） |
| **order/test** | ✅ | @03:37:17 返回 `{}`（`/fapi/v1/order/test` 成功时的正常响应） |
| **未真下单** | ✅ | `actualOrderPostRequests: 0` |
| 费率报价 | ✅ | BTC/ETH/TRX：maker 0.0200% · taker **0.0400%** |
| 诚实标注 | ✅ | 适配层自标 `feeVerification: "requires_actual_fills"` |
| 五表 | ✅ | demo_orders / demo_fills / demo_positions / demo_account / demo_signal_comparisons |
| 前端入口 | ✅ | `vben/apps/web-antd/src/views/quant/binance-demo/index.vue`（36 KB） |
| 人工闸门 | ✅ | `DemoRunnerController.start(account_id)` |

## 二、时间线（03:35 → 03:37 的变化）

```
03:35:20  order_permission_check        testAccepted=【false】· DemoSafetyError
03:36:45  order_test_reconciliation     对账 openOrders=[] · actualOrderPostRequests=0
03:37:17  order_test_response_diagnostic  返回 {}  ← 成功
03:37:17  connection_check              connected=【true】
```

**⇒ 03:35 的失败已在 03:37 前被修复。**

## 三、唯一剩余步骤

```
demo_orders = 0 行
⇒ 需要在币安 Demo 前端页面【人工启动】: 选一个已冻结的单腿账户 → 点「开始」
   （首版只支持单腿；配对账户拒绝启动）
```

启动后我会立刻核对（7 项）：
1. `demo_orders` 是否出现记录
2. 是否为 **post-only 限价单**（GTX）
3. **30 秒**未成交是否撤单（`entry_order_ttl_ms = 30_000`）
4. `demo_fills` 是否出现 **commission / commissionAsset / is_maker**（Q1 后半）
5. `demo_signal_comparisons` 是否记录**同一信号的两条路径**（内部模拟 vs Demo 对照）
6. 是否存在 `intent → canceled/expired` 比例（Q3 真实未成交率）
7. 是否有真实强平（对照我发现的「收盘口径低估 2.4×」）

## 四、⚠️ 潜在风险（非当前阻塞，待确认意图）

```python
def _assert_api_base(api: str, url: str):
    version = "3" if api.endswith("V3") else "1"   # ← V2 永不可能通过
    if url != f"https://{DEMO_HOST}/fapi/v{version}":
        raise DemoSafetyError("Demo API version or base path changed")
```

实测（用真实 ccxt demo URLs）：

| api | url | 结果 |
|---|---|---|
| fapiPrivate | .../fapi/v1 | ✅ |
| **fapiPrivateV2** | .../fapi/v2 | ❌ Demo API version or base path changed |
| fapiPrivateV3 | .../fapi/v3 | ✅ |
| **fapiPublicV2** | .../fapi/v2 | ❌ 同上 |
| fapiPublicV3 | .../fapi/v3 | ✅ |

**⚠️ 风险**：若 ccxt 某天把某调用路由到 V2，会被拒且报「版本或基础路径已改变」，
**误导排查方向**（实际不是环境变了，而是适配层没覆盖 V2）。
**⚠️ 但也可能是有意的**（只允许已审查的 v1/v3）。→ 已请 Codex 确认意图。

## 五、我这几轮的错误（诚实记录）

- **第 47 处**：曾怀疑「资产名被截断（USDT→U）」与「余额全 0（解析 bug）」→
  核对后 `'U'` 是币安真实资产、余额非全 0（USDT 5000）→ 适配层未篡改响应。
- **第 48 处**：查询 `demo_orders` 用了 `created_at` 列（实际是 `updated_at`），
  导致事务中止并级联污染后续查询 → 已改用逐表独立事务。
