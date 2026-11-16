# P2 研究版本、尝试与留出注册表

该存储层持久化研究计划与实验事实，不能启动回测、训练或统计任务，也不证明数据来源真实。所有候选 production_authorized、acceptance_eligible 与来源认证始终为 false。只有调用方先将实验与输出写入该注册表，纯研究协议才可消费这些声明；数据库提交也不能证明没有外部隐藏尝试。

## 版本和实验族

ResearchRegistryStore(data_store, account_id) 按研究账号隔离各事实。register_version(kind, version) 记录 dataset、factor、strategy 或 exit 的稳定版本 ID、申报 SHA-256、独立 source SHA-256、URI、采集时间、quality ref 和规范定义。原始定义不会被覆盖。URI 拒绝 userinfo、query 和 fragment，数值只能使用精确 Decimal/字符串/整数；JSON 不接受浮点或未知对象。

register_family(spec, dataset_version_id, strategy_version_id) 必须解析所有已登记版本，并调用真实 freeze_experiment_family。冻结结果保存完整笛卡尔积、规范 JSON 和哈希。数据窗口、source ref、数据哈希、策略哈希及每个因子/退出版本 SHA 必须与已登记版本一致。family SHA 不代表来源真实性。

## 尝试和产物

验证尝试须在执行前用 register_attempt 提交固定 family/config/producer 身份；之后追加 started 和一个 terminal event。时间不可倒退，terminal 后不能重启或改写失败。UNKNOWN 与失败仍保留在追加事件历史中；重启读取时由历史重放状态，不依赖可变投影。

final holdout 尝试只能由 claim_holdout 原子创建，必须是冻结族内同一 base/stress 对，身份及 producer 固定。验证尝试不能冒充最终尝试。最终产物通过 register_artifact 绑定到已完成的实际尝试，存 URI/SHA/media type，不下载或信任任意路径。

## 一次性留出消费

claim_holdout 在研究账号 advisory lock 内提交不可变消费记录以及两条 final_holdout started 尝试，数据库唯一约束限制同 account/family 及同数据指纹/留出时间窗只能领取一次。它只在连接事务成功提交后返回首次 execution_ticket=True。完全相同的重试返回原 claim 但永远不重新出票；状态为 consumed、unknown、failed 或 completed 都不重置消费。统计进程只有在取得并提交这张票之后才可由外部调用方启动。

执行器应把 base 和 stress 结果及各自已提交 artifact 挂到这两条尝试。注册表核对实际 claim、冻结 family/config、五个身份哈希、相同成本场景以外配置、artifact attempt ID 与时间，再调用 evaluate_candidate。结果哈希冻结，精确重试幂等，改变输入冲突。成功研究裁定仍只是 research_pass，绝不是 Demo 执行授权。UNKNOWN/FAILED 不允许重新提交相同或新 claim 使用同一数据与留出窗口。

纯协议 audit_attempt_history 的 holdout_reused 语义把同一 claim 的 base 和 stress 两条当成两次使用；消费者应以本注册表的同一 claim bundle 审计二者，不能通过伪造 phase 隐去 stress 失败。

## 部署与验证

必须先显式运行全局 DataStore.migrate_schema()，再由运维安装 Demo migrations DemoStore.ensure_schema()，其中包括 0006。普通服务启动只检查结构，不隐式应用迁移。所有八张 refactor_research_* 表的事实都拒绝 UPDATE/DELETE/TRUNCATE；精确重放原身份才幂等，修改内容返回冲突。

测试使用独立 quant_test 数据库的随机 schema，覆盖完整冻结、版本冲突、敏感 URI/不精确数据拒绝、启动前登记、失败重启、并发留出只出票一次、提交回滚、产物与身份绑定、追加历史不可改写。真实统计执行、真实完整尝试清单与数据真实性由注册表外部系统承担，本实现没有自动运行任何任务。
