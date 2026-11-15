# 研究账本数据库保护与上线核验

对应六周计划：`DEVELOPMENT_PLAN_20261116.md`。研究事实保存在 PostgreSQL
`research_ledger_events`，应用层通过事件 ID、类型和 payload 检查幂等与冲突。
前向事件使用生产者稳定 ID，不能将其重算成 payload 哈希，也不能重新排列旧序列。

## 保护范围

本次拟增加的数据库约束拒绝直接 UPDATE、DELETE 和 TRUNCATE；修订必须追加事件。
触发器使用语句级 BEFORE 检查，因此即使操作未匹配到行，也不能成为后续绕过路径。
合法 INSERT 与 `ON CONFLICT (event_id) DO NOTHING` 不受该触发器影响。

`sequence_id` 保留现有值；旧表仅在值非空且唯一时补齐 NOT NULL 和唯一索引。
发现空值或重复值时初始化事务失败，要求先保留证据并独立分析，不能偷偷重排或补号。
序列可以有间断，BIGSERIAL 不保证并发提交顺序，不得把缺号直接解释为事件丢失。

触发器设置为 ALWAYS，防止复制会话角色意外跳过保护。数据库管理员仍能改变 DDL、
删除表或禁用触发器；普通写入角色应与 schema 所有者、迁移管理员分离。
当前改造不宣称已经完成生产角色拆分，也不改变现有 `_record_event` 的来源身份语义。

## 上线前核验

1. 使用现有数据库备份入口生成并校验备份；记录部署前事件数量、最大序列和
   事件表逻辑摘要。备份结果中不记录凭据。
2. 在隔离测试数据库运行 `tests/test_research_ledger_postgres.py` 和原有研究账本、
   交易审计、DataStore 集成测试。测试必须实际连接独立数据库，不能以 skip 代替通过。
3. 只读检查生产表的 NULL/重复序列、现有唯一索引、表所有者和触发器目录，
   并核对 `pg_get_serial_sequence` 对应的默认表达式、序列高水位没有落后于表中
   最大 sequence_id。本次约束补齐保留现有 BIGSERIAL，不自动回调或重置序列；
   非标准旧表若没有默认序列，应先在隔离恢复库确定独立迁移方式。
   使用部署工具提供的凭据环境，避免把 DSN 打印到日志或命令行。
4. 在隔离恢复库执行当前 `database/schema.sql` 两次，比较事件 ID、payload、
   source_id、created_at 和 sequence_id，确认所有历史值保持一致。
5. 按计划依次切换写入服务并初始化 schema。`DataStore.initialize()` 持有 schema
   advisory lock，在一个事务中执行 DDL；数据库锁超时或旧事实异常使事务失败。
   不要为了“启动成功”而删除触发器或覆盖原事实。

可在目标 schema 已明确设置的只读事务内检查：

```sql
SELECT count(*) AS events, min(sequence_id), max(sequence_id),
       count(*) FILTER (WHERE sequence_id IS NULL) AS null_sequences
FROM research_ledger_events;

SELECT sequence_id, count(*)
FROM research_ledger_events GROUP BY sequence_id HAVING count(*) > 1;

SELECT t.tgname, t.tgenabled, pg_get_triggerdef(t.oid)
FROM pg_trigger AS t
WHERE t.tgrelid = 'research_ledger_events'::regclass AND NOT t.tgisinternal;

SELECT pg_get_serial_sequence('research_ledger_events','sequence_id') AS sequence_name;
SELECT pg_get_expr(d.adbin,d.adrelid) AS sequence_default
FROM pg_attrdef AS d
JOIN pg_attribute AS a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
WHERE a.attrelid='research_ledger_events'::regclass AND a.attname='sequence_id';
```

生产核验使用目录查询，不对真实事件执行“试删后回滚”。破坏性尝试仅在隔离测试
schema 中运行。新增数据库级约束不等于生产迁移已经发生，部署记录必须单独写明。

## 回退与恢复

应用回退保留追加保护和所有新增事件。保护不改变表列和合法插入格式，旧应用仍应
使用 INSERT/DO NOTHING；若旧程序依赖改写事件，应修复调用方，不为兼容静默放宽约束。
出现旧表约束异常时，恢复到隔离目标分析。恢复旧备份覆盖正在使用的数据库会丢失
切换后的事件，不能作为常规回退方式。

## 验证记录

2026-11-16：主代理在独立 PostgreSQL 测试库完成 RED，11 项失败、1 项通过。
失败均为数据库接受直接改写或旧表缺少序列约束；追加重试原有用例通过。
已增加数据库保护与约束，等待主代理串行 GREEN 和现有测试回归。
尚未对生产执行迁移、重启服务或修改事件。
