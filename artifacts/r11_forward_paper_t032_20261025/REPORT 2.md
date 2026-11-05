# T032 R11 conditional daily forward paper

- execution_evidence: `forward_paper`
- run_mode: `dry_run`
- spec_sha256: `b764c3d35ff617eb4c450064c70a736e46c9f3232ce728870e6e6a72afb65f4f`
- paper-only: no order or hedge endpoint is called

The conditional and unconditional tracks share the read-only quote cache and keep separate ledgers and equity state files.

## Daily decision and equity

| track | decision | entry DVOL 1h | trailing median | active positions | equity | daily PnL | observations | skipped_low_iv |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| conditional | unknown | unknown | 37.795 | 0 | 50000.0 | 0.0 | 0 | 0 |
| unconditional | unconditional | unknown | unknown | 0 | 49987.27162888269 | -12.728371117307688 | 0 | 0 |

## Files

- Conditional: `conditional_ledger_events.jsonl`, `conditional_equity_state.json`, `conditional_equity_daily.jsonl`.
- Baseline: `unconditional_ledger_events.jsonl`, `unconditional_equity_state.json`, `unconditional_equity_daily.jsonl`.
- Shared DVOL history: `conditional_dvol_history.jsonl`.

At least 20–30 daily observations are required before an execution comparison. A low-IV decision is recorded as `skipped_low_iv` and creates no conditional fill. A missing current DVOL or incomplete trailing window is fail-closed as `conditional_unknown`.

## Schedule

Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t032_20261025/cron.log 2>&1`.
Launchd on an Asia/Shanghai machine: use the generated `com.quant.r11forward.t032.plist` at local 08:00 (00:00 UTC).
