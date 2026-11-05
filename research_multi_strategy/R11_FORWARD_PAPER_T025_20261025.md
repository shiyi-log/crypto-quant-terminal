# T025 / R11 forward paper: live Deribit ATM straddle observation

- **Research identity:** read-only forward paper; no order placement
- **execution_evidence:** `forward_paper` (真实盘口/mark/IV/greeks 已记录，但所有成交与对冲仍是 paper ledger；没有真实下单)
- **Decision boundary:** daily at 00:00 UTC; `--once` captures one snapshot at any time; `--daily` exits unless the current UTC timestamp is within the configured 00:00 UTC window
- **Underlying:** BTC; nearest available Deribit option expiry to `now + 30d`
- **Strike:** nearest active call/put pair to the Deribit BTC USD index price

## Paper unit and contract granularity

The paper position is fixed at **2,500 USDT notional per leg** to match T023. For each leg, `target_quantity_btc = 2500 / spot`. Deribit BTC options have a 1 BTC contract step in the captured instrument metadata, so the runner records `executable_contract_quantity = floor(target_quantity_btc / 1 BTC) * 1 BTC`, `actual_notional_usdt`, and `notional_residual_usdt`. The forward paper PnL is scaled to `target_quantity_btc` so the comparison stays on the 2,500 USDT/leg basis; the integer-contract residual is disclosed and never silently rounded into a filled trade.

## Read-only market capture and lifecycle

The runner calls only Deribit public read endpoints: `get_index_price`, `get_instruments`, `ticker` for the call, put, and `BTC-PERPETUAL`. It records each URL, timestamp, raw-response SHA-256 and cache path. Each option leg stores best bid, best ask, mark price, bid/ask/mark IV, greeks delta, amount, instrument name, strike, expiry, contract size, and underlying price.

At every daily 00:00 UTC run, the paper lifecycle is:

1. Mark every existing paper position at the current option mark and perp mark; append `mark` events.
2. Rebalance the perp hedge to the observed target delta; append a `hedge` event and charge 8 bp of paper hedge notional.
3. Open at most one new 30d ATM straddle at the observed call/put bid; append `fill` events using the 2,500 USDT scaled quantity and the integer-contract residual.
4. Close positions at expiry using the observed mark/settlement proxy; append `exit` events and release the paper position.

`--dry-run-lifecycle` performs one complete same-snapshot open → mark → hedge → exit cycle for auditability. The same-snapshot exit is explicitly labeled a dry-run lifecycle and is not a trading-day PnL observation.

Deribit inverse option quotes are recorded in native quote units and converted to USD using `quote_price * contract_size * underlying_price`; raw and converted values remain in the ledger. The paper hedge target is the offsetting quantity implied by the observed call and put deltas, scaled to the 2,500 USDT paper quantity.

## Spread scenarios and evidence rules

The observed sell-at-bid entry is the primary forward paper price. Reports also show synthetic comparison scenarios at option premium spread assumptions `{0.2%, 1.0%, 2.25%}`, where 2.25% is the captured bid/ask width divided by bid premium in the first dry-run. Missing bid/ask, mark, IV, delta, spot, or perp price is `unknown` and fails closed; no missing input becomes zero.

Every run writes `snapshot`, `risk_gate`, `fill`, `mark`, `hedge`, and `exit` events as applicable, with `rule_hash`, `read_no`, `burn`, and `execution_evidence=forward_paper`. No order or hedge endpoint is called. The daily paper report compares forward paper PnL with the T023 synthetic expectation only after at least 20–30 trading-day observations. If forward results materially underperform the synthetic expectation or show sustained slippage, label the evidence `rejected`.

## Required artifact

`tools/run_r11_forward_paper.py` writes `artifacts/r11_forward_paper_20261025/` with `REPORT.md`, `REPORT.json`, `ledger_events.jsonl`, `snapshots.jsonl`, `api_requests.jsonl`, `manifest.json`, `reproduce.sh`, and cached raw public responses under `raw_cache/`. Tests are in `tests/test_run_r11_forward_paper.py`.
