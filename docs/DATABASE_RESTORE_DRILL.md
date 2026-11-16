# 数据库业务指纹与隔离恢复演练

本工具用于证明某一份已校验归档能恢复出同一份业务事实，并生成可审计回执。
它不启动、停止或迁移生产数据库，不发起交易，不自动清理备份或演练数据库。
实际演练完成前，归档回执和 pg_restore 成功均不能当作业务恢复通过。

## 接口与事实覆盖

使用项目 Python 从仓库根目录执行模块：

    ./.venv/bin/python -m tools.database_restore_verification --help

连接仅从环境读取：源库使用 QUANT_DATABASE_URL，隔离恢复库使用
QUANT_RESTORE_DATABASE_URL；连接串和密码不能放进命令参数。工具不自动读取
.env，调用者应使用现有可信运行环境加载过程，不打印环境内容。

capture_business_manifest(conn, schema) 读取指定 schema 的全部普通表、分区表与
物化视图，包括已存在的订单、成交、资金流水/证据、风险事件/决策/预占、研究
事件、策略账户、冻结定义和绑定，以及 documents 内的历史业务文档。尚未
存在的可选表不会虚构成空表；实际表的完整集合进入清单，所以空表丢失也失败。
外部表不在 pg_dump 的本地业务数据范围，工具明确拒绝。

逐列在 PostgreSQL 内转为 text 后构造有空值边界的 JSON 数组；单行 SHA-256
排序后逐项汇总。排序多集保留重复行数量，不依赖主键，不因物理行顺序变化
误报。numeric、jsonb 精确数值没有经过客户端 float；json 列原文中的空格、键序
和重复键保持在哈希中。固定 UTC、ISO 日期、interval、浮点和 bytea 输出格式。
时间戳微秒和 Decimal 末位变化都会改变摘要。客户端仅流式接收摘要，原始行不
进入文件、日志或回执。清单仍是敏感审计产物，应保持 0600 权限、可信存储。

每张表包括行数、摘要、列名/类型/可空性/identity/generated/default 摘要、
冻结身份列摘要和 append sequence 的 min/max/distinct 观测。表结构、约束、
索引、触发器及其函数体、例程、视图和 RLS policy 计入 catalog 摘要。分区键、
边界、继承父表、列 collation、schema 内 collation 与 enum/domain 定义同样参与
catalog 指纹，空分区的路由语义和空表的文本排序规则改变也会失败。函数体和
默认值本身不写入清单。序列 last_value/is_called/参数也单独比对。ACL、owner、
数据库角色与完整集群环境不在本次业务恢复通过范围；已有恢复命令使用
--no-owner --no-privileges，演练库不得成为生产流量入口。

## 快照与序列边界

backup_with_business_manifest(dsn, destination, schema=..., container=...) 新建
0700 备份目录，打开只读 REPEATABLE READ 事务，导出 pg_export_snapshot，
在同一快照生成业务清单，把 --snapshot=<id> 传给 pg_dump 并保持导出事务直到
命令结束。归档 magic、pg_restore --list 和 SHA-256 成功后才发布 manifest.json。
原 tools/database_backup.py 的恢复入口可直接消费这份兼容归档格式。

MVCC 表行与 catalog 绑定于导出快照。PostgreSQL sequence 不受 MVCC 保护：
工具对清单中的序列状态在 pg_dump 前后观测，任何漂移直接拒绝发布成功清单。
前后相等只能检测普通推进，不能证明中间没有 setval 回绕或 cycle 循环；清单
明确标记 stable_observation_restore_comparison_required，而不宣称全局线性一致。
最终业务通过还必须逐项匹配隔离恢复后的序列状态。因此有并发写入时可能需要
重试；需要完整非 MVCC 静止证明的演练应在调用方已有维护窗口内执行，工具
本身不会停服务或锁写入。不要为得到绿色结果忽略序列差异。

## CLI 操作顺序

