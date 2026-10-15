# 止损执行与方向偏差实验预注册草案

建立：2026-10-13  
用途：在新账户产生结果前冻结比较口径。本文是研究设计，不是上线、盈利或可用性裁定。

## 0. 研究边界

数据来自 `quant.strategy_sim_events` 的本地 paper forward。当前成交模型是
`next_closed_bar_open_proxy`；`slippage`、`funding` 未知，不能等同 Binance Demo 实盘。
所有变体必须新建账户，保留原版账户并行运行，不原地修改 `execution_hash` 或
`rule_hash`。实验账户默认 `execution_enabled=false`，另经人工闸门才可激活。

模拟强平价是引擎抽象 `entry * (1 - direction / leverage)`，距离恒为 `1 / leverage`；
没有维持保证金、阶梯保证金或 mark price 模型。报告中的强平率只解释该模拟口径。

## 1. 主实验：止损执行时序

### 1.1 人口与对照

优先覆盖 2x2 尚未覆盖的族：
`s34, s09, s03, s33, s01, s11, s04, s18, s16`。每个族建立同一冻结入场规则的两臂：

| 臂 | 止损执行 | 其余条件 |
|---|---|---|
| T0 | `close_confirmed`，下一根开盘代理成交 | 原版杠杆、fee、币池、入场、名义/数量、持仓上限 |
| T1 | `intrabar`，5m OHLC 触发；同 K 止损优先；跳空按下一开盘 | 与 T0 完全相同 |

2x2 已覆盖的 `s05/s06/s08/s12/s14/s17` 可作为执行机制先验与实现回归，不把其结果
直接外推到上述九族；若再次运行，只作预先登记的复现/异质性层。

### 1.1a 九族新臂的准入裁决

九族 T0/T1 是一个**新的预注册实验**，不是把全库模式汇总的混杂证据重新命名。准入
动机只引用干净的同族同杠杆 A-vs-C 先验：当前 A 为 `8/26=30.8%`、C 为
`2/32=6.3%`，Fisher 双侧 `p=0.0320809`。该先验必须同时标注其稀疏依赖：
s05 单族 `p=0.155844`，s17 `p=0.073684`，s14 `p=1.0`，s08 无事件；去掉 s05
后 `p=0.113756`，去掉 s17 后 `p=0.207412`。它只能说明值得预注册验证，不能作为
九族已有因果效果的证明。

九族实验只有同时满足以下四条才可进入新账户实施：

1. 使用新的实验身份和新的 `rule_hash/execution_hash`，冻结原版并行对照；不得把
   A-vs-C 的结果拼接进九族的主分析。
2. 每族至少规划 `100` 个有效配对，目标 `200` 个/臂；族级主检验使用
   Bonferroni `alpha=0.05/9=0.005556`。
3. 若真实差异接近 `17% vs 11%`，当前规模不足，目标约 `500` 个/臂；不得通过
   降低阈值、合并不相同族或改用未预注册终点来制造显著性。
4. 所有配对在同族、同币、同杠杆、同信号机会和同时间窗内生成；skip、未平仓和
   数据缺失必须保留分母并单列。

### 1.1b 样本量分档

以下数字回答不同问题，不能互相替代：

| 目标比较 | 粗略 iid 参考 | 设计约束 |
|---|---:|---|
| 合并主分析，30.8% vs 6.3% | 约 74/臂 | 只作 pooled 粗基线；需再报告配对/块设计效应 |
| 每族结论 | 不以 74/臂替代 | 每族至少 100 有效配对，目标 200/臂；Bonferroni `alpha=.005556` |
| 17% vs 3% | 约 71/臂 | 稀疏事件仍建议每族 100、目标 200/臂 |
| 11% vs 3% | 约 159/臂 | 目标至少 200/臂 |
| 17% vs 11% | 约 500/臂 | 目标约 500/臂；低于此规模明确标注规模不足 |

这些是独立 Bernoulli/Fisher 的粗参考；最终以配对差、日期块设计效应、族/币分层和
校正后的置信区间为准。`N_min=100` 是最低观察门槛，不是充分功效。若实际效应小于
预先登记的目标，报告“规模不足”，不得凑显著或延长观察后改写停止规则。

