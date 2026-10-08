# 公开行情持续入库

`market_stream.py` 将币安公开成交和前 20 档盘口持续写入 PostgreSQL。只读取公开市场数据，不需要交易所 API Key，不访问账户、不下单，也不启动或修改模型迭代。

## 数据含义与官方接口

| 市场 | 成交流 | 盘口流 | 默认交易对 |
| --- | --- | --- | --- |
| USDⓈ-M USDT 合约 | `aggTrade`，同价、同主动方向成交聚合，官方推送间隔 100 ms | `depth20@100ms`，前 20 档部分深度快照 | `BTC/USDT:USDT`、`ETH/USDT:USDT` |
| 现货 | `trade`，原始逐笔成交 | `depth20@100ms`，前 20 档部分深度快照 | 指定 `--market spot` 后对应现货交易对 |

合约 `aggTrade` **不是每一笔原始撮合**。一条消息可能汇总多个成交，原始首尾成交 ID `f/l` 随交易所正文保存于 `extras.raw`。数据库与运行状态明确标记 `aggregated=true` / `trade_type=aggregated`，不会把它宣传成完整的原始逐笔记录。

盘口只存每次公开推送的前 20 档快照，不重建完整 L2 订单簿，也不声称逐条记录所有深度变化。盘口正文、更新 ID 和事件时间均保留。现货部分深度没有交易所事件时间，使用本地接收时间，并在 `extras.timestamp_source` 中明确标记 `received`。

当前官方接口将合约成交与盘口划分为不同的连接分区，采集器分别建立两个连接：

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
| `QUANT_STREAM_SPOOL_DIR` | `database/runtime/market_spool` | 故障暂存批次的位置，应置于有足够容量的本地盘 |

合约交易对统一输出 CCXT 的 `BASE/USDT:USDT` 格式，以便与历史行情和前端查询一致。当前只支持 USDT 本位合约，不订阅币本位或 USDC 合约。

## 写入、恢复与停止

内存队列默认最多 20,000 条消息；每批最多 1,000 条，最长等待 1 秒。写数据库之前先将该批次原子落盘并执行 `fsync`，随后在线程中批量写 PostgreSQL，避免同步数据库调用阻塞 WebSocket 收包。

- 成交写入 `ticks`，唯一键为交易所、市场、交易对、成交 ID。
- 盘口写入 `orderbooks`，唯一键为交易所、市场、交易对、时间戳、更新 ID。
- 不同种类/交易对分别提交事务；一部分事务成功而另一部分失败时保留整个暂存批次，后续重播依靠唯一键去重。
- 数据库故障后批次继续保留，按照 1、2、4、8 秒等间隔退避，最长 30 秒。恢复后优先重播旧批次。
- 内存队列满时，新消息立即持久化为额外批次；不因队列满静默丢弃。
- 磁盘写入失败时停止采集，并记录 `storage_error`、错误原因及未持久化消息数。运行期间应监控磁盘容量。
- SIGINT / SIGTERM 后停止连接、把内存队列落盘，并在限定时间内尝试补写；未补写的批次保留供下次启动恢复。
- 暂存文件携带原始市场和交易对，修改启动参数后重播不会把旧合约数据错写为现货。
- 启动会发现遗留 `.tmp` 文件。完整文件可以重播；中断产生的损坏文件会原样改名隔离为 `.corrupt`，主动在数据库与本地状态告警，然后继续处理其他有效批次。隔离文件不自动删除，部分内容可能无法恢复，需要人工检查。不要删除尚未确认提交的暂存文件。
- 采集器在整个运行生命周期对暂存父目录的 `.market_stream.lock` 持有跨进程 `flock` 独占锁。同一运行目录的第二实例明确以退出码 `2` 退出，不读取暂存、不覆盖状态。锁随进程退出自动释放；锁文件保留，不应在运行时手动删除。

暂存目录仅用于数据库故障恢复，已被 Git 忽略，正式查询仍以 PostgreSQL 为准。写入前的内存批次存在最多约 1 秒的正常等待窗口；SIGKILL、断电等非优雅中断可能丢失仍在内存中的消息。当前实现不提供每条消息的端到端持久化确认。

WebSocket 断开时指数退避重连，连接期间的原始消息照实存储。断线期间的盘口和成交**不会自动历史补齐**，状态通过 `data_gap_possible`、`reconnects` 和错误时间明确报告。没有 REST 轮询降级，也没有生成或填补虚构行情。

## 状态与验证

运行状态同步为数据库文档 `market_stream_status.json`；同时保存本地 `database/runtime/market_stream_status.json`，以便数据库故障时仍能检查采集器。

主要字段包括连接状态、成交类型、盘口类型、收到/写入计数、队列长度、内存批次长度、在写批次长度、暂存批次数、溢出暂存计数、重连计数、最后消息时间、最后写入时间、最后错误、数据库故障次数与下一次重试等待时间。`corrupt_batches`、`corrupt_files`、`storage_warning` 明确报告保留的损坏批次，并跨重启持续提示；`data_gap_possible=true` 说明可能存在行情缺口，损坏批次的缺失条数无法准确推算。`persisted/replayed` 是本次进程成功提交的暂存事件数，包含从前次进程恢复的事件及幂等重播，不能当作数据库新增唯一行数。

离线测试不访问交易所、不使用真实账户，不写生产数据库：

```text
uv run python -m unittest discover -s tests -p test_market_stream.py -v
```

测试覆盖官方分区 URL、原始/聚合成交、现货/合约盘口字段、精度和交易对规范、非法消息可见拒绝、队列满后落盘、磁盘写失败停止、数据库故障退避、部分提交后的幂等重播、改市场后的恢复、优雅停止和数据库不可用时的本地状态。另外用真实子进程验证锁互斥与异常退出释放，并验证残缺批次隔离后继续恢复、完整临时批次重播、损坏告警跨重启保留。
