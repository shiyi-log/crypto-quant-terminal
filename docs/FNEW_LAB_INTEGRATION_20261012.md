# Fnew 人口接入策略实验室（实施记录）

> 2026-10-12 · 由 DSH 会话执行 · 用户指令「开始做」
> ⚠️ 全部账户仍标**前向研究假设**；禁止标盈利/通过/可用。

---

## 一、接入前的事实

Fnew（`s32`~`s36`，45 个定义）的**四层已全部实现并有测试**：

| 层 | 文件 | 状态 |
|---|---|---|
| 工厂 / 身份 / 哈希 | `strategy_fnew.py` | ✅ 45 定义，27 单腿启用 + 18 双腿阻断 |
| 信号层 | `strategy_fnew_signals.py` | ✅ `evaluate_fnew` |
| 数据源 | `strategy_fnew_data.py` | ✅ `load_fnew_candles` / `fnew_data_status`（含 s33 独立 spot 市场） |
| 纸面引擎 | `strategy_simulation.py` | ✅ 配对入口、配对 ATR、配对止损、强平传染 |

**缺的只有服务层**：`strategy_lab_service.py` 完全不认识 Fnew——
数据库里 `%Fnew%` 账户数为 **0**，45 个定义从未被实例化。

---

## 二、本次改动

### 2.1 `strategy_lab_service.py`（+93 −10）

| 位置 | 改动 | 原因 |
|---|---|---|
| `is_fnew_spec()` | 新增判定 | Fnew 有独立 design version，不能与旧路径混用 |
| `feed_series()` | 新增 | s33 的 `spot` 是**嵌套映射**，旧代码把它当行列表会抛错 |
| `normalize_account_spec` | 加 Fnew 分支 | 派发到 `normalize_fnew_spec` |
| `research_pause_reason` | Fnew 用 `data_universe` | 币池判定口径不同 |
| `evaluate_account` | 加 Fnew 分支 | 派发到 `evaluate_fnew` |
| `blocked_evaluation` | Fnew 用 `fnew_rule_hash` | 旧路径调 `strategy_rule_hash` 会失败 |
| `_canonical_id` | 识别 Fnew | 横截面上下文判定 |
| `load_strategy_inputs` | Fnew 走 `load_fnew_candles` | **缓存键含 feed 契约**（是否带 spot），避免 s33 与单腿机制共用窗口 |
| `_context_candles` | 用 `feed_series` 展平 | 对齐检查必须**包含 spot 尾**，否则基差信号会被错窗口喂食 |
| `_refresh_native_warmup` / `bootstrap` | 同上 | 否则 s33 直接抛 `KeyError` |
| `main()` | `--fnew`（默认开）、`--fnew-paired-execution`（默认关） | 保持「显式启用」闸门 |

### 2.2 `manage.py`

1. 受管服务 `strategy-lab` 的命令加入 `--fnew-paired-execution`。
   `matches_process()` 对 script 型服务**忽略额外参数**，因此 `stop/status` 匹配不受影响（已实测）。
2. 新增受管服务 **`spot-basis`**：`tools/backfill_spot_basis_history.py --apply --watch`，
   独立输出目录 `database/runtime/spot_basis` 与独立状态文档 `spot_basis_status.json`。
   它是 `s33` 的前向喂数前置；该工具自身声明不改动其他采集器与任何账户状态。

### 2.3 新增测试（21 项，全绿）

- `tests/test_strategy_lab_fnew.py`（16 项）：人口加载、派发、阻断、嵌套 spot、缓存键隔离、原生周期循环
- `tests/test_strategy_lab_fnew_integration.py`（5 项）：**真实 PostgreSQL 隔离 schema**，验证建账、幂等、旧账户零改写、启用即新身份

---

## 三、配对执行验收（决定 s32/s36 能否启用）

阻断理由原为 `paired_atr_stop_policy_unimplemented`。**逐条核对后判定：该理由已过期。**

