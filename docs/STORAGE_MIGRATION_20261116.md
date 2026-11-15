# 量化终端存储迁移与恢复

本记录对应 2026-11-16 开始的六周计划。当前阶段提供预复制、文件校验、SQLite 在线备份和 PostgreSQL 备份恢复入口；运行服务的路径切换须在完成预复制、核对守护器暂停语义后逐项实施。

## 当前核验

2026-11-16 02:07—02:12（北京时间）只读核验：

- 外盘 `/Volumes/yidong` 为 USB HFS+ 卷，Volume UUID `A6991AF4-5504-351A-BA2B-F0B615A17885`；约 652 GiB 可用，内盘约 29 GiB 可用。
- PostgreSQL 数据目录已在 `/Volumes/yidong/quant-terminal-data/postgres`。保留现有容器及挂载；禁止为迁移再初始化本地 cluster。
- `/Volumes/yidong/quant-terminal-data/files` 已有 11 月 5 日复制的 `logs/`、`bot/logs/`、`database/runtime/`，目录存在不能作为完整迁移证据。新预复制逐文件校验并为覆盖过的不同目标文件留存原副本。
- `bot/tradesv3.dryrun.sqlite` 仍在仓库内，存在活跃 `-wal`、`-shm`；不能把裸主文件复制作为一致性备份。
- 行情文件、模型、回测结果仍在 `bot/user_data/`，采集缓冲位于 `database/runtime/`，日志仍在仓库 `logs/`。主采集缓冲目录条目很多，完整扫描和复制需要单独观察，不应阻塞实时采集。

## 目标布局与配置

`QUANT_DATA_ROOT=/Volumes/yidong/quant-terminal-data` 为运行数据根，运行文件沿用项目相对路径存入 `files/` 子目录，例如模型映射到 `files/bot/user_data/models`，日志映射到 `files/logs`。未配置时保留原路径。历史归档仍为 `/Volumes/yidong/quant_archive`。

设置 `QUANT_DATA_MOUNT=/Volumes/yidong` 和 `QUANT_DATA_VOLUME_UUID` 绑定预期卷身份。任何实际写入前检查挂载点、卷 UUID、可写状态和预留空间；不满足条件时拒绝写入，不新建同名本地目录。配置函数只返回路径，不创建目录。

Git 中的研究规格、策略实现和冻结证据保留现有位置；运行数据迁移不会改写历史研究身份或删除内盘副本。预复制只是阶段证据，清单中 `ready_for_cutover=false` 明确表示它不能证明所有写入进程已经切换。

## 逐服务切换顺序

1. 读取清单并执行默认 dry-run，记录文件数、字节数、目标现有差异与空间。
2. 在线预复制可保持采集和前端运行；普通文件使用原子替换及 SHA-256 验证，SQLite 使用在线备份并执行 `PRAGMA integrity_check`。源文件在复制中变化时记录并跳过，不将它标为一致。
3. 等待训练和回测到安全边界。通过唯一 supervisor 暂停单个写入者，确认其他守护器不会恢复它。
4. 最后一次复制和校验，再建立兼容路径；SQLite 最终切换须短暂关闭数据库写入者，不能将不同时间的主文件与 WAL 混用。
5. 恢复该服务，核验实际文件描述符/有效配置、最新写入位置、业务健康及记录连续性；最后恢复下一项。
6. 保留旧副本和切换前后清单。切换后新增的数据必须带入回退过程，不能直接恢复陈旧副本。

## 备份与隔离恢复

PostgreSQL 使用 `pg_dump` 自定义格式，校验可列出的归档后发布包含 SHA-256 的清单；密码通过子进程环境传递，不进入命令行、清单或公开错误。Docker 模式进入现有数据库容器执行工具，不创建第二套实例。

恢复必须显式提供本机新隔离数据库名称 `quant_restore_*`，先创建新库再在单事务中恢复；拒绝应用生产库、共享测试库、远程目标和覆盖已有库。仅清单状态与哈希通过后运行恢复。恢复验证尚需在隔离数据库执行真实演练并记录关键表行数、最新时间和研究事件；单元测试不能替代这项验收。

同一外盘上的备份覆盖误操作与文件损坏，不覆盖整盘故障。保留最近两份已验证的数据库备份和七天文件快照，并预留恢复空间；自动清理应仅针对有已验证清单的备份，不能删除唯一副本。

## 运维入口示例

先仅在当前命令环境设置存储配置，再执行清单或预览；这些命令不会切换运行中的服务：

```bash
export QUANT_DATA_ROOT=/Volumes/yidong/quant-terminal-data
export QUANT_DATA_MOUNT=/Volumes/yidong
export QUANT_DATA_VOLUME_UUID=A6991AF4-5504-351A-BA2B-F0B615A17885
.venv/bin/python -m tools.storage_migration inventory bot/tradesv3.dryrun.sqlite
.venv/bin/python -m tools.storage_migration precopy bot/tradesv3.dryrun.sqlite
.venv/bin/python -m tools.storage_migration precopy bot/tradesv3.dryrun.sqlite --apply
```

预复制覆盖已有不同目标时，旧目标保留在 `migration/<run_id>/previous/`；原文件继续存在。根目录迭代状态、模型注册表 JSON/JSONL 与 `auto_spaces` 纳入默认清单，进程锁和 PID、SQLite WAL/SHM 排除。运行配置可能含私有凭据，复制目标和清单目录应仅当前用户可访问。

当前预复制会对每个文件再次核验外盘身份，适合先迁移 SQLite、模型与回测产物；海量采集缓冲应在优化流式清单和挂载检查频率后单独批次执行，避免用全量扫描拖慢实时服务。
