# R11 Forward Status

- run mode: `dry_run`
- forward gap count: `3`
- rejection threshold: annualized return >= `5.7%` after `20` real observations

## Conditional

- observations: `13 / 20`
- decision: `enter`
- entry IV: `39.92`
- trailing median: `38.894999999999996`
- equity: `49983.10007005059`; cumulative PnL: `-16.899929949409852` USDT
- active positions: `0`; cap: `10`; margin: `0.0` / `480.0` USDT per position; spread: `3.3613431621572305`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Unconditional

- observations: `14 / 20`
- decision: `unconditional`
- entry IV: `unknown`
- trailing median: `unknown`
- equity: `49902.314708723854`; cumulative PnL: `-97.68529127614602` USDT
- active positions: `0`; cap: `10`; margin: `0.0` / `480.0` USDT per position; spread: `3.3613431621572305`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Missed Dates

| missed dates | gap days | reason |
|---|---:|---|
| 2026-10-28, 2026-10-29 | 2 | watchdog_down |
| 2026-11-05 | 1 | watchdog_down |
| 2026-11-08, 2026-11-09 | 2 | watchdog_down |
