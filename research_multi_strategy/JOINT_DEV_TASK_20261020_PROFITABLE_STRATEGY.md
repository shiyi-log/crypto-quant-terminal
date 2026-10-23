# 联合任务卡：寻找可盈利策略

建立时间：2026-10-20（北京时间）  
项目：`/Users/shiyi/Documents/ChatGPT/量化`  
通道：`quant`  
状态：联合研究框架已建立；尚未冻结具体候选 `research_id`，未开始新的业务策略实现

## 1. 角色与决策权

| 角色 | 负责事项 | 不得越过的边界 |
|---|---|---|
| Codex | 代码实现、架构、数据与执行口径、实验编排、最终工程取舍、合并前验证 | 不把未经独立审查的结果称为盈利或可上线 |
| DeepSeek | 独立审查假设、数据泄漏、样本切分、成本与成交模型、统计结论、证据完整性 | 不直接改 Codex 正在负责的业务文件；不以单次回测或单次正收益作上线建议 |
| 用户 | 目标、风险约束、资金与上线授权 | 未明确授权前不进行真实交易或改变生产配置 |

按用户授权，Codex 对工程和研究取舍作最终决定；DeepSeek 给出独立审查意见，不拥有代码写入权或最终决策权。Codex 可以采纳或用可复核证据回应审查意见，并记录理由。DeepSeek 标记 `dispute=unresolved` 的事实或实现问题，在证据解决前阻断晋级；纯方法判断分歧由 Codex 留下证据化裁决；涉及用户风险偏好或资金约束的分歧升级用户决定。真实交易、部署和生产变更仍须用户明确授权。DeepSeek 的审查结论进入证据账；`accepted` 只代表消息送达，不代表审查通过。

## 2. 文件分工

- DeepSeek 独占：`tools/codex_bridge/desktop_link.py`、`tools/codex_bridge/DEEPSEEK_HANDOFF.md` 以及审查意见文件。
- Codex 独占：`tools/codex_bridge/bridge_core.py`、`tools/codex_bridge/CONNECTION_*.md` 以及执行实现文件。
- 研究规格由双方先提案后冻结；同一文件同一时间只允许一方编辑。
- 每次业务修改开始前，通过 `quant` 发送文件清单、假设、验证命令和预期产物；完成后发送 diff 摘要和证据路径。

## 3. 研究目标

目标不是寻找历史上看起来最赚钱的曲线，而是寻找在完整交易成本、现实成交约束和未见数据上仍有正期望的策略。研究输出必须区分：

1. **候选假设**：机制和参数已写死，但没有收益结论。
2. **研究结果**：按预先登记的口径得到的样本内、样本外和前向统计。
3. **可继续观察**：证据达到保留门槛，但还没有独立复现或部署资格。
4. **可盈利候选**：同时满足第 6 节所有硬门槛，DeepSeek 已完成独立审查，且 P0/P1 事实或实现问题已修复或由证据排除。
5. **可上线**：另需用户明确授权、运维闸门和小额纸面/模拟阶段；本任务卡本身不授予上线权。

任何报告禁止把 `KEEP`、正回测、浮盈、未平仓收益或 `accepted` 消息写成“盈利已证实”。

## 4. 单个策略模板

每个候选策略建立一份冻结记录，至少包含：统计与成本细节以[可盈利策略研究模板](PROFITABLE_STRATEGY_RESEARCH_TEMPLATE.md)为唯一口径，以下为任务索引：

```yaml
strategy_id: "唯一且不可复用"
hypothesis: "可证伪的机制假设"
universe: ["BTCUSDT", "ETHUSDT", "TRXUSDT"] # 首个研究风险预检基准币池；候选仍须在独立预注册中冻结
timeframe: "原生周期；只使用已收盘数据"
signal_time: "信号形成的时间边界"
entry: "价格、方向、订单类型、next-open 或可验证成交规则"
exit: "止盈、止损、时间、反向信号及其优先级"
risk:
  max_leverage: "100x（首个研究的全局上限；不是各币实际杠杆）"
  position_leverage_by_symbol: {"BTCUSDT": {"notional_bracket": "待账户快照", "bracket_max_leverage": "待账户快照", "chosen_position_leverage": "待冻结，且 <= min(100x, bracket_max_leverage)"}, "ETHUSDT": {"notional_bracket": "待账户快照", "bracket_max_leverage": "待账户快照", "chosen_position_leverage": "待冻结，且 <= min(100x, bracket_max_leverage)"}, "TRXUSDT": {"notional_bracket": "待账户快照", "bracket_max_leverage": "待账户快照", "chosen_position_leverage": "待冻结，且 <= min(100x, bracket_max_leverage)"}} # 键可写 BTCUSDT 或 BTC/USDT:USDT；规范化后统一为后者
  tier_snapshot_hash: "待账户适用的 Binance signed leverageBracket 快照冻结"
  tier_snapshot_source: "Binance USDⓈ-M leverageBracket USER_DATA；当前未调用账户端点"
  tier_snapshot_fetched_at_utc: "待补"
  tier_snapshot_account_scope: "待补（账户/撮合环境/币种档位适用范围）"
  initial_equity_usdt: "10000（Codex 选定的合成研究基准，不代表实际账户）"
  account_p99_loss_limit_initial_equity_pct: "5% = 500 USDT（仅首个研究）"
  margin_mode: "isolated（合成研究基准）"
  per_trade_risk_cap: "1% 初始权益 = 100 USDT（合成研究基准）"
  max_total_notional: "50000 USDT = 5x 合成权益"
  per_symbol_notional_cap: "25000 USDT = 2.5x 合成权益；BTCUSDT/ETHUSDT/TRXUSDT 各一项"
  isolated_initial_margin_rule: "逐仓初始抵押金=该仓位名义/实际杠杆；未分配权益不自动补仓"
  liquidation: "账户适用杠杆/维持保证金档和强平成交模型待核实；未完成前风险状态 not_estimable"
cost_model: "maker/taker、手续费、滑点、资金费、价差、延迟"
parameters: "全部参数及冻结时间"
data_snapshot: "数据版本、覆盖区间、缺失与修复记录"
rule_hash: "规则 SHA-256"
spec_hash: "预注册规格 SHA-256"
code_commit: "Git commit SHA 或工作树补丁哈希"
config_hash: "执行配置 SHA-256"
code_hash: "代码 SHA-256 或 commit"
data_hash: "数据快照 SHA-256"
seed: "随机过程种子；无随机过程填 none"
risk_segment_status: "open | closed"
risk_data_acquisition_plan: "not_estimable 时的数据源、负责人和截止日期"
risk_feasibility_preflight: {status: "not_estimable", reason: "风险行情统计已完成；账户适用杠杆/维持保证金档、强平成交、滑点与延迟冲击模型未完成", strategy_signal_or_return_accessed: false}
runner_version: "回测/前向 runner 版本"
holdout_reads: {global_interval_id: "venue-timeframe-symbol-set-UTC-range", family_id: "预注册研究族", primary_oos: 0, independent_replication: 0}
historical_access_audit: {status: "clean | not_proven", scope: "范围", reviewed_at: "UTC时间", evidence_hashes: []}
evaluation_plan: "IS/OOS/forward 切分和停止规则"
null_model: "零假设、对照策略或置换方法"
promotion_status: "hypothesis | insufficient_sample | invalid_run | provisional | undecided | candidate_pending_replication | candidate_pending_forward | profitable_candidate | rejected"
```

