# 待执行变更（需你或 Codex 落地）· 2026-10-16

本文档记录 DSH 已**做好但无权执行**的两项变更，含精确改动与验证方法。

---

## ① 「≤50 USDT/笔」超限的修复：启用**结构性止损钳制**

### 问题（R444 实测，非推测）

| cap 档 | 平仓笔数 | 超自身 cap | 比例 | 最差单笔 |
|---|---|---|---|---|
| **50** | 833 | **220** | **26.4%** | **−204.70（4.09 倍）** |
| 200 | 790 | 209 | 26.5% | −818.81 |
| 1000 | 790 | 96 | 12.2% | −2415.93 |
| **合计** | **2,440** | **525** | **21.5%** | — |

**机制**（R430）：`max_loss_per_trade_usdt` **只决定仓位大小**，**不限制实际亏损**；
当 `k × ATR% > 1%`（100× 下强平距离恒为 1%）时，**止损落在强平价之外** ⇒ 先被强平 ⇒ 损失不受 cap 约束。

### 修复（机制已实现，但**382/382 账户均未启用**）

> 🔴🔴 **【2026-10-17 更正 · Codex 复核 + DSH 独立核实】下面的原始补丁是【错的 · 会破坏实验室】**
> `strategy_signals.py:328` 在 `risk_sizing_mode="structural"` 时**强制要求 `structural_stop` mapping**
> （`stop_spec_id` · `structure_lookback`/`lookback` · `buffer_pct` · `frozen_notional`），
> 只加下面 7 个顶层字段会抛 `ValueError: structural risk sizing requires structural_stop configuration`
> ⇒ **`strategy_lab_service.load_specs()` 直接失败 ⇒ 实验室起不来。**
>
> 且 `stop_clamp_policy` **只存在于 `structural` 分支**（`strategy_simulation.py:245-289`）；
> `entry_atr` 分支（`:290-310`，当前 261 个 v3 账户的实际模式）**没有钳制**。
> 而 `structural` 用的是 **OHLC 结构止损**（非 ATR）⇒ 切过去等于**改变策略假设**。
>
> **✅ 正确做法（已与 Codex 确认）**：保持 **ATR 语义**，
> **在 `entry_atr` 分支【新增】钳制**（`stop_clamp_policy="liquidation_distance_fraction"` +
> `stop_liquidation_clamp_factor` 严格 0.7），策略规则字段**一律不动**。
> 见本文档 §①-更正。

在 `research_multi_strategy/strategy_specs.json` 的目标策略条目上增加：

```jsonc
{
  // 保留原有字段…
  "risk_sizing_mode": "structural",
  "max_loss_per_trade_usdt": 50,
  "stop_policy_version": "structural-stop-v1",
  "stop_clamp_policy": "liquidation_distance_fraction",
  "stop_liquidation_clamp_factor": 0.7,      // 代码强制 == 0.7
  "notional_policy": "frozen_signal",
  "experiment_id": "structural-stop-clamp-v1"
}
```

**代码依据**：
- `strategy_simulation.py:326-360` `_stop_clamp`：`stop = price − direction × min(raw_distance, price × (1/leverage) × factor)`
- `strategy_simulation.py:262`：`factor` 必须严格等于 `0.7`
- `strategy_signals.py:352-361`：同一校验
- 测试样例：`tests/test_strategy_structural_stop.py:30-37`

### 影响与代价

| 项 | 说明 |
|---|---|
| **效果** | 止损被限制在强平距离的 70% ⇒ **止损必然先于强平触发** ⇒ 损失被限制在 cap 内 |
| **收益影响** | **R431 实测：仅 +1.18 bp**（在 18 个可比样本上）⇒ **不是盈利路径，但能消除合规缺口** |
| **身份变更** | ⚠️ 新增 `stop_clamp_policy` ⇒ **`execution_hash` 变更** ⇒ **必须新建账户**（不可就地修改现有账户） |
| **治理** | ⚠️ **属策略注册表变更，DSH 无权执行** |

### ①-更正：正确做法（ATR 语义 + 在 entry_atr 分支新增钳制）

