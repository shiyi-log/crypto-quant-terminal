# Demo 外部前台 writer

本小节提供内部显式组合的同步前台保护循环。导入、构造和冻结目标不会加载环境、读取凭据、创建适配器、初始化数据库或启动线程。HTTP、manage、launchd 和旧 CLI 没有接入；当前生产保护继续沿用既有路径。

## 冻结目标

FrozenWriterTarget 接收精确 FrozenCommandBinding，要求完整 environment/account_id/stage_id/strategy_version_id/run_id、五项 hash、namespace，以及单独的既有 strategy_id、rule_hash、execution_hash、demo_policy_hash。完整运行身份由服务端明确提供，不从旧策略、凭据指纹或三项旧 hash 推导。目标与依赖在构造后冻结，公开导出为副本；结构、摘要和一致性不认证来源或批准。

初版只恢复一个已有冻结 namespace。共享注册表非空、已 yieldedFlat、无恢复指针、无既有账本绑定、身份或政策漂移均拒绝。不会创建新策略、启用入口、切换 namespace 或新增人为 flatten 动作；自有减仓及既有风险退出继续运行。

## 激活与周期

DemoWriterDriver(controller, command_store, archive, target) 要求内部 external_paused 模式 Controller、实际 DemoCommandStore 和 DemoEvidenceStore，二者使用相同持久存储。activate 显式取得 Controller 能力：先获得既有 binance_demo_runner.lock，再检查 schema、命令绑定和既有会话，最后才允许内部凭据读取及恢复保护。旧 owner 占锁时拒绝，不抢锁、不用 PID/租约/时间代替账户互斥。

能力绑定当前 Controller、真实持锁句柄、进程 PID 和文件 inode。PID 仅拒绝 fork 误用；owner 字符串只在持锁后内部生成。能力不暴露给 HTTP，也不声称能防御同进程任意恶意 Python 代码私自解锁。

cycle_once(command_id=None) 只在激活后使用，且同一 driver 不允许并行周期。无命令时照常暂停保护；每轮最多一个 service.cycle。新的 pause_entries_without_cycle 仅在账户锁和服务锁内关闭本地入口、发布状态快照，不访问适配器或执行周期。所有外部周期均保留 config.trading_enabled=True，确保实际自有减仓仍可执行。

外部模式不启动 daemon。start、stop、attach、adopt_existing_session、start_session、stop_session、switch、resume_protection、close_if_flat 等旧公开写入口均拒绝；只有内部能力方法可以暂停、周期和安全交接。legacy_thread 默认模式保持原行为。

## 命令事实

第一版由内部前台循环传入最多64个唯一 command ID；没有 HTTP 命令入口、队列扫描或用户自定 handler。只处理与完整目标精确一致的 pause_entries/reconcile_owned。

accepted 经真实命令存储 claim，必须返回 committed=true 且 action_ticket=true 才派发该命令。无票据或 claim 失败不派发；普通保护仍执行当轮一次。执行前持久保存 entries=false，再运行暂停下的对账/保护。周期内部返回 reconciliation_error、保存失败或保护不完整均不能视为成功。

成功先提交真实 DemoEvidenceStore 的 demo-writer-command-observation-v1 控制观察，重读核对全文与 record hash，再追加 applied。归档只包含完整绑定、固定控制结果、命令/意图引用和观察时间；没有任意状态正文、金额或上游异常。applied 表示这次本地控制观察已归档，所有 execution_complete/source_authenticated/acceptance_eligible/production_authorized 始终 false。

claim 后提交结果不明、周期错误或归档失败尽力追加 unknown；不会重复派发动作。新实例对 claimed 写 owner_restarted 的 unknown，对 unknown 只追加一次实例观察。重启、长时间过去、更换 owner 均不重 claim，也不清除 unknown。unknown 裁定的成功恢复接口尚未提供。

命令失败不会阻止持续普通保护。命令的 action_ticket 与周期里的订单保留/提交票据不同，不能作为可重复 POST 权限。本地暂停尚未与风险预占形成事务栅栏，不能承诺请求入队瞬间所有其它路径停止开仓。

## 前台停止

run_foreground 是同步循环；调用者通过 stop_event 请求停止。停止请求关闭入口并尝试完整 flat 交接，遇持仓、普通/条件挂单、unknown、查询或持久化失败时继续持锁保护，不直接退出，也不在 finally 中释放锁。失败后的等待有明确间隔，防止停止事件已设置时忙循环。

cycle_once 是已激活对象的测试/内部接口，不提供真实 CLI --once 后无条件解锁。当前没有自动读取凭据、动态 import 工厂、--owner 或服务注册。正式进程组合根、信号接线、可信绑定生产者、HTTP 提交/读取、共享会话外部恢复与旧可导入 CLI 封口仍是后续任务。

进程崩溃由操作系统释放 flock，只能视为异常恢复，不写正常 yieldedFlat，也不会为下一实例重新发 claimed/unknown 票据。正式迁移需另行审查完整交接及运行授权，测试通过不产生策略、资金或异常恢复批准。
