# Demo 被动命令账本

本模块只提交请求和处理观察，不创建适配器、执行线程、网络请求或定时任务。
记录 accepted/claimed/applied 均不代表下单、成交、空仓、对账通过或上线批准。
现有 HTTP 控制器、保护线程与旧 CLI 没有接入该模块，生产行为由后续独立小节迁移。

## 绑定与请求

内部组合根创建 FrozenCommandBinding(binding_id, scope, identity_hashes, namespace)。
scope 必须完整且只有 environment/account_id/stage_id/strategy_version_id/run_id，
environment 固定 binance_demo；identity_hashes 必须完整且只有 code_hash、
strategy_hash、execution_hash、risk_policy_hash、cost_model_hash。所有 SHA256
为64位小写十六进制，namespace 是不可变订单账本引用。构造类型和哈希只验证结构，
不会认证来源、策略批准或账户所有权。公开 to_dict 返回副本。

DemoCommandStore(data_store, account_id) 构造不连接数据库。显式迁移使用继承的
ensure_schema，普通业务启动仍应只调用 check_schema；本模块不自动安装 SQL。
0007 新增三张独立表，不改旧迁移 checksum。

服务端先 register_binding，再 enqueue。命令只有 pause_entries 和
reconcile_owned 两种，分别接受固定原因码 operator_pause/safety_pause，及
operator_reconcile/restart_reconciliation。没有任意 JSON payload、脚本、
金额、订单、provider、approval、truth flag 或凭据字段。身份限制为128字符内的
字母、数字、下划线、点和连字符，拒绝路径穿越、URL、userinfo、空白和控制字符。
actor 由外部已认证服务端提供，本模块不验证登录；它不是 HTTP 可自封的批准人。

相同账户 command_id 的固定动作、绑定和 actor 精确重试返回原事实；后来提交
时间不会改写首次 submitted_ms。固定内容变化冲突。意图和 accepted 事件同事务
提交后才返回 committed 收据，action_ticket 为 false。缺失绑定拒绝，不补造身份。

## 单次处理票据与事件

claim(command_id, owner_id=..., claimed_ms=...) 在账户 advisory 事务锁和意图行锁内
把 accepted 变为 claimed。owner 必须是 writer- 加32位小写十六进制的进程实例
引用；PID、租约和时间戳均不充当互斥锁。该字符串本身不证明调用方实际持有
账户 flock。未来生产调用只能由持锁的内部 writer 上下文注入，HTTP 不得自行
构造 owner。被动存储尚未提供该执行上下文或账户所有权认证。

只有首次成功 COMMIT 后返回 action_ticket=true。事务提交失败直接抛异常，
不交出票据；同一命令以后任何 claim 都不再次出票，包括同 owner 精确重试、
重启、更换 owner、unknown 和长时间过去。命令ID与普通/条件单 client ID 分开；
真正订单仍须先持久预占并单次 claim，不得把处理票据当作可重复 POST 权限。

append_event 仅接受 unknown/applied/rejected/observed，时间不得倒序。未知或
观察可以由新的实例追加；applied/rejected 只能由首次 claim 的实例提交。新实例
对旧 unknown 只查询和追加 observed，状态仍 unknown；本模块没有自动重跑、
重新 claim、撤销 unknown 或 lease 续租。成功恢复的进一步裁定接口留待独立设计。

applied 必须提供1至16个真实已提交 DemoEvidenceStore 引用和 record_sha256，
重验归档链、五维 scope 与五 hash。其它事件可不附引用；若附引用，同样重验。
只接受 evidence:64hex 引用，不接收路径或签名 URL。引用证实持久资料与身份一致，
不验证其控制语义、账户快照最终性或交易所真实性。因此所有结果始终带
execution_complete=false、source_authenticated=false、acceptance_eligible=false、
production_authorized=false。

## 存储、重放与日志

绑定、意图与账户事件哈希链都是不可变事实。三张表的 ALWAYS 触发器拒绝
UPDATE/DELETE/TRUNCATE。事件含账户连续序号、前一记录摘要、规范 payload
摘要和记录摘要；读取核对链、意图、绑定与状态转换后返回副本，没有可被单独
信任的 mutable current 表。每账户最多100000个事件的回放上限，达到上限明确
失败；不截掉旧历史后继续声称完整。

事务锁仅序列化本模块账户事件，不代替进程期 flock，也尚未与风险预占锁形成
“暂停提交后禁止新开仓 claim”的事务栅栏。命令 accepted 只表示排队，页面应
显示待处理；要承诺更强暂停语义，后续必须在风险 claim 同事务核对持久暂停状态。
网络始终在事务外，本模块没有任何网络路径。

中文日志只有固定动作、状态和首次票据布尔值，不输出 actor、账户、完整输入、
凭据、归档内容或原始异常。调用层必须保持类型/固定错误码脱敏。

## 后续接线

后续依次实施持锁的专属 writer、可信运行绑定生产者、安全运行时 selector/语义
验证器、HTTP 命令提交与持久状态读取、旧 CLI 恢复适配、launchd 唯一生命周期和
安全交接。当前 controller 的 flat 检查仍须补条件挂单与触发子单核对；本模块
没有调用或放宽这些检查。已有保护保持原路径，不因新表存在而停止或重启服务。

正式 Demo、策略变更、资金升级和异常恢复批准仍为独立关口。代码发布和测试
不会创建这些批准，也不会启动真实订单或自然30天验收。
