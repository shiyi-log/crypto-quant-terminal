# 🔴 Bug：Demo runner 把「未来 663ms 的事件」判为 `paper_signal_stale`，信号被永久丢弃

> 2026-10-12 · DSH · 由 s01 的 `entry_denied` 反查得出 · 属 Codex 域（`demo_execution_service.py`）

## 一句话

`demo_execution_service.cycle()` 在周期开始采样一次 `now`，之后才读取事件并用这个旧 `now`
判定时效。若事件恰在采样之后写入，`now − observed_at_ms` 为**负数**，
被 `not 0 <= … <= max_signal_lag_ms` 判为 `paper_signal_stale`，
而**游标照常前进 ⇒ 该信号被永久丢弃、永不重试**。

## 一、代码

```python
# demo_execution_service.py
def cycle(self, now=None):
    ...
    for event in self.paper.events(self.config.strategy_id, self.last_sequence):
        self._paper_event(event, now, conflicts)          # ← 共用周期开始时的 now
        self.last_sequence = int(event["sequence_id"])    # ← 被拒也推进

def _paper_event(self, event, now, conflicts):
    denied = None
    if not self._entries_enabled:
        denied = "entries_paused_or_read_only"
    elif conflicts:
        denied = "demo_account_ownership_conflict"
    elif not 0 <= now - event["observed_at_ms"] <= self.config.max_signal_lag_ms:
        denied = "paper_signal_stale"                     # ← 命中
    elif event["observed_at_ms"] < self.binding["forward_boundary_ms"]:
        denied = "before_demo_forward_boundary"
```

## 二、实测证据（完全吻合）

| 项 | 值 |
|---|---|
| runner 的 `now`（`entry_denied` 记录的 `observed_ms` 列） | **04:45:01.464Z** |
| 事件的 `observed_at_ms`（`strategy_sim_events`） | **04:45:02.127Z** |
| **差值 `now − observed`** | **−663 ms（负数）** |
| 判定结果 | `paper_signal_stale` |
| 被拒事件 | `s01_orb_breakout_B15m_R1000_k1.0_100x_maker` · seq 19148 · `reason=first_session_breakout` |
| 同批其它 s01 账户 | seq 19086/19130/19133/19136/19139/19142/19145/19151/19154（同一时刻） |

**⚠️ 注意：`max_signal_lag_ms = 90_000`，若按绝对值算只有 663ms，远未超限 ——
问题出在下界 `0 <=`，负值一律判 stale。**

## 三、影响

```
① 常规 Demo runner（demo_default，绑 s01）【从未成功下过一单】
   已成交的 12 笔 = manual_smoke（直接调适配器）2 笔 + second_level（自己的 runner）10 笔
② 被拒信号【永久丢失】（游标前进，不重试）
③ 该 bug 【不是 s01 特有】—— 任何策略的 order_submitted 落在
   「周期取 now 之后、读事件之前」这个窗口内都会被误拒
   窗口约几百毫秒 vs 轮询约 8 秒 → 命中率低但一旦命中即损失该信号
```

## 四、修复建议（属 Codex 判断）

| 方案 | 内容 | 评价 |
|---|---|---|
| **A（推荐）** | **每个事件单独取 `now`**，或在判定时用 `max(now, event["observed_at_ms"])` | 直接消除负值 |
| **B** | 把下界放宽为 `-allowance`（如 `-5000ms`），容忍时钟/写入抖动 | 更简单，但语义变模糊 |
| **C** | 被拒事件**不推进游标**（或进入重试队列） | 治本但改动大；⚠️ 需防「永久卡住」 |
| **D** | 用 `signal_observed_at_ms`（事件自身字段）而非周期级 `now` | 与 A 类似 |

**⚠️ 我倾向 A + C 组合**：A 消除误判；C 保证即使误判也不丢信号。
**⚠️ 但 C 需谨慎**（若某事件永远无法处理，游标会卡住）—— 建议 C 配「最大重试次数/超时后跳过并告警」。

## 五、局限

- 该结论基于**一次** `entry_denied` 记录 + 代码路径分析；未构造复现用例
- `max_signal_lag_ms` 的运行时取值我没能直接读到（我查到的最新 binding 是 second_level 的，
  其 `demo_policy` 无此字段）→ 建议 Codex 确认 demo_default 的实际配置值
- 未验证「同批另外 9 个 s01 账户是否也被拒」（我只看到 1 条 `entry_denied` 记录）
