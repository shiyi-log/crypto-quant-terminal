# Fnew 五个前向研究假设的执行定义

本文件定义 s32..s36 的独立前向人口：五个机制 × R50/R200/R1000 × k1.0/1.5/2.5，共45个模拟账户。它们是待验证假设，外部作者的 APY、盈利或实盘自述不构成本项目的验证。原29项机制、261个ATR账户及其冻结身份均不改动。

候选输入来自 `research_multi_strategy/strategy_specs.json` 的 `strategy_proposals_by_dsh.candidates`。该区是提案；`strategy_fnew.py` 将执行需要的数学细节补全到各账户的 `params`、`params_frozen` 和经济哈希中。旧 `strategies` 区不参与 Fnew 工厂。未来提案增删不会改变已保存在数据库的冻结定义。

## 身份、预算与成本

账户格式为 `<canonical>_Fnew_R50_k1.5_100x_maker`。s33使用 `_100x_taker`，明确费用为0.0005/边；其它四项为maker 0.0002/边。所有ID长度≤80。人口标识是 `fnew_design_version` / `sample_population = fnew-20261012-v1`，`arm_id = Fnew_20261012`，因子在 `fnew_factors`；没有 `factorial_design_version`，不能把新45项计入原261人口。

每账户初始资金10,000 USDT、100倍杠杆、权益30%的保证金预算、最多两个持仓。R为计划止损距离及已建模往返费用的风险预算；剩余保证金、槽位和现金可进一步限制仓位。收盘确认、下一根open代理成交、跳空、强平和未建模成本可能使实际亏损超过R。

| 类型 | R的范围 | 名义反解 |
| --- | --- | --- |
| s33/s34/s35 单腿 | 每笔交易腿 | `N = R / (k*entry_ATR/actual_entry_price + 2*fee_per_side)` |
| s32/s36 原子双腿 | 整个配对组合 | 每腿同名义 `N = R / (k*entry_pair_ATR_return + 4*fee_per_side)` |

双腿一入一出共四次成交。以每腿名义N计，maker组合往返费用为0.08%×N；以总开仓名义2N计为0.04%。必须说明分母，不能将单腿往返与双腿费用混为一谈。

费用与成交方式仍有研究限制：maker账户使用下一根open代理且假设成交，未模拟限价排队或未成交概率。资金费与滑点也未建模。以maker代理产生的小额正收益不直接说明真实可交易；需要成本、滑点和未成交敏感性核对。

默认工厂输出27个单腿启用定义、18个配对阻断定义，理由为 `paired_atr_stop_policy_unimplemented`。只有执行层完成并通过独立验收，首次建账才可显式调用 `paired_execution_enabled=True`。该选择、组合止损与强平约定进入rule/execution身份。已经创建的阻断账户不可在原身份内直接开启；更改冻结经济规则必须使用新身份，保留原数据。

配对执行版本为 `atomic-pair-equal-notional-ohlc-v1`：只交易选中的两腿；ATR方法 `synchronous_ohlc_envelope_true_range_actual_entry_v1` 按实际入场价格归一化两腿联合OHLC极值包络，冻结为 `entry_pair_atr_return`。该包络是保守代理，不能视为已观测两腿盘中共同路径。实际入场起算的两腿有符号收益之和≤`-k*entry_pair_atr_return`时，同一次收盘决策退出整个组合，下一open两腿一并代理成交。单腿强平触发整组结算；盘中另一腿按不利OHLC极值结算的规则为 `conservative_peer_bar_extreme_after_leg_liquidation`，属于保守代理，不能声称知道两腿同一时刻的真实成交价。一次原子组合平仓只算一个样本，不能把两腿当两份独立样本。费用4f是入场名义基准预算，实际退出名义随价格变化，也不能据此承诺实际净亏严格≤R。

## 五项精确定义