所有目录/输出文件必须新建，不覆盖已有备份、回执或数据库。示例路径是占位
路径，实际路径由运维任务确定，日志可重定向至可信审计目录。

    ./.venv/bin/python -m tools.database_restore_verification backup \
      --schema quant --backup-dir /trusted/new-backup \
      --output /trusted/new-backup-operation.json

    ./.venv/bin/python -m tools.database_restore_verification restore \
      --backup-dir /trusted/new-backup --output /trusted/new-restore-receipt.json

    ./.venv/bin/python -m tools.database_restore_verification verify \
      --schema quant --backup-dir /trusted/new-backup \
      --receipt /trusted/new-restore-receipt.json --output /trusted/new-verification.json

restore 复用原工具：只允许明确本机 host/hostaddr 和 quant_restore_* 名称，
先 createdb，目标已经存在就失败；不执行 --clean，不覆盖、不删除。verify 同样
限制隔离库；公开 Python API 至少强制实际连接的库名为 quant_restore_*。

capture 只读导出当前业务清单，可用于诊断；它单独运行时不证明任何已有归档
与该清单共享快照，不能代替 backup 产生的 business-manifest.json。

## 迁移校验与首次接管

默认 verify 要求 global_schema_migrations 对代码中的冻结 SQL/checksum/物理
基线及不可变历史保护均兼容；存在 Demo 表时也要求 Demo 历史无未知版本、
checksum 变化、空洞或待迁移。Demo 的已应用连续前缀还必须匹配代码内冻结
物理摘要，包括基线订单/成交表、各迁移创建的表列、约束、索引、序列归属、
ALWAYS 不可变触发器及其逐字函数体。仅有版本历史或保护已禁用不能通过。
这些锚点由独立 quant_test 随机空 schema 执行原始 DDL 后生成，运行时只读
比对，不在源库执行 DDL；新增迁移若尚无物理锚点直接拒绝，不能自动信任。
不检查通过绝不生成 business_validation=passed。
恢复后的约束必须已验证，索引必须 valid/ready。行数据已由 pg_restore 成功
执行约束安装，工具只读检查 catalog，绝不在生产执行 ALTER/修复。

首次将旧库纳入全局版本管理时，旧归档可能缺 global_schema_migrations，直接
要求版本当前会与 adopt 所需恢复凭据互相依赖。仅该运维场景可显式添加
--allow-legacy-baseline：仍要求源/恢复业务精确相等、约束/索引通过、冻结物理
基线完全兼容；全局缺版本表记录为 legacy_baseline_compatible_adoption_pending。
Demo 仅允许代码已知 checksum 的连续已应用前缀，待迁移明确记为
known_prefix_pending_explicit_migration。未知、空洞、改 checksum 或物理基线
不兼容始终拒绝。该回执证明旧事实可恢复和接管前兼容性，不证明新迁移已经
执行。接管和 Demo 显式迁移后，应再运行默认严格校验及正常启动检查。

verify 只能消费调用者掌握来源的本地归档 manifest、业务 manifest 和 restore
receipt。工具校验这些文件的哈希关联并拒绝文件 symlink，但不会认证一个能
同时篡改所有可信文件的操作者；不能把 HTTP 客户端自报 passed 当作可信凭据。
验证输出仅取白名单字段，不复制输入回执中的 DSN、任意元数据或原始行。
输出含 archive_sha256、business_manifest_sha256、catalog_sha256、行表计数、
迁移模式与序列状态，并明确 verified_namespace 和 coverage=scoped_schema：
pg_dump 归档可能包含其它 schema，但本次通过只覆盖指定 schema。必须保存
归档和所有回执，并在开发日志引用哈希。

## 验证与当前边界

新测试仅使用 quant_test 中随机命名 schema，清理自己的 schema；外部
pg_dump 边界使用替身，真实 PostgreSQL 连接证明导出快照导入和并发隔离。
没有自动执行生产备份、创建实际演练库或停止任何服务。实际演练由总任务在
检查空间、运行态与备份目标后执行并记录，不能用单元测试替代真实恢复结果。

## 2026-11-17 隔离合成业务演练回执

