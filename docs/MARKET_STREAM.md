# 公开行情持续入库

`market_stream.py` 将币安公开成交和前 20 档盘口持续写入 PostgreSQL。只读取公开市场数据，不需要交易所 API Key，不访问账户、不下单，也不启动或修改模型迭代。

## 图表实时推送

`market_live.py` 为行情图表提供独立 WebSocket。浏览器首次取历史 K 线，之后只合并交易所推送的同根更新或追加新根，保留最近 200 根。成交量、OHLC 和收盘状态直接使用官方 K 线字段，不通过聚合成交估算。官方 K 线推送频率与逐笔/盘口不同，本地 25 毫秒合并窗口不是交易所端到端延迟保证。

```text
uv run python manage.py start market-live
uv run python manage.py status market-live
uv run python manage.py restart auth
```

所有图表共享一条浏览器连接；后端按现货/合约共享上游，根据页面实际币种和周期增减订阅，同一订阅不会因多个浏览器重复采集。后台逐笔/盘口的默认 BTC、ETH 范围保持独立。图表策略信号另外刷新，行情不等待信号请求。

推送路径为交易所 → 后端内存 → 浏览器，不等待数据库。后端默认 25 毫秒合并窗口，前端默认 50 毫秒重绘窗口；这只限制本地转发与绘图调度，不改变交易所 K 线的官方推送频率。K 线归档由有界后台线程批量执行，保存失败时保留并退避重试；归档状态、拒收计数和连接状态可在 `http://127.0.0.1:8892/health` 检查。这个 K 线队列在内存中，骤停可能丢失未提交更新；断线重连后通过真实历史数据对账，原逐笔/盘口暂存恢复机制保持原样。

行情带交易所事件时间、本机接收时间和发送时间。图表显示接收新鲜度、连接状态及本机转发耗时；跨交易所与本机的时间差依赖时钟同步，负差必须视为时钟偏差，不能解释为零延迟。上游断开时重连并补取历史，不补造缺失 K 线；浏览器慢到超过有界缓冲时关闭连接，让客户端重连恢复。

K 线保存采用版本检查，较旧的 WS 更新和 REST 快照不会覆盖更新版本；同版本优先保留收盘状态。`candle_datasets` 的计数与时间边界增量更新，无需每次扫描全部历史；缺失摘要时在事务内恢复一次。连接认证与外部 `wss://` 配置见 [运维说明](OPERATIONS.md)。

## 数据含义与官方接口

| 市场 | 成交流 | 盘口流 | 默认交易对 |
| --- | --- | --- | --- |
| USDⓈ-M USDT 合约 | `aggTrade`，同价、同主动方向成交聚合，官方推送间隔 100 ms | `depth20@100ms`，前 20 档部分深度快照 | `BTC/USDT:USDT`、`ETH/USDT:USDT` |
| 现货 | `trade`，原始逐笔成交 | `depth20@100ms`，前 20 档部分深度快照 | 指定 `--market spot` 后对应现货交易对 |

合约默认使用 `aggTrade`，**不是每一笔原始撮合**。一条消息可能汇总多个成交，原始首尾成交 ID `f/l` 随交易所正文保存于 `extras.raw`。数据库与运行状态明确标记 `aggregated=true` / `trade_type=aggregated`，不会把它宣传成完整的原始逐笔记录。

如需试运行合约原始逐笔，可设置 `QUANT_STREAM_TRADE_STREAM=trade` 或传入
`--trade-stream trade`。原始成交的数据库 ID 使用 `trade:<t>` 命名空间，原始交易所 ID
仍保存在 `extras.original_trade_id`；这是为了避免 `aggTrade.a` 与 `trade.t` 的不同 ID
空间撞击现有 ticks 主键。默认部署不会切换成 raw trade，也不会同时把两种流写入同一个
未区分命名空间的 ID。

