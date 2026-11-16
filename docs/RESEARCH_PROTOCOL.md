# P2 冻结研究协议与离线门槛

本小节对应全栈重构计划第 5 节与 P2，实现为 `research_protocol.py`。模块只做纯计算，不运行回测、训练、网络采集或交易，不修改既有历史研究结论。输出采用 frozen/slots 数据类，保存规范 JSON 与 SHA-256，`to_dict()` 每次返回独立副本。

`acceptance_eligible` 始终为 false；候选通过研究门槛也只表示可提交 Demo 预检，不是上线批准。模块不能证明数据来源真实、没有隐瞒尝试历史，也不能代替数据库中的留出消费锁。

## 冻结实验族

`freeze_experiment_family(spec)` 严格要求完整字段；调用方在研究运行前归档该结果。支持 BTC/ETH/TRX 的 USDⓈ-M 交易对，周期为 1m/5m/15m/1h。

| 字段 | 合同 |
| --- | --- |
| family_id、registered_ms | 安全身份、研究预注册时间；不是数据采集时间 |
| dataset | start_ms、end_ms、source_ref、sha256；区间至少 365 天且按所有周期对齐 |
| symbols、timeframes | 明确的非空、无重复轴；没有把三币研究套进至少五币的横截面 IC 判据 |
| factor_sets、exit_sets | 每项为 id、sha256；同 id 不允许不同 hash |
| cost_scenarios | 必须恰有 base、stress |
| seeds | 明确 uint32，种子也计入配置数 |
| identities | code_hash、data_hash、cost_hash、risk_hash、strategy_hash；data_hash 与 dataset.sha256 一致 |
| label_horizons | 每个周期明确最大未来根数 |
| label_span_ms、purge_ms、embargo_ms | purge/embargo 都不得小于跨度；跨度至少覆盖 (horizon+1)×周期，包含最终根收盘可用时间 |
| comparison | time_block_bootstrap 或 hac；block_ms 至少覆盖标签跨度；holm 或 bonferroni；alpha 在 (0,0.05] |
| execution_policy | 明确最小成交率、最大部分成交率、p99 延迟、逆向选择、未成交机会成本 |

所有轴先规范排序，因此相同语义的轴重排不产生新证据。笛卡尔积包括币种、周期、因子组合、退出版本、成本场景、种子，完整展开不得超过 120；超限直接拒绝，不随机抽样或截断。每个配置 hash 冻结其全部参数。

时间均为 UTC 毫秒，区间统一为 `[start_ms,end_ms)`。最后固定 90 天只作最终留出，位置不随候选或表现移动。此前使用名义 120 天训练、30 天验证、30 天步长。每折训练尾部减去 purge，验证开始增加 embargo，验证末尾再减 purge，避免训练标签或验证标签跨越其边界。365 天数据得到 5 折；余下不足一折的日数不拼凑进留出。

留出对象保留原始 90 天边界、加 embargo 后的可评估起点和扣除标签跨度的最晚锚点排他边界。执行器必须按这些边界选取样本。本模块不执行指标或拟合，不证明信号实现本身没有未来泄漏。

## 数据与标签

`assess_candle_coverage(bars, *, symbol, timeframe, start_ms, end_ms, as_of_ms, source_ref, trusted_source)` 逐根检查真实网格、重复、内部缺根、闭盘、可用时间、OHLC 和非负成交量。单根字段为 timestamp_ms、available_ms、closed、open、high、low、close、volume；数值用十进制字符串或 Decimal，拒绝 float/bool/NaN/Infinity。

真实零成交量有效，缺根返回 `not_estimable`，不补零、不前向填充。完整 365 天窗口每根均需闭盘并且在 as_of_ms 前可用。数据指纹由规范化逐根事实计算；来源可信度仍由归档采集链保证，trusted_source 不能直接由未经验证的 HTTP 输入赋真。大数据集仍需调用方分块读取和持久化清单；此纯函数不会创建数据仓库。

`mature_close_label(bars, *, anchor_ms, timeframe, horizon_bars, as_of_ms)` 使用连续锚点至第 N 根收盘的 close-to-close 收益。第 N 根未收盘为 pending；到期后缺失、重复、不闭盘或不可用为 not_estimable。仅使用该窗口内的有效时间戳，正常未来根的数值变化不会改变历史标签及其 hash。时间戳损坏而无法定位所属窗口的行会保守阻断。该函数用于离线结果标签，不向信号输入暴露未来价格。

