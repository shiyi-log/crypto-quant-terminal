# 量化策略联合研究总结（Codex 实现 + DSH 审核）

日期：2026-10-25  
范围：2026-10-24 起，Codex 负责实现、DSH 负责方向与独立审核；所有结论基于历史模拟/paper，未部署、未接真实下单。  
原则：否决必须有模拟证据（n、成本、funding、强平、CI/统计），不以先验算术否决。

## 一、已测试方向与结论

| 方向 | 证据 | 结论 |
|---|---|---|
| S36 资金费/基差 carry（15m） | 0/0.5/1bp net≈-17bp、CI 全负；2bp n=76 not_estimable | rejected |
| S37 配对（15m） | n=1,399 net -9.12bp；18/18 变体 gross<0 | rejected |
| S38 1d 动量 | 191 期；q20 net +24.4bp CI[-43.9,+98.9] p=0.50 | rejected |
| S39 quarter-hour | 36/36 净负，最好 -5.32bp；成本>毛边缘 | rejected |
| S40 funding 轮换（8h） | 6 变体全负；k2_h1 -10.17bp、k3_h1 -9.82bp 显著负 | rejected |
| R1 maker | 1–5s markout -0.3~-1.1bp（逆向选择） | 不成立 |
| R7 清算冲击 | 无 forceOrder，仅 55 代理事件，CI 跨 0 | 数据不足 |
| R9 现货-永续 carry | 28d +10.53bp p=0.11；2025/26 负；OOS 失效 | rejected |
| R10 跨所 funding 价差 | spread 0.465bp/8h，net -15bp | rejected |
| R11 期权波动率溢价（合成） | BTC +163.9bp/30d CI[95,233]；lookahead 已修、独立复现、非重叠显著；但 MDE 118bp、execution unknown | 候选（未部署） |

**共同教训**：8bp/腿 taker + 中频是主杀手；funding 本质 carry，2025–26 失效；数据/口径 bug（tick、lookahead、fixture、median 小时/日）会造假信号；重叠仓位必须用 ≥持有期 block bootstrap。

## 二、R11 条件版（当前唯一候选）

规则（预注册，无绝对阈值）：入场 DVOL（00:00 可见 1h close）≥ 过去 252 个**交易日** DVOL 中位数才卖 30d ATM straddle；否则空仓。

- 非重叠 OOS（2024–26，真实 spread 2.25% + hedge 16bp）：n=23，mean **+162.7bp/30d**，CI [61.3,269.0]，p=0.0013，MDE 152bp（not_estimable）。
- 无条件/全样本：BTC +163.9bp/30d，CI [95.3,233.4]；high IV regime +326bp、low IV +58bp → edge 集中于高 IV。
- 可执行规模（0.1 BTC/腿，50k 权益，cap 10）：无条件 ann 8.85%（0.2% spread）、5.73%（2.25%+16bp），maxDD 4–5%；条件版尚未做组合级重算。
- 当前 IV 36.78% < median 41.83% → `skipped_low_iv`（空仓正确）。
- 限制：合成期权价（无真实成交）、MDE 大、尾部 q01 -677bp/CVaR99 -996bp、样本 2022–2026。

## 三、Forward paper（进行中）

- 双轨日度 runner：条件轨 + 无条件 baseline；0.1 BTC/腿；exit at ask；live/ dryrun 隔离。
- watchdog `tools/r11_forward_watchdog.py`：每日 08:00 Asia/Shanghai（00:00 UTC）运行。
- 输出 `artifacts/r11_forward_paper_t032_20261025/`；20–30 个交易日后对比合成 +163bp/月，若显著低于 5.7% 年化或滑点恶化 → rejected。
- 当前低 IV，预计首日记录 `skipped_low_iv`，不会建仓。

## 四、结论

- **尚无“已部署/已确认可盈利”的策略**；R11 条件版是唯一通过合成 OOS 且可执行规模为正的候选，但 execution 未验证。
- 接下来只做 forward 观察，不再新开无数据支撑的方向；若要继续扩展，需付费历史期权数据或更低费率/大资金假设。

## 五、关键文件

- 队列/进度：`TASK_QUEUE_20261024.md`、`TASK_PROGRESS_20261024.md`
- 判决：`S36_VERDICT`、`S37_DSH_INDEPENDENT_REPLICATION`、`S38_VERDICT`、`S39_VERDICT`、`S40_VERDICT`、`R1_MAKER_V3_AUDIT`、`R9_DSH_AUDIT`、`R10_DSH_AUDIT`、`R11_AUDIT`、`R11_CONDITIONAL_T031`
- 汇总：`NEGATIVE_RESULTS_SUMMARY_20261024.md`、`SOCIAL_DIRECTIONS_20261025.md`

## 六、如何查看后续 forward 证据（会话结束后）

- 每日 08:00 本地（00:00 UTC）watchdog 写入：
  - `artifacts/r11_forward_paper_t032_20261025/conditional_equity_daily.jsonl`
  - `conditional_REPORT.json`（decision / IV / median / equity / spread）
  - `unconditional_equity_daily.jsonl`（无条件 baseline 对照）
  - `logs/watchdog.log`（运行/重试记录）
- 手动检查命令：
  ```bash
  cd /Users/shiyi/Documents/ChatGPT/量化
  tail -3 artifacts/r11_forward_paper_t032_20261025/conditional_equity_daily.jsonl
  .venv/bin/python tools/dsh_audit_r11_forward.py --dir artifacts/r11_forward_paper_t032_20261025 --prefix conditional_
  ```
- 判决标准：
  - 至少 20–30 个交易日；
  - 条件轨真实 spread/exit-at-ask 下年化 ≥5.7%（否则 rejected）；
  - 跨低/高 IV regime 后仍正，才考虑 forward 继续/小资金 paper；
  - 不接真实下单、不部署。
- 当前状态（2026-10-25 03:47）：live observations=0，条件 `skipped_low_iv`，等 IV 回升到中位数以上。

## 七、前端重构与门禁（2026-10-30）

- T034：Codex 使用 UI 技能重构前端（Vben 5.7）：全局暗色量化终端设计 token；总览、订单流突增、爆发行情观测三页改版；修复 Vue/Vue Router 重复注入导致的 `beforeEach` undefined；`vite.config.ts` dedupe；`uv run python manage.py build` 发布到 `web/`；截图见 `/tmp/ui_front.png`、`/tmp/ui_orderflow_clean.png`、`/tmp/ui_spikes_new.png`。
- T035：消除隐藏门禁，全部前端可控：
  - `gate_registry.py` 12 个 gate，持久化 `auth/gates.json`，审计 `auth/gate_audit.jsonl`；
  - `GET/PUT /api/locals/gates`；前端“系统门禁”页可切换、显示风险/来源/说明；
  - 入口级 enforcement：`manage.py`、`market_stream.py`、`strategy_lab_service.py`、`bot/auto_iteration.py`、`bot/monitor.py`、`tools/check_candle_integrity.py`、`demo_execution_service.py`、`demo_runner_controller.py`；
  - `hideInMenu` 归零；风险门禁默认关闭但可见；真实下单在 API 启动前硬检查；
  - 测试 17 passed；证据 `artifacts/t035_gate_enforcement_20261030/`。
- 前端 API 路由修复：`serve_web.py` 增加 `/api`、`/auth` 反向代理到 8890（否则 8888 全 404）。
