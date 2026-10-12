# Candle integrity check

`tools/check_candle_integrity.py` is a read-only audit for the candle rows used
by research and data-quality checks. It reports, per `(exchange, market, pair,
timeframe, candle_type)` dataset:

- coverage of required `extras` fields (`quote_volume`, `num_trades`,
  `taker_buy_base`, `taker_buy_quote`, `flow_imbal`, and `flow_imbal_base`
  by default);
- duplicate timestamps and gaps against the fixed-width timeframe;
- optional tail freshness (`--max-age-bars`) so a continuous but stopped
  source is still reported as stale;
- optional samples reconciled against Binance Futures REST (`--rest`).

The audit does not write candle rows and does not request the exchange unless
`--rest` is supplied. Database mode uses a repeatable-read, read-only
transaction and does not run schema initialization or migrations. A report can
be saved locally with `--output`; `--persist` is an explicit opt-in to save the
finished report under `documents`.

For CI or a forensic snapshot, use an offline JSON file:

```bash
uv run python tools/check_candle_integrity.py \
  --input /path/to/candle-snapshot.json \
  --output /path/to/candle-integrity.json
```

The input shape is `{ "datasets": [...] }`; each dataset includes the five
identity fields and a `rows` list containing `timestamp` and `extras`.

To inspect selected production datasets, set `QUANT_DATABASE_URL` and pass the
pair and timeframe explicitly. An empty requested dataset is reported as a
failure rather than omitted:

```bash
QUANT_DATABASE_URL=... uv run python tools/check_candle_integrity.py \
  --pair BTC/USDT:USDT --pair ETH/USDT:USDT --pair TRX/USDT:USDT \
  --timeframe 5m --output artifacts/candle-integrity-5m.json
```

The process exits `0` only when every selected dataset passes. Missing fields,
gaps, duplicates, stale tails, failed REST reconciliation, malformed input, and
unavailable configuration all return a non-zero exit code. For the live 5m
feeds, a bounded freshness check can be enabled with for example
`--max-age-bars 2`; this is intentionally opt-in for old forensic snapshots.
`--min-coverage 0.99` can be
used for an explicitly documented exploratory threshold; the default is 100%.

The optional `tools/backfill_orderflow_fields.py` utility uses the local
`data/orderflow/*_5m_orderflow.feather` archives as a source for missing fields
in existing `futures` rows. It is also dry-run by default; `--apply` requires a
new `--backup` JSONL file and only fills null fields. It does not insert a row
or replace an existing non-null value.
