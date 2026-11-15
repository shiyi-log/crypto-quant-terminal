# R11 Forward Status

- run mode: `capture`
- forward gap count: `3`
- rejection threshold: annualized return >= `5.7%` after `20` real observations

## Conditional

- observations: `13 / 20`
- decision: `enter`
- entry IV: `39.94`
- trailing median: `38.894999999999996`
- equity: `49910.02159517527`; cumulative PnL: `-89.97840482473111` USDT
- active positions: `8`; cap: `10`; margin: `3840.0` / `480.0` USDT per position; spread: `3.3613431621572305`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Unconditional

- observations: `15 / 20`
- decision: `unconditional`
- entry IV: `unknown`
- trailing median: `unknown`
- equity: `54817.1030618298`; cumulative PnL: `4817.103061829803` USDT
- active positions: `10`; cap: `10`; margin: `4800.0` / `480.0` USDT per position; spread: `3.3613431621572305`%
- annualized / Sharpe / maxDD / CVaR99: pending until 20 real observations

## Missed Dates

| missed dates | gap days | reason |
|---|---:|---|
| 2026-10-28, 2026-10-29 | 2 | watchdog_down |
| 2026-11-05 | 1 | watchdog_down |
| 2026-11-08, 2026-11-09 | 2 | watchdog_down |
