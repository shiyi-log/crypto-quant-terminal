# R11 Forward Status

- run mode: `real_boundary`
- forward gap count: `1`
- rejection threshold: annualized return >= `5.7%` after `20` real observations

## Conditional

- observations: `9 / 20`
- decision: `enter`
- entry IV: `39.74`
- trailing median: `39.085`
- equity: `49952.545783258814`; cumulative PnL: `-47.45421674118552` USDT
- active positions: `6`; cap: `10`; margin: `2880.0` / `480.0` USDT per position; spread: `2.2471892769474198`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Unconditional

- observations: `9 / 20`
- decision: `unconditional`
- entry IV: `unknown`
- trailing median: `unknown`
- equity: `49915.198521821156`; cumulative PnL: `-84.80147817884426` USDT
- active positions: `9`; cap: `10`; margin: `4320.0` / `480.0` USDT per position; spread: `2.2471892769474198`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Missed Dates

| missed dates | gap days | reason |
|---|---:|---|
| 2026-10-28, 2026-10-29 | 2 | watchdog_down |
