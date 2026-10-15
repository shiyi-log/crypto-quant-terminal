# 规格：修复 Demo 入场单的成交率（OrderNotFillable + TTL 未成交）

- 日期：2026-10-14
- 作者：DSH（验证角色）
- 状态：**待 Codex 实现**（我只找方向与验证，不改代码）
- 证据来源：`quant.demo_orders` / `quant.demo_signal_comparisons` / `quant.candles`，全部实测

---

## 1. 问题陈述（实测）

Demo runner 的**入场**成交率约 20–25%，且自 20:45Z 起连续 6 次入场尝试 **0 成交**。

7 笔订单全貌（`demo_orders`，按 `created_ms`）：

| # | created (UTC) | pair | side | type | TIF | ro | 状态 | 成交 |
|---|---|---|---|---|---|---|---|---|
| ① | 16:15:09.728 | TRX | buy | limit | GTX | F | canceled | 415 / 3056（13.6%）· maker 0.0199999%/边 |
| ② | 19:00:21.906 | TRX | sell | market | — | **T** | filled | 415 / 415 · taker 0.0400000%/边 |
| ③ | 20:45:06.575 | BTC | buy | limit | GTX | F | abandoned_after_absent_lookup | 0 |
| ④ | 21:45:29.310 | TRX | buy | limit | GTX | F | abandoned_after_absent_lookup | 0 |
| ⑤ | 22:00:28.364 | TRX | buy | limit | GTX | F | abandoned_after_absent_lookup | 0 |
| ⑥ | 22:15:21.850 | TRX | buy | limit | GTX | F | canceled | 0（挂上 30s 无人成交） |
| ⑦ | 22:30:13Z | TRX | buy | limit | GTX | F | canceled | 0（同上） |

两个**独立**的失败模式：

### A. `OrderNotFillable`（③④⑤）
`demo_signal_comparisons` 的 `submission_error`：

```
22:00:28.651Z  error_kind=OrderNotFillable  error_type=DemoSubmissionUnknown
20:45:07Z      error_kind=OrderNotFillable  error_type=DemoSubmissionUnknown
```

机制（已用价格数据验证）：
- 挂单价 = 参考 bar 的**开盘价**（`intent_payload.reference_source = paper_closed_candle_from_live_market`，
  paper 的 fill_model = `next_closed_bar_open_proxy`）。
- 提交时点 = 该 bar 开盘后 **6.6–29.3 秒**（`created_ms - floor(created_ms/300000)*300000`）。
- 若价格在提交时刻已越过挂单价 ⇒ BUY post-only 会吃单 ⇒ 交易所拒绝。
- 实例：第 ④ 笔挂 `0.32779`；TRX 21:45Z bar `O=0.32779 H=0.32779 L=0.32761 C=0.32774`
  ⇒ 提交时价格已在 0.32761 以下 ⇒ 拒绝。

**副作用（比未成交更严重）**：提交抛异常 ⇒ `demo_orders.status='unknown'` ⇒
`demo_execution_service.py:494-495` 追加 `unknown_submission` ⇒
`demo_execution_service.py:831` 置 `status='blocked_unknown_exposure'` ⇒
**共享会话的全部 6 个会话一起被阻断**（`binance_demo_shared_sessions.json` 实测 6/6 均 blocked），
须人工走 `operator_verified_absent_lookup` + `absent_lookup_threshold_reached` 终结才恢复。
⇒ 每 ~15 分钟一个信号 ⇒ **循环**。

### B. 挂上但 30 秒内不成交（⑥⑦）
`entry_order_ttl_ms = 30000`（policy `binance-demo-single-paper-v1`）。
第 ⑥ 笔的 `audit_observations`（交易所原始响应）：

```
{"status":"NEW",      orderId 1157818922, origQty 3052}   ← 挂单成功
{"status":"NEW",      time 1791929722462}
{"status":"CANCELED", cumQty 0}                            ← TTL 撤销，零成交
{"status":"CANCELED"}
```

⇒ post-only 挂单**成功**（`exchange_order_id=1157818922`），但 30 秒内无对手方 ⇒ 撤销。
**不产生 `unknown` ⇒ 不阻断**（这是正确行为）。

### 入场延迟汇总（bar 开盘后）

| # | 延迟 | 结果 |
|---|---|---|
| ① | 9.7 s | 挂上 ⇒ 部分成交 13.6% |
| ③ | 6.6 s | OrderNotFillable |
| ④ | 29.3 s | OrderNotFillable |
| ⑤ | 28.4 s | OrderNotFillable |
| ⑥ | 21.9 s | 挂上 ⇒ 30 s 无成交 |

延迟本身与结果**不是单调关系**（6.6 s 失败而 21.9 s 成功）⇒ **决定因素是"提交时价格是否已越过挂单价"**。

---

## 2. 修复规格（按优先级）

### R1（最关键）挂单价：从「过时的参考价」改为「按盘口可成交的 maker 价」

目标：**既保住 maker 费率，又让挂单能成交/不被拒**。

建议规则（buy 为例；sell 对称）：

```
ref      = intent_payload.reference_price          # 保持不变（可比性锚）
bid, ask = 当前盘口（提交前拉取一次 bookTicker / depth=1）
tick     = 该 symbol 的 price tick（TRXUSDT = 0.00001；BTCUSDT = 0.10）

# post-only 买单必须严格低于 ask，否则会被拒
candidate = min(ref, ask - tick)                   # 不高于参考价，且保证能挂上
# 再往内收一档，提高成交概率（代价：可能成交价略差，但仍 ≤ ref，不劣于原设计）
candidate = min(candidate, bid) - tick  if bid > 0 else candidate
limit     = 向下取整到 tick 的 candidate
```

