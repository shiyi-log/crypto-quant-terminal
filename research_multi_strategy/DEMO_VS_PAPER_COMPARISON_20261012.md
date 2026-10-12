# 内部模拟 vs Demo 对照（纪律第 5 条交付物）

> 2026-10-12 · DSH · 目标第 78 轮 · 数据: `demo_signal_comparisons`（second_level 会话）

## 一、对照结构（成立）

**关联键 = `signal_id`（`hft-<hash>`）**，同一 signal_id 下同时存在：

| stage | 含义 |
|---|---|
| `second_level_signal` | 信号 |
| `second_level_paper_start` | 纸面开始 |
| `second_level_paper_result` | **纸面（内部模拟）结果** |
| `second_level_cycle_result` | **Demo 真实结果** |
| `submission_timing` | 提交时序 |
| `second_level_source_observation` | 源观测 |

**配对率：7 / 7 = 100%** ✅

## 二、⚠️ 我先犯的一个错误（第 55 处）

我最初的验证脚本去关联 `strategy_sim_events ↔ demo_signal_comparisons`，
结果「关联 0 条」。原因：
1. **纸面事件表里根本没有 `signal_id` 字段**（有 `order_id`/`paper_order_id`/`signal_candle_ms`）
2. 那 29 个是 **K 线策略**，而秒级链是**独立策略**（`second_level_obi_research`）
   → **本来就没有对应的纸面对手盘**

**⇒ 对照是【在同一张表内、按 signal_id 关联 paper_result 与 cycle_result】，不是跨表。**

## 三、成交侧对照（清晰）

| arm | 信号数 | entry 成交 | 真实手续费 | position_flat |
|---|---:|---:|---:|---:|
| **maker** | 4 | **0**（全部 canceled） | 0 笔 | 4 |
| **taker** | 3 | **3** | 3 笔合计 **0.120426 USDT** | 3 |

**逐笔 fills：**

| signal | arm | fills 数 | 明细 |
|---|---|---:|---|
| 79b7405ff1 | maker | **0** | — |
| bede27f4ed | taker | 2 | 83625.30 / 83639.90（各 0.02007） |
| 231676f216 | maker | **0** | — |
| 2befabc491 | taker | 2 | 83630.70 / 83633.50（各 0.02007） |
| 055787f426 | maker | **0** | — |
| cafa72ca58 | taker | **4** | **部分成交正确拆分**（0.00668960+0.01337921+0.01337996+…） |
| ed79a8770e | maker | **0** | — |

## 四、⚠️ 纸面侧：诚实但导致 P&L 无法对比

```json
{
  "hypothetical_crossing_entry_price": 83625.30,
  "hypothetical_crossing_exit_price":  83639.90,
  "hypothetical_crossing_gross_pnl_usdt": -0.008760,
  "hypothetical_crossing_fee_usdt":    null,
  "hypothetical_crossing_net_pnl_usdt": null,
  "hypothetical_fee_source": "demo_account_commission_rate_quote_not_fill",
  "maker_limit_price_unrounded": 83639.90,
  "maker_distance_bp": 0,
  "last_reason": "horizon_reference_recorded_maker_fill_unknown"
}
```

**✅ 这个设计是【故意的保守】，而且完全符合 Q1 的要求：**

```
hypothetical_crossing_fee_usdt = null
  ← 因为 fee_source = "demo_account_commission_rate_quote_not_fill"
  ← 【它拒绝用「账户报价费率」冒充「实际成交费用」】

hypothetical_crossing_net_pnl_usdt = null
  ← fee 是 null → net 无法算 → 也填 null（不猜）

last_reason = "horizon_reference_recorded_maker_fill_unknown"
  ← 明确承认 maker 成交状态未知
```

## 五、⚠️ 由此产生的缺口

**纸面侧的净盈亏是 `null` ⇒ 「paper vs Demo 的 P&L 差额」目前【算不出来】。**

| 要计算 | 需要 |
|---|---|
| paper 的净盈亏 | 一个不算「报价费率」的费用来源 |
| 方案 A | paper 侧**事后**用实测真实费率回填重算（事后修正，不污染决策时） |
| 方案 B | 让 paper 侧拿到 `actual_commission` 后回填，再算 net |

**⚠️ 我的判断：方案 B 更自然**（与 Codex 已有的 `actual_commission` 字段一致），
且必须**明确标注「事后回填，非决策时可得」**，避免被误读成「当时就知道」。

## 六、能对比的部分（现在就有结论）

| 维度 | 内部模拟（paper） | Demo（真实） | 差额 |
|---|---|---|---|
| **maker 成交率** | 隐含 100%（穿价假设） | **0%（4/4 canceled）** | **−100 个百分点** |
| **taker 成交率** | 隐含 100% | **100%（3/3）** | 0 |
| **taker 往返手续费** | `null`（拒绝用报价） | **0.040142 USDT/笔往返** | 无法比 |
| **部分成交** | 未建模 | **已观测**（1 笔拆 4 条 fill） | — |
| **未成交原因** | — | **TTL 3s 到期（≈3.5s 撤单）** | — |

**⇒ 最重要的可对比结论：内部模拟假设「限价单一定成交」，
而 Demo 实测 maker 成交率 0% ⇒ 【内部模拟在 maker 语义上高估 100 个百分点】。**

## 七、局限

- 样本 **7 笔**（maker 4 / taker 3）—— 不足以统计推断
- 单一币对（BTC）· 单一时段（约 10 分钟）
- Demo 与实盘不同（资金费率 2.2× · 合约数 742vs924 · 强平费 1.6~2×）
- 禁止标「盈利/通过/可用」
