#!/usr/bin/env python3
"""Extend a mirrored candle series from an already-stored sibling series.

``market_stream`` writes every closed 5m futures kline to both
``candle_type='futures'`` and ``candle_type='orderflow'``.  The mirrored series
is therefore expected to be timestamp-complete, but a collector deployed later
than the futures importer leaves a hole that no later tick can repair.

This tool copies *only missing timestamps* from the source candle type to the
target candle type for the selected datasets.  It never updates an existing
row, never changes OHLCV of a row that exists, and is dry-run by default;
``--apply`` requires a new JSONL backup that lists every inserted key so the
insert can be rolled back by deleting exactly those rows.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IDENTITY = ("exchange", "market", "pair", "timeframe")
COLUMNS = ("open", "high", "low", "close", "volume")


class KeyJournal:
    """Write-ahead JSONL inventory of inserted primary keys."""

    def __init__(self, path: Path | None):
        self.handle = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.handle = os.fdopen(descriptor, "w", encoding="utf-8")
            self.write({"type": "metadata", "version": 1,
                        "rollback": "DELETE FROM candles WHERE exchange=%s AND market=%s "
                                    "AND pair=%s AND timeframe=%s AND candle_type=%s "
                                    "AND timestamp=%s for every record with type=inserted."})

    def write(self, record: dict[str, Any]) -> None:
        if self.handle is None:
            return
        self.handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False,
                                     default=str, sort_keys=True) + "\n")

    def close(self) -> None:
        if self.handle is not None:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.handle = None


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid numeric value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid numeric value")
    return number


def mirror_dataset(conn, *, schema: str, identity: tuple[str, ...], target: str,
                   batch_size: int, apply: bool, journal: KeyJournal | None,
                   counts: Counter, since_ms: int | None = None) -> dict[str, Any]:
    """Insert target rows for timestamps the source has and the target lacks."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    exchange, market, pair, timeframe = identity
    dataset = {name: value for name, value in zip(IDENTITY, identity)}
    dataset.update({"target": target, "source_rows": 0, "inserted": 0, "present": 0})
    last_timestamp: int | None = None
    while True:
        with conn.transaction():
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                sql.Identifier(schema)))
            statement = ("SELECT timestamp,open,high,low,close,volume,extras FROM candles "
                         "WHERE exchange=%s AND market=%s AND pair=%s AND timeframe=%s "
                         "AND candle_type=%s")
            params: list[Any] = [*identity, "futures"]
            if since_ms is not None:
                statement += " AND timestamp >= %s"
                params.append(int(since_ms))
            if last_timestamp is not None:
                statement += " AND timestamp > %s"
                params.append(last_timestamp)
            statement += " ORDER BY timestamp LIMIT %s"
            params.append(batch_size)
            source_rows = [dict(row) for row in conn.execute(statement, params).fetchall()]
            if not source_rows:
                break
            last_timestamp = int(source_rows[-1]["timestamp"])
            dataset["source_rows"] += len(source_rows)
            counts["source_rows"] += len(source_rows)
            existing_query = ("SELECT timestamp FROM candles WHERE exchange=%s AND market=%s "
                              "AND pair=%s AND timeframe=%s AND candle_type=%s "
                              "AND timestamp = ANY(%s)")
            present = {int(row["timestamp"]) for row in conn.execute(
                existing_query, (*identity, target, [row["timestamp"] for row in source_rows])
            ).fetchall()}
            pending = [row for row in source_rows if int(row["timestamp"]) not in present]
            dataset["present"] += len(present)
            counts["present"] += len(present)
            if not pending:
                continue
            dataset["inserted"] += len(pending)
            counts["inserted"] += len(pending)
            for row in pending:
                if journal is not None:
                    journal.write({"type": "inserted", "exchange": exchange, "market": market,
                                   "pair": pair, "timeframe": timeframe,
                                   "candle_type": target, "timestamp": int(row["timestamp"])})
            if apply:
                values = [(
                    exchange, market, pair, timeframe, target, int(row["timestamp"]),
                    *(_finite(row[column]) for column in COLUMNS),
                    Jsonb(dict(row["extras"] or {})), row.get("source_key"),
                ) for row in pending]
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO candles (exchange,market,pair,timeframe,candle_type,"
                        "timestamp,open,high,low,close,volume,extras,source_key) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (exchange,market,pair,timeframe,candle_type,timestamp) "
                        "DO NOTHING",
                        values)
    return dataset


def run(*, dsn: str, schema: str, pairs: set[str], timeframes: set[str],
        source: str, target: str, batch_size: int = 2000, apply: bool = False,
        backup: Path | None = None, since_ms: int | None = None) -> dict[str, Any]:
    if apply and backup is None:
        raise ValueError("--apply requires --backup")
    if source == target:
        raise ValueError("source and target candle types must differ")

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row, tuple_row

    counts: Counter = Counter()
    journal = KeyJournal(backup if apply else None)
    try:
        with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
            conditions = ["exchange=%s", "market=%s", "candle_type=%s"]
            params: list[Any] = ["binance", "futures", source]
            if pairs:
                conditions.append("pair = ANY(%s)")
                params.append(sorted(pairs))
            if timeframes:
                conditions.append("timeframe = ANY(%s)")
                params.append(sorted(timeframes))
            query = ("SELECT DISTINCT exchange,market,pair,timeframe FROM candles WHERE "
                     + " AND ".join(conditions)
                     + " ORDER BY exchange,market,pair,timeframe")
            with conn.transaction():
                conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)))
                with conn.cursor(row_factory=tuple_row) as cursor:
                    datasets = [tuple(row) for row in cursor.execute(query, params).fetchall()]
            if not datasets:
                raise ValueError("no matching source datasets")
            report = {"datasets": [mirror_dataset(
                conn, schema=schema, identity=identity, target=target,
                batch_size=batch_size, apply=apply, journal=journal, counts=counts,
                since_ms=since_ms,
            ) for identity in datasets]}
        return {"mode": "apply" if apply else "dry_run", "source": source, "target": target,
                "since_ms": since_ms, "counts": counts, **report}
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
    parser.add_argument("--source", default="futures", help="candle type that already has the rows")
    parser.add_argument("--target", default="orderflow", help="candle type to extend")
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--timeframe", action="append", dest="timeframes")
    parser.add_argument("--schema", default="quant")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--since", help="only mirror rows at or after this UTC instant "
                                        "(ISO-8601, e.g. 2026-10-08T15:40:00Z)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        dsn = os.getenv("QUANT_DATABASE_URL", "")
        if not dsn:
            raise ValueError("QUANT_DATABASE_URL is not configured")
        since_ms = None
        if args.since:
            since_ms = int(datetime.fromisoformat(
                args.since.replace("Z", "+00:00")).timestamp() * 1000)
            if since_ms < 0:
                raise ValueError("--since must not be before the epoch")
        result = run(dsn=dsn, schema=args.schema, pairs=set(args.pairs or []),
                     timeframes=set(args.timeframes or []), source=args.source,
                     target=args.target, batch_size=args.batch_size, apply=args.apply,
                     backup=args.backup, since_ms=since_ms)
        print(json.dumps(_json_safe(result), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Orderflow series mirror failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
