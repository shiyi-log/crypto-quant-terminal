# R11 / T023 BTC/ETH 波动率风险溢价（DVOL + 合成期权模拟）

- **任务：** T023 / R11
- **研究身份：** historical synthetic simulation only；不部署、不接真实下单
- **execution_evidence：** `unknown`（没有真实期权成交/盘口与真实期权结算账本）
- **requested window：** 2021-01-01 00:00:00 UTC through 2026-10-01 00:00:00 UTC exclusive
- **effective DVOL window：** Deribit public endpoint returned no rows before 2022-01-01 in this run. The runner preserves the requested 2021 window, records that coverage limitation, and fail-closes positions until the first valid DVOL observation. The effective simulated position window therefore starts at 2022-01-01 and includes the 2022 LUNA and FTX stress windows.
- **assets：** BTC、ETH
- **time unit：** all timestamps are normalized to integer milliseconds since Unix epoch; all boundaries are UTC

## Data and fail-closed rules

1. Deribit public `get_volatility_index_data` supplies DVOL OHLC observations at 1h resolution (`resolution=3600`) and 1d resolution (`resolution=86400`). The runner stores every request URL, response cache path, response SHA-256, and parsed coverage in `dvol_requests.jsonl` and `manifest.json`.
2. Entry IV is the 1h DVOL close at the exact UTC entry timestamp (`dvol_1h[entry_ts]`). The 1d bar is retained for coverage only; its value keyed at 00:00 is a daily close containing later information and is never used as an entry input. The earlier T023/T024 results that used the daily close are invalidated.
3. Binance Vision UM futures monthly 1h klines supply `BTCUSDT` and `ETHUSDT` perpetual close prices. The requested Binance history starts in 2021 so the 2022 stress periods have a complete price history.
4. An input series is invalid when a timestamp cannot be normalized, a duplicate timestamp has conflicting values, a price/volatility is non-positive or non-finite, or an expected hourly/daily grid point is missing. Missing rows are never forward-filled. Pre-DVOL 2021 rows are reported as unavailable and do not become zero-volatility observations.
5. The runner does not infer option execution quality from DVOL. `execution_evidence` remains `unknown` in all reports.

## Fixed parameters

```json
{
  "requested_start": "2021-01-01T00:00:00Z",
  "end_exclusive": "2026-10-01T00:00:00Z",
  "holding_days": 30,
  "variance_notional_usdt": 2500,
  "option_notional_usdt": 2500,
  "option_bid_ask_rates": [0.002, 0.005, 0.01, 0.02],
  "perp_hedge_fee_bps_per_trade": [8, 16],
  "risk_free_rate": 0.0,
  "bootstrap_iterations": 20000,
  "bootstrap_block_days": [30, 60],
  "bootstrap_seed": 20261024,
  "family_tests_primary": 4,
  "alpha": 0.05,
  "delta_econ_bps": 12,
  "entry_frequency_default": "daily",
  "entry_frequency_sensitivity": "weekly_monday_00utc"
}
```

The option notional is fixed at 2,500 USDT of underlying notional at entry, so the straddle quantity is `option_notional_usdt / S_entry`. This makes USDT PnL and basis-point reporting comparable with the variance-swap proxy. A bid-ask assumption is applied once to the modeled premium received. Every hedge trade, including initial hedge and terminal unwind, pays the selected fee per traded perp notional.

## A. 30-day variance-swap proxy

For every valid UTC-day entry timestamp `t`, use the Deribit DVOL 1h close at `t` as `sigma_iv = DVOL_1h(t) / 100`. Do not use the 1d DVOL close keyed at `t`; that close reflects later observations in the same UTC day. Use the 1h Binance log returns from `(t, t + 30d]` to compute sample standard deviation and annualize it as:

`sigma_rv = std_sample(log(S_i / S_{i-1})) * sqrt(365 * 24)`.

The gross proxy PnL is:

`pnl_var = 2500 * (sigma_iv^2 - sigma_rv^2) * (30 / 365)`.

The modeled option-liquidity cost is:

