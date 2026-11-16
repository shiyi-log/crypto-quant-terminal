# 全局数据库显式迁移

业务服务建立连接时只检查已安装的全局迁移版本、校验和、历史保护和冻结对象结构。检查在只读事务中进行；缺少版本、待迁移或结构漂移会阻止启动，并关闭本次新建的连接池。初始化不执行建表、迁移锁或历史订单流事实导入。

连接配置来自私有环境中的 QUANT_DATABASE_URL。不要把 DSN 放入命令行或报告。以下命令均从项目目录运行：

    .venv/bin/python manage.py db check
    .venv/bin/python manage.py db migrate
    .venv/bin/python manage.py db check

db check 使用独立数据库连接与只读事务，输出兼容版本和结构摘要。它可以在业务连接池尚未初始化时诊断，且不会安装缺失结构。db migrate 使用独立连接和事务，显式取得迁移锁。新 schema 安装冻结基线；失败回滚本次事务，成功提交后才返回回执。它不会启动、重启或恢复应用服务，也不会启动 PostgreSQL。

db init 保留创建本地 PostgreSQL 集群、角色和数据库的原有用途。它不安装应用 schema；新集群随后仍需显式运行 db migrate。db status 保留运行状态及统计读取用途，缺版本时不会顺便安装 schema。已停止的受管集群只报告停止状态。

## 接管已有无版本 schema

存在业务表而缺少全局迁移历史时，普通迁移拒绝执行。先完成归档验证与独立隔离恢复的业务验收，再将可信的本地 JSON 回执交给命令：

    .venv/bin/python manage.py db migrate --adopt --backup-receipt /absolute/path/verified-restore.json
    .venv/bin/python manage.py db check

CLI 在连接数据库前读取回执，要求：

- business_validation 为字符串 "passed"；布尔值、"pending" 或仅有恢复成功状态均不合格。
- archive_sha256 为 64 位小写十六进制 SHA-256。
- reference 为 1–256 个 ASCII 字母、数字或 _.:/@- 字符，可指向独立恢复验证记录。

回执作为受信任运维文件输入，不是 HTTP 认证或用户自报的通过标志。旧备份工具的 business_validation="pending" 回执不能用于接管。CLI 仅把这三个审计字段传入迁移器；文件错误、非法 JSON、超出 1 MiB 的文件或缺失字段均被拒绝，错误输出不带文件内容。迁移器还会检查实际物理结构；合格回执不会授权自动修复不兼容结构。接管不会重新执行冻结基线覆盖存量对象。

## 历史订单流导入与独立迁移域

历史订单流导入仅作为显式全局迁移的事务内回调执行，保留旧表，依靠导入标记保持幂等。它不会在普通连接或状态读取时执行。业务临时暂存表仍由业务写入事务管理，不属于启动迁移。

当前全局迁移器仅安装或接管第一版冻结基线；未提供通用旧结构修复或未来版本升级循环。Demo 迁移仍由独立迁移域管理，不能改写既有 Demo 迁移文件或把它们并入全局基线。迁移历史与冻结对象不匹配时应保留失败证据、调查差异，并采用经过审查的显式迁移或隔离恢复流程。

## 开发与验证

新测试数据库先调用 DataStore.migrate_schema()，再调用 initialize() 或构造依赖池的业务存储。对存量 fixture 不应恢复初始化隐式建表。单元测试覆盖 SQL 边界、池清理、连接错误脱敏、提交失败不得返回成功、CLI 生命周期隔离与回执拒绝规则；真实 PostgreSQL 测试须限制为独立测试数据库与随机 schema。
