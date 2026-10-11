# 成交路径固定的成本敏感性审计

2026-10-12。新增只读工具 `tools/strategy_cost_sensitivity.py`，对完整的 `trade_closed` 台账比较每边 maker 0.0002、taker 0.0005、stress 0.001 三个研究情景。它不改账户、定义、订单、成交或裁定；这三个数值尚不代表本交易账户经核验的费率。新261账户仍按已冻结费率运行，本轮没有另建费率身份。

```bash
uv run python tools/strategy_cost_sensitivity.py \
  --output artifacts/closed-candle-cost-sensitivity-20261012/report.json

# 只看新因子人口，或使用完整的 account_detail/快照 JSON 离线审计。
uv run python tools/strategy_cost_sensitivity.py --population factorial
uv run python tools/strategy_cost_sensitivity.py --input /absolute/path/complete-detail.json
```

数据库连接直接开启单个 `REPEATABLE READ READ ONLY` 事务，不调用会执行迁移的 `DataStore.initialize()`。读取全部闭合事件，无 API 的1000条截断；逐账户与 `state.closed_trade_count` 对账。截断、缺字段、重复交易、身份/规则hash不匹配、费用或净收益不对账时整体拒绝出结论。输出不包含连接串或凭据，默认新报告权限为0600；上述 artifacts 目录已在现有忽略规则范围内。

每一闭合腿独立报告入场名义、离场名义、毛收益、原费用/净收益、三情景费用/净收益、每边盈亏平衡费率及开仓时间。原强平事件的毛损失保留执行模型的保证金封顶，不以价格差重写；更高费率不重算强平路径。已平腿但其它腿仍持仓的 group 单独标记，不能当作完整组合或独立样本。人口总额是会计核对，不能把关联因子账户或不同冻结规则合并裁定。

## 真实库初次审计

固定快照时间：2026-10-11 17:42:11.857401 UTC，台账最高 sequence_id=4483。

| 人口 | 账户 | 完整闭合腿 | 可用于新因子评估的闭合腿 |
|---|---:|---:|---:|
| 新因子 | 261 | 0 | 0 |
| 旧冻结身份 | 49 | 134 | 0 |

所有134腿均在2026-10-12 00:00 Asia/Shanghai前开仓，属于止损口径缺陷期；晚些平仓也不能变成新样本。49旧账户包含29个 maker 和20个旧 taker 身份，各自冻结hash与原费率逐笔核对后才重估。21个未平仓位排除在闭合腿审计之外。本次没有仍留未平腿的已平 group。

| 旧人口全部闭合腿的会计总额 | USDT |
|---|---:|
| 冻结毛收益 | +181.1423 |
| 重估 maker 净收益 | −4,885.7347 |
| 重估 taker 净收益 | −12,486.0501 |
| 重估 stress 净收益 | −25,153.2425 |

这些数值不能合并用于策略证伪，也不能代表当前261账户的期望。新人口零闭合样本只意味着暂无法评估。报告按账户保留hash、人口、样本标注和费用；早期旧人口的亏损不会被归入新规则。

## “2倍成本差”门槛的精确条件

对腿 i，已冻结数量为 q_i，实际进出代理成交价为 E_i、X_i，毛收益为 G_i。每边费率 f 下：

`P_i(f) = G_i − f × |q_i| × (E_i + X_i)`。

从 maker 变为 taker，在同一成交序列、同一数量上精确得到：

`P_taker = P_maker − Σ_i [(f_taker − f_maker) × |q_i| × (E_i + X_i)]`。

因此 maker 净收益小于**这笔实际追加费用**才必然转负，等于它则刚好为零。不能拿USDT净收益与无名义分母的百分比直接比较。单腿以入场名义归一化时追加费用为 `Δf × (1 + X/E)`；仅在进出名义近似相等时约为 `2Δf`。两腿若各有同样名义，往返共四笔成交，费用约为 `4Δf × 单腿名义`，或 `2Δf × 两腿入场总名义`。

maker 0.0002→taker 0.0005 单腿往返近似多6bp；→stress 0.001近似多16bp。“2倍成本差”只有在“成本差”明确定义为**每边费率差**、名义分母一致、固定尺寸且进出名义近似相等时才有该含义。如果“成本差”已是完整往返费用，再乘2会重复计算。它可用来标记成本脆弱性，不能自动淘汰配置或证伪整个机制族。

## 与重新按ATR风险定仓的区别

冻结同一 ATR、入场价 E、k 和风险档 R 时，执行层的计划名义公式为：