本次实际运行 pg_dump、createdb、pg_restore 与业务验证，源库和目标库均为
新建 quant_restore_*。源库只包含合成订单、成交、资金流水、研究事件和冻结
策略身份；没有备份、迁移或恢复 quant_terminal。测试源和目标均保留，不自动
删除。它证明当前工具与迁移包可完成隔离恢复，不证明生产业务库已经演练。

- 源库：quant_restore_source_20261116_192413_758603。
- 恢复库：quant_restore_checked_20261116_192413_758603。
- 审计目录：/Volumes/yidong/quant-refactor-backups/isolated-drill-20261116_192413_758603。
- 归档 SHA-256：f4a3a8604eb2bf9579b3169131658d4ad0862fda70f77ae50ffdf49d973d9b65。
- 业务清单 SHA-256：2a2679f3068af6164e771d941ab34b495531ce03f71c6ff5ba74a02653e65fec。
- 验证范围：quant schema，47 张表、17 行；业务指纹、catalog、约束与索引、
  全局/Demo 迁移以及序列状态均匹配；business_validation=passed。
- 结果：business-verification.json；原始归档、manifest.json、业务清单与
  restore-receipt.json 同目录保留。命名时间使用 UTC，本地日志为北京时间。

演练首轮发现真实故障：归档头检查使用 BufferedReader 后，即使 seek(0)，
底层文件描述符可能因预读仍指向后方；pg_restore 继承描述符后读不到文件头。
独立真实 Python 子进程回归先失败，改为归档头检查使用无缓冲二进制读取后
通过，再完成上述真实演练。没有采用自动重试或忽略归档校验绕过问题。
相邻备份/恢复校验定向 43 项通过；该结果与实际演练回执分别记录。

## 2026-11-17 非空研究注册表恢复回执

前次演练中的八张研究注册表均为空。本次通过正式存储接口新增冻结版本、
12 配置实验族、base/stress 尝试及事件、一次留出 claim、两份产物和候选裁定，
再次实际执行共享快照备份、新库恢复与全业务指纹验证，补足非空数据覆盖。

- 源库：quant_restore_research_source_20261116_200220_126353。
- 恢复库：quant_restore_research_checked_20261116_200220_126353。
- 审计目录：/Volumes/yidong/quant-refactor-backups/nonempty-research-drill-20261116_200220_126353。
- 实际完成时间：2026-11-16T20:02:28Z（北京时间 2026-11-17 04:02）。
- quant schema 共 47 表、34 行；研究八表均非空，共 17 行。业务内容、catalog、
  约束、索引与序列精确匹配，全局/Demo 迁移均为 current。
- 归档 SHA-256：e04f12a696c77dbcffaecb84a43cfa138b976750d334fa873ce8ab650681e856。
- 业务清单 SHA-256：7583d483595bcd00010dbc89842b3d60aedee8acc718aa2545b7b398238017b2。
- 研究语义回执 SHA-256：2fb564248b7fff0027b5a860aa3fae95879cde1e240b07891c303290aeaadff0。

恢复后的 family、两条 completed attempt 及各自事件、claim、产物和裁定与源端
相同；留出仍为 completed，同 claim 精确重试 execution_ticket=False，未重新
出票。八表逐一 UPDATE/DELETE 加一次联合八表 TRUNCATE，共 17 个事务探针均
得到 SQLSTATE 55000 并回滚；catalog 另确认八表不可变触发器为 ALWAYS。联合
TRUNCATE 只证明整个语句被拒绝，不声称逐表独立执行了截断触发器。探针后再次
比对完整业务清单，行内容、catalog 与序列保持相同。

原始归档、全部回执、research-semantic-verification.json、演练脚本与中文日志
均以 0600 保留；目录 0700，源库与恢复库均保留。独立审阅和主任务均重新计算
关键工件摘要并核对回执链接。该结果是合成业务恢复证据，未备份或恢复生产库，
不证明真实研究数据来源、收益、全族统计或自然 30 天验收通过；所有来源、
验收和生产授权标志继续为 false。
