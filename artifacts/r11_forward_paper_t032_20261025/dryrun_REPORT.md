# T032 R11 conditional daily forward paper

- execution_evidence: `forward_paper`
- run_mode: `dry_run`
- spec_sha256: `207f140d6d43ab5f67d989aac9826b2793ed015b434e2a6077b75bc62891f094`
- forward_gap_count: `3`
- paper-only: no order or hedge endpoint is called

The conditional and unconditional tracks share the read-only quote cache and keep separate ledgers and equity state files.

## Daily decision and equity

| track | decision | entry DVOL 1h | trailing median | active / cap | margin used | equity | daily PnL | observations | skipped_low_iv |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| conditional | enter | 39.92 | 38.894999999999996 | 0 / 10 | 0.0 / 480.0 | 49983.10007005059 | 0.0 | 13 | 3 |
| unconditional | unconditional | unknown | unknown | 0 / 10 | 0.0 / 480.0 | 49902.314708723854 | 0.0 | 14 | 0 |

## Files

- Conditional: `conditional_dryrun_ledger_events.jsonl`, `conditional_dryrun_equity_state.json`, `conditional_dryrun_equity_daily.jsonl`.
- Baseline: `unconditional_dryrun_ledger_events.jsonl`, `unconditional_dryrun_equity_state.json`, `unconditional_dryrun_equity_daily.jsonl`.
- Shared DVOL history: `conditional_dvol_history.jsonl`.
- DVOL gate history: `330` daily rows, spacing `86400000` ms, span `329` days.
- Dry-run lifecycle files are isolated and do not advance live state.

At least 20–30 daily observations are required before an execution comparison. A low-IV decision is recorded as `skipped_low_iv` and creates no conditional fill. A missing current DVOL or incomplete trailing window is fail-closed as `conditional_unknown`.

## Schedule

Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t032_20261025/cron.log 2>&1`.
Launchd on an Asia/Shanghai machine: use the generated `com.quant.r11forward.t032.plist` at local 08:00 (00:00 UTC).
