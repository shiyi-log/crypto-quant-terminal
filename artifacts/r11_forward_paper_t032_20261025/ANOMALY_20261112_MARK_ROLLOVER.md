# 2026-11-12 估值异常（R11 forward）

## 现象

- unconditional 11-12 记录 `daily_pnl_usd=-1782.06`、equity 48,082.35；
- 同日 conditional 落入 `capture`（DVOL 缺失），无有效观测。

## 原因

- 11-12 快照 `selected_expiry_iso` 从 2026-11-27 变为 **2026-12-25**；
- `paper_premium_mark_usd` 从 726.41 跳到 **902.12**；
- 所有存量头寸仍是 **11-27 到期**，但 mark 使用了新选 12-25 的 premium；
- 同时 `perp.mark_price`/`spot_index` 时间戳停在 2026-10-24（1792866477044），价格未更新。

结论：这是**到期滚动后的 mark 定价错误 + 陈旧行情**，-1782 不是真实亏损。

## 处置

- 11-12 unconditional equity 行已从 CSV 移除（备份：`unconditional_equity_daily.jsonl.bak_20261112_rollover_mark`）；
- 不纳入绩效统计；
- 修复前，后续 mark 估值不可直接用于裁决。

## 2026-11-14 修复

- 已实现按持仓 `expiry_ts`+`strike` 单独取 mark/ask（`fetch_expiry_premiums`）；
- mark 使用 `observed_option_mark_at_position_expiry`；
- 新持仓会持久化 `strike`；
- dry-run（含 11-27 旧仓 + 12-25 新选到期）exit=0；
- watchdog 已重启，11-15 08:00 起恢复有效观测。