`tier_snapshot_hash` 绑定规范化档位集合；`evidence_sha256`、`request_id`、`response_sha256`、`signed_at_utc` 和人工 `verified` 标记用于标识提交的证据账或人工复核状态。这些字段不能单独证明 Binance USER_DATA 请求的 HMAC 签名真实性，也不能证明解析后的档位确实来自该原始响应。只有在原始响应字节、签名请求元数据与解析载荷之间完成独立核验并由审查记录确认后，才可把该快照用于执行；否则风险状态必须保持 `not_estimable`、`risk_segment_status=open`。

规则、账户身份、执行策略、成本和数据版本改变时必须生成新的 `strategy_id` 或新的实验版本，不能覆盖旧账。

## 5. 实验顺序

1. 先登记假设、参数、数据区间、零假设和停止规则，再运行候选搜索。
2. 数据按时间切分为训练/样本内、验证/模型选择和最终样本外；最终样本外在参数冻结后只能读取一次用于裁定。
3. 参数搜索只能使用训练和验证段；禁止用最终样本外或前向结果挑参数、挑币或挑策略。
4. 样本外结果通过后，进入实时前向纸面或模拟执行；记录信号、可成交价格、拒单、延迟、费用和每次状态变化。
5. DeepSeek 完成独立审查、Codex 逐项处置 findings 且无未解决的 P0/P1 事实/实现问题后，才可标记为 `profitable_candidate`；没有用户授权不得接入真实下单。

## 6. “可盈利候选”硬门槛

必须全部满足；统计、成本和单位细节以[可盈利策略研究模板](PROFITABLE_STRATEGY_RESEARCH_TEMPLATE.md)为唯一口径。证据尚缺但运行有效时标 `undecided`；若数据/实现缺陷或重复揭盲违反冻结协议则标 `invalid_run`，不得把无效运行当成策略否决：

