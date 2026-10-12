#!/usr/bin/env python3
"""Explicitly migrate legacy orderflow imbalance fields to canonical semantics.

The command is read-only by default. Applying changes requires ``--apply`` and
``--backup``; the backup contains the original extras for every changed row.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


def _number(extras: dict[str, Any], name: str) -> float | None:
    value = extras.get(name)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def migrate_extras(extras: dict[str, Any], *, volume: Any = None) -> tuple[dict[str, Any] | None, str]:
    """Return migrated extras and classification without mutating ``extras``.

    ``volume`` is a top-level candles column in production. It remains an
    optional keyword for the offline helper and for legacy feather payloads
    that still carry it inside ``extras``. The returned mapping keeps the
    input mapping's unrelated keys unchanged.
    """
    volume = _number({"volume": volume}, "volume") if volume is not None else _number(extras, "volume")
    quote_volume = _number(extras, "quote_volume")
    taker_base = _number(extras, "taker_buy_base")
    taker_quote = _number(extras, "taker_buy_quote")
    old_flow = _number(extras, "flow_imbal")
    existing_base = _number(extras, "flow_imbal_base")
    existing_quote = _number(extras, "flow_imbal_q")
    if (volume is None or quote_volume is None or taker_base is None
            or taker_quote is None or volume <= 0 or quote_volume <= 0):
        return None, "missing_source_fields"
    if taker_base < 0 or taker_base > volume or taker_quote < 0 or taker_quote > quote_volume:
        return None, "invalid_source_values"

    # A present but non-numeric derived field is corruption, rather than a
    # missing field that this migration should silently overwrite.
    for name in ("flow_imbal", "flow_imbal_base", "flow_imbal_q"):
        if name in extras and extras[name] is not None and _number(extras, name) is None:
            return None, "flow_formula_conflict"

    base = 2 * taker_base / volume - 1
    quote = 2 * taker_quote / quote_volume - 1
    old_base = old_flow is not None and _close(old_flow, base)
    old_quote = old_flow is not None and _close(old_flow, quote)
    if old_flow is not None and not old_base and not old_quote:
        return None, "flow_formula_conflict"
    if existing_base is not None and not _close(existing_base, base):
        return None, "flow_formula_conflict"
    if existing_quote is not None and not _close(existing_quote, quote):
        return None, "flow_formula_conflict"

    migrated = dict(extras)
    migrated["flow_imbal_base"] = base
    migrated["flow_imbal"] = quote
    migrated["flow_imbal_q"] = quote
    classification = "already_canonical" if old_quote and not old_base else "migrated"
    if old_base and old_quote:
        classification = "formulae_equal"
    return migrated, classification


def _write_backup(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "exchange", "market", "pair", "timeframe", "candle_type", "timestamp", "extras",
        ))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "extras": json.dumps(row["extras"], ensure_ascii=True,
                                                          allow_nan=False, sort_keys=True)})
        handle.flush()
        os.fsync(handle.fileno())


def run(*, dsn: str, schema: str, apply: bool = False,
        backup: Path | None = None) -> dict[str, int]:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    if apply and backup is None:
        raise ValueError("--apply requires --backup")

    counts = {"eligible": 0, "migrated": 0, "already_canonical": 0,
              "formulae_equal": 0, "missing_source_fields": 0,
              "invalid_source_values": 0, "flow_formula_conflict": 0}
    changed: list[dict[str, Any]] = []
    updates: list[tuple[Any, ...]] = []
    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
        with conn.transaction():
            if apply:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            else:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            query = """SELECT exchange,market,pair,timeframe,candle_type,timestamp,volume,extras
                FROM candles WHERE candle_type='orderflow'
                ORDER BY exchange,market,pair,timeframe,timestamp"""
            if apply:
                # Hold each source row stable until its update is committed;
                # this prevents a concurrent collector write from being lost.
                query += " FOR UPDATE"
            for row in conn.execute(query):
                original = dict(row["extras"] or {})
                migrated, status = migrate_extras(original, volume=row["volume"])
                counts[status] += 1
                if migrated is None:
                    continue
                if migrated == original:
                    continue
                counts["eligible"] += 1
                changed.append({**{key: row[key] for key in (
                    "exchange", "market", "pair", "timeframe", "candle_type", "timestamp",
                )}, "extras": original})
                updates.append((Jsonb(migrated), row["exchange"], row["market"], row["pair"],
                                row["timeframe"], row["candle_type"], row["timestamp"]))

            if apply:
                assert backup is not None
                _write_backup(backup, changed)
                statement = """UPDATE candles SET extras=%s
                    WHERE exchange=%s AND market=%s AND pair=%s AND timeframe=%s
                      AND candle_type=%s AND timestamp=%s"""
                with conn.cursor() as cursor:
                    cursor.executemany(statement, updates)
                counts["migrated"] = len(updates)
            else:
                counts["migrated"] = 0
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="quant")
    parser.add_argument("--apply", action="store_true", help="write canonical fields after creating a backup")
    parser.add_argument("--backup", type=Path, help="new CSV path for pre-migration extras; required with --apply")
    args = parser.parse_args(argv)
    dsn = os.getenv("QUANT_DATABASE_URL", "")
    try:
        if not dsn:
            raise ValueError("QUANT_DATABASE_URL is not configured")
        result = run(dsn=dsn, schema=args.schema, apply=args.apply, backup=args.backup)
        print(json.dumps({"mode": "apply" if args.apply else "dry_run", **result}, ensure_ascii=False))
        return 1 if result["flow_formula_conflict"] or result["invalid_source_values"] else 0
    except Exception as exc:
        print(f"Flow imbalance migration failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
