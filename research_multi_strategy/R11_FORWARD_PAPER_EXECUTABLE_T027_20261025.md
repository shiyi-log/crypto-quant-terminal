# T027 / R11 executable-size daily forward paper

- **Research identity:** daily read-only forward paper at the executable Deribit minimum
- **execution_evidence:** `forward_paper` (real public bid/ask/mark/IV/greeks captured; fills and hedges remain paper ledger events)
- **Unit:** 0.1 BTC (or 0.1 underlying units) per call and per put, using the T026 executable-size convention
- **Equity:** initial paper equity 50,000 USDT
- **Schedule:** one run at 00:00 UTC per day; `--once` is available for an ad-hoc capture and `--daily` skips outside a five-minute UTC window

## Daily lifecycle

Each scheduled run appends a snapshot and then processes the paper book in order:

1. Mark all open positions at the observed option mark.
2. Rebalance the perp hedge to the observed delta and charge an 8 bp paper hedge fee.
3. Exit positions at expiry using the observed mark/settlement proxy.
4. Open at most one new 30-day ATM short straddle at the observed call/put bid.
5. Append the daily equity and PnL record.

Before step 4, the runner enforces the independent active-position capacity `min(10, floor(initial_equity / margin_per_position))`. T026 supplies `margin_per_position = 480 USDT` for the 0.1 BTC paper unit (`4,800 USDT / 1.0 BTC straddle × 0.1 BTC`). When the cap is reached, no fill is created and a `capacity_blocked` ledger event records `cap`, `active`, `margin_per_position_usd`, and `margin_used_usd`. Restored historical positions remain in the state and count toward the cap.

The ledger records `snapshot`, `risk_gate`, `fill`, `mark`, `hedge`, and `exit`. A state file carries cash equity and open paper positions between runs; the same-snapshot dry-run is explicitly excluded from trading-day performance conclusions.

## Monitoring and decision rule

Every report includes observed bid/ask spread, executable premium, today's paper PnL, cumulative paper equity, cumulative observations, active positions/cap, margin per position, margin used, and the deviation from the T023 synthetic expectation of +163 bp/month. After 20–30 trading days, compare the realized annualized paper return with the T026 executable-scale reference. If realized annualized return is materially below 5.7% or observed slippage is persistently large, set `status=rejected`; otherwise keep collecting until the execution review is complete. No real order or hedge endpoint is ever called.

## Required artifacts

`tools/run_r11_forward_paper_t027.py`, `tests/test_run_r11_forward_paper_t027.py`, and `artifacts/r11_forward_paper_t027_20261025/` with `REPORT.md`, `REPORT.json`, `ledger_events.jsonl`, `snapshots.jsonl`, `api_requests.jsonl`, `equity_daily.jsonl`, `equity_state.json`, `manifest.json`, and `reproduce.sh`.