### 1.2 配对单位和冻结字段

主分析以同一 `signal_id × pair × signal_candle_ms` 的潜在交易机会配对；两臂应共享
实际 entry、方向、entry ATR、止损原始距离、杠杆、费用、名义/数量预算和数据窗。
若一臂跳过或缺失，保留为 skip 并报告原因，不把另一臂单独当独立样本。

固定每个配对机会的数量或名义作为主 estimand；按止损距离重新计算的风险缩放作为
secondary estimand，避免把止损规则效果和仓位缩放效果混在一起。

### 1.3 主要终点

1. 配对机会的 `liquidation` 指示变量差 `I(T1)-I(T0)`，报告配对比例、95% 日期块
   bootstrap CI 和 sign-flip permutation p 值。
2. 族级主检验为 T1 相对 T0 的强平率下降；双侧 α=0.05/9（Bonferroni）作为九族
   家族级确认阈值。未达到该阈值只能报告估计和区间。

### 1.4 次要终点

- `stop_loss`、其它退出原因和退出时点；
- 止损超调：实际成交相对冻结 stop 的距离，以及开盘跳空指标；
- `net_pnl`、`gross_pnl`、fees、持仓根数；
- 风险缩放 estimand 下的上述指标；
- clamp 触发率、原始/裁剪距离、`stop_distance < liquidation_distance` 比例；
- `equity` 非正、异常事件、缺失配对和未平仓率。

强平率是主要安全终点；intrabar 可能提高 `stop_loss` 计数，因此不能只用止损率判定优劣。

### 1.5 统计方法

- 主估计：同一配对的差值，按 UTC 日期或固定时间块重采样；块长至少为该族中位持仓期，
  并覆盖信号重叠。报告朴素配对 SE、日期块/族聚类 SE、设计效应和有效样本量。
- 稀疏强平事件：优先 exact/sign-flip permutation；补充 Fisher 或 CMH，但不把未配对
  汇总比例当作因果证据。
- 分层：族、BTC/ETH、long/short、市场状态（趋势/震荡按冻结的事前规则）。每增加一层
  都重新列出分母；不把不同 `stop_execution_mode` 混在同一族内。
- `fixed_entry_atr_close_confirmed_next_open` 单列为第三种机制，不能与 opt-in 两臂
  汇总后解释。

### 1.6 先验数据的独立复核

截至本次只读快照水位，已对全库 2,161 个 position_opened/trade_closed 配对做复核：

| 模式 | 配对 | 强平 | 率 | Wilson 95% CI |
|---|---:|---:|---:|---:|
| intrabar | 63 | 2 | 3.17% | 0.88%–10.86% |
| close_confirmed | 48 | 8 | 16.67% | 8.70%–29.58% |
| fixed entry ATR + next open | 1,749 | 186 | 10.63% | 9.29%–12.16% |

未匹配模式还包括 legacy/other 301 笔、36 次强平，单列不并入主比较。
intrabar vs close 的未配对 Fisher 双侧 p=0.01896；共享四族的 CMH p=0.01779，
但只有 `s05/s08/s14/s17` 四个共享族且单元稀疏，因此这是机制先验而非确认结果。
模式不是随机分配，且止损距离、杠杆和族构成不同；新实验必须同族同币同信号并行。

粗略两比例功效参考：若真实强平率为 17% vs 3%，独立两臂约 71 笔/臂；11% vs 3%
约 159 笔/臂；17% vs 11% 约 524 笔/臂。实际应以配对差、日期块设计效应和
Bonferroni 后的目标效应重算；建议每族至少 100 个有效配对，目标 200 个/臂，
若要区分 17% 与 11% 则目标接近 500 个/臂。100 只是最低观察门槛，不是充分功效。

## 2. 辅助实验：clamp × 执行时序

使用新实验身份，四臂只在已覆盖的共同机会中比较：

| 臂 | stop distance | stop execution |
|---|---|---|
| C0 | 原始距离（但必须记录是否超过强平距离） | close_confirmed |
| C1 | `min(raw_distance, 0.7 * liquidation_distance)` | close_confirmed |
| C2 | 原始距离 | intrabar |
| C3 | 同一 clamp | intrabar |

