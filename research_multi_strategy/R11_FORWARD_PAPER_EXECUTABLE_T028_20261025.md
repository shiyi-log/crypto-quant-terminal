# T028 / R11 canonical executable-size stateful runner

- **Research identity:** canonical launchd/cron entry point for the T027 executable-size forward paper
- **Canonical runner:** `tools/run_r11_forward_paper_executable.py`
- **Fixed output:** `artifacts/r11_forward_paper_t027_20261025/`
- **launchd label:** `com.quant.r11forward`
- **execution_evidence:** `forward_paper`; no real order or hedge endpoint

The runner uses 0.1 BTC (or 0.1 underlying units) per call and put. It reads public Deribit quotes, restores open positions from `equity_state.json` or the existing ledger, marks every open position at the current mark, appends the perp hedge and 8 bp fee, exits positions at expiry/30 days by buying back at the observed ask (`realized = entry_bid - exit_ask`), opens at most one new paper straddle, and appends `equity_state.json` plus `equity_daily.jsonl`. Mark-to-market equity continues to use the mark; exit events separately record the ask-minus-mark spread cost. Missing ask at exit fails closed.

A retry with the same observation timestamp is idempotent: it does not append a second snapshot, fill, mark, hedge, exit, or equity row. The manifest's `code_sha256` always hashes this canonical executable runner, while `state_runner_code_sha256` hashes the state implementation it delegates to.

`--dry-run-lifecycle` is isolated from the live book. It writes `dryrun_*` ledger, snapshot, API-request, equity-state and equity-daily files, reports `run_mode=dry_run` and `observations=0`, and never creates or advances live `ledger_events.jsonl`, `snapshots.jsonl`, `equity_state.json` or `equity_daily.jsonl`. A real boundary run reports `run_mode=real_boundary` and is the only run that advances live observations.

The boundary dry-run uses `--timestamp-ms` at `2026-10-25T00:00:00Z` and must show `open → mark → hedge → exit → equity`. Daily production invocation is `--daily --record-entry` at 00:00 UTC. This remains paper-only until at least 20–30 trading-day observations are collected.
