#!/usr/bin/env python3
"""Fill missing order-flow fields on candle rows that already exist.

The command is deliberately dry-run by default. ``--apply`` requires a new
JSONL backup. Only absent ``extras`` keys are filled: OHLCV columns and any
existing non-null value are never replaced, and no candle row is ever inserted.

Three official sources are supported, all reconciled to the canonical
``flow_imbal`` = quote convention recorded in
``research_multi_strategy/verified/DECISION_flow_imbal_semantics_20261012.md``:

``derived``
    Recompute ``flow_imbal`` / ``flow_imbal_base`` / ``flow_imbal_q`` from the
    row's own ``volume`` column and ``extras``.  This is the same derivation the
    synchronizer applies on import, so a historical row imported before the
    derivation existed can be completed without touching a stored source value.

``rest``
    Fetch the most recent closed Binance USD-M futures klines and fill the
    quote/base/trade-count fields on rows that already exist.

``archive``
    Read an official ``data.binance.vision`` kline CSV or ZIP and fill the same
    fields, which is how pre-REST history is covered.

Rows missing from the database are counted as ``missing_target``; extending a
mirrored candle series is a separate operation
(``tools/mirror_orderflow_candles.py``).
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_sync import derived_flow_imbalance
from tools.backfill_orderflow_fields import Backup, merge_missing
from tools.check_candle_integrity import (fetch_rest_klines, pair_to_symbol,
                                          timeframe_milliseconds)

# Official kline CSV columns (REST lists carry the same values positionally).
ARCHIVE_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
)
DATASET_FILTERS = ("exchange", "market", "pair", "timeframe", "candle_type")


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def official_extras(*, quote_volume: Any, num_trades: Any,
                    taker_buy_base: Any, taker_buy_quote: Any) -> dict[str, Any]:
    """Canonical extras for one official closed kline; invalid parts are dropped."""
    extras: dict[str, Any] = {}
    quote = _finite(quote_volume)
    base = _finite(taker_buy_base)
    quote_bought = _finite(taker_buy_quote)
    trades = _finite(num_trades)
    if quote is not None and quote >= 0:
        extras["quote_volume"] = quote
    if base is not None and base >= 0:
        extras["taker_buy_base"] = base
    if quote_bought is not None and quote_bought >= 0:
        extras["taker_buy_quote"] = quote_bought
    if trades is not None and trades >= 0 and float(trades).is_integer():
        extras["num_trades"] = int(trades)
    return extras


def derived_extras(volume: Any, extras: dict[str, Any]) -> dict[str, Any]:
    """Recompute the three imbalance fields from facts already stored on the row."""
    if not isinstance(extras, dict):
        return {}
    result: dict[str, Any] = {}
    base_total = _finite(volume)
    if base_total is None:
        base_total = _finite(extras.get("volume"))
    quote = derived_flow_imbalance(extras.get("taker_buy_quote"), extras.get("quote_volume"))
    base = derived_flow_imbalance(extras.get("taker_buy_base"), base_total)
    if quote is not None:
        result["flow_imbal"] = quote
        result["flow_imbal_q"] = quote
    if base is not None:
        result["flow_imbal_base"] = base
    return result


def mislabeled_repairs(volume: Any, extras: dict[str, Any]) -> dict[str, Any]:
    """Fields whose stored value provably uses the *other* imbalance convention.

    This never guesses: a stored value is only replaced when it is not its own
    formula and is strictly closer to the sibling formula.  Rows where both
    conventions coincide, or where the stored value tracks neither, stay
    untouched for an explicit decision.
    """
    if not isinstance(extras, dict):
        return {}
    quote = derived_flow_imbalance(extras.get("taker_buy_quote"), extras.get("quote_volume"))
    base = derived_flow_imbalance(extras.get("taker_buy_base"), volume)
    if quote is None or base is None:
        return {}
    if math.isclose(quote, base, rel_tol=1e-9, abs_tol=1e-9):
        return {}
    fixes: dict[str, Any] = {}
    if _closer_to_other(_finite(extras.get("flow_imbal_base")), base, quote):
        fixes["flow_imbal_base"] = base
    if _closer_to_other(_finite(extras.get("flow_imbal")), quote, base):
        fixes["flow_imbal"] = quote
        fixes["flow_imbal_q"] = quote
    return fixes


def _closer_to_other(stored: float | None, own: float, other: float) -> bool:
    """True when a stored value is not its own convention but matches the sibling one."""
    if stored is None:
        return False
    if math.isclose(stored, own, rel_tol=1e-6, abs_tol=1e-9):
        return False
    return math.isclose(stored, other, rel_tol=1e-2, abs_tol=1e-3)


def rest_rows(pair: str, timeframe: str, window: int,
              fetch: Callable[..., list[dict[str, Any]]] = fetch_rest_klines
              ) -> dict[int, dict[str, Any]]:
    """Official closed REST klines keyed by timestamp."""
    return {int(row["timestamp"]): official_extras(
        quote_volume=row.get("quote_volume"), num_trades=row.get("num_trades"),
        taker_buy_base=row.get("taker_buy_base"),
        taker_buy_quote=row.get("taker_buy_quote"),
    ) for row in fetch(pair, timeframe, limit=window)}


def archive_rows(path: Path) -> list[tuple[str, int, dict[str, Any]]]:
    """Official kline rows as ``(symbol, timestamp, extras)`` triples."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as bundle:
            members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"archive must hold exactly one CSV, found {len(members)}")
            payload = bundle.read(members[0]).decode("utf-8")
    else:
        payload = path.read_text(encoding="utf-8")
    result: list[tuple[str, int, dict[str, Any]]] = []
    reader = csv.reader(io.StringIO(payload))
    for index, fields in enumerate(reader):
        if index == 0 and fields and not fields[0].strip().isdigit():
            continue  # header row
        if len(fields) < len(ARCHIVE_COLUMNS):
            continue
        raw = dict(zip(ARCHIVE_COLUMNS, fields))
        symbol = path.name.split("-")[0].upper()
        try:
            timestamp = int(float(raw["open_time"]))
        except (TypeError, ValueError):
            continue
        # Official kline exports use seconds, milliseconds, or (since 2025)
        # microseconds; the magnitude identifies the unit unambiguously.
        if timestamp > 100_000_000_000_000:
            timestamp //= 1000
        elif timestamp < 100_000_000_000:
            timestamp *= 1000
        result.append((symbol, timestamp, official_extras(
            quote_volume=raw.get("quote_volume"), num_trades=raw.get("num_trades"),
            taker_buy_base=raw.get("taker_buy_base"),
            taker_buy_quote=raw.get("taker_buy_quote"),
        )))
    return result