采集器也支持显式配置多个 K 线周期，例如
`QUANT_STREAM_KLINE_INTERVALS=1m,5m`。每个周期按自己的 `timeframe` 写入 candles，
合约行继续分别写入 `futures` 与 `orderflow`。1m 原始 K 线不会喂给 5m 突增检测器；本阶段
尚未把 1m 聚合成 15m/1h，也不会用不完整分钟桶伪造高周期 K 线。

盘口只存每次公开推送的前 20 档快照，不重建完整 L2 订单簿，也不声称逐条记录所有深度变化。盘口正文、更新 ID 和事件时间均保留。现货部分深度没有交易所事件时间，使用本地接收时间，并在 `extras.timestamp_source` 中明确标记 `received`。

当前官方接口将合约成交与盘口划分为不同的连接分区，采集器分别建立两个连接。默认订阅为：

- 合约成交：`wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade`
- 合约盘口：`wss://fstream.binance.com/public/stream?streams=btcusdt@depth20@100ms/ethusdt@depth20@100ms`
- 现货：`wss://stream.binance.com:9443/stream`，同一连接订阅成交和盘口。

地址与字段依据 [Binance 合约成交官方文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)、[Binance 合约部分深度官方文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public)、[Binance 现货官方文档](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~)，核对日期为 2026 年 10 月 9 日。

## 启动与配置

由 Python 运维入口管理生命周期：

```text
uv run python manage.py start market
uv run python manage.py status
uv run python manage.py stop market
```

TRX 合约使用独立采集侧车，加入第三币时可以保留现有 BTC/ETH 采集进程与内存基线：

```text
uv run python manage.py start market-trx
uv run python manage.py status market market-trx
uv run python manage.py stop market-trx
```

`market-trx` 固定订阅 `TRX/USDT:USDT`，使用
`database/runtime/trx/market_spool` 暂存、`database/runtime/trx/.market_stream.lock`
实例锁和 `database/runtime/trx/market_stream_status.json` 本地状态。其数据库状态文档为
`market_stream_trx_status.json`，与原采集器的 `market_stream_status.json` 分开。
管理器按入口脚本、工作目录和暂存目录识别两组进程；TRX 侧车还要求匹配独立币池与状态文档。
不指定服务名的 `start`、`stop`、`restart`、`status` 会包括两个采集器；单独管理
`market-trx` 不会停止或复用原 `market` 进程。`QUANT_STREAM_ENABLED` 对两者均生效。

现货实时采集使用独立 `market-spot` 服务：

```text
.venv/bin/python3 manage.py start market-spot
.venv/bin/python3 manage.py status market-spot
.venv/bin/python3 manage.py stop market-spot
```

固定订阅 BTC/USDT、ETH/USDT、TRX/USDT 的原始成交、前 20 档盘口和已收盘
1m/5m K 线。正式数据分别写入 PostgreSQL 的 `ticks`、`orderbooks` 和
`candles`（`market=spot`、`candle_type=spot`），状态写入数据库文档
`market_stream_spot_status.json`。`database/runtime/spot/market_spool` 仅用于提交前
暂存与故障恢复；本地状态文件和实例锁也与合约采集器隔离。管理器核对市场、币池、
周期、状态文档和暂存目录，避免把现货进程误认成合约进程。

该服务从启动时开始积累真实收到的数据，不补齐此前缺失的历史。现有 `spot-basis`
继续负责独立的 1h REST 前向喂数；其它现货币种需另外配置后才会实时采集。

横截面白名单中尚无专用源的 17 个合约交易对使用独立的 `market-breadth` 侧车。
BTC/ETH/TRX 继续由现有专用采集器负责，避免重复订阅和重复写入。侧车写入独立的
`database/runtime/breadth/market_spool` 与 `market_stream_breadth_status.json`。该服务默认关闭，
上线前需要确认 WebSocket 带宽、数据库写入量和历史缺口补采计划后显式开启：

