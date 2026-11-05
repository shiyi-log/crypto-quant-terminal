# T032 / R11 条件版日度 forward paper

## 研究身份与边界

- `execution_evidence=forward_paper`：每日读取 Deribit 公共盘口并记录真实 bid/ask/mark/IV，但所有成交、对冲和退出均为 paper ledger 事件。
- 不接真实下单、真实对冲或资金账户；不得把 paper PnL 称为可部署收益。
- 条件轨与无条件 baseline 使用同一盘口快照，但各自拥有独立 ledger、持仓状态、权益状态和日度权益文件。

## 日度条件规则

每个 UTC 00:00 边界运行一次。入场 IV 必须是当前 entry timestamp 的 Deribit DVOL 1h close；当前值不可使用日线 close，也不可进入自己的阈值窗口。历史数据源仍为 DVOL 1h，但 gate 先按 UTC 日聚合，只保留每个日历日 `00:00` 的 1h close。因此阈值为当前 timestamp 严格之前的 252 个有效**日** entry DVOL 观测的中位数；252 个小时观测不能满足 252 日窗口。

- 若当前 IV 缺失或历史少于 252 个有效观测：记录 `conditional_unknown`，fail closed，不建仓。
- 若 `entry_iv < trailing_252d_median`：记录 `skipped_low_iv`，不建仓。
- 若 `entry_iv >= trailing_252d_median`：记录 `enter`，按 Deribit 最小可执行规模 `0.1 BTC/腿` 建立 paper short ATM straddle。
- 持有期 30 天；已有仓位每日按 mark 重估、按目标 delta 记录 paper perp hedge（8 bp），到期按 observed ask 买回，realized PnL 使用 `entry_bid - exit_ask`。
- Conditional and unconditional tracks independently enforce `cap = min(10, floor(initial_equity / margin_per_position))`. Per T026, `margin_per_position = 480 USDT` for the 0.1 BTC paper unit (`4,800 USDT / 1.0 BTC straddle × 0.1 BTC`). A full track records `capacity_blocked` and does not create the next fill; existing positions are preserved and counted.

DVOL 历史请求按不超过 Deribit 公共 API 的小时分页上限分段抓取，`conditional_dvol_history.jsonl` 的审计字段必须显示 `history_spacing_ms=86400000`，并且 `history_span_days` 至少覆盖 252 天；旧版小时粒度缓存会被迁移为仅保留 UTC 00:00 日观测。

## Baseline 与报告

同一次运行同时维护无条件 baseline。baseline 每个 UTC 00:00 都尝试开一笔 0.1 BTC/腿 paper short ATM straddle，文件名统一使用 `unconditional_` 前缀。条件轨使用 `conditional_` 前缀。每日报告必须包含：

- `entry_dvol_1h`、`trailing_252d_median`、`decision`；
- conditional/unconditional active positions；
- conditional/unconditional cash/equity/daily PnL；
- 当日观察 spread、累计 observations 与 `skipped_low_iv` 计数。
- active positions, position cap, margin per position, margin used, and `capacity_blocked` lifecycle counts.

## 调度与幂等

入口为 `tools/run_r11_forward_paper_t032.py`。`--daily --record-entry` 仅在 00:00 UTC 的五分钟窗口推进状态；同 timestamp 重跑必须幂等。`--dry-run-lifecycle` 只能写入 `dryrun_*` 文件，不得推进 live 状态。

建议 cron：

```text
CRON_TZ=UTC
0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t032_20261025/cron.log 2>&1
```

## 测试与解释

测试覆盖低 IV skip、高 IV entry、当前值不进入 trailing median、条件/无条件状态隔离及同 timestamp 幂等。至少积累 20–30 个交易日后再比较 paper PnL 与合成基准；历史合成结果中 MDE 大于 12 bp 的结果仍标记 `not_estimable`，不得称显著盈利。