| 契约条款（`docs/FNEW_EXECUTION_20261012.md`） | 生产代码 | 测试 |
|---|---|---|
| `atomic-pair-equal-notional-ohlc-v1` | `strategy_simulation.py:174` 强制校验 | `test_execution_hash_binds_pair_path...` |
| `synchronous_ohlc_envelope_true_range_actual_entry_v1` | 同上 `pair_atr_policy` | `test_actual_next_opens_recompute_atr...` |
| 组合止损 = 两腿有符号收益之和 ≤ `-k*entry_pair_atr_return`，同一次决策、次开两腿同出 | `strategy_simulation.py:1159` | `test_close_stop_is_portfolio_based...` |
| 单腿强平触发整组结算 | `conservative_peer_bar_extreme_after_leg_liquidation`（`:1045`） | `test_open_liquidation...`、`test_intrabar_liquidation...` |
| **一次原子组合平仓 = 一个样本** | `sample_unit = completed_atomic_pair`（`:1015`） | `test_pair_api_exposes_completed_group_samples...` |
| 费用 4f 按每腿名义 | `risk_round_trip_fee = 4 × fee`（`:188`） | 实测 0.0008 |

**专项测试 22 项全通过**（`tests/test_strategy_pair_execution.py`），覆盖等名义、缺腿不裸敞口、重启撤整组、强平传染、幂等。

⇒ **验收通过，首次建账即启用**（而非先建阻断账户）。

---

## 四、上线结果

`manage.py restart strategy-lab` → PID 43294

| 项 | 值 |
|---|---|
| 账户总数 | 310 → **355** |
| 新建 Fnew 账户 | **45**（s32~s36 各 9 = 3 R × 3 k） |
| 既有 310 账户 | **rule_hash / execution_hash 零改写** |
| 服务错误 | `last_error: None` |

### Fnew 各机制最终就绪状态

| 机制 | ready | valid_bars | required | 就绪时间 |
|---|---|---:|---:|---|
| **`s32` 协整价差（双腿）** | ✅ true | 750 | 520 | 建账即就绪 |
| **`s33` 基差动量** | ✅ true | 750 | 120 | 现货补数 + 喂数后 |
| **`s34` VWAP 锚回归** | ✅ true | 750 | 60 | 5m 缺口修复后 |
| **`s35` 级联顺势** | ✅ true | 750 | 60 | 5m 缺口修复后 |
| **`s36` 相关性 regime（双腿）** | ✅ true | 750 | 337 | 建账即就绪 |

**45 / 45 全部就绪，相对价值族（s32/s36，18 个账户）与其余机制均开始前向积累。**
三者上线时曾被 fail-closed 正确拦住，暴露的两个真实数据问题已在第五节修复。

---

## 五、暴露并已修复的数据阻塞

### 5.1 ✅ 已修复：5m 有一个 25 分钟空洞，连锁打断 15m 与 1h

```
实测（修复前）：5m 期货 BTC 在 2026-10-12 17:35 → 18:00 缺 5 根（1500 秒）
              15m 直连表停在 00:45 · 1h 直连表停在 10-11 23:00
              （15m/1h 均从 5m 重采样，故同一个洞同时打断两个周期）
```

后果（**这是本次最关键的连锁**）：
- 15m 的严格末尾连续后缀被截成 **2 根** → `s34`/`s35` 无法预热；
- 1h 无法生成 17:00 桶 → 1h 组整体卡在 16:00。

**修复**：用仓库既有的 `tools/repair_closed_candles.py`（默认 dry-run、`--apply` 前写回滚日志、不改账户/策略状态）：

```
1) 先干跑复核：9 个身份，conflict_timestamps 全为 0
   BTC/ETH/TRX 5m 各缺 4 根 · 15m 各缺 71/70 根 · 1h 各缺 18 根
2) --apply 执行，回滚日志 224 KB
```

**修复后实测**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 5m 缺口数 | 1（1500 秒） | **0** |
| `s34` 连续后缀 | **2** 根 | **750** 根 |
| `s32` 连续后缀 | 750 | 750 |

### 5.2 Fnew 最终就绪状态（36 / 45）