```text
QUANT_BREADTH_STREAM_ENABLED=true uv run python manage.py start market-breadth
uv run python manage.py status market-breadth
```

侧车只需持续提供 `1m,5m`；`15m/30m/1h/4h` 应由连续、已收盘的 5m 数据重采样，
不会依赖按请求写入的 `live:binance-klines` 缓存。启动侧车不会回补启动前的历史缺口，
断线回补仍由独立的 REST worker 负责，不能阻塞 WebSocket 收包循环。

调试时可直接运行，默认保持采集直到收到 SIGINT 或 SIGTERM：

```text
uv run python market_stream.py --pairs BTC/USDT,ETH/USDT --market futures
uv run python market_stream.py --pairs BTC/USDT --market spot
uv run python market_stream.py --help
```

私有 `.env` 支持以下变量：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `QUANT_DATABASE_URL` | 无 | PostgreSQL 连接串，由统一 DataStore 管理 |
| `QUANT_STREAM_ENABLED` | `true` | 设置 `false`、`0` 或 `no` 时跳过启动 |
| `QUANT_STREAM_MARKET` | `futures` | `futures` 或 `spot` |
| `QUANT_STREAM_PAIRS` | `BTC/USDT,ETH/USDT` | 逗号分隔；也接受 `BTCUSDT` 形式 |
| `QUANT_STREAM_TRADE_STREAM` | 合约 `aggTrade`、现货 `trade` | 显式设为 `trade` 时，合约原始成交使用 `trade:<t>` ID |
| `QUANT_STREAM_KLINE_INTERVALS` | `5m` | 逗号分隔的收盘 K 线周期，例如 `1m,5m` |
| `QUANT_STREAM_GAP_THRESHOLD_MS` | `60000` | 事件时间缺口阈值；只记录告警，不在 WebSocket 循环内补采 |
| `QUANT_BREADTH_STREAM_ENABLED` | `false` | 显式开启 17 对缺失白名单合约侧车；默认不启动 |
| `QUANT_INTEGRITY_PAIRS` | 20 对白名单 | 完整性监控的逗号分隔交易对；使用合约格式或 `BASE/USDT` |
| `QUANT_STREAM_SPOOL_DIR` | `database/runtime/market_spool` | 故障暂存批次的位置，应置于有足够容量的本地盘 |
| `QUANT_STREAM_DISK_WARNING_PCT` | `15` | 暂存目录所在分区剩余百分比低于此值时写入 `warning` 告警 |
| `QUANT_STREAM_DISK_CRITICAL_PCT` | `5` | 暂存目录所在分区剩余百分比低于此值时写入 `critical` 告警 |
| `QUANT_STREAM_DISK_WARNING_BYTES` | `0` | 剩余字节低于此值时告警；`0` 表示不启用 |
| `QUANT_STREAM_DISK_CRITICAL_BYTES` | `0` | 剩余字节低于此值时进入临界告警；`0` 表示不启用 |

合约交易对统一输出 CCXT 的 `BASE/USDT:USDT` 格式，以便与历史行情和前端查询一致。当前只支持 USDT 本位合约，不订阅币本位或 USDC 合约。

## 写入、恢复与停止

内存队列默认最多 20,000 条消息；每批最多 1,000 条，最长等待 1 秒。写数据库之前先将该批次原子落盘并执行 `fsync`，随后在线程中批量写 PostgreSQL，避免同步数据库调用阻塞 WebSocket 收包。

