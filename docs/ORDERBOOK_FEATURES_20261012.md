# 订单簿低频前向特征：orderbook-hourly-v1

本管道把 Binance 永续 20 档快照转换成闭合 1h 特征，支持 BTC、ETH、TRX。
它不创建交易账户、不生成订单、不回放历史交易。前向账户把特征用作 K 线入场过滤器，
与无过滤器账户比较；特征的存在、方向一致率或质量合格均不代表策略已盈利。

实现文件：`orderbook_features.py`、`orderbook_feature_service.py`。
冻结方法版本为 `orderbook-hourly-v1`。方法和质量阈值都随证据落盘。

## 数学定义

单条快照必须至少有双边各 20 档；每档价格、数量有限且严格为正；
bid 按价格严格递减，ask 严格递增，且最高 bid 严格小于最低 ask。
数量为零、盘口交叉/锁定、重复价格、档数不足和非有限值均被过滤并记录理由。

- `obi_1`、`obi_5`、`obi_20`：各前 N 档数量和的 `(bid − ask)/(bid + ask)`。
- `relative_spread`：`(ask1 − bid1)/mid`，`mid = (bid1 + ask1)/2`。
- `depth_slope_bid`、`depth_slope_ask`：20 档累计数量占本侧总量的比例，
  对各档价格距 mid 的相对距离做带截距 OLS，取斜率。量、价格都做归一化，
  斜率越大表示在单位相对价差内累积的深度份额越快；它不是绝对流动性规模。
- 小时均值：每条快照保持到下一条有效快照，单次保持最多 3 秒。
  按有效保持毫秒积分后除以总覆盖毫秒；长中断不会由旧报价伪装成完整覆盖。
  所有字段同时记录 `*_time_mean` 和 `*_last`。
- `lagged_obi_mid_directional_consistency`：在 UTC 对齐的 1 秒网格采样新鲜快照，
  比较前一秒 `obi_5` 符号与随后一秒 mid 变化符号的一致比例。
  OBI 或价格变化为零不计入分母；有效比较少于 30 时为 `null`。
  同一毫秒多个序列取最高序列状态，不会创造额外持有时长。

方向一致率只是一种逆向选择审计代理。它不是独立交易样本、可成交收益或成本后的 alpha。
10 Hz 快照数量不能当作策略的 `N_min`；每条记录明确标注
`independent_research_samples = 0`，并同时记录 1 秒比较数。

## 因果边界与数据质量

小时以 UTC K 线开盘毫秒 `candle_ms` 标记，`cutoff_ms = candle_ms + 3,600,000`。
输入同时满足：

1. 交易所事件时间在 `[candle_ms, cutoff_ms)`；恰好 cutoff 的快照属于下一小时。
2. PostgreSQL `received_at` 不晚于 cutoff，保留微秒精度，不向下取整。
3. 若有采集器 `extras.received_timestamp`，它也不得晚于 cutoff；
   没有此字段时由数据库接收时间提供最低可用性证据。

交换所事件与本机接收时钟分别截断，不假设两时钟同步，亦不强加
`event_timestamp <= local_receive_timestamp`。
记录正的事件/接收时差计数及最大值；该量混合了时钟偏差与传输延迟，不能当作纯网络延迟。
即使某条事后补写数据的事件时间在小时内，只要数据库接收晚于 cutoff，也不会进入该小时特征。

质量须同时满足以下冻结条件，才有 `quality.eligible = true`：

| 字段 | 条件 |
| --- | --- |
| `valid_snapshots` | 至少 100 个不同事件毫秒的有效状态 |
| `coverage_ratio` | 有效保持时长 / 3600 秒至少 0.95 |
| `minute_bins` | 有效保持时长覆盖至少 55 个不同 UTC 分钟 |
| `largest_gap_ms` | 小时边缘及相邻有效状态最大间隔不超过 30,000 ms |
| `age_at_close_ms` | 最后有效快照距收盘不超过 3,000 ms |
| `query_truncated` | 必须为 false |

缺失、断流、陈旧或截断统一返回 `status = unknown`，并保留拒绝原因、
覆盖秒数、分钟编号、最长间隔、有效快照数和末时刻年龄。
诊断均值可以非空，但未知质量不能用于放行新入场。
已有持仓的离场不能因缺少订单簿特征被阻止。

## 不可覆盖的发表规则

进程启动时向上取整到下一完整小时开盘，作为 `forward_start_ms`。
启动前和已经部分经过的小时可做只读数学预览，不能成为前向账户输入。
例如 17:55 UTC 启动，首个前向小时为 18:00–19:00 UTC，最早 19:00:15 发表。
进程重启重新设边界，不补发停机期间的历史决策输入。

