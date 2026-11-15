# 自动迭代运行与研究判定

自动迭代入口是 `bot/auto_iteration.py`。该入口使用隔离的 FreqAI dry-run 配置，只登记 challenger，不提升当前 champion，也不修改交易配置。既有门禁 `iteration.auto_enabled` 和显式 `--enable-auto-iteration` 都需要开启。

每轮生成独立 `run_id`，默认进度文件位于状态文件同目录的 `iterations/<run_id>/progress.json`。包装器必须返回相同运行编号和 `status=done`，才会读取其结果；其他轮次或共享旧进度不能用于本轮登记。回测包装器的独立结果目录、配置和数据身份校验另由 `bot/run_backtest_task.py` 管理。

兼容字段 `status` 继续保留，同时增加两个字段：

| 字段 | 含义 | 示例 |
|---|---|---|
| `execution_status` | 工程执行结果 | `running`、`succeeded`、`failed` |
| `research_verdict` | 按原有固定判据形成的研究结论 | `passed`、`rejected`、`not_estimable` |

收益不达标但数据和结果有效时，执行成功、研究拒绝。样本小于既定门槛时只能是 `not_estimable`。结果缺失、来自其他轮次或关键指标不是有限值时，执行失败，不登记候选。判据本身沿用现有定义；工程状态修复不代表新的盈利结论。

终态保留在 `auto_iteration_history.jsonl`，每轮冻结结果为 `iterations/<run_id>/result.json`，通过完整临时写入后原子发布。注册表引用该轮冻结结果，登记收据单独保存在 `registration.json`。登记失败明确报告错误；以相同编号重试时只恢复登记，不重新训练。重复使用已结束且登记完成的编号会跳过，不覆盖原记录。配置文件可能包含凭据，公开接口不返回配置内容或本机绝对路径。

只读接口通过现有认证服务提供：

- `GET /api/locals/iterations?limit=50`：返回 `generated_at`、总轮数 `total` 和最近轮次 `items`，`limit` 范围为 1–100。
- `GET /api/locals/iterations/{run_id}`：返回指定轮次，非法编号返回 400，不存在返回 404。
- 原 `GET /api/locals/iteration` 保留历史研究汇总语义。

新接口从 PostgreSQL 中同步的 `auto_iteration_history.jsonl`、`auto_iteration_status.json` 和当前轮次进度文档读取。同步尚未完成时，页面会反映最近已入库的状态；文件写入与数据库同步是独立环节。接口不接受任意文件路径，也不触发训练或注册表写入。

迭代回归使用临时配置、临时注册表和替代的外部训练进程，不访问真实门禁或启动研究任务：

```bash
.venv/bin/python -m pytest tests/test_auto_iteration_contract.py tests/test_iterations_api.py -q
```
