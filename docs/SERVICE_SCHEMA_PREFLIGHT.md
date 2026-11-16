# 服务启动前的只读结构检查

普通服务启动只核对已安装的 PostgreSQL 全局迁移，结构安装、升级和无版本旧库接管使用独立的 `manage.py db migrate` 运维命令。`service_supervision.py` 不调用 `migrate_schema`、业务 `initialize` 或任何 DDL 修复入口。

## 依赖范围

以下管理服务在启动或自动补启前需要 PostgreSQL 结构检查：

| 服务 | 依赖原因 |
| --- | --- |
| auth | 登录、账户、业务 API 的统一 DataStore |
| sync | 文件事实入库 |
| market、market-trx、market-spot、market-breadth | 逐笔、盘口和 K 线持久化 |
| market-live | 标准入口注入 CandleArchiveWriter，收到 K 线后归档到 DataStore |
| strategy-lab | 纸面账户和策略事实持久化 |
| spot-basis | 原生 PostgreSQL 查询及状态/行情写入 |
| integrity | 原生 PostgreSQL 完整性查询和报告 |

api、webserver 使用各自的旧 Freqtrade 存储；web 提供静态文件并代理 HTTP，三个服务自身不连接本项目 PostgreSQL。它们不会因为应用库迁移状态而被单独阻止。一个显式 start/restart 批次包含数据库服务时，整个批次先通过预检，再启动任何子进程。

新增管理服务必须明确加入依赖或独立服务集合，缺少分类时以 `service_dependency_unclassified` 拒绝启动。不要把新服务默认当作不需要数据库。

数据库进程的 `manage.py db start` 是独立分支，不经过应用服务生命周期；启动数据库本身不要求应用迁移已经安装。应用预检先调用 `ensure_local_postgres(root)`，只保证既有规则允许管理的本项目数据库进程可用，再调用 `DataStore(root=root).check_schema()`。它不初始化集群，不安装结构，不更改数据库地址，也不管理其它项目数据库。

## 生命周期与失败行为

start/restart 在生命周期文件锁内先验证所选服务的依赖分类。需要数据库时，先确保既有 PostgreSQL 进程，再执行只读结构检查，严格要求返回 `status=compatible`。检查失败、缺少结果、未迁移、漂移或数据库不可达均以固定公共错误码 `service_schema_preflight_failed` 结束。可用 `manage.py db check` 独立获得迁移模块提供的安全诊断，再按运维流程显式迁移。

预检在写入 running 意图之前执行，因此一个失败的 start 不会把之前明确暂停的服务变成未来自动补启目标。restart 也在停止现有子进程之前检查，失败时现有服务与其意图文件保持原状。预检成功后的 restart 继续使用原有逆序停止、正序启动规则；停止失败仍保留 stopped 意图并拒绝替换。stop 完全不依赖数据库可达性。

监督器只为“意图 running 且当前缺失”的数据库服务检查结构，每轮最多执行一次。暂停服务及已经运行的服务不会触发检查，也不会因检查失败而被停止。检查失败时，该轮所有缺失的数据库服务都保留停止状态，快照及轮转日志记录固定公共错误码；独立服务仍可恢复。下一轮重新检查，不缓存成功结果跨越监督轮次。

错误输出不包含原始 PostgreSQL 异常、连接串或凭据。DataStore 使用临时运维连接执行 SELECT-only 检查，预检结束总是关闭自己的资源，不创建常驻业务连接池。模块没有环境变量、CLI 参数或依据运行目录跳过预检的生产开关；离线测试在外部 DataStore 边界替换连接，既有 manage 单测可替换 `service_supervision.check_service_schema`。

## 验证边界

`tests/test_service_schema_preflight.py` 在临时运行目录中运行真实生命周期、服务意图文件和监督器快照逻辑，仅替换数据库连接及子进程外部边界。覆盖各服务的依赖分类、PostgreSQL/检查/启动顺序、失败时零子进程、restart 保留现有进程、暂停意图保留、stop 无数据库依赖、未知服务拒绝、只读调用、检查结果损坏、监督器逐轮检查和错误脱敏。

这些测试不连接生产数据库，不执行迁移，不启动或停止真实服务。它们验证启动结构门禁，不证明 Demo 账户、原生保护、策略批准或资金阶段获得交易资格。直接独立执行业务脚本时，仍依靠各自 DataStore 初始化的只读校验；本模块只覆盖管理器的生命周期和自动补启路径。
