# PostgreSQL 数据同步

前后端使用 PostgreSQL 作为统一查询存储。同步器只读现有文件及 Freqtrade SQLite 快照；模型训练、模型迭代和原始数据下载继续使用原来的文件路径，训练进程无需停止。

```bash
uv run python data_sync.py --once
uv run python data_sync.py --daemon
uv run python data_sync.py --once --fast
```

连接串从私有 `.env` 中的 `QUANT_DATABASE_URL` 读取，不通过命令行参数传递。`--force` 可重新导入未变化的来源；`--root` 指定项目根目录。

## 数据映射

| 原始来源 | PostgreSQL 目标 | 隔离维度 |
| --- | --- | --- |
| `bot/user_data/**/*.json` | `documents` | 相对于 `user_data` 的完整路径，例如 `models/demo/backtest_detail.json` |
| `bot/user_data/**/*.jsonl` | `events` | 完整相对路径及事件顺序 |
| `models/**/*.feather` 预测结果 | `events` + `artifacts` | 完整相对路径；原预测字段、ISO 日期及文件元信息 |
| `backtest_results/*.zip` 内非配置 JSON | `documents` + `artifacts` | `backtest_results/archive.zip/member.json` |
| `bot/user_data/**/*.csv` 回测表格 | `events` + `artifacts` | 完整相对路径、原字段及数值 |
| `data/binance/**/*.feather` OHLCV | `candles` | Binance、现货/合约、交易对、周期、spot/futures/mark/index |
| `data/binance/futures/*-funding_rate.feather` | `series_points` | 交易所、`funding_rate`、合约交易对，周期保留在 `extras` |
| `data/okx/futures/*.feather` | `candles` | OKX、合约及周期 |
| `data/merged/*.feather` | `candles` | `exchange=merged`，与原交易所数据隔离 |
| `data/orderflow/*_orderflow.feather` | `candles` | Binance 合约、orderflow；额外订单流字段保留在 `extras` |
| `data/orderflow/*_oi.feather` | `series_points` | `open_interest` 和 `open_interest_value`，保留周期 |
| `newsrc/funding_binance.pkl`、`funding_okx.pkl` | `series_points` | 交易所、`funding_rate_archive`、合约交易对，独立于 Freqtrade 下载的费率数据 |
| `newsrc/dvol_btc.pkl`、`dvol_eth.pkl` | `series_points` | Deribit、`dvol`、BTC/ETH |
| `bot/*.sqlite` | `external_records` | 源数据库文件、trades/orders/wallet_history/pairlocks/KeyValueStore/trade_custom_data 等固定白名单表 |
| `auth/users.json` | `users` | 一次迁移原有用户及密码哈希，数据库已有用户时不覆盖 |
| models/freqaimodels/cache/backtest_results/hyperopt_results 内大型文件 | `artifacts` | 文件路径、字节数、修改时间；NumPy/Parquet 添加形状、类型或表结构元信息 |

市场行情和辅助序列的时间统一为 UTC 毫秒。无时区的历史数据按其下载脚本约定解释为 UTC。非法时间、非有限 OHLCV、负成交量及重复时间戳不作为有效 K 线导入；资金费率允许为负。资金费率 Feather 支持 `date/funding_rate` 两列格式，也兼容旧 OHLCV 格式并取 `close` 数值。`newsrc` 的费率 PKL 保留原始八小时采样，使用独立指标 `funding_rate_archive` 防止覆盖行情目录中的费率数据。重复时间保留文件中最后一条，导入行数按清洗后的唯一主键计算。

配置、private/secrets 目录、smoke 测试产物及符号链接不参与同步。普通文档中的凭据字段递归剔除；日志及错误状态只记录异常类型。pickle 仅允许项目 `newsrc` 固定目录内四个已知文件，不读取任意外部 pickle。模型权重和训练缓存保留原文件，数据库保存可追溯索引，避免复制大型张量影响训练。结构化预测结果实存事件表，不作为市场行情混入 K 线。ZIP 在内存解析，拒绝绝对路径、目录穿越和超过 128 MiB 的单个 JSON 成员。

## 增量与恢复

同步进程在 `database/runtime/data_sync.lock` 持有操作系统独占锁，阻止同一项目两个同步进程互相覆盖状态。锁在正常退出或崩溃后自动释放。

每次同步先更新 JSON/JSONL、用户和交易快照。守护模式默认每 5 秒更新这些快速来源，每 60 秒增量检查行情、辅助序列及大型文件索引。初次历史行情导入按 5,000 行一批提交，并每 5 秒服务一次快速来源。`--interval`、`--scan-interval` 可调整周期。

`sources` 记录最后成功的文件修改时间、字节数和导入行数。读到半条 JSONL、文件同步期间变化或数据库批次失败时，保留最后成功指纹并标记错误；下一轮重试。行情及序列写入使用主键更新，已完成批次重复执行不会增加重复行。JSONL 全部解析成功后才原子替换事件。

SQLite 使用 `mode=ro` 和 `query_only` 连接，在短读事务中获得所有表的一致快照，释放 SQLite 后才写 PostgreSQL。单次读取默认最多 3 秒，锁等待最多 300 毫秒；不写回原库、不修改 Freqtrade 表结构。增量指纹同时检查主文件和 WAL 文件。

## 验证

```bash
uv run python -m unittest tests.test_data_sync -v
QUANT_TEST_DATABASE_URL='postgresql://...' uv run python -m unittest tests.test_data_sync -v
```

单元测试覆盖来源排除、JSONL 半写恢复、市场隔离、UTC 时间、订单流扩展字段、合法数值、准确批次行数、幂等导入、WAL 增量快照、原密码哈希保留和进程锁。提供测试连接串时，额外创建随机 PostgreSQL schema，执行真实导入、查询和重复导入后清理该 schema。