每小时收盘后等 15 秒让本地批次提交，然后计算；发表必须在 cutoff 后 90 秒以内完成。
所有输入依然在 cutoff 截断，15 秒等待不会读取收盘后的盘口来替代收盘信息。
这是闭合 K 线实验室的延后观察代理，不能称为该毫秒真的成交。
缺失和迟到时前向账户须拒绝新入场，不得通过延长发表窗口伪装及时输入。

使用现有 `documents` 表，不迁移 schema。证据键如下：

```text
orderbook_features/orderbook-hourly-v1/binance/futures/BTC_USDT/1h/{candle_ms}/{source_hash}.json
orderbook_features/orderbook-hourly-v1/binance/futures/BTC_USDT/1h/{candle_ms}/accepted.json
```

两键在同一事务以 `INSERT ... ON CONFLICT DO NOTHING` 写入。
`accepted.json` 只指向第一个发表的证据，包含未知质量时也不允许事后覆盖。
源行、方法、截止时间和查询截断状态参与 SHA256；之后修订产生新 hash 证据，
不改变已经接受的小时输入。方法语义修订必须新 method version。

读取接口为：

```python
from orderbook_features import load_published_feature

record = load_published_feature(store, "BTC/USDT:USDT", candle_ms)
# record is None when the immutable selector is absent or metadata inconsistent.
# Gate requires both quality.eligible and forward_eligible, exact pair/hour/method,
# and published_at_ms <= evaluated_at_ms <= the account's allowed observation window.
```

记录另含 `feature_key`、`method_spec`、`observed_at_ms`、`published_at_ms`、
`received_cutoff_ms`、`source_hash`、首末事件毫秒与首末 update ID。
读取时重验小时/市场/方法/forward 边界、推导键及发表时间的一致性，
不会信任单独一个 `forward_eligible` 布尔值。

## 运行和资源边界

```bash
# 默认只读：PostgreSQL 强制 read-only 事务，不执行 DataStore 初始化 DDL。
uv run python orderbook_feature_service.py --once

# 仅研究一个已闭合小时，同样只读。
uv run python orderbook_feature_service.py --once --bar-open-ms 1791734400000

# 持续前向发表，独立于 BTC/ETH 和 TRX 的采集器。
uv run python orderbook_feature_service.py --watch --apply \
  --pairs BTC/USDT:USDT ETH/USDT:USDT TRX/USDT:USDT \
  --status-path database/runtime/orderbook_feature_status.json \
  --lock-path database/runtime/orderbook_features.lock
```

`--once --apply` 不把启动前的最近小时发布成前向输入，返回不发表原因。
`--bar-open-ms` 不能与 `--apply` 或 `--watch` 同用，防止人工构造历史发表路径。

每 15 秒轮询，45 秒状态心跳；每个小时每币最多查询一次，
迟过 90 秒窗口只等下一小时，不遍历停机历史。
查询限定 `exchange/market/pair/timestamp` 的单小时半开范围，
利用现有 `orderbooks_pair_time` 索引；默认最多每币 150,000 行，超额质量 fail closed，
命令行上限 500,000 行。最多三币，不扫整张约四百万行表。

状态文档为 `orderbook_feature_status.json`，本地状态文件原子替换；
独立 OS 锁的 inode 不删除，与主 collector 的锁或 spool 不共享。
服务自身不更新任何采集器状态文档，不控制行情连接。

## 2026-10-12 实际只读样例与验证

观察时刻 2026-10-11 17:56:39 UTC，最新完整小时 16:00–17:00 UTC。
三币只读计算耗时 5.713 秒；没有生产写入和历史决策发表。

| 币 | 有效快照 | 覆盖秒 / 比例 | 分钟 | 最大间隔 | 尾部年龄 | OBI5 时间均值 | 相对价差均值 | 质量 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| BTC | 34,789 | 3599.933 / 99.9981% | 60 | 396 ms | 20 ms | -0.03118 | 0.00000119977 | ready |
| ETH | 34,843 | 3599.999 / 99.99997% | 60 | 247 ms | 37 ms | -0.03244 | 0.00000394988 | ready |
| TRX | 0 | 0 / 0% | 0 | 3,600,000 ms | 3,600,000 ms | null | null | unknown |

TRX 侧车在 17:00 UTC 后才开始采集，该历史小时未知符合实际覆盖。
BTC/ETH 的原接收时钟较事件时间中位慢约 2.05 秒，最大正差 2066 ms，已审计记录。
BTC/ETH 的 1 秒方向比较分别为 871/1406；一致率 0.8071/0.7560 仅是诊断，
没有交易、成本、独立收益样本，也不能用于通过盈利裁定。

28 项回归测试通过，包括独立 `quant_test` 数据库的事务发表/幂等/修订不覆盖测试；
Ruff 通过。独立只读复核已确认微秒 cutoff、历史 eligibility 伪造、
非法序列三类反例被拒绝，同时核对小时索引查询、三币界线、时间权重和断流门控。
