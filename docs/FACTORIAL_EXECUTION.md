# ATR 风险因子执行说明

新人口为29项策略 × R50/R200/R1000 × k1.0/1.5/2.5，共261个独立模拟账户。旧29个maker账户冻结定义与历史保留，作为单独对照；早于2026-10-12（Asia/Shanghai）的旧样本属于止损口径缺陷期，不能用于新ATR规则的策略评估。旧账户不能通过规格更新变成新样本。

每个新账户初始资金10,000 USDT，100倍杠杆，最多两个仓位，权益30%的保证金预算。单腿入场冻结信号时ATR；真实下一根open代理成交价为E，stop=E-side×k×ATR，名义上限=R/(k×ATR/E+0.0004)。剩余保证金、槽位、手续费现金与策略仓位系数可降低名义。R是计划止损及费用预算；收盘确认、下一根open、跳空及100倍强平可能让实际亏损超过R。资金费、滑点和限价单未成交仍未建模。

15m层按BTC/ETH执行；1h/1d的独立单资产机制按BTC/ETH/TRX执行。跨资产机制按原机制保留实际腿：s10只有ETH订单、BTC是信号源，s30是BTC/ETH两资产确认。TRX完整5m/15m历史已能回补，但本轮15m因子币池继续按已确认的BTC/ETH范围。

strategy_signals.py 由备用3号独占。新ID、哈希校验及跨资产ATR止损放在 strategy_factorial_signals.py；执行定仓在 strategy_simulation.py；所有新经济规则形成新hash。该适配器不改写旧身份。

以下27个账户创建并显示阻塞原因，不能下新单：

- s04的9档：配对级ATR与合成风险定仓尚未实现。单腿止损会破坏两腿原子性，因此不假装已有统一止损。
- s19、s24各9档：原机制需要20币上下文，而用户已暂停其他币线。禁止把三币悄悄替换成原20币宽度/横截面机制；s24六腿组合执行也尚未实现。

依赖暂停币线的旧账户同样暂停评估，保留原definition与数据。s07裁定为KEEP，证据见 S07_REVIEW_20261012.md；不存在单方自动归档。

## 最小正确信号调用

以下仅复核指标，不生成历史成交：

```python
from data_store import DataStore
from runtime_config import load_environment
from strategy_data import load_spec_candles, timeframe_milliseconds
from strategy_factorial import load_factorial_specs
from strategy_factorial_signals import normalize_factorial_spec, evaluate_factorial

load_environment()
store = DataStore()
spec = next(s for s in load_factorial_specs(normalizer=normalize_factorial_spec)
            if s["strategy_id"] == "s07_surge_continuation_B15m_R50_k1.5_100x_maker")
inputs = load_spec_candles(store, dict(spec, universe=spec["data_universe"]), limit=750)
# 行时间戳是开盘UTC毫秒；extras.closed必须为True。
# 历史指标核对以最后一根的收盘时刻作为as_of，避免被实时90秒过期门槛拒绝。
as_of = max(rows[-1]["timestamp"] for rows in inputs.values()) + timeframe_milliseconds(spec["timeframe"])
result = evaluate_factorial(spec, inputs, positions={}, as_of_utc=as_of)
print(result["ready"], result["warmup"],
      {pair: (s["target_side"], s["reason"]) for pair, s in result["signals"].items()})
store.close()
```

ready=True但target_side=None可以是正常未触发。不能把零交易样本等同于无效策略，也不能把缺字段/过期/预热不足结果当成真实信号频率。前向服务使用真实接收时刻，历史回补只恢复指标，不补造订单。