- 成交写入 `ticks`，唯一键为交易所、市场、交易对、成交 ID。
- 盘口写入 `orderbooks`，唯一键为交易所、市场、交易对、时间戳、更新 ID。
- 不同种类/交易对分别提交事务；一部分事务成功而另一部分失败时保留整个暂存批次，后续重播依靠唯一键去重。
- 数据库故障后批次继续保留，按照 1、2、4、8 秒等间隔退避，最长 30 秒。恢复后优先重播旧批次。
- 内存队列满时，新消息立即持久化为额外批次；不因队列满静默丢弃。
- 磁盘写入失败时停止采集，并记录 `storage_error`、错误原因及未持久化消息数。运行期间应监控磁盘容量。
- 暂存文件读取失败时保留原批次并停止采集；临时 I/O 或权限故障不会被当成内容损坏而隔离。
- 数据库提交后，暂存文件删除或目录 `fsync` 失败同样以 `storage_error` 停止采集。此时数据已提交，不计作数据库写失败；删除成功但目录同步失败时，原暂存文件可能已消失。
- SIGINT / SIGTERM 后停止连接、把内存队列落盘，并在限定时间内尝试补写；未补写的批次保留供下次启动恢复。
- 暂存文件携带原始市场和交易对，修改启动参数后重播不会把旧合约数据错写为现货。
- 启动会发现遗留 `.tmp` 文件。完整文件可以重播；中断产生的损坏文件会原样改名隔离为 `.corrupt`，主动在数据库与本地状态告警，然后继续处理其他有效批次。隔离文件不自动删除，部分内容可能无法恢复，需要人工检查。不要删除尚未确认提交的暂存文件。
- 采集器在整个运行生命周期对暂存父目录的 `.market_stream.lock` 持有跨进程 `flock` 独占锁。同一运行目录的第二实例明确以退出码 `2` 退出，不读取暂存、不覆盖状态。锁随进程退出自动释放；锁文件保留，不应在运行时手动删除。

暂存目录仅用于数据库故障恢复，已被 Git 忽略，正式查询仍以 PostgreSQL 为准。写入前的内存批次存在最多约 1 秒的正常等待窗口；SIGKILL、断电等非优雅中断可能丢失仍在内存中的消息。当前实现不提供每条消息的端到端持久化确认。

## 订单流突增与价格爆发观测

合约 BTC、ETH 的已收盘 5m Binance K 线会进入两条独立的在线证据路径：

- `orderflow_surge_events` 只记录量比达到 `2x/4x/8x/20x` 的突增及其 5m、30m、1h、4h 成熟结果。
- `orderflow_price_candidates` 记录基线就绪后的**每一根**有效 K 线，包括低于 2x 的量比档；否则无法计算 `<1x`、`1-2x` 和无条件概率的分母。
- `orderflow_price_candidate_results` 以追加事实保存价格结果与剖面，不覆盖候选时刻的量比或指纹。

价格观测当前方法版本为 `orderflow-price-spike-close3-v3`，突增方法为 `orderflow-volume-surge-v3`。两者固定使用带实时采集标记的连续基线 K 线，并将零成交额收盘 K 线纳入 `<1x` 样本；旧版本记录保留在库中，不与新版统计合并。候选根的量比为当前 `quote_volume` 除以前 288 根连续收盘 K 线成交额中位数。用户于 2026-10-11 确认冻结收益锚点为 `close[t+3] / close[t] - 1`，结果只在候选根后第 3 根 K 线收盘后判定：收益 ≥+1.5% 为上涨爆发，≤−1.5% 为下跌爆发，其余为 `none`。前两根的盘中或收盘涨跌不替代第 3 根收盘价。v3 使用十进制价格比较包含阈值边界，修复 v2 浮点相减可能漏判恰好 +1.5% 的问题，不用容差扩大阈值。窗口缺少任一精确时间戳或缺少采集器 `live_event_timestamp` 标记时记为 `invalid`，不会跳过缺口，也不会把未知窗口算成无爆发。

候选观测只由进程运行期间真实收到的收盘 K 线产生。启动时只用 `futures` K 线中带 `live_event_timestamp` 的连续记录准备 288 根基线；研究归档和缺少该标记的历史行不会参与。基线指纹包含来源、起止 K 线时间、周期和成交额序列。首次运行或可用实时历史不足 288 根时，检测器继续预热，不会为历史 K 线补发候选或结果。一个窗口完整剖面要等到候选根之后第 12 根收盘，再要求 `t-12` 至 `t+12` 的 25 个在线候选比值全部存在；缺失点会使剖面无效，不用后续数据冒充。REST/研究归档数据不用于价格结果标签。

