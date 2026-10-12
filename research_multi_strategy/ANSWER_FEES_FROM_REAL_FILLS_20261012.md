# 币安 Demo 实际手续费：真实成交验证（13 笔 · 三币 · maker 与 taker 全覆盖）

> 2026-10-12 · DSH · 用户指示「先在交易所做几笔交易，把实际手续费抓出来」
> session = `fee_capture_20261012_1942` · 工具 = `tools/run_binance_demo_fee_capture.py`
> **所有费率由我自行计算 `commission/(price×quantity)`，不采信工具结论**

## 一、结论（13 笔真实成交）

| 类别 | 笔数 | 费率（每边） | 往返 | 与账户报价 |
|---|---:|---:|---:|---|
| **maker**（LIMIT/GTX post-only） | 4 | **0.0200000%** | **4.00 bp** | ✅ 一致 |
| **taker**（MARKET / LIMIT-IOC） | 9 | **0.0400000%** | **8.00 bp** | ✅ 一致 |

```
commissionAsset = USDT（无 BNB 抵扣）
覆盖币种: BTC · ETH · TRX（三币 maker 与 taker 全部取得真实成交）
```

## 二、逐笔明细（我独立验算）

| # | 币 | 方向 | is_maker | 类型 | TIF | 名义(USDT) | 佣金(USDT) | 我算费率 |
|---:|---|---|---|---|---|---:|---:|---:|
| 1 | BTC | buy | false | limit | IOC | 58.3253 | 0.02333010 | 0.0400000% |
| 2 | BTC | sell | false | market | — | 58.3244 | 0.02332974 | 0.0400000% |
| 3 | BTC | buy | **true** | limit | **GTX** | 24.9970 | 0.00499941 | **0.0200000%** |
| 4 | BTC | buy | **true** | limit | **GTX** | 33.3294 | 0.00666588 | **0.0200000%** |
| 5 | BTC | sell | false | market | — | 58.3244 | 0.02332976 | 0.0400000% |
| 6 | ETH | buy | false | limit | IOC | 20.3586 | 0.00814342 | 0.0400000% |
| 7 | ETH | sell | false | market | — | 20.3550 | 0.00814201 | 0.0400000% |
| 8 | ETH | buy | **true** | limit | **GTX** | 20.3550 | 0.00407100 | **0.0200000%** |
| 9 | ETH | sell | false | market | — | 20.3516 | 0.00814063 | 0.0400000% |
| 10 | TRX | buy | false | limit | IOC | 5.3038 | 0.00212153 | 0.0399999% |
| 11 | TRX | sell | false | market | — | 5.3037 | 0.00212147 | 0.0400000% |
| 12 | TRX | buy | **true** | limit | **GTX** | 5.3045 | 0.00106089 | **0.0199999%** |
| 13 | TRX | sell | false | market | — | 5.3038 | 0.00212153 | 0.0399999% |

**注**: 第 10/12/13 笔显示 0.0399999%/0.0199999% 是**舍入**（佣金按最小单位截断），非费率差异。
第 12 笔是 `--recover-only` 补救运行中成交的 TRX maker 单。

## 三、本次验证填补的空白

| 空白 | 验证前 | 验证后 |
|---|---|---|
| **maker 费率** | ❌ 只有账户报价，**从未由成交验证** | ✅ **4 笔真实成交，0.0200000%** |
| **ETH / TRX 的 taker 费率** | ❌ 只有 BTC 的 10 笔 | ✅ 三币各 3 笔，均 0.0400000% |
| commissionAsset | 已知 USDT | ✅ 复核确认 USDT（无 BNB 抵扣） |

**验证前**：`demo_fills` 仅 10 笔，全部 BTC、全部 `is_maker=False`。
**验证后**：21 笔（+11），覆盖三币、含 maker。

## 四、对项目费率假设的意义

`strategy_fee_schedule.py` 当前的费率表：
```
MAKER_FEE_PER_SIDE = 0.0002   ← 与本次实测 0.0200000% 【完全一致】✅
TAKER_FEE_PER_SIDE = 0.0004   ← 与本次实测 0.0400000% 【完全一致】✅
```
⇒ **费率表已与交易所实际费用一致**（该表在 2026-10-12 由用户指示「与币安 Demo 一致」而修改）。

⚠️ **但费率表正确 ≠ 成本建模正确**。仍存在两个独立问题（与本次实测无关）：
1. **`fill_model = next_closed_bar_open_proxy`（市价语义）却按 maker 0.0002 扣费** ⇒ 若真按市价成交，成本应为 8bp，**当前低估一半**
2. **`EXECUTION_MODEL["funding"]` 与 `["slippage"]` 仍为 `"unknown"`**（资金费率有 ±0.75%/8h 上限）

## 五、执行过程与安全

```
① 前置: demo_default flat（无非终态订单、无净敞口）
② 停 auth_service（用户授权）→ 释放 runner 锁
③ 首次运行: success=false —— entry_cancellation 报 exchange_code -2011
   （-2011 = Unknown order sent：撤单时订单已不在交易所挂单里）
   ⇒ 留下 1 个 TRX GTX 幽灵挂单（本地 open · 交易所 unknown）
   ⚠️ 但持仓始终 flat（三币净敞口 0）
④ 【收尾】立即恢复 auth_service（同命令串接，无论成败都执行）
⑤ 二次运行 --recover-only: success=true · position_flat=true · open_orders_zero=true
   · recoveryRequired=false · errors=[] · maker_actual_fill_verified=true
⑥ 最终: filled 12 · canceled 5 · 非终态 0 · 三币敞口全 0
```

**⚠️ 我自查出的一处判断错误（第 61 处）**：
首轮运行后我称「工具把 `maker_actual_fill_verified` 因整体失败而误报为 false」。
**这是错的** —— 工具报告的 `maker_verified_by_pair = {BTC: True, ETH: True, TRX: False}`
是**准确的细粒度报告**：当时 TRX 的 GTX 确实未成交。补救运行后 TRX maker 成交，该值转 true。
⇒ 工具的逐币 reporting 是正确的，我的批评不成立。

## 六、局限

- 单一时点样本（13 笔 · 2026-10-12 19:41~19:48 北京）
- 名义金额小（5~58 USDT），未验证大额费率是否相同
- 未测试 BNB 抵扣路径（本次 commissionAsset 全为 USDT）
- 未验证 VIP 档位（账户为散户档）
- 费率可能随账户档位/活动变化，本结论仅代表该账户当日
