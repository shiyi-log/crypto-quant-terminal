# T027 executable-size daily forward paper

- execution_evidence: `forward_paper`
- run_mode: `real_boundary`
- status: `collecting`
- observations: `16` (live observations: `16`) / `20` minimum
- spec_sha256: `34b1707989c60444a93e502e4c5a64be8005e1290747398e3ffa1deae822e128`
- active-position cap: `min(10, floor(initial_equity / 480))`; margin per position: `480 USDT` (`4800 USDT/BTC straddle × 0.1 BTC`)

Real public Deribit quotes are recorded at 0.1 BTC/leg. Fills, hedges and exits are paper-only; no order endpoint is called.

Dry-run lifecycle records are isolated under `dryrun_*` files and do not advance live equity or observations.

## Latest daily report

- observed bid/ask spread: `3.3613%` of bid
- executable premium at bid: `885.7488 USDT`
- today paper PnL: `-24.9143 USDT`
- cumulative equity: `54792.1887 USDT`
- monthlyized paper PnL: `-465.4042 bp`
- deviation from synthetic +163.45 bp/month: `-628.8542 bp`
- active positions: `10` / `10`; margin used: `4800.00 USDT`

The same-snapshot dry-run is explicitly excluded from trading-day performance conclusions.

## Schedule

Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_executable.py --output artifacts/r11_forward_paper_t027_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t027_20261025/cron.log 2>&1`.
Launchd on an Asia/Shanghai machine: run at local 08:00 (00:00 UTC) with `StartCalendarInterval` Hour=8, Minute=0 and the same canonical `--daily --record-entry` command.

After 20-30 trading days, mark `rejected` if annualized paper return is below 5.7% or slippage is persistently large; otherwise continue the execution review.