前端的近期候选列表会保留待判定窗口并标注 `unknown`；条件概率、无条件概率和提升倍数按 BTC、ETH 分开，并排除 `unknown` 和 `invalid`。零成交额但有效的收盘 K 线保留在 `<1x` 样本中。存储层会校验候选数据指纹与原始观测内容一致。这条观测线描述的是波动概率，不是涨跌方向或独立入场信号。历史第三轮统计保留为单独的回顾性参考，不并入在线样本；原研究材料没有明确 1.5% 收益的精确锚点，因此该在线版本的 close-to-close 规则单独编号，不能直接宣称复现了历史百分点。原始 tick 与 orderbook 仍没有自动保留期或清理策略。

WebSocket 断开时指数退避重连，连接期间的原始消息照实存储。断线期间的盘口和成交**不会自动历史补齐**，状态通过 `data_gap_possible`、`reconnects` 和错误时间明确报告。没有 REST 轮询降级，也没有生成或填补虚构行情。

## 状态与验证

运行状态同步为数据库文档 `market_stream_status.json`；同时保存本地 `database/runtime/market_stream_status.json`，以便数据库故障时仍能检查采集器。状态中的 `gap_metrics` 按事件类型、市场、交易对和 K 线周期记录上次事件时间、缺口次数、最大缺口和最近缺口；超过阈值会写入日志、设置 `data_gap_possible=true`，并更新 `gap_alert_count` 与 `last_gap_alert`（`alert=gap_threshold_exceeded`）。这只是发现与告警，历史补采仍需独立工具或 worker；采集器不会在收包循环中直接发 REST 请求或写回补数据。

主要字段包括连接状态、成交类型、盘口类型、收到/写入计数、队列长度、内存批次长度、在写批次长度、暂存批次数、溢出暂存计数、重连计数、最后消息时间、最后写入时间、最后错误、数据库故障次数与下一次重试等待时间。`corrupt_batches`、`corrupt_files`、`storage_warning` 明确报告保留的损坏批次，并跨重启持续提示；`data_gap_possible=true` 说明可能存在行情缺口，损坏批次的缺失条数无法准确推算。`persisted/replayed` 是本次进程成功提交的暂存事件数，包含从前次进程恢复的事件及幂等重播，不能当作数据库新增唯一行数。
状态发布还会检查暂存目录所在分区，并提供 `disk_total_bytes`、`disk_used_bytes`、`disk_free_bytes`、`disk_free_pct`、`disk_alert_level`、`disk_alert_reason` 和 `disk_checked_at`。`disk_alert_level` 为 `ok`、`warning`、`critical` 或 `unknown`；检查失败时保留 `unknown` 和错误原因，不会把容量误报为正常。数据中心的“实时采集”区域展示剩余容量、检查时间与告警原因。

离线测试不访问交易所、不使用真实账户，不写生产数据库：

```text
uv run python -m unittest discover -s tests -p 'test_market_stream*.py' -v
```

测试覆盖官方分区 URL、原始/聚合成交、现货/合约盘口字段、精度和交易对规范、非法消息可见拒绝、队列满后落盘、磁盘写失败停止、数据库故障退避、部分提交后的幂等重播、改市场后的恢复、优雅停止和数据库不可用时的本地状态。另外用真实子进程验证锁互斥与异常退出释放，并验证残缺批次隔离后继续恢复、完整临时批次重播、损坏告警跨重启保留。低层故障注入还覆盖暂存写入、刷新、文件和目录同步、重命名、删除、读取，以及状态侧车写入失败和队列排空后不再阻塞。