`cost_option = 2500 * sigma_iv^2 * (30 / 365) * option_bid_ask_rate`.

`net = pnl_var - cost_option`. There is no perp hedge fee in this proxy. The cost assumption is reported explicitly and is not treated as observed option execution.

## B. Synthetic short ATM straddle (primary evidence)

At each daily UTC midnight entry (and in the weekly sensitivity, each Monday UTC midnight), set `K = S_entry`, `T = 30/365`, and use `sigma = DVOL_1h(entry) / 100`. With `r = 0`, compute Black–Scholes call and put prices and receive the modeled premium for a short ATM straddle. The terminal payoff is:

`payoff = quantity * abs(S_expiry - K)`.

The position is delta-hedged using Binance perpetual closes at each UTC midnight, including the entry hedge and expiry unwind. The hedge target is the negative of the short-straddle delta, using entry IV and remaining time to expiry. Hedge PnL is the cash PnL from carrying the previous hedge quantity across each daily close-to-close interval. Hedge fees are `abs(delta_quantity) * spot * hedge_fee_bps / 10000` for every hedge trade, including initial and terminal trades.

`gross = premium_received - payoff + hedge_pnl`

`net = gross - option_bid_ask_cost - hedge_fees`

The primary conclusion must retain `execution_evidence=unknown`; no positive mean or CI may be described as deployable profitability when the MDE exceeds 12 bp.

## Optional tail-protected risk version

For comparison only, simulate a `tail_protected_straddle`: short the ATM call and put and buy a 0.80K put with the same underlying quantity. The protective put premium and payoff are included, and the combined position is delta-hedged. This version is exploratory and does not upgrade the primary evidence status.

## Stress and tail reporting

Report calendar years 2021–2026 and explicit stress windows:

- **LUNA:** 2022-05-01 through 2022-06-30 UTC
- **FTX:** 2022-11-01 through 2022-12-31 UTC

For every asset/method, report the worst single position, 5% and 1% net-PnL quantiles, CVaR95/CVaR99 (mean PnL at or below those quantiles), maximum drawdown on entry-date ordered cumulative net PnL, and the longest run of consecutive losing entry months.

## Statistics and sensitivity

For each asset and method, report `n` (overlapping positions), `n_unique` (unique UTC entry dates), the number of 30d and 60d blocks, effective independent sample size, mean gross/net and every cost component in USDT and bp of the fixed notional, 95% block-bootstrap CI, two-sided p-value, cluster standard error, design effect, and MDE. Bootstrap resamples contiguous 30d or 60d UTC entry blocks for 20,000 iterations with seed `20261024`. The family correction is `alpha / 4` across BTC/ETH × primary A/B; the exploratory tail-protected comparison is labeled separately. `MDE = (z_(1-alpha/(2K)) + z_0.80) * cluster_se`.

A series is `not_estimable` if `n_unique < 400`, if no known observations remain, or if MDE exceeds `delta_econ_bps=12`. MDE greater than 12 bp prevents any “significant profitability” wording even when a CI excludes zero.

Run the straddle cost grid `option spread ∈ {0.2%, 0.5%, 1.0%, 2.0%}` × `hedge fee ∈ {8, 16 bp}` and report net mean/CI/MDE/status for each asset. Split summaries by asset-specific entry DVOL tercile (`low`, `mid`, `high`). Regime labels are descriptive strata computed from the valid entry sample and are not an ex-ante trading rule.

## Required artifacts

The runner writes `artifacts/r11_vol_risk_premium_20261024/` with:

- `REPORT.md` and `REPORT.json`
- `positions.jsonl` with one row per primary simulated position
- `dvol_requests.jsonl` with every Deribit request/cache record
- `manifest.json` containing file SHA-256 values, `spec_hash`, `code_hash`, git state, data coverage, and `execution_evidence`
- `reproduce.sh`
- cached raw Deribit responses and Binance monthly archives under `raw_cache/`

The runner is `tools/run_r11_vol_risk_premium.py`; tests are `tests/test_run_r11_vol_risk_premium.py`. No function places orders or calls a trading endpoint.