| 机制 | 周期 / 币池 | 已落实的参数与约定 |
| --- | --- | --- |
| s32 协整价差回归 | 1h / BTC、ETH、TRX三对中选一对 | 前500根log价格做带截距OLS及真实Engle–Granger `statsmodels.coint`；`adf_maxlag=5`、`adf_autolag=AIC`、p≤0.05；前500根残差mean/sample stdev构成z基准，当前价格仅触发；`|z|≥2`反向、`|z|≤0.5`退出、H=72。只选最大|z|合格配对，等值时按pair词典序。要求beta>0；退出沿用入场冻结的回归系数与残差标准化。 |
| s33 基差变化率回归 | 1h / BTC、ETH、TRX | `(perp_close-spot_close)/spot_close`；delta lag=1；当前delta之前100个差分的sample stdev为sigma。delta≥2sigma做空永续，delta≤-2sigma做多永续；short delta≤+0.5sigma或long delta≥-0.5sigma时退出，H=24。只交易永续一腿，现货仅供信号。 |
| s34 当日VWAP锚定回归 | 15m / BTC、ETH、TRX | UTC当日锚，优先使用原始quote_volume/base_volume；缺quote时用HLC3×base_volume代理。离VWAP≥2ATR反向、回归至0.25ATR范围内退出，H=32。入场冻结VWAP日期；UTC日末强制退出，不跨日悄悄替换锚。缺当日完整前缀或入场锚日期时fail-closed。 |
| s35 疑似清算瀑布顺势 | 15m / BTC、ETH | 同向实体连续3根、累计位移≥2ATR，顺势入场，H=8。`same_body_run_atr_move_without_exhaustion_v1` 为K线事件代理，没有独立清算数据，不能称已观测清算。 |
| s36 相关性regime切换 | 1h / BTC、ETH | 收益相关窗口168，比较滞后168根的不重叠窗口；先前rho≥0.7且下降≥0.15时，多当前168根收益较强腿、空较弱腿。rho相对入场回升0.05退出，H=72；相同强弱不入场。 |

源提案没有定义的H、基差delta lag、sigma退出、VWAP价格来源及日末、协整检验细节、配对竞争选择、rho比较窗口与强弱窗口，都属于本次透明设计选择。`implementation_parameter_defaults` 保存逐项补入来源；完整参数进入经济哈希。ATR窗口为14，k按九个因子组合单独冻结。

s32原热身520根足够当前触发所需501根；s33保留120、s34/s35保留60。s36原180不够两个不重叠相关窗口，数学最低为337根，normalizer已提高到337。不能把预热不足或数据陈旧计为真实零触发。

等名义的两腿不是beta对冲，即使beta>0也可能有残留市场敞口；s32只是明确的候选策略，不能因协整p值过线就称风险中性。s36的rho下降不能保证等名义价差扩张，相关性与方向收益是不同量。

## 数据契约与前向边界

三币是BTC/USDT:USDT、ETH/USDT:USDT、TRX/USDT:USDT；s35/s36仍只用BTC/ETH。所有研究、数据及执行币池进入哈希，不能暗中加入暂停币线。

普通 `candles` 映射保存futures行。s33还需显式 `candles['spot']`，其键是BTC/USDT、ETH/USDT、TRX/USDT，使用真实spot市场行。定义的 `data_feeds` 独立声明两个市场与perp→spot映射。futures行不能替代spot行，也不能把同时间戳的不同市场混到一张输入表。

所有行需已收盘、合法OHLCV、UTC开盘时间戳；市场之间只按精确时间戳inner join，不前向填充。spot文件历史多不代表spot实时输入新鲜：缺当前闭合1h、历史连续段不足或spot陈旧时，s33明确fail-closed。历史回补只恢复指标，不补造前向订单或历史成交。

定义接口：

```python
from strategy_fnew import load_fnew_specs, normalize_fnew_spec, fnew_rule_hash
from strategy_fnew_signals import evaluate_fnew

specs = load_fnew_specs()  # 45 definitions; paired entries blocked by default
spec = next(s for s in specs if
            s['strategy_id'] == 's33_basis_momentum_1h_Fnew_R50_k1.5_100x_taker')
# candles: futures pair -> closed rows, plus 'spot' -> spot pair -> closed rows
# as_of_utc: true current observation for forward service;
# historical indicator inspection may use the final candle close timestamp.
result = evaluate_fnew(spec, candles, positions={}, as_of_utc=as_of_utc)
assert normalize_fnew_spec(spec)['rule_hash'] == fnew_rule_hash(spec)
```

`normalize_fnew_spec` 不依赖旧策略DEFAULTS，单独绑定参数、市场、预算、费用、信号/成交时序及执行阻断。任何已有rule_hash不符都拒绝处理；文案、外部收益自述和证据路径不改变经济规则。

## 裁定纪律

s35与s21方向相反不代表至少一个必定盈利：费用可以让二者同时亏损，触发和退出规则也并非完全镜像。需要分别验证完整条件规则和成本。

“12×成本”可登记为研究筛选或敏感性指标；仅低于该比例不构成统计或机制证伪，不能自动归档。盈利期望取决于净收益分布、胜负幅度、频率和成本，外部kill list或相似族名也不能替代本规则的证据。归档遵循双方共同确认、证据不足默认KEEP。

族内策略可能同时在同币、同一根K线上触发；不能将同族策略数直接乘进独立样本量。若研究机制族合并估计，应先登记合并目标、事件聚类、相依误差与各规则权重，避免把同一市场事件重复当作独立观察。新增五个方向扩大检验覆盖，不自动缩短每个机制达到裁定样本的时间。