- 最终样本外和前向结果均按净收益统计，完整扣除手续费、滑点、资金费、价差和可观测延迟影响。
- 结果来自已平仓交易；未平仓浮盈单独列出，不计入盈利证明。
- 正式候选限定 15m；唯一入场时刻数须达到模板按 `K`、时间设计效应和经济效应阈值算出的 `n_required`，且绝不低于 400。400 是下限，不是充分样本；MDE 须 `<= delta_econ=max(6 bp, 1.5×平均全包成本)`。报告当前唯一入场速率及按该速率计算的 ETA。1h/1d 仅作机制验证。收益/成本统一按入场名义计，`1 bp=10^-4`、`1‰=10 bp`。
- 搜索总数 `K` 包括本轮失败/否决配置及可查的同族历史搜索；策略级和族级分开做 BH-FDR `q<0.05`，并报告 Bonferroni `alpha/K`。若搜索历史无法界定，用 Hansen SPA 或保持 `undecided`。MDE 按 `(z_(1-alpha/2) + z_0.80) × SE_cluster` 计算，alpha 使用 `0.05/K`。
- 净收益均值 `>= delta_econ=max(6 bp, 1.5 × 平均全包成本)`，相对同持有期预注册基准超额 `> 4 bp`；点估计为正但未过校正或复现只能标 `provisional`。
- 复现要求同时满足：4 个连续 UTC 日历季度净收益均为正（每季至少 20 个唯一入场时刻）；季度不在 train/validation 区间内，且完整候选/研究族访问账证明从未用于任何候选级分析，包括探索性统计、绘图、相关性、分布查看或人工检查；访问账缺失/不完整时按已读处理。预注册币池至少 2 币各自净收益为正。独立复现必须使用时间不相交区间或标的/市场集合不相交的 OOS，并以同一规则/参数复现、校正后 CI 下界 `>0`；同一时段重新下载、修复或换版本的数据快照只能作可复现性附注。每个新 `research_id` 必须附历史访问审计结论；不能证明未读时，计划与 ETA 按纯前向路线计算。纯前向路线从注册后首个完整季度 Q1 2027 起算，Q1-Q4 2027 四季最早于 2028-01-01 后且数据齐全时裁定。OOS 揭盲前须登记共享区间的全部研究族、变体及 `K`；一个区间首次被任何候选级分析读取后，对市场/周期/时间和标的集合有重叠的所有族一并烧录，并将重叠区间检验纳入 `K` 与 BH-FDR；重复/新增读取标 `invalid_run`。首个 `research_id` 冻结前必须完成纯风险数据可行性检查，只读历史风险数据，不查看策略信号/收益，并把已读区间标为 exploratory/burned。禁止事后扩币池或改周期凑样本。
- 前向至少 30 个唯一入场时刻、10 个交易日，单日净收益贡献低于 60%；完整闭合前向样本也须达到正式门槛。
- 执行/控制类违规（止损落在强平价外、方向或数量错误、cap 钳制未执行、拒单/部分成交未入账等）在历史、前向和压力样本中的发生率必须为 0%。市场跳空超出名义单笔 cap 可发生，须独立报告其超 cap 比例、P99 和最差单笔；P99 容忍值按初始账户权益比例事前冻结，并同时报告 USDT。历史跳空压力按每币连续相邻 15m K 线计算 `gap=(open_next-close_prev)/close_prev`；分别对多头不利 `max(-gap,0)`、空头不利 `max(gap,0)` 取非零值经验 99 分位数（升序第 `ceil(0.99N)` 个）。将每币该单次冲击同时施加于压力时点该币所有同向持仓并报告组合权益影响；非连续 K 线间隔作为数据中断压力单列；无足够不利跳空观测则标为不可判定，不得通过。压力集还包含基准滑点 2×、1,000 ms 延迟和强平价不利滑点 1/5 tick；写明破产价、保证金上限及保险基金/ADL 的证据依据，任何超过保证金且未获证据支持的损失记为账户负权益/未覆盖赤字，不得裁剪。逐档报告负权益/赤字金额与发生率；压力 P99 不得超过冻结容忍值，出现未覆盖负权益/穿仓则风险门槛失败。
- 组合跳空压力细化：`N_gap` 为每币、每方向连续相邻有效 15m K 线中的非零不利跳空数，必须 `N_gap >= 100`；不足时记录数量并标 `not_estimable`，风险门槛不得通过。只用冻结仓位账本中的 UTC 时间、币种、方向、数量和名义金额选出总绝对名义敞口最大的时点，并列取最早；不得读取收益或按收益、盈亏、回撤挑时点。主档在该同一时点对全部币种同时施加各自方向的 P99 不利跳空，另报逐币单独冲击和逐笔入场冲击。纯风险预检没有候选仓位账本时，以预注册的最大允许同时暴露上限构造风险组合，不得查看候选信号或收益。
- 压力快照网格固定为每个 UTC 15m K 线收盘 `t`，敞口名义按 `close_t × abs(position_qty)` 计算；快照只含 `t` 收盘时已确认且已持有的仓位，不含由该收盘信号在 `open_(t+1)` 才成交的订单。只有存在下一根有效、已收盘且时间戳恰为 `t+15m` 的 K 线时，`t` 才参与选择。取达到最大总绝对名义敞口的最早 `t`，在 `open_(t+1)` 施加该币/方向经验 P99 不利跳空幅度；`gap_t` 只表示收盘到开盘的对齐区间，不能使用该区间实际跳空替代 P99。记录 `t`、`t+1`、仓位、名义及快照哈希。
- 纯风险预检固定使用 A/B/C 三档风险包络：`N_max` 是预注册全组合最大总绝对名义；如有单币上限，其总和不得低于 `N_max`，否则预检不能 `pass`。A 档分别为全币同向做多、全币同向做空，各自总名义达到 `N_max`；B 档按标准 symbol 升序交替分配多空，两侧各为 `N_max/2`，总计 `N_max`。C 集中档按每币/方向 `max(trade P99, mark P99)` 降序填充，方向并列取 long，排序平手按 symbol 升序；有单币上限时逐币填至上限或填满 `N_max`，无单币上限时全部配置给排序第一项。A/B/C 各档均叠加完整冻结压力集（基准滑点 `2×`、执行延迟 `1,000 ms`、强平后不利滑点 `1 tick` 与 `5 tick`）并独立判定，任一不通过则预检不得 `pass`，并报告较差档。
- B 档至少需要两个标的，且按冻结单币上限无法令多空两侧各达到 `N_max/2` 时标记 `not_estimable`，整个预检不得 `pass`。
- 双路径执行口径（作为上述跳空简写的详细定义）：交易价 OHLC 与标记价 OHLC 按 symbol/UTC 对齐。标记价负责未实现盈亏、保证金余额和强平触发；交易价负责止损/强平成交、已实现盈亏、手续费与滑点。强平后按交易价成交模型结算并移除该仓位的标记价浮亏，同一段价格变化不得重复计损。逐仓抵押金按名义/实际杠杆配置，未分配权益不自动补仓；规则依据见 [Binance Futures Liquidation Protocols](https://www.binance.com/en/support/faq/detail/360033525271) 和 [USDⓈ-M 强平价格说明](https://www.binance.com/en/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93)。
- 用户指定的 5% hard gate 是每个 A/B/C 组合在同步 trade/mark 有效 15m 窗口逐窗重置、用 `t` 收盘建立冻结名义、以 `t+1` 开盘标记价先检查强平再用交易价退出所得“单窗口账户损失分布”的 nearest-rank P99。另报同步历史窗口最大账户损失；该单窗口指标不代表多根 K 线累计权益路径风险。候选风险评估须用冻结仓位账本连续回放，并按事前规定的损失观察单位计算 P99/最大损失；观察单位或持仓期未冻结时仍为 `not_estimable`。各币/方向分别计算的 trade P99 与 mark P99 同时施加属于“联合边际 P99 压力包络”，不得称作单窗口账户损失分布 P99；还须报 trade-only 和 mark-only 敏感性。将不同时间的样本最大值拼接时标为“合成最大包络”，不得称历史同步事件。
- 样本最大跳空为强制披露，但不单独受 5% P99 门槛约束；任一预设压力情景出现负权益或未覆盖赤字仍判风险失败。trade/mark 每币每方向分别要求 `N_gap >= 100`；15m OHLC 无法恢复盘中触发顺序或 1,000ms 后成交，若无逐笔/盘口证据或独立支持的保守成交上界，仍为 `not_estimable`。同区间可能同时止损与强平时按强平优先。
- A/B 风险包络使用等名义水位填充，触及单币上限后按标准 symbol 顺序填余额；C 按每币/方向 `max(trade P99, mark P99)` 降序集中填充。每档分别报告同步账户损失 P99、同步最大窗口损失、联合边际 P99 包络、单路径敏感性和 1/5 tick 压力结果。完整口径以[研究模板第 5 节](PROFITABLE_STRATEGY_RESEARCH_TEMPLATE.md)为准。
- 风险预检的“完成”指执行并登记状态，不代表 `pass`。`not_estimable` 时研究协议可冻结，但必须登记数据来源、负责人和截止日期，`risk_segment_status` 保持 `open`；风险数据和用户风险边界（杠杆、保证金模式、单笔 cap、初始权益比例 P99 容忍值）齐备且预检通过后才可关闭。风险段关闭前不得运行或查看候选回测、OOS、前向结果，也不得晋级；无数据计划不得冻结。预检失败须先降低风险或终止该风险配置，并以新修订重新预注册。
- 成本、成交和容量按模板固定：maker 必须有可证伪且由 Demo 校准的 fill/队列模型；无证据时按未成交或 taker 计。项目已知 hybrid maker-entry/taker-exit 手续费为 6bp，taker/taker 为 8bp；费率须在新实验启动时刷新。
- 成交规则可被审计：信号时间、订单时间、成交价格、订单状态和成本字段完整，缺失字段不猜测。
- 与简单基准、随机/置换零假设和成本敏感性对照后仍有经济意义的净边际。
- DeepSeek 的独立审查逐项列出“无前视、无选择偏差、成本足够保守、数据和执行证据可复核”；Codex 对每条 finding 记录修复或证据化处置。Codex 作最终研究决策，用户保留部署与真实交易授权。

## 7. 自动否决条件

出现以下任一项，直接标记 `rejected`，除非提出新的机制并建立新的实验版本：

- 使用未来 K 线、未来成交、最终样本外结果或人工挑选窗口生成信号或参数。
- 毛收益为正但扣除现实成本后非正，或正收益只来自未成交订单、浮盈或缺失成本。
- 只报告最赚钱的策略，未报告完整候选集合或未处理多重比较。
- 回测使用了现实中不可获得的盘口、延迟、成交率或流动性假设，且没有证据支持。
- 研究声明的杠杆上限或任一币种/名义档位的实际杠杆未实现对应强平模型，或风险上限与真实止损口径不一致。
- 结果无法由固定数据快照、规则哈希、执行哈希和事件账复现。
- DeepSeek 指出严重缺陷，Codex 无法用代码或新证据排除。

## 8. 交付与审查记录

每轮交付包含：

- 候选清单（包括被否决项）和本轮唯一实验编号。
- 数据快照指纹、规则/执行 hash、时间切分和参数表。
- 毛收益、净收益、成本分解、交易数、胜率、收益分布、最大回撤、尾部损失、成交率和容量。
- 样本外/前向原始事件文件及生成报告路径。
- runner 在任何候选级数据读取（含探索统计/绘图）或结果暴露前，追加 `snapshot` ledger 事件，记录 `access_type`、用途、区间、`family_id`、完整 `variant_ids`、`K`、`read_attempt_id`、`read_no` 和 burn 状态；研究族首次揭盲即烧录该区间的全部预注册变体。重复揭盲标 `invalid_run`，账本缺失或不完整的历史季度按已读处理。
- 预检和每次风险段转换追加不可变 `risk_gate` 事件，至少包含 `research_id`、风险段状态、预检状态、`boundary_hash`、`inputs_hash`、数据计划、审查者、证据哈希、UTC 时间及前一状态事件 ID；同一研究链的 `boundary_hash` 不得变化，时间戳不得倒签；首次 `open` 与 `open -> closed` 分开记账，关闭必须以预检 `pass` 为前提。`research_ledger.py` 提供字段与状态链校验；生产前向/纸面 CLI 和 strategy-lab 服务现在要求显式 `research_id` 并在读取候选输入前通过关闭闸门，缺失或未通过时 fail-closed。无研究身份的直接合成 API 仅用于实现回归，不能作为候选研究入口或盈利证据。
- Codex 的实现与取舍说明。
- DeepSeek 的独立审查：`PASS`、`FIX_REQUIRED` 或 `REJECT`，逐项对应第 6、7 节证据。
- 最终决策：`continue`、`rejected`、`candidate_pending_replication` 或 `user_review_required`。

本任务第一阶段只建立模板和审查流程；不因现有策略历史结果直接宣布任何策略盈利。

## 9. 审查记录

### 2026-10-20 DeepSeek 初审与 Codex 修订

- DeepSeek 初审结论：`FIX_REQUIRED`；复审指出模板已覆盖部分初审意见，最终必改项收敛为样本门槛、风险 cap、复现规则、治理权和最低净边际，并要求任务卡引用模板作为唯一口径。
- 第一轮修订的历史记录（已由下节本轮决策取代）：该版曾使用 `n_unique >= 400`、MDE `<= 6 bp` 和历史/前向/压力样本超 cap `0%`，并加入四季度、双币和独立 OOS 复现、前向门槛、净收益门槛、K 与风险字段。
- 本轮不扩币池；新的币池/周期研究需要独立预注册。首轮晋级研究限 15m，1h/1d 为机制观察。
- 治理口径遵循用户“Codex 负责写代码和决策、DeepSeek 负责审查”的授权：DeepSeek 提交独立意见，Codex 必须逐项回应；未解决的 P0/P1 事实或实现缺陷阻断晋级，部署和真实交易仍由用户授权。
- 当前状态：上述版本等待复审；其后 DeepSeek 第二次复审提出季度定义、跳空压力、样本量与揭盲账本问题，详见下节；未开始业务策略实现。

### 2026-10-20 DeepSeek 第二次复审与 Codex 修订

- Codex 复核日期前提：[`DATA_PIPELINE_DIAGNOSIS.md`](DATA_PIPELINE_DIAGNOSIS.md) 记录 15m 行情从 2024-01 起，且既有报告已分析多个历史区间；[`OPERATOR_DECISIONS_20261015.md`](OPERATOR_DECISIONS_20261015.md) 的 2026-01-01 是特定资金费分析口径，不是全库价格数据起点。因此不能仅凭行情长度断言所有候选都须等到 2027-01。历史季度只有在候选级访问账证明未读，且完整数据/执行字段通过审计后才计入 OOS。没有合格历史 holdout 时，纯前向路线从注册后首个完整季度 Q1 2027 起算，四季最早于 2028-01-01 后裁定。
- 统计门槛统一到 `delta_econ=max(6 bp, 1.5×全包成本)`；400 仅为下限，`n_required` 随 `K`、按唯一入场序列估计的时间设计效应和 `SD_unique` 变化。历史 `DE=4.3` 只作规划敏感性参考，须在去重后的唯一入场序列上重估，避免重复校正聚类。
- 风险门槛拆分为必须为 0% 的执行/控制类违规与可超过名义单笔 cap 的市场跳空损失；后者须报告超 cap 比例、P99、最差单笔，并通过冻结压力场景、账户级 P99 限值和无负权益/破产/穿仓要求。
- 状态名统一为 `candidate_pending_replication`；明确 bp 分母；要求 runner 对每次 OOS 揭盲先追加 snapshot ledger 事件，重复揭盲标 `invalid_run`；规格字段补充 `spec_hash`、`code_commit`，初始 `holdout_reads` 为 0。
- 当前版本待 DeepSeek 只读复审；尚未开始业务策略实现。

### 2026-10-20 DeepSeek 第三次复审与 Codex 修订

- 季度 OOS 资格收紧：必须排除 train/validation，且覆盖账证明无任何候选级读取（包括探索分析）；无完整访问账即按已读处理。
- 独立复现必须来自时间不相交或标的/市场集合不相交的数据；同一时期的重下载/修复快照只作为可复现性附注。OOS 预算按研究族共享，首揭盲烧录全部预注册变体并连同 `K` 记账。
- 风险压力门槛以初始账户权益比例冻结 P99 容忍值，明确破产价结算及保险基金/ADL 证据；增加强平价外 1/5 tick 不利滑点，赤字不得裁剪并须报告金额和发生率。
- 正式样本状态与否决使用 `n_required`；历史 [PREREGISTRATION.md](PREREGISTRATION.md) 的 `N_min=100` 仅适用于原版本已冻结账户，新候选统一以研究模板为准。

### 2026-10-20 DeepSeek 第四次复审与 Codex 补强

- 第四次复审结论 `PASS`，P1-1/P1-2/P2-1/P2-2/P2-3 均闭环。非阻断建议已纳入首个研究 ID 的前置规范：逐币方向性 15m 跳空 99 分位算法及组合冲击、共享区间的全局族访问矩阵与跨族校正、强制历史访问审计；无法证明历史区间未读时按 2027 前向路径估算。
- 首个 `research_id` 前执行纯风险数据可行性检查，只报告跳空/强平压力与权益影响，不看策略信号或策略收益；检查暴露的区间记为 exploratory/burned。若按账户适用分档杠杆构造的风险配置出现超过冻结门槛的未覆盖负权益/穿仓，应降低实际杠杆、缩小名义或终止该风险档，不得放宽压力标准。
- 截至该次旧检查，工作区未配置 `QUANT_DATABASE_URL`、没有项目 `.env`，本地未发现原始 15m OHLC；该状态随后由 2026-10-20 公开行情预检更新，见本卡第 185 行和风险预检报告。没有查看策略信号或收益。历史访问审计为 `not_proven`（仓库有既有 15m 研究报告，未发现可证明候选级访问完整性的事件账），因此旧历史季度不作为新候选 holdout；首个研究按纯前向 ETA 规划。

### 2026-10-20 DeepSeek 补强复核与 Codex 修订

- 组合压力时点按冻结仓位/名义暴露选择总绝对名义敞口最大的 UTC 时点，并列取最早；选择不得读取收益。主档在该时点同时冲击全部币种，另报逐币单独冲击与逐笔入场冲击。
- 每币、每方向至少需要 100 个有效非零不利 15m 跳空观测；不足时报 `N_gap` 和 `not_estimable`，不得通过风险门槛。
- `not_estimable` 可以在有数据来源、负责人和截止日期的数据获取计划时冻结协议，但 `risk_segment_status` 保持 `open`。只有账户适用签名档位快照、逐币逐档的实际杠杆矩阵、风险数据与用户风险边界齐备且预检通过后才可关闭；关闭前不得运行或查看候选结果。
- 首个 `research_id` 尚未建立；账户级风险预检仍为 `not_estimable`，历史访问审计为 `not_proven`。Codex 为首个研究选定合成基准：初始权益 10,000 USDT、isolated、单笔风险 cap 为权益 1%（100 USDT）、`N_max=50,000 USDT`（相对权益为 5x gross/equity 名义预算）、单币上限 25,000 USDT，币池 BTCUSDT/ETHUSDT/TRXUSDT。`N_max` 是独立的名义预算，不能与用户确认的 `max_leverage=100x` 上限直接比较；实际持仓杠杆必须逐币按账户适用名义档位解析，不能把 100x 当作三币统一仓位杠杆。这些合成值仅用于研究预检，不代表用户真实账户。用户确认的账户损失分布 P99 上限为初始权益 5%（此基准下 500 USDT）。公开行情及原始快照已归档；当前账户适用杠杆/维持保证金档、完整滑点和 1,000 ms 延迟压力尚未验证/完成，因此状态保持 `not_estimable`、风险段 `open`。不读取账户密钥，不进行真实交易。研究账本、前向/纸面 CLI、strategy-lab 服务和 Demo 交易入口均已接入关闭的 `risk_gate` 校验，但当前没有关闭 gate 的 `research_id`，所以不允许候选运行。
- DeepSeek 对模板 `672fe51e…cf6b`、任务卡 `ab588a27…0108` 的 D1/D2 复审结论为 `PASS`。该次唯一非阻断建议是 A/B/C 每档都叠加完整冻结压力集，已在当时版本补入；该历史 PASS 不覆盖本轮新增的 trade/mark 双路径和单窗口账户 P99 规则。

### 首个研究风险边界确认

- 实际杠杆与档位快照补充冻结：`max_leverage=100x` 只是首个研究的全局上限；`position_leverage_by_symbol` 必须由账户适用的 Binance `leverageBracket` 快照按 symbol、名义档位逐项解析。快照必须落盘 `tier_snapshot_hash`、来源、`fetched_at_utc` 和 `account_scope`；`leverageBracket` 返回的是该档位允许的最大杠杆与维持保证金参数，不是研究实际要采用的仓位杠杆；在任何候选运行前，必须冻结逐币逐档的 `chosen_position_leverage`，且满足 `chosen_position_leverage <= min(max_leverage, bracket_max_leverage)`。在账户适用的 signed USER_DATA 快照取得前，不得把仓库静态档位表当作可执行权威。`tier_snapshot_hash`、请求/响应哈希、`request_id`、`signed_at_utc` 和 `verified` 只能证明提交的证据载荷彼此一致或已被人工复核，不能单独证明 Binance HMAC 签名真实性或解析载荷来自该原始响应；必须另行核验原始响应字节、签名请求和解析绑定。当前代码中的静态诊断表显示 25,000 USDT 名义下 BTCUSDT 可到 125x、ETHUSDT 为 75x、TRXUSDT 为 25x；这些只是待核验的非账户绑定上限，不能据此宣称三币均可用 100x。
- 待核验杠杆矩阵（静态诊断值，仅用于发现冲突，不是冻结的执行配置）：在每币名义 25,000 USDT 时，BTCUSDT 档位上限 125x、受研究全局 cap 限制后可选实际杠杆最高 100x；ETHUSDT 档位上限 75x；TRXUSDT 档位上限 25x。若仅为诊断而假设每币均选用该矩阵允许的最高实际杠杆，逐仓初始保证金估算分别为 250、333.33、1,000 USDT；三项合计 1,583.33 USDT 是三个单币上限同时占满的值（总名义 75,000，超出 `N_max`），不能当作本研究组合保证金。在这一固定静态诊断最高杠杆矩阵下，并在 `N_max=50,000` 和每币 25,000 上限下，所有允许名义分配中的最大简单名义/杠杆保证金约为 1,333.33 USDT（TRXUSDT 25,000@25x + ETHUSDT 25,000@75x，初始权益 13.33%）；若冻结时选择更低实际杠杆，所需保证金会更高。该数字不是所有可选杠杆矩阵下的通用上界、A/B/C 账户风险包络、账户级完整保证金需求或预检通过证据；当前没有账户适用快照和冻结矩阵，不能据此给出 A/B/C 各档保证金诊断。实际 A/B/C 档必须在账户绑定快照后先冻结逐币实际杠杆矩阵，再逐档重算；快照或实际杠杆矩阵缺失时仍 `not_estimable`。
- 用户确认（2026-10-20）：首个研究的最高杠杆为 `100x`；同步历史窗口下的账户损失分布 P99 不得超过冻结初始账户权益的 `5%`，结果同时报告权益比例与 USDT。
- 该边界应用于完整冻结压力集下的 A/B/C 风险包络。最高杠杆是上限，不是仓位目标；`N_max`、单币上限和实际持仓必须受 P99 风险上限约束。5% hard gate 针对同步历史窗口账户损失分布 P99；样本最大跳空单独披露，不因最大值单独超过 5% 自动否决。任何压力场景出现负权益或未覆盖赤字，风险预检均不得通过。
- Codex 已按用户授权确定首个研究的合成风险基准：初始权益 `10,000 USDT`，isolated 保证金模式，单笔风险 cap `1%`（`100 USDT`），组合 `N_max=50,000 USDT`（`5x` 权益），单币上限 `25,000 USDT`（`2.5x` 权益），风险预检币池为 BTCUSDT/ETHUSDT/TRXUSDT。用户确认的 `100x` 是全局杠杆上限；不将组合名义按 100x 实际仓位杠杆设置，实际杠杆由账户适用的逐币名义档位决定。此为合成研究参数，不代表用户实际账户，也不构成真实交易授权。
- 账户级损失分布 P99 上限为初始权益 `5%`，在该合成基准下为 `500 USDT`。六个行情序列的边际跳空 P99 只可作初筛；精确强平/维持保证金、延迟与滑点模型齐备后，才能计算并裁定完整 A/B/C 风险组合。
- 校准说明修订：撤回“按 `position_leverage=100x` 部署全部 `N_max`、总初始保证金恰为权益 `5%`”的旧推导。仅在“采用当前静态诊断矩阵中各档允许的最高实际杠杆”这一固定假设下，符合 `N_max` 与单币上限的所有允许名义分配里，简单名义/杠杆保证金的最大值约为 1,333.33 USDT（13.33% 权益），其集中配置为 TRXUSDT 25,000@25x + ETHUSDT 25,000@75x；若冻结更低的 `chosen_position_leverage`，保证金会更高。该 1,333.33 仅是无账户范围静态诊断矩阵下的全组合算术值，不是账户级保证金上限、任何 A/B/C 档的完整保证金诊断或预检通过证据。实际 A/B/C 初始保证金必须在账户适用 `tier_snapshot_hash`、账户范围、逐币逐档冻结的 `chosen_position_leverage` 矩阵及强平/赤字模型齐备后逐项重算；在此之前不能给出可执行的组合保证金上限，也不能把 `5%` P99 门槛解释为保证金封顶。用户确认的 `5%` 仍是账户损失分布 P99 硬门槛；风险状态保持 `not_estimable`，任一压力场景出现负权益或未覆盖赤字均独立判失败。
- 数据预检计划：行情原始文件、请求/响应哈希和分页清单已保存。Codex 负责在首个 `research_id` 建立前核验 Binance 官方 USDⓈ-M 合约规则、账户适用的 `leverageBracket` 名义档（需记录 `tier_snapshot_hash`、来源、`fetched_at_utc`、`account_scope`）和维持保证金档，并为 1,000 ms 延迟、强平成交及 1/5 tick 压力取得足够的逐笔/盘口数据或独立支持的保守成交上界；数据源为 Binance 官方市场数据、账户适用的 signed USER_DATA 档位快照与合约说明，负责人 Codex，目标截止 2026-10-27。若截止时仍无法完成，继续保持 `risk_segment_status=open` / `not_estimable` 并更新计划，不建立 `research_id`、不读取候选结果。
- 2026-10-20 纯行情风险预检已完成并归档于 [`verified/risk_preflight_20261020/REPORT.md`](verified/risk_preflight_20261020/REPORT.md)：BTC/ETH/TRX 交易价与标记价各 98,208 根 15m K 线，2024-01-01 至 2026-10-20 UTC，六个序列均无无效行、重复、缺失或非连续相邻对，每币每方向 `N_gap >= 100`。交易价和标记价 P99、最大跳空、官方请求清单和哈希及原始快照均已归档。首个研究合成参数已确定；完整账户级风险仍为 `not_estimable`，因为精确维持保证金档、强平成交和同步双价格路径下完整滑点/1,000 ms 压力尚未执行，风险段保持 `open`。

### 2026-10-23 执行引擎与预检 schema 收尾

- 执行引擎已完成逐币逐档杠杆接入：普通账户和配对账户都从 `leverage_by_symbol` 计算名义、保证金、数量和档位校验；`binance_tiered` 只接受冻结 `tier_snapshot`，不再回退静态诊断表；档位不可用时在现金、手续费和持仓写入前原子拒单。Fnew 的 `BTCUSDT`、`BTC/USDT`、`BTC/USDT:USDT` 键会归一化为 canonical symbol，重复别名 fail-closed；档位名义 cap 缺失、`None`、Infinity 或仅旧别名输入均 fail-closed。
- 预检模板的 `risk_feasibility_preflight` 已与 `PreflightReport` 字段统一，并补充完整 `ScenarioEvidence` 单条 JSON 示例、22 个固定场景清单、压力参数、canonical hash、Decimal/tuple 恢复规则和 artifact 约束。DeepSeek 2026-10-23 最终只读复审结论为 `PASS`，无代码阻断项；唯一可选 P3 是报告摘要可继续附带完整 P99/max/sample/envelope 快照，不影响当前门禁。
- 本轮验证：全仓库 `PYTHONPATH=. .venv/bin/pytest -q tests` 为 **1,903 passed, 64 skipped, 2,997 subtests passed**；本轮受影响执行、导入、预检、Fnew 与模拟器测试为 **253 passed, 90 subtests passed**；`py_compile` 与 `git diff --check` 通过。上述验证只覆盖实现和纯风险数据边界，未运行或查看候选策略信号、收益、回测、OOS 或前向结果。
- 当前仍缺账户适用的 signed USER_DATA `/fapi/v1/leverageBracket` 原始响应字节、签名请求及 HMAC 核验结果、原始响应到解析档位的绑定证据、冻结的逐币 `chosen_position_leverage` 矩阵，以及与同一执行模型绑定的 8 项假设证据和 22 个 `ScenarioEvidence` 损失/赤字序列。仓库现有风险快照只有公开 trade/mark 行情，不含账户档位或账户成员响应。故 `risk_feasibility_preflight=not_estimable`、`risk_segment_status=open`，不建立 `research_id`，不启动候选策略研究。

### 2026-10-23 离线档位导入与跨层契约复审

- 已新增严格离线 `bracket_response` parser 与 `bracket_import` 导入器，详见[操作契约](../docs/BRACKET_ARTIFACT_IMPORT.md)。精确原始响应 SHA、脱敏元数据、解析档位与完整 pending manifest 可离线重解析核对；导入器不读凭据、不联网、不验证签名或账户归属。产物固定为 `pending_manual_verification`，每条 schedule 为 `verified=false` / `snapshot_kind=candidate_import`，且没有执行 `snapshot_hash`；执行 loader 拒绝直接使用。
- `symbols.py` 为预检与执行快照提供统一 canonical symbol/hash 契约，compact/CCXT 别名不能改变档位集 hash，重复别名拒绝。Decimal canonical 序列化不依赖调用方精度 context；跨层合成测试覆盖 typed schedule、mapping loader、preflight 与高精度 hash。
- DeepSeek 独立只读复审 `quant-bracket-import-review-reply-20261023-01` 结论为 `PASS`，无阻断缺陷。Codex 复跑受影响 8 文件 **253 passed / 90 subtests**；全仓库 **1,903 passed / 64 skipped / 2,997 subtests**；`py_compile` 与 `git diff --check` 通过。未运行或查看候选策略信号、收益、回测、OOS 或前向结果。
- 待真实账户原始响应到位后，先在私有证据目录验证 `notionalCoef` / `cum` 的 JSON 类型及连续性。当前严格契约拒绝字符串数值、非单位系数和非精确连续性；若真实 fixture 不匹配，须先修订 parser 并交独立审查，不临时放宽。哈希/重解析通过仅证明内容绑定，不替代签名请求、响应来源和账户适用性核验。
- 下一步顺序：取得原始响应与脱敏元数据 → 离线导入并独立核验来源/签名/账户范围/端点归属 → 冻结账户适用档位与实际杠杆矩阵 → 补齐 8 项执行假设和 22 个压力场景 → 账户预检及独立复审。真实材料未到位前继续 `not_estimable/open`；首个 `research_id` 仍未建立，候选研究未开始。数据计划的负责人 Codex、目标截止 2026-10-27 保持不变；不提交、清理、重置或推送现有工作树。

### 2026-10-23 Decimal context 门禁加固与用户输入

- DeepSeek 确认 loader/preflight 的维持保证金负值、档位连续性和初始保证金缓冲会受 Decimal context 舍入影响。现 parser、loader 与 preflight 共用 `tier_math.exact_margin_offset()` 的 `Fraction` 算术，缓冲以精确有理数比较；另修复单币额度合计与 `N_max` 比较中的同类舍入问题。
- 新增合成测试在 `prec=28/80` 下覆盖 `1e-30` 连续性偏差、精确连续高精度档位、微小负维持保证金、缓冲正负及等值边界，以及额度不足/恰好足额。Codex 验证受影响 8 文件 **273 passed / 90 subtests**、全仓库 **1,923 passed / 64 skipped / 2,997 subtests**，`py_compile` 与 `git diff --check` 通过；本轮精度修复等待 DeepSeek 增量只读复审，既有审查 `PASS` 不自动覆盖新版本。
- 用户于 2026-10-23 回复账户原始响应与脱敏元数据“尚未采集”。当前无真实账户 fixture 可供导入，不凭公共档位或合成数据替代，不读取凭据或自动发起账户请求。数据计划目标截止仍为 2026-10-27；账户材料与执行假设缺口未闭合前继续 `not_estimable/open`，不建立 `research_id`，不运行或查看候选策略结果。
- DeepSeek 增量只读复审 `quant-bracket-context-review-reply-20261023-01` 核对五文件 SHA 并判 `PASS`；envelope 报表末位精度和 typed loader 的 Decimal complexity 上限两项 P3 保留为非阻断限制。
- 后续独立检查发现账户 P99 限值 `equity × pct` 的 Decimal 乘积存在同类边界漂移。场景与顶层 P99 现共用 `_account_p99_loss_limit()` 的精确 `Fraction`，保持严格 `>` 与等值通过语义；新增 `prec=28/80 × 低于/等于/高于 1e-30` 合成回归，覆盖完整 22 场景及顶层原因，旧 5% 等值 fixture 改用精确 `500` 字符串和有理数核验。本次受影响 8 文件 **279 passed / 90 subtests**、全仓库 **1,929 passed / 64 skipped / 2,997 subtests**，`py_compile` 与 `git diff --check` 通过。DeepSeek 增量只读复审 `quant-p99-limit-review-reply-20261023-01` 核对 SHA、复跑 279/90 并判 `PASS`；其唯一新 P3 是私有 helper 可增加直接输入防御，未发现门禁缺陷。

## 2026-10-24 权威预检导出路径

- 为避免账户材料到位后依赖手工调用 Python 对象，新增 `preflight_report_to_mapping()` 与 `tools/export_account_risk_preflight.py`。导出器接受受控 JSON bundle，使用权威 `evaluate_preflight()`，拒绝未知字段、重复 key、浮点金额、超限输入和已有输出目录；报告和清单写入 `700/600` 私有目录，标准输出只含状态和 hash。
- 当前无账户档位、P99 gap 或执行证据时，导出器仍只能返回 `risk_feasibility_preflight=not_estimable`；它不读凭据、不联网、不创建 `research_id`，也不能把合成输入变成账户通过证据。
- 新增导出器回归覆盖缺失证据、Decimal 字符串、路径/敏感值不回显和输出目录拒绝；本轮相关测试与既有预检/导入测试共 `100 passed`。
- 新增 [`ACCOUNT_RISK_PREFLIGHT_BUNDLE.md`](../docs/ACCOUNT_RISK_PREFLIGHT_BUNDLE.md) 作为非证据输入模板，明确十进制字符串、账户档位、8 项执行假设、22 个场景和私有目录边界；空模板只能得到 `not_estimable`。

## 2026-10-24 账户材料仍未提供

用户确认目前仍没有可提供的账户 `leverageBracket` 原始 UTF-8 响应文件和脱敏元数据 JSON。仓库审计未发现可替代的真实账户 fixture；本地账本也没有账户风险事件或 Demo credential 记录。继续保持 `risk_feasibility_preflight=not_estimable`、`risk_segment_status=open`，不读取凭据、不调用 Binance USER_DATA 端点、不建立 `research_id`，也不运行或查看候选策略回测、OOS 或前向收益。现阶段只执行离线 parser、风险门禁和执行契约回归；取得两份私有材料后再按既定顺序进行导入、独立核验和账户级预检。
