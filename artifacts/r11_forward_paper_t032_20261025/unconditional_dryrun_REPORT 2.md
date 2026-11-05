# T027 executable-size daily forward paper

- execution_evidence: `forward_paper`
- run_mode: `dry_run`
- status: `dry_run`
- observations: `0` (live observations: `0`) / `20` minimum
- spec_sha256: `056a352757cebff60d5bdbffbf698f1125983eb7b12f89f83c466cc0e2f1f85c`

Real public Deribit quotes are recorded at 0.1 BTC/leg. Fills, hedges and exits are paper-only; no order endpoint is called.

Dry-run lifecycle records are isolated under `dryrun_*` files and do not advance live equity or observations.

## Latest daily report

- observed bid/ask spread: `1.6854%` of bid
- executable premium at bid: `717.4877 USDT`
- today paper PnL: `-12.7284 USDT`
- cumulative equity: `49987.2716 USDT`
- monthlyized paper PnL: `-237.9655 bp`
- deviation from synthetic +163.45 bp/month: `-401.4155 bp`

The same-snapshot dry-run is explicitly excluded from trading-day performance conclusions.

## Schedule

Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_executable.py --output artifacts/r11_forward_paper_t027_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t027_20261025/cron.log 2>&1`.
Launchd on an Asia/Shanghai machine: run at local 08:00 (00:00 UTC) with `StartCalendarInterval` Hour=8, Minute=0 and the same canonical `--daily --record-entry` command.

After 20-30 trading days, mark `rejected` if annualized paper return is below 5.7% or slippage is persistently large; otherwise continue the execution review.
