# T032 R11 conditional daily forward paper

- execution_evidence: `forward_paper`
- run_mode: `capture`
- spec_sha256: `207f140d6d43ab5f67d989aac9826b2793ed015b434e2a6077b75bc62891f094`
- forward_gap_count: `3`
- paper-only: no order or hedge endpoint is called

The conditional and unconditional tracks share the read-only quote cache and keep separate ledgers and equity state files.

## Daily decision and equity

| track | decision | entry DVOL 1h | trailing median | active / cap | margin used | equity | daily PnL | observations | skipped_low_iv |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| conditional | enter | 50.02 | 38.894999999999996 | 8 / 10 | 3840.0 / 480.0 | 49910.02159517527 | 0.0 | 13 | 3 |
| unconditional | unconditional | unknown | unknown | 10 / 10 | 4800.0 / 480.0 | 54792.18872896858 | -24.914332861226285 | 16 | 0 |

## Files

- Conditional: `conditional_ledger_events.jsonl`, `conditional_equity_state.json`, `conditional_equity_daily.jsonl`.
- Baseline: `unconditional_ledger_events.jsonl`, `unconditional_equity_state.json`, `unconditional_equity_daily.jsonl`.
- Shared DVOL history: `conditional_dvol_history.jsonl`.
- DVOL gate history: `331` daily rows, spacing `86400000` ms, span `330` days.
- Live files are appended only at a UTC boundary and are idempotent by timestamp.

At least 20–30 daily observations are required before an execution comparison. A low-IV decision is recorded as `skipped_low_iv` and creates no conditional fill. A missing current DVOL or incomplete trailing window is fail-closed as `conditional_unknown`.

## Schedule

Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t032_20261025/cron.log 2>&1`.
Launchd on an Asia/Shanghai machine: use the generated `com.quant.r11forward.t032.plist` at local 08:00 (00:00 UTC).
