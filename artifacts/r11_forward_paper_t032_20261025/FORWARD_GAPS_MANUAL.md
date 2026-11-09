# R11 forward 数据缺口（人工记录）

- 2026-11-05：watchdog_down（工作区重置期间）。
- 2026-11-08：conditional DVOL 1h 取数缺失 → `conditional_unknown`，runner 落入 `capture`，未产生有效观测；该无效行已从 equity CSV 移除（备份：`conditional_equity_daily.jsonl.bak_20261109_capture_row`）。
- 2026-11-09：watchdog 在 08:00 连续 3 次 rc=1（Deribit DVOL 取数异常），未产生观测；19:32 的补跑因不是 UTC 00:00 边界，只得到 capture/unknown，同样不作为有效观测（已移除）。
- 处置：不补造历史观测；增强 watchdog 重试；下一有效观测从 2026-11-10 08:00 开始。
