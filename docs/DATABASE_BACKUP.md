# PostgreSQL 备份与隔离恢复

本机数据库由现有 Docker 容器 `quant-terminal-postgres` 管理，宿主机 `127.0.0.1:5433` 映射到容器 `5432`，数据目录为外盘 `/Volumes/yidong/quant-terminal-data/postgres`。备份和恢复入口不初始化、启动或停止其他 PostgreSQL 集群。

`tools.database_backup.backup_database(dsn, destination, container=None)` 向全新的目标目录写入 custom-format `database.dump`。完成 `pg_restore --list` 格式检查、文件大小和 SHA-256 校验后才发布 `manifest.json`，清单状态为 `verified`。失败目录保留，不能复用为新备份目录；没有成功清单的目录不能用于恢复。清单不保存连接密码。

主机有兼容的 PostgreSQL 客户端时使用普通连接。当前本机客户端未在 PATH 中，使用 `container="quant-terminal-postgres"` 可复用容器内客户端；仅接受本机连接，并把连接映射到容器内部 `127.0.0.1:5432`。密码经 libpq 环境变量传递，不放在命令参数中。

`restore_database(backup_dir, target_dsn, container=None)` 先验证清单与归档 SHA-256，再创建隔离目标库并用单事务恢复。目标必须为本机 `quant_restore_` 前缀的数据库名称，不能使用业务库、共享测试库或远程地址。入口不使用 `--clean`，已有数据库会在创建步骤失败，不会覆盖已有目标。恢复账号需要创建数据库的权限。

完成工具级格式与哈希检查并不代表业务恢复演练通过。真实演练仍须在隔离库恢复后核对关键表计数、最新记录时间、研究事件和抽样业务查询，并记录耗时。数据库备份与业务源数据在同一外盘时，只覆盖误操作和逻辑损坏恢复；整盘故障恢复需要独立介质。

迁移期间保留已有 Docker 挂载和业务库，采用在线 `pg_dump` 一致性快照。交易 SQLite、模型和行情文件使用各自的文件快照机制，不能通过复制正在写入的 PostgreSQL 数据目录替代逻辑备份。
