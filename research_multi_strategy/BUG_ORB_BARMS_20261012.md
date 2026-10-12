# 🔴 Bug：`_orb` 硬编码 5 分钟 BAR_MS，导致非 5m 的 s01 永不交易

> 2026-10-12 · DSH · 目标第 67 轮 · **已用合成数据实测确认**

## 一句话

`strategy_signals._orb()` 内部使用**模块级常量** `BAR_MS = 300_000`（5 分钟），
而**不接收策略的实际周期**。因此 **s01_orb_breakout 在 15m/1h 上永远返回
`session_range_not_ready`，永不入场**。

**⚠️ 这直接卡住当前目标**：Demo runner 选中的账户是
`s01_orb_breakout_B15m_R1000_k1.0_100x_maker` → **Demo 永远不会下单**。

## 1. 证据

### 1.1 代码

```python
# strategy_signals.py
BAR_MS = 300_000                                  # L20 · 模块级 = 5 分钟
TIMEFRAME_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, ...}   # L22

def _orb(rows, params, pos):                      # L599 ✗ 不接收 bar_ms
    day = current["timestamp"] // DAY_MS * DAY_MS
    if pos and (pos["opened_candle_ms"] < day or
                current["timestamp"] + BAR_MS >= day + DAY_MS):         # L604 ✗ 5 分钟
        return 0, "session_day_end", evidence
    session = [row for row in rows if row["timestamp"] >= day]
    count = params["N_bars"]
    expected = [day + index * BAR_MS for index in range(count)]         # L608 ✗ 5 分钟
    if len(session) <= count or [row["timestamp"] for row in session[:count]] != expected:
        return None, "session_range_not_ready", evidence                # ← 必然命中
    ...
    if current["timestamp"] + BAR_MS >= day + DAY_MS:                   # L624 ✗ 5 分钟
        return None, "session_day_end_no_entry", evidence
```

**对比**：`evaluate()` 在 L1239 正确地取了 `bar_ms = TIMEFRAME_MS[timeframe]`，
并用它加载 `_history` / `_position`（L1252、L1259）。**只有 `_orb` 没拿到 `bar_ms`。**

### 1.2 实测（合成数据）

| 输入 | `_orb` 返回的 reason |
|---|---|
| **15m 真实间隔**（当日 78 根） | **`session_range_not_ready`** ❌ 不该出现 |
| 5m 间隔（对照） | `no_session_breakout` ✅ 正常通过 |

```
expected[:4] = 00:00, 00:05, 00:10, 00:15      ← 5 分钟刻度
actual  [:4] = 00:00, 00:15, 00:30, 00:45      ← 15 分钟真实刻度
相等? False
```

### 1.3 与现场观测一致

```
s01_orb_breakout_B15m_R1000_k1.0_100x_maker 的 decision:
  19:15:10 UTC · BTC reason = ['session_range_not_ready']
  19:00:13 UTC · BTC reason = ['session_range_not_ready']
  18:45:02 UTC · BTC reason = ['session_range_not_ready']
（19:40 UTC 时当日开盘区间早已成立，本应能算）
```

## 2. 影响范围（已确定）

| 项 | 结论 |
|---|---|
| 受影响的策略 | **仅 `s01_orb_breakout`**（`_orb` 的唯一调用者） |
| 受影响的条件 | **所有非 5m 实例**（15m / 1h / 1d） |
| 受影响账户 | `s01_orb_breakout_B15m_*`（9 个 R×k 组合）+ 其它非 5m 的 s01 实例 |
| 其它 28 个策略 | ✅ **不受影响**（`_single_mechanism` 区间内 `BAR_MS` 出现 0 次） |
| `_history` / `_position` | ✅ 正确（`evaluate` 显式传入 `bar_ms`） |
| **对当前目标** | 🔴 **Demo runner 选中的是 s01_B15m → 永远不下单** |

## 3. 修复建议（属 Codex 域，我不改）

**最小改动**：让 `_orb` 接收并使用 `bar_ms`。

```python
def _orb(rows, params, pos, *, bar_ms: int = BAR_MS):
    ...
    expected = [day + index * bar_ms for index in range(count)]
    if current["timestamp"] + bar_ms >= day + DAY_MS:
        ...
```

并在调用处传入策略周期：

```python
side, reason, orb_evidence = _orb(rows, params, pos, bar_ms=bar_ms)
```

⚠️ **注意**：`_single` / `_single_mechanism` 当前签名也没有 `bar_ms`，
需一并串起来（`evaluate` 已有 `bar_ms`，从 L1239 起可用）。

⚠️ **修复后必须复核**：
1. 15m 的 s01 是否返回 `no_session_breakout` / `daily_first_entry_already_consumed`
2. 5m 的 s01 行为是否不变（回归）
3. `session_day_end` 的提前量是否从 5 分钟修正为该周期
   （原 15m 下会**提前 10 分钟**结束当日交易窗口）
4. 相关测试是否需要更新（s01 的 5m 测试应仍通过）

## 4. 为什么这条发现对目标关键

```
目标 = 用 Demo 回答三个问题
⇒ 前提是 Demo 能真实下单
⇒ 而 runner 绑定的账户（s01_B15m）【永远不会产生入场信号】
⇒ 若不修，Demo 会一直 0 订单，目标无法推进

⚠️ 且这也解释了纸面前向账户里 s01 从未交易的现象。
```

## 5. 局限（诚实记录）

- 我用的是**合成 K 线**验证 `_orb` 的判定分支，不是真实 15m 数据回放
  （但分支条件是纯时间戳比较，合成数据足以证明）
- 未逐一核对其余 28 个策略是否存在**同类但不同形式**的周期硬编码
  （已确认它们不引用模块级 `BAR_MS`，但不排除用别的常量）
- 未评估修复对其他依赖 `_orb` 证据字段的代码的影响