| 机制 | ready | valid_bars | required | 说明 |
|---|---|---:|---:|---|
| `s32` 协整价差（双腿） | ✅ true | 750 | 520 | 已就绪 |
| `s34` VWAP 锚回归 | ✅ **true** | **750** | 60 | 缺口修复后转就绪 |
| `s35` 级联顺势 | ✅ **true** | **750** | 60 | 缺口修复后转就绪 |
| `s36` 相关性 regime（双腿） | ✅ true | 750 | 337 | 已就绪 |
| `s33` 基差动量 | ❌ false | 750 | 120 | `basis_spot_warmup_incomplete` |

### 5.3 ✅ 已修复：`s33` 的现货喂数

```
修复前：spot 1h 全部 15 个币最后时间戳 2026-10-08 16:00（futures 1h 已到 10-12）
```

基差信号要求现货与永续**逐时间戳精确对齐**（`strategy_fnew_data._basis_feeds`），
现货缺当前闭合 1h 即 fail-closed。

**修复**：用仓库既有的 `tools/backfill_spot_basis_history.py`（预览为默认、`--apply` 写日志、
`--watch` 只轮询最近三根闭合 1h 且使用独立状态文件）。

```
1) 预览：9 个窗口，conflict_timestamps 全为 0
   5m 补 BTC/ETH 各 1192 根、TRX 1281 根；1h 各补 97 根 + 655 根补收盘标记
2) --apply 执行（回滚日志 5.5 MB）
3) 新增受管服务 spot-basis 长驻喂数（manage.py），独立状态文档
```

**修复后实测**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| spot 1h 最后时间戳 | 2026-10-08 16:00 | **2026-10-12 17:00** |
| spot 1h 带收盘标记 | 少 | **752 根** |
| `s33` 就绪 | ❌ `basis_spot_warmup_incomplete` | ✅ **ready / 750 根** |
| 现货-永续时间戳对齐 | 不满足 | **3/3 币精确对齐** |

`spot-basis` 服务状态：`ready: true`，3/3 币各有 3 根闭合 1h，`last_error: null`。

### 5.4 ✅ 更正：`data_stale` **会自愈** —— 我原来的判断是错的

**我原先写的是**：「某根 K 线的 90 秒观察窗一旦被错过，该周期会一直停在最新可用根上，
直到重启刷新 `session_boundary_ms`」。**这个结论不成立，特此更正。**

**反证（8 小时连续运行实测，2026-10-12 18:39 → 10-13 03:13）**：

| 检查 | 结果 |
|---|---|
| 1h 决策覆盖的小时块 | 18:00 → 02:00 共 **9 个连续小时，每档间隔恰好 1 小时，零缺失** |
| 15m 决策覆盖的 K 线 | **34 根，理论应有 34 根（`(max−min)/15min + 1`），零缺失** |
| 两个周期最终状态 | 15m `observing`（latest 02:45）· 1h `observing`（latest 02:00） |
| 服务错误 | 全程 `last_error: None` |

**机制上也是自愈的**：`latest` 取的是**当前最新已收盘根**，会随时间推进；
`data_stale` 只在「最新根的观察窗已过期」这一瞬间成立，下一根新 K 线到达后
`latest` 前移，判定自然恢复。

**我当时看到的 `data_stale` 真因**：那时 5m 有 25 分钟空洞（见 5.1），
**17:15→18:00 之间没有新的 15m/1h 桶能够形成**，所以 `latest` 不前进。
不是服务逻辑卡死，而是**上游没有新数据**。喂数恢复后即自动继续。

⇒ **不应为它加自愈逻辑**（那会掩盖真正的数据中断）。
真正需要登记的是一条设计事实：**喂数中断期间未形成的 K 线会被永久跳过**
（fail-closed 不补造前向样本），因此前向样本的连续性取决于采集连续性。

---

## 五之二、最终实测：45 / 45 就绪且全部产出决策

```
18:47  15m 组推进到 18:30 · Fnew 首批 18 个 decision（s34 9 + s35 9）
18:50  现货补数 + spot-basis 喂数上线 → s33 转 ready
       s32 true / s33 true / s34 true / s35 true / s36 true  →  45 / 45 就绪
19:01  1h 组推进到 18:00 · 五个机制全部产出 decision
```