主要交互为 `(C3-C2)-(C1-C0)`，终点仍为强平率；clamp 触发率和设计距离为安全
终点，P&L 为次要终点。若 C3 仍明显优于 C1，说明执行延迟独立于距离风险；若只在
close 下改善，则不能把 intrabar 的作用归因于 clamp。

原始结构/ATR 距离都必须在 entry 时计算并冻结，写入 `raw_stop_distance`、
`clamped_stop_distance`、`clamp_flag` 和 `liquidation_distance`。clamp 后允许跳空或
开盘先强平导致超出设计上限；记录并报告超限率，不能承诺每笔严格不超过预算。

## 3. 实现落点（交给备用3号）

结构止损属于**信号/入场证据层**：在 `strategy_signals.py` 的 `_single`（或等价族级
信号函数）用 t-1 及更早的已收盘数据计算并冻结 `stop_spec_id`、`raw_stop_level`、
`raw_stop_distance`；不得在 `strategy_simulation.py` 里用未来 K 线重新寻找结构。

`strategy_simulation.py` 负责执行层通用逻辑：读取冻结 stop evidence，应用 opt-in
clamp policy、stop mode、gap/强平优先级，并将 `clamped_stop_distance`、`clamp_flag`、
`liquidation_distance` 写入 `position_opened`/`trade_closed`。对于
`risk_sizing_mode="structural"`，必须新增“直接采用冻结 stop”的分支；不得调用现有
`_entry_atr_plan` 重新用 ATR 覆盖 `evidence.stop_level`。账户 identity 必须包含结构
stop policy、clamp policy 和 stop mode，变体使用新 `experiment_id` 与新账户。

### 冻结字段契约

入场 evidence 至少包含以下字段，且在两臂/四臂的同一 signal opportunity 中相同：

`stop_spec_id`、`stop_policy_version`、`structure_anchor_candle_ms`、
`structure_lookback`、`structure_inputs_hash`、`stop_direction`、`entry_price`、
`raw_stop_level`、`raw_stop_distance`、`raw_stop_distance_pct`、`liquidation_distance`、
`clamped_stop_level`、`clamped_stop_distance`、`clamp_flag`、`clamp_ratio`、
`notional_policy`、`frozen_notional`、`frozen_quantity`、`signal_id` 和
`signal_candle_ms`。实际 position/trade 还应记录 `stop_level`（执行值）、
`entry_atr`（若仅作诊断可为 null）以及 `risk_sizing_mode`。

`execution_identity`/`execution_hash` 必须覆盖 `experiment_id`、
`risk_sizing_mode`、`stop_policy_version`、`stop_execution_mode`、`stop_clamp_policy`、
`clamp_ratio`、`notional_policy`、`max_loss_per_trade_usdt`、fee version 和 execution
universe；`stop_spec_id`/结构输入哈希进入 `rule_hash` 或 canonical stop-rule hash。
这样即使杠杆和入场信号相同，结构 stop、clamp 和名义口径也不能与旧账户混淆。

### 名义口径：主 estimand 与安全执行分离

不接受把“每个臂各自按 clamp 后距离重新算名义”作为主比较，因为它同时改变了
stop rule 与仓位规模。主 estimand 使用 `notional_policy="frozen_signal"`：在同一
signal opportunity 先计算并冻结 `frozen_notional/frozen_quantity`，所有臂复制该值；
clamp 缩短 stop 时只改变计划风险输出，不改变主比较的实际名义。也就是说，方案 (b)
只有在这里被接受：它是**共同基线名义冻结**，不是每臂重新计算的 raw cap。

真实执行安全可另设方案 (c) 的双限：
`execution_notional = min(frozen_notional,
max_loss / (clamped_stop_distance_pct + round_trip_fee))`，并记录
`notional_adjusted_for_safety`。该值若导致两臂名义不同，只进入 secondary safety
分析，不能与 frozen-signal 主 estimand 混称。通常 clamp 只会缩短距离，此双限与共同
raw cap 相同；它仍能防止异常输入、费率或结构路径使执行名义超过安全上限。

