# 数据保存审计与修复规格（用户指示：数据是一切操作的前提）

> 2026-10-12 · DSH · 用户指示「先修数据保存，这是一切操作的前提，怎么可以漏数据」
> 本文为**审计证据 + 修复规格**，实现交 Codex，我负责验证

## 一、⚠️ 先更正我自己两处误报（避免误导）

| 我先前说 | 实际 | 性质 |
|---|---|---|
| 「BTC/ETH 订单流字段只有 0.27%，漏了 99.7%」 | 实际是从 10-11 18:00 起**连续满额**；缺的是**历史导入部分** | 表述错误（第 65 处） |
| 「dryrun_monitor 的 20/20 新鲜是假绿灯」 | 它查的是**日线**（代码 L23 注释写明），与 5m 无关 | 判断错误（第 66 处） |

**⇒ 但用户的核心关切成立：确实有数据不一致，且是真 bug（见下）。**

## 二、审计证据

### 2.1 表清单（27 张，无漏表）

orderbooks 5,838,612 · candles 5,789,168 · ticks 3,901,553 · events 1,641,757 ·
series_points 374,470 · strategy_sim_events 31,583 · …

### 2.2 关键发现：`source` 决定字段有无

```
5m 表（三币）:
  BTC: (无)            292,072 行 · num_trades 【377】 · flow_imbal 0
       binance_kline_5m    410 行 · num_trades 410   · flow_imbal 410  ✅
  ETH: (无)            292,072 行 · num_trades 376   · flow_imbal 0
       binance_kline_5m    410 行 · num_trades 410   · flow_imbal 410  ✅
  TRX: (无)            292,242 行 · num_trades 【291,804】 · flow_imbal 0
       binance_kline_5m    240 行 · num_trades 240   · flow_imbal 240  ✅
```

**🔴 核心异常**: 同为「历史导入」（source 为空），
**TRX 的文件含 num_trades（291,804 行）而 BTC/ETH 的文件不含（仅 377 行）**
⇒ **两个币的历史数据文件来自不同下载方式。**

**🔴 第二异常**: `flow_imbal` 在**所有**历史导入数据里都是 0
（market_stream.py L273 只在实时路径计算它）
⇒ 历史数据缺 flow_imbal。

### 2.3 代码位置（market_stream.py L261-273）

```python
quote_volume = _decimal(raw.get("q"))
candle["quote_volume"] = finite_float(quote_volume, "成交额")
if ...: candle["num_trades"]     = _nonnegative_int(raw["n"])
if ...: candle["taker_buy_base"] = _decimal(raw["V"])
taker_buy_quote = finite_float(raw["Q"], "买方成交额")
candle["taker_buy_quote"] = taker_buy_quote
if candle["quote_volume"] > 0:
    candle["flow_imbal"] = 2 * taker_buy_quote / candle["quote_volume"] - 1
```
⇒ **实时路径的字段提取是完整的**；问题在**历史导入路径**。

### 2.4 其余项的实际影响（已核实，非缺口或影响有限）

| 项 | 实情 | 影响 |
|---|---|---|
| 17 个其它币无 10-11 03:00 后 5m | 策略 `execution_universe` 只用 BTC/ETH/TRX（326/326/135 账户） | **不影响策略**，仅研究 |
| 高周期表滞后（15m 185分 · 1h 260分） | 靠 data_sync 同步文件 | 策略有 resample 兜底（见另一份文档） |
| 30m/4h/1w 表近空（200/6085/200） | 未被策略使用 | 低 |
| candles 写入 33 行/时 | 3 币 × 12 根 = 36 预期 | ✅ 正常 |
| ticks 173,049/时 · orderbooks 89,309/时 | — | ✅ 正常 |

## 三、修复规格（交 Codex）

### 3.1 补齐 BTC/ETH 的历史订单流字段（最高优先）

**可行性已验证**：`GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m`
返回字段包含：
```
numTrades=26259 · quoteVolume=124340961.60
takerBuyBase=634.005 · takerBuyQuote=52313628.02
```
⇒ 与库中 extras 的 `num_trades` / `quote_volume` / `taker_buy_base` / `taker_buy_quote` 对应。

**注意**: REST 只保留最近约 1500 根 5m（≈5.2 天）。
更早的历史需从 Freqtrade data 文件（用带订单流字段的下载方式）或 aggTrades 补。

**建议做法**（Codex 判断）:
- (a) 用正确参数重新 `freqtrade download-data` BTC/ETH 的 futures 数据 → data_sync 重导
- (b) 或写一次性回填脚本走 REST（覆盖近 5 天）+ 文件（更早）
- ⚠️ 无论哪种，**必须先验证文件里确实含这些字段**（TRX 的文件含，BTC/ETH 的不含）

### 3.2 历史数据也应计算 `flow_imbal`

`data_sync` 导入时应计算 `flow_imbal = 2 × taker_buy_quote / quote_volume − 1`
（与 market_stream.py L273 口径一致），否则历史与实时两段口径不同。

### 3.3 统一两个采集器的字段集合

两个进程用同一脚本（PID 23441 主 / 47181 TRX 专用），但**产出不一致**：
- TRX 实时段 240 行有 flow_imbal；BTC/ETH 410 行有 flow_imbal
- 起点不同（BTC/ETH 10-11 03:00 · TRX 10-11 17:10）
⇒ 请查明为何起点不同，并确保两路产出一致。

### 3.4 建议加一道「数据完整性自检」（防止静默漏数据）

用户的核心诉求是「不能漏数据」。建议加一个可周期性运行的自检：
```
对每个 (pair, timeframe, 应含字段集合):
  ① 覆盖率: count(字段非空)/count(*) —— 低于阈值则告警
  ② 时间连续性: 相邻 timestamp 间隔是否等于周期（找 gap）
  ③ 与 REST 对账: 抽样最近 N 根，比对 num_trades/quote_volume 是否与 REST 一致
⇒ 输出到 documents + 非零退出码，便于定时监控捕获
```
⚠️ **这比「事后我手工发现」可靠得多。** 我今天是因为要测「量能×方向」才发现字段缺失。

## 四、我要验证的清单（修复后）

```
① BTC/ETH 的 num_trades/quote_volume/taker_buy_* 覆盖率 → 应与 TRX 同量级（历史全量）
② flow_imbal 在历史段也有值，且与 2×tbq/qv−1 一致
③ 与 REST 抽样对账: 最近 10 根的 num_trades/quote_volume 与 REST 完全一致
④ 三币的字段集合与起点一致
⑤ 自检工具能复现「修复前的缺口」（即它会报错）—— 否则它抓不住问题
```

## 五、局限

- 本审计基于 2026-10-12 21:30 的快照
- 未审计 funding rate / open interest 等其它数据源（若存在对应表）
- 未验证「TRX 文件为何含字段」的下载参数细节（需看 freqtrade 下载配置）