`notional_plan(f) = R / (k × ATR/E + 2f)`。

例如 E=100、ATR=0.347、k=1.5、R=50，假定现金/保证金/槽位不构成更严约束：

| 每边费率情景 | 计划名义 USDT |
|---|---:|
| maker 0.0002 | 8,920.6066 |
| taker 0.0005 | 8,058.0177 |
| stress 0.001 | 6,939.6253 |

这只是同一冻结入场条件下的公式示例，**不是**工具已重放了不同费率的账户。新费率会改变数量、手续费现金、后续权益、保证金约束及强平路径；需要另建冻结执行身份并前向采样，不能由本报告的固定尺寸减费推断其完整结果。收盘确认、下一open及跳空也使R不能成为严格实际亏损上限。

next-open 是价格与成交时点代理；它本身不能证明该笔是 taker，也不能证明是 maker。缺少挂单驻留、盘口、拒单/未成交和实际成交角色资料时，保证成交加 maker 费率仍是乐观假设。单纯提高费用不会解决限价单未成交选择偏差。

## 外部资料复核与待验证假设

独立取证：2026-10-11 17:30–17:35 UTC。外部仓库锁定提交 `5df0c43f6d48b7d8dbb74843d6747e5ddbb6819b`；其收益与实盘部署均为作者自述，不是本项目验证证据。

- [Quattro README（固定版本）](https://github.com/EstebanSP23/crypto_systematic_research/blob/5df0c43f6d48b7d8dbb74843d6747e5ddbb6819b/2_strategies/01_quattro_donchian/README.md) 确有4H BTC、20/10 Donchian、冻结ATR14、最多四单位、2N单位止损和5%灾难止损。§10已更正无成本 headline：加入每次fill taker 0.06%与资金费后，自述总收益+1107%→+610%、APY57.8%、DD−41.7%，P2 APY60.0%→38.2%，仍不含滑点。旧headline APY83%与同库无成本表78.4788%不一致，不能混引。
- [公开基线代码](https://github.com/EstebanSP23/crypto_systematic_research/blob/5df0c43f6d48b7d8dbb74843d6747e5ddbb6819b/2_strategies/01_quattro_donchian/backtest.py#L61-L66) 用 `close > 前日已收盘EMA200`，README却描述日EMA200上升。复现前必须选定并冻结一种明确的regime规则；禁止把两个口径当同一已验证结果。基线代码没有扣手续费，不能用作成本后收益证据。
- [成本版结果CSV](https://github.com/EstebanSP23/crypto_systematic_research/blob/5df0c43f6d48b7d8dbb74843d6747e5ddbb6819b/2_strategies/01_quattro_donchian/results/quattro_v2_costs.csv) 支持其成本修正自述。公开基线交易CSV可复算94笔、25胜、11笔>10R；大赢家集中应检验剔除最大1/3/5笔后的敏感性和不确定性，不能把作者的部署声明当作成交账单。
- 第三方kill list里的资金费carry、Counterpunch、Bear Edge部分结果仍为正，作者因低于放贷基准放弃。仓库同时宣称其它趋势配置存活。可提取失败模式和研究优先级，不能据族名“收敛”就归档当前不同条件规则。
- [Binance官方Futures connector费率查询](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/account.py#L839-L858) 将 `/fapi/v1/commissionRate` 标为签名 `USER_DATA`。公开资料不能证明本账户费率；本轮没有读取账户凭据或调用私有接口。
- [traderabyss原文](https://traderabyss.com/artigos/negative-perpetual-funding-looks-like-free-money-in) 本次返回403，“0.40%地板”等转述未独立核实，不能作为普遍的交易所费率下限。

仅登记候选 `quattro_4h_btc_regime_pyramid_v0`，状态为**待规格澄清、待实现、待前向验证**。候选限定BTC 4H、Donchian20入场/10退出、先前已闭合日线regime、入场冻结ATR14、+0.5N/+1.0N/+1.5N加仓、最多四单位、2N单位止损、每单位2%与组合5%灾难止损。尚须澄清regime口径、每次加仓的风险资金基准、单位追踪触发、灾难止损确认/成交时点、100倍强平和组合保证金约束。它未加入现有261冻结账户；外部自述不足以标“通过”或提高既有策略的证伪等级。

本轮专项20项回归通过；独立代码审查通过。覆盖实际进出名义、做空、强平保留、费用临界翻负、完整台账/截断/重复与配对缺腿、开仓跨截止时间、旧/因子人口分离、零样本、只读事务和无配置凭据输出。