| 机制 | account_created | decision | equity_snapshot |
|---|---:|---:|---:|
| `s32` 协整价差（双腿） | 9 | **9** | 9 |
| `s33` 基差动量 | 9 | **9** | 9 |
| `s34` VWAP 锚回归 | 9 | 18 | 18 |
| `s35` 级联顺势 | 9 | 18 | 18 |
| `s36` 相关性 regime（双腿） | 9 | **9** | 9 |
| **合计** | **45** | **63** | **63** |

服务全程 `last_error: None`。**相对价值族（s32/s36）与其余 Fnew 机制均已开始前向样本积累。**

---

## 六、29 个基础账户身份已与新规格分叉（已定案：不动）

预检发现 `s01`~`s30` 的 29 个 `_100x_maker` 基础账户，**新加载规格与库中冻结身份不一致**（rule 与 execution 双不符）。

- 原因：`research_multi_strategy/strategy_specs.json`（10-12 14:23 改）与 `strategy_signals.py`（10-12 10:15 改）在账户建账（10-11 10:49 ~ 10-12 01:25）**之后**被改动。
- 非本次改动引起（本次只动服务层与测试）。
- 处理：`resume_frozen_specs` 把 29 项放入 `identity_changes_deferred`，**继续使用库中冻结定义**，不迁移、不覆盖。
- 服务状态已记录：`identity_changes_deferred: 29`。

### 6.1 差异是实质性的（逐字段核对）

| 字段 | 冻结值 | 新规格值 | 影响 |
|---|---|---|---|
| `params.risk_stop_atr_mult` | **无** | `1.5` | **启用 ATR 硬止损** |
| `params.atr_window` | **无** | `14` | 同上 |
| `stop_spec` | **无** | 完整 `hard_stop` 定义 | 同上 |
| `min_warmup_bars` | `60` | `16`~`22` | 原值被自评为「凭感觉写的保守余量，导致可用热身不足时永久阻塞」 |
| `evaluation` | taker 0.05%/边 | taker 0.04% / maker 0.02% | 成本口径对齐 Demo |

新规格里的 `atr_risk_enabled_20261012` 自述：**「实现用 opt-in：规格必须供给 `risk_stop_atr_mult` 才启用 ATR 硬止损（否则走 LEGACY_DEFAULTS）」**。

**含义**：这 29 个账户跑的是**没有硬止损**的旧规则 —— 正是 `STOP_DECISION.md` 认定必须修掉的那个缺陷
（「用户的『每笔最多亏 50 USDT』当前未被实现」）。

**实测佐证**：这 29 个账户约 200 笔离场中，`stop_loss_close_confirmed` 仅 **8 笔**；
其余为 `orderflow_direction_reversed`(56) / `opposite_range_breakout`(50) / `slow_trend_broken`(32) /
`holding_period_elapsed`(24) / **`liquidation`(15)**。且它们在持续交易（s07 37 笔 −4224、s02 28 笔 −3600、s09 19 笔 −2767）。

### 6.2 决策：**保持冻结，不重建、不迁移**（本次不动）

理由：

1. **分叉源自他人在建账之后编辑的规格**（`strategy_specs.json` 14:23 / `strategy_signals.py` 10:15），
   该规格在本次决策时仍在同一工作区被并发修改中；**不应基于一份非本方且在动的规格去分叉人口**。
2. **代价不对称**：账户一旦创建即不可变、不可回收；而「先不建」完全可逆。
   在不可逆与可逆之间，选可逆。
3. **符合仓库既有纪律**：`PREREGISTRATION` §8 与 `resume_frozen_specs` 的注释均要求
   「研究编辑适用于**新身份**，绝不覆盖既有账户，也不阻止既有账户按冻结规则继续运行」。
4. **本次核查未发现这 29 项存在前视或算错**，只发现它们缺硬止损——这是**规格缺陷**，不是实现错误。

### 6.3 若要推进，需要的动作（留给用户/执行侧决定）

```
若要让「带硬止损的正确版本」开始前向积累，正确做法是【新身份并存】，而不是改这 29 个：
  1. 确认 strategy_specs.json 已定稿（当前自 10-12 14:23 起未再变动）
  2. 为 s01~s30 生成带 risk_stop_atr_mult 的新身份（例如后缀 `_atrstop`）
  3. 旧 29 个身份标注「止损口径缺陷期」，其样本不参与评估（STOP_DECISION.md 已如此要求）
未做这一步的原因见 6.2：这是人口设计决策，且规格仍由他人持有。
```