建议新增：

- `strategy_signals.py`：每族结构 stop 纯函数；输入只含已收盘历史，校验 long stop<entry、
  short stop>entry；历史不足或结构在错误一侧时返回 `skip_reason`，绝不回退 ATR；输出上面
  的冻结字段和 `risk_sizing_mode="structural"`。
- `strategy_simulation.py`：`_structural_stop_plan(...)` 的直接采用分支、
  `stop_clamp_policy`、`apply_stop_clamp(...)` 和统一证据序列化；保留 `raw` 与
  `clamped` 两套字段，支持 `frozen_signal` 与 `dual_limit` 两种明确名义策略。
- 测试：无前视/冻结时点、long/short 方向、历史不足 skip、结构错误侧 skip、clamp 边界、
  30x/100x liquidation distance、同 K stop-first、开盘跳空、四臂 identity 不可混淆、
  名义冻结与风险缩放 secondary。

## 4. 调整5：方向偏差独立 holdout 检验

### 4.1 发现窗与留出窗

发现窗只用于冻结机械规则和候选族，不用于估计最终方向效应。冻结后设置按时间顺序
切分的未来 holdout，至少覆盖两个市场状态；holdout 期间不得调方向、阈值、持仓期或
止损参数。若为 15m，按预注册规则达到 N_min=100 后才启动裁定；1h/1d 仅描述性。

### 4.2 成对反向假设

对同一 `signal_id × pair × timestamp` 同时生成原方向和反方向潜在交易，固定 entry、
数量、fee、stop、持仓期和执行模型。主端点为 `D = net_return_reverse - net_return_original`
（同时报告 R 单位）。不使用多头占比或胜率作为主要方向证据。

原假设 `H0: E[D]=0`；预先指定单边方向 `H1: E[D] > δ_min`，δ_min 在发现窗结束前
登记，例如 0.1R 或 0.2% 名义。使用日期/事件块 paired bootstrap CI 与保持整块的
sign-flip permutation；按币、族、long/short 和市场状态分层。固定 6 个目标族时，
族级 α=0.05/6≈0.00833；pooled 结果只作 cluster-robust secondary。

### 4.3 样本量与停止规则

配对差的有效样本量按 `N_eff ≈ (2.8 * σ_D / δ_min)^2`，再乘日期/重叠设计效应。
前导窗只估计 σ_D 和持仓/事件重叠，不估计方向均值，也不选择方向。若 `σ_D=0.5R`、
`δ_min=0.1R`，约需 196 个有效配对/族；实际目标取公式值与 100 中较大者，并按
设计效应膨胀。建议 holdout 至少 200 个有效配对/族并覆盖两个市场状态。

预注册停止规则：达到目标样本前不下方向结论；达到目标后只做一次主检验。达到 N_min
但 CI 含 0，记录为不确定并停止该窗的参数调整；不得看结果后延长、换族或反向取信号。
若数据质量不满足，则暂停统计、保留原始事件并报告缺失/skip 分母。

## 5. Skip、数据窗和报告纪律

- 结构历史不足、结构方向错误、缺 entry/stop/liquidation evidence、缺共同 signal_id：
  `skip`，按族和原因报告；不得回退 ATR或删除另一臂。
- 发现窗：截至冻结日期的历史 paper forward，仅用于定义机械规则和 σ_D 前导估计。
- 执行主实验：冻结后新产生的连续 forward 窗；原版与变体同窗并行。
- 方向 holdout：发现窗之后的未来窗，至少两个预先定义的市场状态；不得把发现窗
  交易并入 holdout。
- 所有结果同时报告 raw N、有效配对 N、skip N、未平仓 N、日期块数、设计效应、CI、
  p 值和多重校正方式。禁止用跨族账户数量制造样本量，禁止把两条配对腿当两次独立观察。

## 6. 结论边界

本文件只规定可归因实验和统计检验。任何强平率差、净收益差或方向差异都必须结合
模式混杂、模拟执行、未知滑点/资金费、市场状态和有效样本量解释；不得据此标注盈利、
通过、可用或自动上线。