约束（必须保持）：
- `limit ≤ ref`（买单）⇒ **成交价不劣于原设计**，可与 paper 的 `ref` 做可比对照。
- 若 `limit` 与 `ref` 的偏离 > 某个阈值（如 0.2%），**记录但不下单**（避免在剧烈行情里追价）。

**必须记录到 `intent_payload`（可审计、保持可比性）**：

```
submitted_limit_price          （已有）
reference_price                （已有）
limit_deviation_bps = (ref - limit) / ref * 1e4        ← 新增
book_at_submit      = {bid, ask, observed_ms}          ← 新增
limit_policy        = "ref_capped_by_book_maker_price_v1"  ← 新增
```

⇒ 这样 `demo_signal_comparisons` 仍能回答「Demo 成交价 vs paper 参考价」的偏离，
   不会因为修 R1 而破坏可比性论证。

### R2 降低阻断的爆炸半径（与 R1 独立，必须做）

现状：单次 `unknown` 提交 ⇒ **全部 6 个会话** blocked。

建议：
1. **缩小范围**：`unknown_submission` 冲突只应阻断
   - 该 `virtual_account_key`（提交方），以及
   - 该 `pair`（因为敞口可能与 pair 相关），
   而不应阻断无关的会话与无关的 pair。
2. **加自动时限终结**：`demo_execution_service.py:453-456` 目前对
   `status in ("submitting","unknown") and not order` **直接 return**，永不终结。
   建议：`absent_lookup_count` 达到阈值（现有 `absent_lookup_threshold_reached` 已存在）
   **自动**终结为 `abandoned_after_absent_lookup`，不必人工触发。
   ⇒ 人工验证路径保留为「可选的更快通道」，而非唯一通道。
3. **降级为告警**：若出于安全必须阻断，则 `blocked_unknown_exposure` 必须**立即告警**
   （现在静默阻断，无任何声音）。

### R3 TTL 与重挂（提升 B 类成交）

- 现在 `entry_order_ttl_ms = 30000`。对一个刚挂出的 maker 单偏短。
- 建议（二选一，需用户/Codex 决定）：
  1. 提高到 60–90 秒；**或**
  2. 保持 30 秒，但**未成交则按 R1 的新盘口价重挂一次**（限制重挂 1 次，避免无限追价）。
- 无论哪种，都要记录 `non_fill` 结果（现有 `entry_expiry` 事件已含 `non_fill` 字段 ✅）。

### R4 可见性（指标 + 告警）

新增可查指标（建议进 runner status）：
```
entry_attempts / entry_submitted / entry_rejected_order_not_fillable /
entry_expired_non_fill / entry_filled / entry_partial
rejection_rate, non_fill_rate
last_rejection_ms, last_rejection_kind
```
⇒ 现在这些只能靠翻 `demo_signal_comparisons` 才能看出来。

---

## 3. 验收标准（谁改谁自证；我复核）

1. **R1 生效**：连续 ≥ 10 次入场尝试中，`OrderNotFillable` 次数 ≤ 1，
   且每次 `intent_payload.limit_deviation_bps` 有记录、`limit ≤ reference_price`。
2. **R2 生效**：人为构造一次 `unknown` 提交（或重放），
   验证**只有**该会话+该 pair 被阻断，其余会话仍 `observing`；且该 `unknown` 在
   阈值内**自动**终结为 `abandoned_after_absent_lookup`。
3. **R3 生效**：B 类「挂上未成交」占比可见，且（若选重挂）每次重挂有审计记录。
4. **不回归**：
   - `demo_execution_service.py:22` 的 `ACTUAL_STOP_EXECUTION_MODE`
     仍为 `close_confirmed_closed_candle_poll_reduce_only_market`（保护性平仓不变）。
   - 平仓仍为 `reduce_only` 市价（实测 taker 0.0400000%/边）。
   - maker 费率仍 0.0199999%/边（不因改价而退化为 taker）。
   - `demo_signal_comparisons` 仍记录 `reference_price`（可比性不破坏）。

---

## 4. 本规格**不做**的事

- 不改策略参数、不改冻结的 rule_hash / execution_hash。
- 不改 `strategy_fnew.py` / `strategy_lab_service.py` / `strategy_2x2_*.py` /
  `strategy_fee_schedule.py` / registry / 前端。
- 不改 paper 的 `next_closed_bar_open_proxy` 成交模型（它是对照基线）。
- 不提议"取消 post-only 改成吃单"作为默认 —— 那会翻倍费率（taker 0.04% vs maker 0.02%）；
  作为**可选项**列出，由用户决定。

---

## 5. 附：本次已确认的其它事实（非本规格范围）

- 首笔完整往返 **+0.2255 USDT**（毛利 +0.30710 − 手续费 0.081581）；
  费率表在真实策略驱动成交上**双向验证**：maker 0.0199999%/边 · taker 0.0400000%/边。
- `sizing_notional_proxy` 与 Demo 实际名义差 **437 倍**
  （s02：437,591.72 vs 999.89；s34：42,604.72 vs 999.76）
  ⇒ Demo 受 `maxNotionalUsdt=1000` 截断，**无法复现 paper 盈亏**，对比时须说明。
- 「signal→order ≈906–930 秒」并非 runner 慢：runner 在参考 bar 开盘后 **6.6–29.3 秒**内下单；
  差额来自 `signal_ms` 是 bar T 的时间戳，而 paper 在 bar T+1 开盘价成交。
- 「≤50 USDT」是 **sizing 目标**而非硬约束：`max_loss=50` 口径 943 笔中 **272 笔（28.8%）**
  亏损超过 50，最差 **−157.41**（3.15 倍）。