⇒ 需知悉：**这 29 个账户跑的不是当前 spec 文件里的规则，且缺硬止损；其样本按项目自身规则不应用于评估。**

---

## 七、纪律与局限

1. 45 个 Fnew 账户为**前向研究假设**，尚无收益结论。截至 10-13 03:13 的实测样本：
   s32/s33/s36 各 81 个决策、**尚无成交**；s34 306 决策 / 90 成交 / **36 笔平仓 / 3 次强平**；
   s35 306 决策 / 18 成交 / 9 笔平仓。**样本量远不足以裁定，禁止标盈利/通过/可用。**
2. 配对账户的 R 档是**计划止损与费用预算**，不是硬顶——契约自身写明收盘确认、跳空、强平可使实际亏损超过 R；对应测试亦断言 `net_pnl < -50`。
3. `s33` 用 taker 0.04%/边（因需现货腿），其余 Fnew 用 maker 0.02%/边；**maker 费率仍未被真实成交验证**。
4. 本次未改任何既有账户、策略规则或历史事件；仅新增 Fnew 账户、修数据缺口、改服务层与 `manage.py`。
5. ~~未加入实时采集的新市场（spot）——`s33` 的前置条件仍缺。~~
   **已解决**：新增受管服务 `spot-basis` 持续维护现货 1h 前向喂数（见 5.3）。

---

## 八、验证证据

```
新测试        21 项（16 单元 + 5 真实 PostgreSQL 集成）全通过
相关套件      178 项全通过（含 fnew / pair / lab / frozen-resume / candle-repair）
全量          1452 项，4 项失败全部位于并发的 demo_fee_capture 模块
              （该模块对本次改动文件引用数为 0）
ruff          manage.py / 两个新测试文件 全部通过
线上          lab service running（observing），account_count 335，last_error None
              spot-basis running，ready=true，last_error null
账户          355 = 310 legacy（零改写）+ 45 Fnew
Fnew          45/45 ready（各 750 根预热）
数据修复(1)   5m 缺口 1 → 0 · 15m 连续后缀 2 → 750
              回滚日志 /tmp/repair-apply-1/rollback.jsonl
数据修复(2)   spot 1h 最后 10-08 → 10-12 17:00 · 752 根带收盘标记
              回滚日志 /tmp/spot-apply-1/rollback-*.jsonl
```

### 8.1 持续运行 8 小时后复测（2026-10-13 03:13）

```
服务存活      lab PID 43294 · spot-basis PID 45751（均为 10-12 启动，未重启）
最近事件      10-13 03:00:53（13 分钟前）

Fnew 累计     base   decisions  fills  closed  liq
              s32         81      -       -     -
              s33         81      -       -     -
              s34        306     90      36     3
              s35        306     18       9     -
              s36         81      -       -     -

连续性        s32(1h)  18:00→02:00 共 9 小时，每档间隔恰 1 小时，零缺失
              s34(15m) 34 根 / 理论 34 根，零缺失
状态          15m observing(latest 02:45) · 1h observing(latest 02:00) · 1d 正确等待 08:00
```

---

## 九、决策记录（用户「你自己决策」授权）

| # | 事项 | 决策 | 依据 |
|---|---|---|---|
| 1 | 29 个基础账户身份分叉 | **保持冻结，不重建不迁移**（并在 6.1 记录逐字段差异） | 分叉源自他人建账后编辑的、且在动的规格；创建账户不可逆、不创建完全可逆；符合仓库既有冻结纪律 |
| 2 | `data_stale` 是否加自愈 | **不加**（并**更正**我此前「不可自愈」的错误结论） | 8 小时实测零缺失，机制本身自愈；加自愈会掩盖真实数据中断 |
| 3 | `demo_fee_capture` 4 项测试失败 | **不动** | 他人并发在制品（18:27 仍在改），对其文件的引用与本次改动无交集 |
| 4 | 本次改动是否提交 | **提交本次 5 个文件** | 工作已验证完成，避免散落在脏工作区；不触碰他人改动的文件 |