def _datasets(conn, *, schema: str, pairs: set[str], timeframes: set[str],
              candle_types: set[str], exchange: str, market: str) -> list[tuple[str, ...]]:
    from psycopg import sql

    conditions = ["exchange=%s", "market=%s"]
    params: list[Any] = [exchange, market]
    if pairs:
        conditions.append("pair = ANY(%s)")
        params.append(sorted(pairs))
    if timeframes:
        conditions.append("timeframe = ANY(%s)")
        params.append(sorted(timeframes))
    if candle_types:
        conditions.append("candle_type = ANY(%s)")
        params.append(sorted(candle_types))
    query = ("SELECT DISTINCT exchange,market,pair,timeframe,candle_type FROM candles WHERE "
             + " AND ".join(conditions) + " ORDER BY exchange,market,pair,timeframe,candle_type")
    from psycopg.rows import tuple_row

    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
            sql.Identifier(schema)))
        with conn.cursor(row_factory=tuple_row) as cursor:
            return [tuple(row) for row in cursor.execute(query, params).fetchall()]


def fill_existing(conn, *, schema: str, datasets: list[tuple[str, ...]],
                  source_for: Callable[[tuple[str, ...]], dict[int, dict[str, Any]]],
                  derive: bool, batch_size: int, apply: bool,
                  journal: Backup | None, counts: Counter,
                  repair: bool = False) -> dict[str, Any]:
    """Fill absent fields on existing rows; never insert and never overwrite."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    report: dict[str, Any] = {"datasets": []}
    for identity in datasets:
        exchange, market, pair, timeframe, candle_type = identity
        source_map = source_for(identity)
        matched = 0
        dataset = {name: value for name, value in zip(DATASET_FILTERS, identity)}
        dataset.update({"source_rows": len(source_map), "updated": 0,
                        "missing_target": 0, "unchanged": 0, "derived_only": 0,
                        "outside_window": 0, "no_usable_source": 0, "repaired": 0})
        last_timestamp: int | None = None
        while True:
            with conn.transaction():
                conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)))
                statement = ("SELECT timestamp,volume,extras FROM candles WHERE "
                             "exchange=%s AND market=%s AND pair=%s AND timeframe=%s "
                             "AND candle_type=%s")
                params: list[Any] = list(identity)
                if last_timestamp is not None:
                    statement += " AND timestamp > %s"
                    params.append(last_timestamp)
                statement += " ORDER BY timestamp LIMIT %s"
                params.append(batch_size)
                if apply:
                    statement += " FOR UPDATE"
                frames = [dict(row) for row in conn.execute(statement, params).fetchall()]
                if not frames:
                    break
                last_timestamp = int(frames[-1]["timestamp"])
                updates = []
                for frame in frames:
                    timestamp = int(frame["timestamp"])
                    original = dict(frame["extras"] or {})
                    official = source_map.get(timestamp)
                    in_source = official is not None
                    base = original
                    changed = False
                    if in_source:
                        matched += 1
                        merged_source = merge_missing(original, official)
                        if merged_source is not None:
                            base = merged_source
                            changed = True
                    # The imbalance fields are derived from the *stored* source
                    # values after the source fields were merged, so the stored
                    # imbalance always matches the stored numerator/denominator.
                    imbalance = derived_extras(frame["volume"], base)
                    if imbalance:
                        merged = merge_missing(base, imbalance)
                        if merged is not None:
                            base = merged
                            changed = True
                    if repair:
                        fixes = mislabeled_repairs(frame["volume"], base)
                        if fixes:
                            base = {**base, **fixes}
                            changed = True
                            dataset["repaired"] += 1
                            counts["repaired"] += 1
                    if not changed:
                        if in_source:
                            key = "no_usable_source" if not imbalance else "unchanged"
                        elif source_map:
                            key = "outside_window"
                        elif imbalance:
                            key = "unchanged"
                        else:
                            key = "derived_only"
                        dataset[key] += 1
                        counts[key] += 1
                        continue
                    dataset["updated"] += 1
                    counts["updated"] += 1
                    if journal is not None:
                        journal.write({"exchange": exchange, "market": market, "pair": pair,
                                       "timeframe": timeframe, "candle_type": candle_type,
                                       "timestamp": timestamp, "extras": original})
                    updates.append((Jsonb(base), *identity, timestamp))
                if apply and updates:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "UPDATE candles SET extras=%s WHERE exchange=%s AND market=%s "
                            "AND pair=%s AND timeframe=%s AND candle_type=%s AND timestamp=%s",
                            updates)
        if not derive:
            dataset["missing_target"] += max(0, len(source_map) - matched)
            counts["missing_target"] += max(0, len(source_map) - matched)
        report["datasets"].append(dataset)
    return report


def run(*, dsn: str, schema: str, source: str, pairs: set[str], timeframes: set[str],
        candle_types: set[str], exchange: str = "binance", market: str = "futures",
        window: int = 1500, archive: Path | None = None, batch_size: int = 2000,
        apply: bool = False, backup: Path | None = None, repair: bool = False,
        fetch: Callable[..., list[dict[str, Any]]] = fetch_rest_klines) -> dict[str, Any]:
    if apply and backup is None:
        raise ValueError("--apply requires --backup")
    if source not in {"derived", "rest", "archive"}:
        raise ValueError(f"unsupported source: {source}")
    if source == "archive" and archive is None:
        raise ValueError("--source archive requires --archive")

    import psycopg
    from psycopg.rows import dict_row

    counts: Counter = Counter()
    journal = Backup(backup if apply else None)
    try:
        with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
            datasets = _datasets(conn, schema=schema, pairs=pairs, timeframes=timeframes,
                                 candle_types=candle_types, exchange=exchange, market=market)
            if not datasets:
                raise ValueError("no matching candle datasets")
            archive_data = archive_rows(archive) if source == "archive" else []
            rest_cache: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}

            def source_for(identity: tuple[str, ...]) -> dict[int, dict[str, Any]]:
                pair, timeframe = identity[2], identity[3]
                if source == "derived":
                    return {}
                if source == "rest":
                    key = (pair, timeframe)
                    if key not in rest_cache:
                        rest_cache[key] = rest_rows(pair, timeframe, window, fetch=fetch)
                    return rest_cache[key]
                symbol = pair_to_symbol(pair)
                return {timestamp: extras for row_symbol, timestamp, extras in archive_data
                        if row_symbol == symbol}

            report = fill_existing(conn, schema=schema, datasets=datasets,
                                   source_for=source_for, derive=source == "derived",
                                   batch_size=batch_size, apply=apply, journal=journal,
                                   counts=counts, repair=repair)
        return {"mode": "apply" if apply else "dry_run", "source": source,
                "counts": counts, **report}
    finally:
        journal.close()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("derived", "rest", "archive"), default="derived")
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--timeframe", action="append", dest="timeframes")
    parser.add_argument("--candle-type", action="append", dest="candle_types",
                        help="restrict to a candle type; omit to audit every candle type")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--market", default="futures")
    parser.add_argument("--schema", default="quant")
    parser.add_argument("--window", type=int, default=1500,
                        help="closed REST bars to reconcile (1-1500)")
    parser.add_argument("--archive", type=Path, help="official kline CSV or ZIP")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--repair-mislabeled-imbalance", action="store_true",
                        help="also rewrite a derived field that provably uses the other convention")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        dsn = os.getenv("QUANT_DATABASE_URL", "")
        if not dsn:
            raise ValueError("QUANT_DATABASE_URL is not configured")
        if not 1 <= args.window <= 1500:
            raise ValueError("--window must be between 1 and 1500")
        for timeframe in args.timeframes or []:
            timeframe_milliseconds(timeframe)
        result = run(dsn=dsn, schema=args.schema, source=args.source,
                     pairs=set(args.pairs or []), timeframes=set(args.timeframes or []),
                     candle_types=set(args.candle_types or []),
                     exchange=args.exchange, market=args.market, window=args.window,
                     archive=args.archive, batch_size=args.batch_size,
                     apply=args.apply, backup=args.backup,
                     repair=args.repair_mislabeled_imbalance)
        print(json.dumps(_json_safe(result), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Candle flow-field backfill failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
