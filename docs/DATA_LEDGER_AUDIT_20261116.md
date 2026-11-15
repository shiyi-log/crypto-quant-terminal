# 数据完整性与研究账本实施核查（2026-11-16）

对应开发计划：`docs/DEVELOPMENT_PLAN_20261116.md`。本核查只读取源码和已有运行状态，没有执行测试、连接生产数据库、修改门禁或启动/重启服务。运行状态为读取时快照，不证明当前数据库完整。

## 数据检查与采集

| 当前事实 | 源码证据 | 下一步 |
|---|---|---|
| 完整性检查把所有匹配历史行一次读到内存，一个 repeatable-read 事务、一个 120 秒查询；无时间窗口和单数据集隔离 | `tools/check_candle_integrity.py:534-578` | 增加数据集清单及持续/冻结角色；实时检查限定最近窗口，历史检查独立分批。按完整身份逐个查询，单个超时只使该数据集失败并继续其余检查。 |
| 超时后 watch 状态写 `dataset_count=0` 和异常类型，完整 report 文件仍停留在上次成功查询的结果 | `tools/check_candle_integrity.py:630-658, 675-683` | 每轮状态保存 started/finished/last_success、成功和失败数据集数、各数据集错误、check window；失败轮也原子发布完整报告，保留上次成功时间。 |
| 当前管理命令指定 20 币 × 5m/15m/1h/1d；查询包含全部 candle_type，字段和新鲜度要求只有全局一套 | `manage.py:77-85, 176-188`; `tools/check_candle_integrity.py:541-551, 644-651` | 在数据集配置中明确 futures/mark/orderflow、原生/重采样、持续/冻结、所需字段和收盘延迟；标记价不能要求成交量订单流字段，冻结历史不接受实时过期判据。保留所有真实历史缺口。 |
| 审核尾部直接用最后一行 timestamp+interval 当作最后收盘，没有过滤 `extras.closed=false` 或未来尚未收盘行 | `tools/check_candle_integrity.py:213-249` | 分开报告 latest_observed 和 latest_final_closed；以交易所收盘边界和 closed 标识核验实时尾部，不把盘中快照当作最终收盘。 |
| 状态文件写入直接截断，读者可能遇到半份 JSON | `tools/check_candle_integrity.py:593-598` | 同目录临时文件、flush/fsync、原子 replace；测试中模拟读取和写入失败，旧状态仍可读。 |
| 采集器已支持原生周期、持久化 spool、重试、确定性失败 dead-letter 和故障停止，不应重做这一套 | `manage.py:84-85`; `market_stream.py:1105-1181, 1294-1320` | 将已有结构化状态接到 API 和页面；补独立缺口补取 worker，使用既有备份/只补缺失工具，不能在收包循环内补历史。 |
| 重连明确标记盘口可能缺口，并保持未确认批次；不存在靠重连自动补齐盘口的保证 | `market_stream.py:1370-1384` | K 线可按交易所范围补取，逐笔/盘口区间必须保留不可恢复原因、起止和影响范围。 |
| 文件同步 completed 只表示来源文件扫描/导入成功，不代表数据新鲜或完整 | `data_sync.py:707-750` | API 分别提供 source import、live collection、archive、integrity 的状态与时间，禁止共用一个“全部健康”标签。 |

已有状态快照：`database/runtime/candle_integrity_status.json` 生成于 **2026-11-16 02:08:10 +08:00**，cycles=106、dataset_count=129、error_count=778、ok=false、last_error=null。报告包含过期、字段不足、缺口和空数据集；这些数量混合多种用途数据集，不能直接解释成 778 个新采集故障。`logs/integrity.log` 当时约 29 MB，日志轮转工作可直接利用这一实物基线。

## API 和运维页面

- `auth_service.py:1877-1878` 的 `/api/locals/ops` 只代理 `ops_status.json`。该文件由 `bot/monitor.py:240-259` 生成，内容是策略、波动率、因子和重估信号，不含完整性/采集/外盘状态。
- `vben/apps/web-antd/src/views/quant/ops/index.vue:44-60` 只加载 getOps 和旧巡检 getAutoIterate；新运行健康模块应独立加载，保留每个数据源的失败状态和上次成功时间。
- `tools/ops_sentinel_check.py` 是顶层执行的打印脚本，没有机器可读结果与基于异常的退出码协议。改为可导入函数 + main，JSON 包含 failed checks；先用离线状态夹具测试，再按只读模式连接数据库。

## 研究账本

数据库源码 `database/schema.sql:101-110` 创建研究事件主键和 sequence；研究账本没有 UPDATE/DELETE/TRUNCATE 保护触发器。`strategy_sim_events` 的拒绝修改触发器已在 `database/schema.sql:261-268`，可借鉴，但必须单独覆盖 TRUNCATE。此结论来自源码，尚未查询生产目录验证实际 DDL。

- `research_ledger.py:67-86` 普通事件的 INSERT ON CONFLICT 后比较 type 和 canonical payload，实现应用层幂等；未比较 source_id，需要明确来源是否属于身份。
- `research_ledger.py:420-470` 前向事件采用生产者稳定 ID，冲突比较 type/payload/source。不能把所有 event_id 当作内容摘要；新增内容哈希应使用独立、版本明确的字段，保留现有稳定 ID。
- schema 新建表使用 `sequence_id BIGSERIAL UNIQUE`，旧表 ADD COLUMN 路径未显式补 unique/NOT NULL。先读查 NULL、重复、max 和 sequence 当前值，再决定迁移，不重排旧事件。BIGSERIAL 允许缺号且不保证并发提交可见顺序；增量游标不能假定连续号。
- schema 由 `DataStore.initialize` 执行；改 schema 文件不等于生产迁移已经生效。测试和上线记录要区分源码、隔离库证据、生产目录证据。

最小数据库验收采用 `tests/test_trading_audit.py:251` 的独立 schema 模式，覆盖直接 UPDATE payload/source/sequence、DELETE、TRUNCATE 被拒，合法 INSERT 成功，重试幂等且冲突拒绝。`tests/test_research_ledger.py:13` 的 FakeConnection 不能证明数据库层保护。

## 建议实施顺序

1. 先写离线数据集策略、已收盘尾部、单数据集超时/继续、原子报告回归测试；新模块与现有纯计算 `inspect_dataset` 分离，避免继续扩大 754 行脚本。
2. 加只读运行健康聚合，分别展示采集、同步、归档、完整性和外盘；与回测/迭代 API 改动协同，避免同时编辑 auth_service.py。
3. 在隔离 PostgreSQL 写研究账本防改写测试；随后新增可幂等 DDL 和显式迁移核查，不改旧事件。
4. 生产先小范围运行新只读窗口检查，记录单轮耗时，再扩大到活动清单；24 小时连续验收不能用一轮测试代替。
5. 缺口补取、数据库迁移和恢复演练在备份验证后单独执行；盘口/逐笔无法重建的区间保持明确缺口。

本报告不宣称上述改造、生产迁移、24 小时检查或七天观察已经完成。
