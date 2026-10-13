# 数据采集缺口审计（面向重构）

> 2026-10-13 · DSH · 用户指示「先做好数据采集，BTC/ETH/TRX，所有数据都要落库，
> 尽量毫秒级，先检查漏了什么」+「采集部分准备重构」
> 全部为实测（只读）

---

## 一、当前采集实现（`market_stream.py` L134-136）

```python
trades = "{symbol}@{'aggTrade' if futures else 'trade'}"   # 逐笔
klines = "{symbol}@kline_5m"                               # ⚠️ 仅 5m
books  = "{symbol}@depth20@100ms"                          # ⚠️ partial depth, 100ms
```

| 数据类型 | stream | 落库表 | 频率（实测） |
|---|---|---|---|
| 逐笔（聚合） | `@aggTrade` | `ticks` | BTC 中位间隔 **1ms** · 64.3% ≤100ms |
| 盘口 20 档 | `@depth20@100ms` | `orderbooks` | 中位间隔 **102ms** |
| K 线 | `@kline_5m` | `candles` | 5 分钟 |

**采集进程**：`market`（BTC/ETH, PID 32263）· `market-trx`（TRX, PID 32348）· `market-live`（8892）

---

## 二、🔴 缺口清单

### G1 · 盘口频率受限于订阅档位（**最重要**）
```
@depth20@100ms ⇒ 币安按 100ms 推送 ⇒ 中位间隔 102ms
币安可用的更快档位:
  · @depth@100ms   ← 增量全档位，可本地重建完整订单簿（更高频）
  · @depth20       ← 无后缀 = 实时推送（比 100ms 快）
  · @bookTicker    ← 最优买卖价，毫秒级（最轻量）
⇒ 【要「毫秒级盘口」必须改订阅，并本地增量重建订单簿】
   当前 20 档 partial snapshot 无法重建成完整深度
```

### G2 · 高周期 K 线完全没订阅（**这是「15m/1h 表陈旧」的根因**）
```
只订阅 @kline_5m ⇒ 15m/1h/4h/30m/1w/1d 靠 data_sync 从【磁盘文件】导入
实测滞后: 15m 【185 分钟】· 1h 260 分钟
⇒ 【应订阅 @kline_1m（最小粒度）+ 在落库层聚合各周期】
   或用 5m 本地聚合（但 5m 比 1m 粗糙）
```

### G3 · `ticks` 用的是 `aggTrade`（聚合成交），不是完整逐笔
```
aggTrade 会把「同价、同时、同方向」的多笔成交【合并为一笔】
实测: 一笔 aggTrade 覆盖 f=8165647040 ~ l=8165647041（2 笔原始成交）
⇒ 【要完整逐笔需订阅 @trade】
⇒ 【影响: 逐笔笔数被低估；对「订单流笔数类」因子有偏差】
```

### G4 · 未采集的币安公开数据
```
❌ @bookTicker        最优买卖价（毫秒级报价流）
❌ 标记价格 markPrice  · 指数价格 indexPrice
❌ 强平订单流 forceOrder（WebSocket，实时强平）
❌ 多空账户比 longShortRatio（5m/15m/1h/4h/1d）
❌ 大户持仓比 topTraderLongShortRatio
❌ 主动买卖量比 takerlongshortRatio
⚠️ openInterest / funding_rate 【有但极少且非实时】:
   series_points.open_interest 仅 【1,000 行 · 2 个 asset】
   series_points.funding_rate  236,558 行 · 57 asset（来自 feather 历史，非实时）
```

### G5 · TRX 覆盖严重不足
```
ticks:      BTC 2,220,053（93.0 小时）· ETH 2,152,677 · 【TRX 65,593（26.1 小时）】
orderbooks: ETH 2,971,322 · BTC 2,907,114 · 【TRX 475,970（26.1 小时）】
⇒ TRX 采集器 10-11 17:13 才启动，比 BTC/ETH【晚 67 小时】
⇒ 且 TRX ticks 频率: 中位间隔 【1,187ms】· 仅 9.5% ≤100ms（BTC 是 1ms/64.3%）
   ⇒ 【TRX 本身成交少（1,835 笔/小时 vs BTC 22,979）⇒ 部分是市场特性】
```

### G6 · 时间连续性缺口
```
ticks      BTC: gap>60s 【35 处】· 累计缺失 【373.3 分钟】
           ETH: 35 处 · 373.0 分钟
           TRX:  3 处 · 11.6 分钟
orderbooks BTC: 38 处 · 349.3 分钟 · ETH: 38 处 · 349.3 分钟

最大单次缺口: 10-09 16:56:49 → 19:10:18 【133.5 分钟】
主要集中: 10-09 12:13~23:16（多次 8-16 分钟）· 10-10 04:47~06:34（多次 8-18 分钟）
⇒ 【BTC/ETH 的 gap 数几乎相同 ⇒ 同一进程中断/重启所致】
⇒ 重构应考虑: 断线重连 + 缺口检测告警 + 补采
```