**为什么不切 `structural`**
- `structural` 的止损是**前 lookback 根 OHLC 极值的结构止损**，不是 ATR 止损
  ⇒ 切过去 = **更换止损机制** = 改变策略假设 ⇒ 破坏当前 261 个 v3 账户的可比性
- `structural-stop-v1` 原本是 **2x2 研究臂**（`execution_enabled=false`）的定义

**正确改动（Codex 域）**
在 `strategy_simulation.py` 的 **`entry_atr` 分支**（`:290-310`）新增与 `structural` 分支**同名同语义**的字段：
```python
"stop_clamp_policy": "liquidation_distance_fraction",
"clamp_ratio": 0.7,
"stop_liquidation_clamp_factor": 0.7,   # 同样 math.isclose(..., 0.7) 严格校验
```
并让 `_stop_clamp`（`:326-360`）同样作用于 **entry_atr 的止损距离**，
上限 = `price * (1/leverage) * 0.7`。

⚠️ **这仍会改变 `execution_hash`** ⇒ **必须新建账户**（不可原地改现有账户）。

**目标人口（DSH 定的最小可验证范围）**
| 族 | R50 实测最差 | 倍数 |
|---|---|---|
| `s35_cascade_continuation_15m_Fnew` | −200.65 | 4.01× |
| `s05_bollinger_fade_B15m` | −143.63 | 2.87× |
| `s02_dual_thrust_B15m` | −124.90 | 2.50× |

⇒ 每族只开 **`R50_k1.0` 一个变体（3 个新账户）**，跑 24–48 小时做 A/B。
**排除**：已退役族（s03/s09/s12/s14/s17/s18/s21/s34）；**不做持仓迁移**，旧账户原样保留
⇒ 形成天然"有钳制 vs 无钳制"对照。

### 验证方法（落地后）

```bash
# 1) 新账户是否带钳制
QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 -c "
import os,psycopg,json
def J(v): return v if isinstance(v,dict) else json.loads(v or '{}')
with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    for sid,ex in c.execute(\"select strategy_id,execution from quant.strategy_sim_accounts where execution->>'stop_clamp_policy' is not null limit 5\"):
        print(sid, J(ex).get('stop_clamp_policy'), J(ex).get('stop_liquidation_clamp_factor'))
"
# 2) 超限比例是否下降（目标：cap=50 档从 26.4% → 接近 0）
python3 tools/assess_retirement_candidates.py   # 或复用 R444 的逐账户 cap 核查
```

**验收标准**：新账户的 `stop_clamp_flag` 在 `k×ATR% > 0.7%` 时为 `true`；
且其**单笔净亏超过自身 cap 的比例 < 5%**。

---

## ② Demo 扩容以加速 100 往返检验点

### 现状（R438 实测）

| 项 | 值 |
|---|---|
| 当前活跃账户 | `demo_strategy_f6cb33743f6146c385dddf3bd061892c`（唯一）|
| 成交 / 出场 | 25 / 15（= 往返数）|
| 跨度 | 16.7 小时 ⇒ **0.897 往返/小时 = 21.5/天** |
| 距 100 往返 | 还需 ~85 ⇒ **约 3.9 天** |
| PREREG | `research_multi_strategy/PREREG_demo_100_roundtrips.md`（判据已锁定）|

### 扩容方案（三选一，需你决定）

| 方案 | 做法 | 预期加速 | 风险 |
|---|---|---|---|
| **A ⭐** | **增加并行账户**：为 s07 的其它参数变体（R50_k1.5 / R200_k1.0 …）各开一个 Demo 绑定 | ~3–10× | 需改 Demo 绑定（policy hash 变更）· 各账户独立统计 |
| **B** | **增加并行策略**：再绑定 1–2 个未退役族（如 s08/s20）| ~2–3× | 扩大样本的**总体定义** ⇒ **须更新 PREREG**（否则口径不一致）|
| **C** | **不改**：等 3.9 天 | — | 最保守；PREREG 无需变更 |

⚠️ **DSH 建议 A**：它把「同策略的更多样本」而非「更多策略」纳入检验 ⇒ **与 PREREG 的总体定义一致**，且能最快把 n 推到 100。

