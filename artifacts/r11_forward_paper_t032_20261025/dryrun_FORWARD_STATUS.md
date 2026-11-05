# R11 Forward Status

- run mode: `dry_run`
- forward gap count: `1`
- rejection threshold: annualized return >= `5.7%` after `20` real observations

## Conditional

- observations: `9 / 20`
- decision: `skipped_low_iv`
- entry IV: `38.29`
- trailing median: `39.025`
- equity: `49983.10007005059`; cumulative PnL: `-16.899929949409852` USDT
- active positions: `0`; cap: `10`; margin: `0.0` / `480.0` USDT per position; spread: `2.2471892769474198`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Unconditional

- observations: `9 / 20`
- decision: `unconditional`
- entry IV: `unknown`
- trailing median: `unknown`
- equity: `49902.314708723854`; cumulative PnL: `-97.68529127614602` USDT
- active positions: `0`; cap: `10`; margin: `0.0` / `480.0` USDT per position; spread: `2.2471892769474198`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Missed Dates

| missed dates | gap days | reason |
|---|---:|---|
| 2026-10-28, 2026-10-29 | 2 | watchdog_down |