### G7 · 延迟与时钟
```
网络延迟（交易所事件时间 → 本地接收）:
  ticks      : 中位 【18ms】· p90 90ms · p99 140ms  ✅ 毫秒级达标
  orderbooks : 中位 【−57ms】← ⚠️ 负数
     ⇒ 本地 received_timestamp 比交易所事件时间【早 57ms】
     ⇒ 【本地时钟比币安快约 57ms】⇒ 需校时（NTP）
写库延迟（received_at 列 − timestamp）:
  ticks 625ms · orderbooks 458ms
  ⚠️ 注意: 这是「落库时延」，非网络延迟；若用批量写会累积
```

---

## 三、缺口影响评估（对策略/研究的实际影响）

| 缺口 | 影响 | 严重度 |
|---|---|---|
| G1 盘口 100ms | 无法做真正的微观结构/HFT 研究（盘口变化被抽样） | 🔴 高 |
| G2 高周期未订阅 | 「15m 表陈旧 185 分钟」的根因；任何用高周期的分析都会读到旧数据 | 🔴 高 |
| G3 aggTrade 非逐笔 | 逐笔笔数低估 ⇒ 订单流笔数类因子有偏差 | 🟡 中 |
| G4 缺 bookTicker/强平/OI/多空比 | 无法研究「强平级联」「持仓变化」「情绪」类信号 | 🟡 中 |
| G5 TRX 覆盖不足 | TRX 无历史可比 ⇒ 我此前的 TRX 结论无法跨期验证 | 🟡 中 |
| G6 时间缺口 373 分钟 | 回测/前向验证中有空洞（需显式标注） | 🟡 中 |
| G7 时钟偏移 57ms | 跨源对齐（ticks vs orderbooks）会有 57ms 系统偏差 | 🟢 低 |

---

## 四、给重构的建议（按优先级）

```
【P0 · 必做】
 1. 高周期改为【订阅 @kline_1m + 落库层聚合】（消除 data_sync 文件路径的滞后）
 2. 盘口改【@depth@100ms 增量 + 本地订单簿重建】，或【@depth20 无后缀】
    并明确: 是否要完整深度（若 HFT 研究，必须完整深度）
 3. 逐笔改【@trade】（完整逐笔）或同时采集 aggTrade 与 trade 两路
 4. 断线重连 + 缺口检测 + 补采（对齐 G6 的 373 分钟缺口）

【P1 · 应做】
 5. 增加 @bookTicker（毫秒级最优价，成本低、价值高）
 6. 增加 forceOrder（强平订单流）—— 与「爆发行情」研究直接相关
 7. openInterest 改为【实时轮询】（币安 REST 每 5s 或 1m）+ 历史回填
 8. longShortRatio / topTrader 定时轮询（5m/15m/1h）

【P2 · 应做】
 9. NTP 校时（消除 57ms 偏移）+ 统一「交易所时间/本地接收时间」字段语义
10. TRX 覆盖补齐（含历史回填，与 BTC/ETH 对齐）
11. 落库层优化: 批量写（降低 625ms 写库延迟）+ 落库时间指标上报
12. 采集完整性自检（复用已有的 check_candle_integrity.py 思路）
```

---

## 五、已落库数据类型清单（现有）

```
表                    行数          内容
ticks                 4,436,855    aggTrade 逐笔（exchange/market/pair/trade_id/
                                   timestamp/price/quantity/buyer_maker/extras/received_at）
orderbooks            6,356,647    @depth20@100ms（含 last_update_id/bids/asks/extras）
candles               5,741,463    5m（+ 高周期由 data_sync 从文件导入）
events                1,641,768    key/ordinal/payload（模型预测、回填记录等）
series_points           374,470    funding_rate / open_interest / dvol 等（历史导入）
orderflow_surge_events      740    surge 检测事件
orderflow_price_candidates  346    price candidate 事件
stream_stats                  6    采集器自报统计（可用于监控）
```

---

## 六、局限

- 频率统计基于【近 1 小时】样本（20,000~34,000 条），非全期
- 「毫秒级」的判断标准: 中位间隔 ≤10ms 视为达标；ticks BTC=1ms ✅· ETH=9ms ✅
- orderbooks 的 −57ms 偏移可能是字段语义差异（raw.E vs raw.T）而非真实时钟偏移，需重构时确认
- 未实测币安各 stream 的【理论上限频率】（需查官方文档或实测订阅）
