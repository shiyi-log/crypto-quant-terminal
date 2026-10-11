# 订单簿低频确认前向实验（2026-10-12）

本实验是尚未验证的研究假设。它检验订单簿确认是否能改善小时级 K 线策略的入场，既有 29 项规则与 261 个 ATR 因子账户保持各自冻结定义。论文、外部策略业绩、历史预览或订单簿快照数量均不构成本实验的盈利证明。

## 独立人口与共同基线

新增 18 个独立模拟账户：R50/R200/R1000 × k1.0/k1.5/k2.5 × 无过滤对照/有过滤。身份格式为 `s37_vol_compression_book_{control|filter}_R50_k1.5_100x_maker`，实际 R、k 按档位替换。实验人口标记是 `orderbook-confirmation-forward-v1`，它不是既有 261 因子人口，也不是旧无止损对照人口。

两组均使用原 canonical `s14_vol_compression_release` 的 1h 压缩区突破机制：ATR14、压缩回看 100 根、压缩分位 0.2、持有期 72 根。BTC/ETH/TRX 为相同数据与交易币池。每个 R/k cell 的 canonical signal hash 相同；账户身份、组别及门控合同使 outward rule hash 各不相同。工厂定义直接冻结在 `strategy_microstructure.py`，不从持续编辑的候选提案数组派生，也不修改现有策略数组。

两个账户各有 10,000 USDT 初始模拟资金、100× 杠杆、30% 保证金暴露预算、2 个持仓槽位和每币至多 1 仓；maker 费用每边 0.02%。入场 ATR 冻结；计划名义为 `R / (k × ATR / 实际入场价 + 2 × 每边费率)`，受资金、保证金和槽位约束。它是计划止损及费用预算：收盘确认、下一根开盘代理成交、跳空和强平可能使净亏超过 R。两个组的费用、ATR 风险、成交代理、槽位和资金规则完全相同。

## 唯一实验差异

无过滤组保留 canonical target、reason 和全部风险证据。有过滤组只在新入场时要求：

- 小时内五档 OBI 的时间加权均值与拟入场方向同号，方向乘 OBI 至少 0.1。
- 相对买卖价差的时间加权均值至多 0.0002（2 bp）。
- 相同信号 bar 的前向订单簿记录已公开可读，且通过覆盖质量与时间检查。

阈值是本次设计选择，没有用这 2.8 天订单簿历史择优拟合。OBI 的分母是对应档位买卖数量之和，不是成交流；但构造不同本身不保证有收益。`lagged_obi_mid_directional_consistency` 只作诊断和审计，不参与开仓阈值、训练或参数选择。

已有持仓的 ATR 止损、持有期退出与逆向突破退出完全沿用共同基线，不受订单簿缺失、质量、方向或价差阻挡。本实验不直接做快照级高频交易，也不把 K 线成本结论外推为所有微观结构策略必败。该过滤器和无过滤基线可以同时亏损。

## 可观察时点与质量

信号 bar 以 UTC 小时 open 为标记，输入截止为该小时 close。`orderbook-hourly-v1` 只接受 Binance futures 的对应币对、对应小时；记录同时包含 exchange event 时间与本地接收时点，未来收到的历史快照不能补入当时决策。特征按快照持有时间积分，每份快照至多延长 3 秒，避免把陈旧报价填满采集中断。特征发布必须发生在 close 后 90 秒内，且不晚于账户的实际观察时间。

过滤账户独立复核 `quality.eligible=true`、`status=ready`、`forward_eligible=true`，以及：时长覆盖至少 95%，至少 55 个分钟桶，最大缺口至多 30 秒，末快照距 close 至多 3 秒，至少 100 份有效快照。100 份快照不是 100 个独立研究样本。特征 key、method、source hash、bar、cutoff、received cutoff、观察时点和发布时点进入审计。新服务启动前的小时预览不能作为前向账户输入；已接受记录不可覆盖。

无仓的两组账户共同等待相同 publication 宽限：close 后 90 秒内等不到发布，或刚发布的记录晚于本轮观察时钟，结果为 `ready=false` 和 `feature_waiting=true`，服务应使用下一轮真实时钟重试该 bar，不接受未来特征。宽限结束后，无过滤对照可按共同基线交易、有过滤组拒绝新入场，两者均把缺失部分标为不可归因。有仓账户不会因等待订单簿而阻断退出。不得事后给已决策的对照事件补上特征，以免产生未来信息。

## 可核查证据与归因

两组的每个 pair decision 都有 `evidence.orderbook`，包括 feature key/method/source hash、特征数值、quality、前向标记、门控结果、原始基线 target/reason，以及 `paired_subsample_eligible`。此标记只说明对应 bar 有合法的共同特征输入；它不说明两个独立账户在该 bar 的仓位、现金与历史路径相同。

归因只使用两组都存在相同 pair、相同小时、相同 R/k、相同 feature key、且覆盖合格的子样本。过滤拒绝的合法输入也必须保留，不能只挑已通过的入场。分析应报告共同可用 bar 数、缺失/质量拒绝数、门控通过率、两组入场数、仓位/资金路径差异以及净收益和成本敏感性。不要把全部无过滤成交和较窄的过滤成交直接混合比较；缺失输入、独立仓位造成的路径差异和稀疏信号都可能混淆效果。

小时低频信号的触发率及有效成交样本必须实际记录。快照频率不能替代经过时间积累的独立交易样本。未达到预注册样本门槛只表明证据不足；方向的否证、归档仍遵守共同确认程序。

## 调用与验收

`build_microstructure_specs()` / `load_microstructure_specs()` 返回 18 个经正常化、带冻结 hash 的定义。service 用 `load_published_feature(store, pair, candle_ms)` 读取每个信号小时的不可变记录，然后传给：

```python
evaluation = evaluate_microstructure(
    spec,
    closed_hourly_candles_by_pair,
    positions=actual_account_positions,
    as_of_utc=actual_observation_utc_ms,
    book_features=published_features_by_pair,
)
```

输入历史只热身指标，不回放成交；账户从服务持久化前向边界开始积累样本。特征计算落盘、两个组的决策证据存在、费用/风险/时点一致以及自然新小时上的执行才是上线验收证据。本模块本身不创建生产账户、不写数据库、不声称已有真实限价成交。

专项测试覆盖 18 个冻结身份、两组相同 canonical hash、所有 R/k 与多空方向、阈值边界、缺失等待/拒绝、未来/错 bar/过期/低质量输入、真实特征计算输出契约、不可抑制的冻结 ATR 与持有期退出、仅诊断字段和安全事件序列化。