`assess_book_history(evidence, *, required_start_ms, required_end_ms)` 要求 source_ref、sha256、start_ms、end_ms、sequence_contiguous=true、snapshots_complete=true。未知、缺口或缺少序列证据为 not_estimable。pass 只说明调用方归档声明覆盖的可见深度，不说明知道全市场挂单数量、撤单或队列位置。

## 成本与候选裁定

`stress_costs(cost)` 接受明确且非负的 fees、adverse_slippage、funding_expense、funding_income、other_expense。前三项翻倍，资金费收入及其他支出保持原值，返回完整分项及 net_cost。内部使用独立高精度 Decimal 上下文，不依赖全局 28 位精度；不对真实 Demo 净收益再次扣除滑点。

`evaluate_candidate(facts, frozen_family)` 要求同一冻结族的 base 配置与除成本场景外完全一致的 stress 配置。固定最终留出窗口、全部身份 hash、完整数据/成本、完整尝试史声明、一次性留出声明、风险及原生保护核验均必须具备。

基准至少 100 笔完整往返，成本后净收益严格大于零；利润因子按精确乘法比较 `gross_wins >= 1.2 × gross_losses`，零损失正收益有效，不除零。这里 gross_wins/gross_losses 指以完整净往返收益分组后的正收益和绝对负收益，需包含已经分配的手续费及资金费；其差必须等于 net_profit。未分配资金费/残余仓位不能被忽略，应由上游完整对账声明阻断。压力净收益可以为零、不得为负。

执行指标必须完整且满足预注册 execution_policy；统计产物的方法、块长度、全族配置数、校正方法必须匹配，校正 p 值不得超过冻结 alpha，成本后净收益下界严格为正。该层验证归档统计声明，不实现 bootstrap/HAC、成交模拟或多重比较算法；上游必须附可离线复算产物，不能把填写统计字段当成统计证据。

所有拒绝原因会完整收集。无原因时 status=research_pass、demo_precheck_candidate=true，但 production_authorized=false、acceptance_eligible=false、holdout_claim_committed=false。

## 尝试历史与留出锁

`audit_attempt_history(frozen_family, attempts)` 检查提供的连续 sequence，从 1 开始；attempt_id 唯一，config_id 属于冻结族，family_sha256 匹配，时间不得早于预注册或数据集最终闭盘。状态为 failed/completed/canceled/not_estimable，phase 为 validation/final_holdout，每个尝试保留 artifact_ref。失败也保留，验证重试不冒充新配置。

最终留出采用一次完整证据包：以一个 base 配置为入口，在同一运行包中产出其 base 与匹配 stress 结果。最终留出失败也已消费，不允许再次提交 final_holdout 尝试。不同候选不能重复看同一个最终留出再调参。

纯审计只能发现提供的列表内部重复，不能证明调用者没有删掉列表或隐藏外部尝试。实际运行前仍必须在权威 PostgreSQL 不可变事件和事务中原子认领该 family 的最终留出；本模块没有写锁、数据库表或认领 API，holdout_claim_committed 始终为 false。

## 日志、验证和后续接线

INFO 中文日志包含族/数据 hash、配置数、折数、固定窗口、数据行数、状态和完整原因码；不打印完整源数据或凭据。应由研究 worker 配置日志落盘和追加式事件身份，本模块不配置全局 logger。

定向测试覆盖时间边界、365 天闭盘覆盖、零量与缺根、未来扰动、120 配置完整计数、精确费用压力、候选拒绝、伪造冻结结构、同场景身份、全族比较、执行门槛和留出重复消费。全库回归、CI compile/Ruff 接线和逐节提交由主任务集中完成。

仍待 P2 后续：数据/因子/实验族的权威登记表与 API、留出事务消费、真实覆盖与来源档案、完整尝试追加事件、bootstrap/HAC 与全族校正执行、成交模型/成本模型、离线复现命令及冻结候选审批。当前没有创建或运行新研究任务，没有宣称找到合格策略。
