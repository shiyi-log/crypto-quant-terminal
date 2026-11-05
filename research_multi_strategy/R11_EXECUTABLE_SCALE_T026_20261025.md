# T026 / R11 executable-size forward paper and portfolio rerun

- **Research identity:** executable-scale forward paper and historical portfolio sensitivity; no order placement
- **execution_evidence:** `forward_paper` for the public Deribit observation; portfolio results remain historical synthetic evidence and are not deployment evidence
- **Decision:** T025's 2,500 USDT/leg fractional quantity is retained only as a research comparison. T026 uses Deribit `min_trade_amount` / contract step of **0.1 BTC (or 0.1 underlying units per leg)** as the executable paper unit.

## Executable forward paper

The runner reads the public Deribit BTC option instruments and tickers. It records the observed bid, ask, mark, IV, greeks, spot and perp. The target quantity is 0.1 BTC per call and 0.1 BTC per put. For each leg it records `target_quantity`, `min_trade_amount`, `executable_quantity`, `actual_notional_usdt`, and `notional_residual_usdt`; paper premium and PnL use the executable 0.1 BTC quantity. The residual is the difference between the target 0.1 BTC quantity and the instrument's minimum step after flooring. No order or hedge endpoint is called.

The report includes the observed option spread as a percentage of the bid premium and compares 0.2%, 1.0%, and the measured spread (2.25% reference; current observation is recorded exactly) with an 8 bp paper hedge fee. Evidence is `forward_paper`: real public quotes are captured, but fills and hedges are paper ledger events.

## Executable portfolio rerun

- Initial equity: 50,000 USDT.
- One new short ATM straddle at most per UTC day, 30-day holding period.
- Each position is 0.1 underlying units per call and 0.1 per put; total option notional is approximately `2 * 0.1 * spot`.
- Initial margin is 30% of total straddle notional and maintenance margin is 15%.
- The active-position cap is the lower of 10 and the number affordable from initial margin; at BTC ~80,000 this is 10 positions (about 4,800 USDT initial margin per BTC straddle).
- Forward-run enforcement uses `cap = min(10, floor(initial_equity / margin_per_position))` independently for each track. The T026 margin convention is approximately 4,800 USDT per 1.0 BTC straddle, scaled to the executable 0.1 BTC paper unit: `margin_per_position = 480 USDT`; at 50,000 USDT initial equity the cap is 10. Existing ledger positions are restored and retained; the check applies only before a new fill.
- Candidate PnL, option costs and hedge turnover are scaled from the T023 2,500 USDT/leg synthetic ledger by `actual_leg_notional / 2,500`.
- Cost scenarios include option spread 0.2%, 1.0%, and measured 2.25%, with hedge fees 8 bp and 16 bp.
- Report annualized return, max drawdown, Sharpe, monthly CVaR95/CVaR99, worst month, margin calls, forced liquidations, bankruptcy path and block-bootstrap bankruptcy, and compare against T024's fixed 2,500 USDT/leg result.

The numeric forward-paper gate is annualized return >10% and maximum drawdown better than -30%. A numeric pass does not authorize deployment because this portfolio rerun is synthetic and the live forward paper remains paper-only.

## Required artifacts

- `tools/run_r11_forward_paper_executable.py`
- `tools/run_r11_portfolio_executable.py`
- `tests/test_run_r11_executable.py`
- `artifacts/r11_executable_scale_20261025/` with `REPORT.md`, `REPORT.json`, `forward_ledger_events.jsonl`, `forward_snapshot.jsonl`, `equity_curves.jsonl`, `portfolio_events.jsonl`, `manifest.json`, `reproduce.sh`
