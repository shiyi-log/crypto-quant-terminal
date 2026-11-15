# 本机服务监督与日志

仓库 `manage.py` 维护服务命令、进程身份、人工期望状态和监督入口。业务门禁继续由 GateRegistry 维护，人工停止不会修改交易或采集门禁。

`manage.py stop NAME` 先把期望状态写为 `stopped`，再发送正常退出信号。即使退出超时，人工停止意图仍保留。`manage.py start NAME` 明确恢复为 `running`；`restart` 只有在所选服务均正常停止后恢复启动。控制文件损坏时监督器报告 `service_control_invalid`，不自动重建或启动服务。

`manage.py supervise` 默认每 60 秒检查原有基础服务，`--once` 仅检查一轮。它使用与手动管理相同的目录和命令身份核验，尊重人工停止，单个启动失败不会阻断其他服务恢复。期望状态与监督快照分别保存在 `database/runtime/service_control.json`、`database/runtime/service_supervisor.json`，生命周期操作由同一文件锁串行化。配置修改不把旧命令进程认定为新版本；仍需在安全边界正常重启。

`tools/quant_supervisor.sh` 只调用仓库监督入口。家目录旧监督脚本必须替换为调用此脚本的包装器，并正常重新加载 supervisor 作业，才能消除旧循环反复调用 `start` 覆盖人工停止的行为。仅修改磁盘脚本不会更新已运行的 shell 循环。切换 supervisor 时保留所有现有业务进程，不同时重启全部服务。

自动迭代的 smoke、universe20 和 R11 watchdog 当前由独立 launchd 作业负责，基础监督器不重复管理它们。它们的暂停/恢复仍需通过对应作业；迁入统一期望状态前须先协调 launchd 的唯一所有权。不能把基础服务人工暂停保证扩展为所有研究作业已受管。

新启动的基础服务保持原始业务进程参数，标准输出经独立管道记录。单份日志上限 10 MiB，保留 5 份轮转；监督器自身日志同样轮转。首次接管已有超大日志会完整保留为 `.preserved-*`，避免丢失历史证据；保留副本不计为已清理空间。已运行服务继续持有旧输出描述符，只有后续安全重启才启用新日志管道，不能声称既有 1 GiB `api.log` 已变小。

新增备份入口为 `manage.py db backup --output DIR [--container NAME]`；恢复为 `manage.py db restore --backup DIR --target-dsn-env ENVVAR [--container NAME]`。连接秘密从环境变量读取，恢复目标仍须通过备份模块的隔离库约束。`manage.py storage` 转交存储工具，预复制默认预览；它不会隐式切换运行目录或修改配置。