### 执行边界

⚠️ **改动 Demo 绑定 ⇒ policy hash 变更 + Demo 重启 ⇒ 属你明确授权范围（历史决定均由你授权）。DSH 未自行执行。**

### 落地后验证

```bash
python3 tools/demo_roundtrips_v2.py --since 2026-10-15T16:00:00Z --by-trip
python3 tools/evaluate_demo_checkpoint.py     # 按 PREREG 机械判读
```

---

## ③ 采集器缺陷（R445，已定位）

### 根因
**Binance WebSocket 断连**（`No PONG received` / `Cannot connect to host fstream.binance.com:443`）
+ **采集器重连耗时 ~35 分钟** + **重连后不回补** ⇒ **每次中断留下永久 K 线空洞**。

**10-16 全天连接失败 130 次**（08 时 84 次最多；本次空洞在 19–20 时共 26 次）。

### 两条修复路径

| # | 做法 | 成本 | 生效条件 |
|---|---|---|---|
| **1 ⭐** | **自动化回补**：把 `tools/repair_closed_candles.py --pairs BTC ETH TRX --timeframes 5m 15m 1h --rest-window 60 --apply --output <新目录>` 做成**每 30 分钟一次的定时任务** | 低 | 立即可用，**无需改代码** |
| **2** | **改采集器**：重连成功后自动回补错过的 K 线 | 中（Codex 域）| **需重启采集器**（当前已运行 1 天 5 小时）|

### 建议同时增补哨兵第 4 条判据

现有三条件（托管数=0 / futures 15m 滞后>30 分 / 最近决策>30 分）**均未触发**本次故障
（因采集器在正常写新数据，只是**历史有洞**）⇒ **建议增加**：
**「实验室吞吐骤降」（事件数 < 基线 30% 持续 30 分钟）** ⇒ 本次可提前 ~3 小时发现。

---

## 附：本文档记录的两项已完成动作（用户已批准）

| 动作 | 结果 |
|---|---|
| **K 线回补** | 插入 **15 根**（含 5 根 1h）· 缺口归零 · 回滚日志 `artifacts/gap-repair-apply-20261016T151157Z/rollback.jsonl` · **实验室恢复：1 族/36 条 ⇒ 10 族/312 条** |
| **根因排查** | 完成（见 §③）|

---

## ④ 桥接缺陷（R447 · 本轮发现）：命名 link `quant-standby` 已失效

### 现象
| 发送方式 | 结果 |
|---|---|
| `send --to codex`（**不带** `--link`）| ✅ `accepted`（thread `01a11fbf…` · `method=turn/steer` · `transport=desktop-native-ipc`）|
| `send --to codex --link quant-standby` | ❌ `{"ok": false, "error": "Codex RPC 拒绝请求：no-client-found"}` |

**对照**：同期的 `watchdog-probe`（也经 desktop_link）是 `accepted` ⇒ **绑定本身可用，是命名 link 陈旧。**

### 影响
本轮 **5 条待派发消息**（1 条写域确认 + 1 条两项修复 + 3 条测试）全部因此被记为 `unconfirmed`，
**没有到达 Codex**；改用默认绑定后**立即 accepted**。

### 结论与建议
- **派发请【省略 `--link`】**（用默认绑定），不要用 `--link quant-standby`
- ⚠️ 建议核查：`desktop_link.py links` 中 `quant-standby` 指向的会话是否已被替换
  （Codex 桌面 PID 本轮从 `26396` → `6140` ⇒ 会话已重开 ⇒ 旧 link 可能仍指向旧 PID）
- ⚠️ **不要**自行改绑定（须人决定）—— 本 R446 仅为记录与规避

### 已完成派发（2026-10-16T15:52Z）
1. **写域确认**（回 `codex-cd-write-scope-20261016`）：无冲突，DSH 让出 `.github/**` + release 文档
2. **任务 1**：启用结构性止损钳制（消除 26.4% 的 cap 超限）—— 含精确 spec 补丁与验收标准
3. **任务 2**：采集器 WebSocket 断连后不回补（含两条修复路径）
