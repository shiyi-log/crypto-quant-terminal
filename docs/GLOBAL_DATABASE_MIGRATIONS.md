# 全局显式数据库迁移

此模块建立重构计划第 4.2 节的全局迁移边界。database_migrations.py 仅操作调用者明确提供的 PostgreSQL 连接，不读取 .env、不创建连接池、不管理数据库进程、不访问交易所。当前源模块仍需由主任务接入 DataStore.initialize 和运维入口；增加本文件不表示普通连接已完成切换。

## 当前冻结版本

database/migrations/0001_global_baseline.sql 是 2026-11-16 原 database/schema.sql 的逐字节副本，SHA-256 为 daf64f37478c24274d922efbdeafb181b37cdd1bf1e951c8dd9f2f877c9d5718，15,849 字节。发布代码中的 manifest 固定完整文件集合和哈希；删尾文件、漏包、未知文件或历史文件改写均失败。后续变更必须新增迁移并升级受评审的 manifest 和物理契约，不编辑该 baseline。

现有 database/demo_migrations 保持独立，未复制进全局 baseline，也不改变其校验和。Demo 普通运行继续使用 DemoStore.check_schema；显式 Demo 安装顺序是全局迁移完成、全局兼容检查通过，再安装 Demo 账本与凭据表。

## 显式安装与只读检查

migrate_schema(conn, schema="quant", legacy_import=import_legacy_facts) 由明确的运维连接调用。连接可使用 dict_row 或普通 tuple row。check_schema(conn, schema="quant") 只发 SELECT，也支持 SET TRANSACTION READ ONLY 的事务。

migrate_schema 以 quant-schema:{schema} 事务锁串行执行，在独立 transaction/savepoint 内完成 SQL、旧事实回调及历史登记。调用者负责外层事务最终提交；异常会回滚迁移 savepoint，调用者捕获异常后继续事务也看不到半个 schema。失败不能返回成功回执。对已登记、物理契约完整的当前版本，第二次调用返回空 applied，不重跑 SQL 或旧导入。

只读检查同时验证完整版本、迁移文件哈希、未知版本、历史空洞、追加历史的 ALWAYS 禁改删截断触发器及对应函数内容，并核对冻结物理语义：表类型和 RLS、列类型/精度/空值/default、主键/外键/check、索引键/唯一/有效/ready/排序、不可变触发器事件和函数内容。函数体和 SQL 字符串字面量不作删改；不能通过改写错误文本、execution_enabled 字段名或停用触发器蒙混过关。全局冻结表不允许新增未登记列、规则或触发器；独立 Demo 新表不因此被误拒。

global_schema_migrations 保存版本、checksum、物理契约 hash、是否接管、备份引用和应用时间。其 UPDATE、DELETE、TRUNCATE 均由 ALWAYS 触发器禁止。拥有 DDL 权限的超级用户仍可删除保护；每次检查会拒绝已检测到的保护漂移，这不替代角色权限和部署管理。

## 接管已有无版本 schema

普通安装发现已有业务表但没有全局迁移历史时，返回 unversioned existing schema requires explicit adopt。不会重跑 baseline、修复缺列/索引或自动登记。

明确接管需要：

1. 已完成一致性备份及独立库恢复的业务验证。
2. 可信运维调用者从已验证回执读取 reference、archive_sha256 和 business_validation=passed。
3. 使用 adopt=True, backup_evidence=...，并通过发布代码中独立冻结的 catalog 契约核验。

该字典是内部部署证据引用，不是用户请求中的认证。模块检查格式并永久记录引用，不会自行读取远端回执、证明提供者真实性或把 passed 字串变成实际恢复演练。运维入口必须先验证可信文件、来源数据库/schema 与备份快照关系，不能直接接受 HTTP 表单的自报证据。

已有无版本 schema 必须物理兼容当前冻结 baseline 才能接管。不兼容旧结构会失败，不能以 adopt 作修复开关；真正旧结构需先提供明确、经测试的版本升级路径。当前没有实现通用任意旧库升级或自动降级。

旧 orderflow_surge_observations / orderflow_surge_events_legacy_v3 存在时必须提供明确 legacy_import(conn)。回调必须无网络、幂等，并保留原表与坏记录诊断。主任务可将现有 DataStore._migrate_orderflow_surges 作为回调，连接中须预先设置它依赖的 SQL/Jsonb 设施；不得经 DataStore._connection 递归启动迁移。没有旧表时可不提供回调；新库和接管时提供的回调只执行一次，和历史登记同事务。

## 普通服务接线与恢复

DataStore.initialize 切换后只建立连接池并调用 check_schema。HTTP、db status、采集 worker、研究 worker 和 supervisor 不能自动调用 migrate。空库/待升级/坏校验和须给可操作的错误；运维人员在迁移窗口执行明确命令。已有集成 fixture 需先在独立 quant_test 随机 schema 显式迁移，再运行业务；不能增加默认 auto_migrate=True。

tools/database_backup.py 已验证 pg_dump 归档和 SHA-256，但原恢复回执 business_validation=pending。该回执不能直接用于本接管关口。实际恢复验证需绑定同一导出快照，比较订单、成交、现金流、风险事件、研究事件、冻结账户身份、序号和触发器；隔离恢复成功不允许覆盖生产数据。代码回滚保留已追加的风险锁、成交和新事件，不能恢复旧库来清空历史。

当前模块测试仅在独立 quant_test 随机 schema；未对运行库迁移、未执行生产备份恢复、未重启服务或发送交易请求。全局普通连接接线、真实恢复演练、权限分离及后续增量版本是主任务的独立交付项。
