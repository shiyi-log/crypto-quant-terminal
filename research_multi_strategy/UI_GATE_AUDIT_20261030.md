# 前端重构与门禁审计（2026-10-30）

## T034 前端 UI 重构（Codex UI 技能）

- 技术栈：Vben 5.7 / Vue 3 / Ant Design Vue / Tailwind。
- 全局设计 token：`vben/apps/web-antd/src/styles/quant-terminal.css`（暗色终端、卡片、边框、青绿强调、字号层级）。
- 重构页面：总览 `overview`、订单流突增 `orderflow`、爆发行情观测 `price-spikes`。
- 关键修复：`vite.config.ts` 增加 `resolve.dedupe=['vue','vue-router']`，消除重复注入导致的 `TypeError: Cannot read properties of undefined (reading 'beforeEach')`。
- 构建：`uv run python manage.py build`（pnpm typecheck + build:antd，11/11 tasks），发布 `web/`。
- 验收截图：`/tmp/ui_front.png`、`/tmp/ui_orderflow_clean.png`、`/tmp/ui_spikes_new.png`。
- 前端 API 路由：`serve_web.py` 增加 `/api`、`/auth` 反向代理到 8890（修复 8888 全 404）。

## T035 消除隐藏门禁

### 注册表

- `gate_registry.py`：12 个 gate，持久化 `auth/gates.json`，审计 `auth/gate_audit.jsonl`。
- API：`GET /api/locals/gates`；`PUT /api/locals/gates/<key>`（admin、高风险需 ≥4 字原因、写审计）。
- 前端“系统门禁”页：分组展示风险/来源/说明/开关；`requires_restart` 显示“修改后需重启服务”。
- `hideInMenu` 归零，研究任务/实盘统计/模型迭代恢复可见。

### 12 个门禁与 enforcement

| key | 生效位置 | requires_restart |
|---|---|---|
| market_stream.enabled | `market_stream.py`、`manage.py` | 是 |
| breadth_stream.enabled | `market_stream.py`（宽度流）、`manage.py` | 是 |
| integrity.monitor_enabled | `tools/check_candle_integrity.py` | 否 |
| ops.monitor_enabled | `bot/monitor.py` | 否 |
| strategy_lab.enabled | `strategy_lab_service.py`、`manage.py` | 是 |
| iteration.auto_enabled | `bot/auto_iteration.py` | 否 |
| strategy.paired_execution_enabled | `manage.py`（条件追加）、`strategy_lab_service.py` | 是 |
| risk.required | `demo_runner_controller.py` | 否 |
| execution.demo_enabled | `demo_execution_service.py` | 否 |
| execution.entries_enabled | `demo_execution_service.py` | 否 |
| execution.trading_enabled | `demo_execution_service.py` | 否 |
| execution.real_orders | `manage.py` API 启动硬闸 | 是 |

### 已验证

- `manage.py` 不再无条件追加 `--fnew-paired-execution`；
- 直接运行 strategy_lab / auto_iteration / monitor / integrity / market_stream 均受 gate 拦截；
- 真实 token 实测：PUT `breadth_stream.enabled=true` → `source=ui:admin`，GET 一致，审计落盘，再恢复 false；
- 测试：`tests/test_gate_registry.py`、`test_gate_api.py`、`test_gate_enforcement.py` **18 passed**；
- 证据目录：`artifacts/t035_gate_enforcement_20261030/`。

## 剩余说明

- 启动型门禁（requires_restart=5 个）修改后需重启对应服务才生效；
- 其余 `disabled` 条件为纯 UI 状态（未运行、未选择、达到上限），不是策略门禁；
- 订单流/爆发行情仍处 288 根 5m 基线预热，属数据状态，不是 UI/门禁问题。
