#!/usr/bin/env python3
"""Fill missing order-flow fields in existing Binance futures candles.

The command is deliberately dry-run by default.  ``--apply`` requires a new
JSONL backup and only updates the ``extras`` JSONB column of rows that already
exist in ``candle_type='futures'``.  It never inserts OHLCV rows and never
replaces a non-null target field, which makes it suitable for reconciling the
rich local ``orderflow`` archive with the live futures dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_sync import candle_identity, derived_flow_imbalance, timestamp_ms


TARGET_FIELDS = (
    "quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote",
    "flow_imbal", "flow_imbal_base", "flow_imbal_q", "sell_volume",
    "buy_ratio", "avg_trade_sz",
)
OHLCV_COLUMNS = {"date", "timestamp", "open", "high", "low", "close", "volume"}


def finite(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def source_extras(row: dict[str, Any]) -> dict[str, Any]:
    """Build canonical quote/base fields from raw order-flow columns."""
    result: dict[str, Any] = {}
    for name in TARGET_FIELDS:
        if name in {"flow_imbal", "flow_imbal_base", "flow_imbal_q"}:
            continue
        value = row.get(name)
        if value is None or not finite(value):
            continue
        if name == "num_trades":
            number = int(float(value))
            if number < 0 or float(value) != number:
                continue
            result[name] = number
        else:
            result[name] = float(value)

    quote = derived_flow_imbalance(row.get("taker_buy_quote"), row.get("quote_volume"))
    base = derived_flow_imbalance(row.get("taker_buy_base"), row.get("volume"))
    if quote is not None:
        # The source feather's historical flow_imbal is base-oriented; derive
        # canonical quote semantics from the raw quote fields instead.
        result["flow_imbal"] = quote
        result["flow_imbal_q"] = quote
    if base is not None:
        result["flow_imbal_base"] = base
    return result


def merge_missing(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any] | None:
    """Return an update containing only fields absent from the target row."""
    merged = dict(existing)
    changed = False
    for name, value in incoming.items():
        if merged.get(name) is None:
            merged[name] = value
            changed = True
    return merged if changed else None


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _safe_json(value.item())
    return value


def load_source(path: Path) -> list[tuple[int, dict[str, Any]]]:
    import pandas as pd

    frame = pd.read_feather(path).copy().reset_index(drop=True)
    if "date" not in frame.columns:
        raise ValueError(f"{path.name} lacks date")
    frame["timestamp"] = timestamp_ms(frame["date"])
    frame = frame[frame["timestamp"].notna() & (frame["timestamp"] >= 0)].copy()
    frame = frame.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    rows = []
    for raw in frame.to_dict(orient="records"):
        stamp = int(raw["timestamp"])
        rows.append((stamp, source_extras(raw)))
    return rows


class Backup:
    def __init__(self, path: Path | None):
        self.path = path
        self.handle = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(path, flags, 0o600)
            self.handle = os.fdopen(descriptor, "w", encoding="utf-8")

    def write(self, row: dict[str, Any]) -> None:
        if self.handle is None:
            return
        self.handle.write(json.dumps(_safe_json(row), ensure_ascii=True,
                                     allow_nan=False, sort_keys=True) + "\n")

    def close(self) -> None:
        if self.handle is not None:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.handle = None


def _identity_from_source(path: Path, data_root: Path) -> tuple[str, str, str, str, str]:
    identity = candle_identity(path, data_root)
    if identity[-1] != "orderflow":
        raise ValueError(f"source is not an orderflow feather: {path}")
    return identity


def run(*, root: Path, source_dir: Path, dsn: str, schema: str,
        pairs: set[str] | None = None, batch_size: int = 2000,
        apply: bool = False, backup: Path | None = None) -> dict[str, Any]:
    if apply and backup is None:
        raise ValueError("--apply requires --backup")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    data_root = source_dir.parent
    paths = sorted(source_dir.glob("*_5m_orderflow.feather"))
    if pairs:
        paths = [path for path in paths
                 if _identity_from_source(path, data_root)[2] in pairs]
    if not paths:
        raise ValueError("no matching orderflow feather files")

    counts = Counter()
    report: dict[str, Any] = {"mode": "apply" if apply else "dry_run",
                              "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                              "datasets": [], "counts": counts}
    journal = Backup(backup if apply else None)
    try:
        with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
            for path in paths:
                exchange, market, pair, timeframe, _ = _identity_from_source(path, data_root)
                source_rows = load_source(path)
                dataset = {"exchange": exchange, "market": market, "pair": pair,
                           "timeframe": timeframe, "source_rows": len(source_rows),
                           "updated": 0, "missing_target": 0, "unchanged": 0}
                for start in range(0, len(source_rows), batch_size):
                    batch = source_rows[start:start + batch_size]
                    begin, end = batch[0][0], batch[-1][0]
                    with conn.transaction():
                        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" +
                                     (", READ ONLY" if not apply else ""))
                        conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                            sql.Identifier(schema)))
                        query = ("SELECT exchange,market,pair,timeframe,candle_type,timestamp,extras "
                                 "FROM candles WHERE exchange=%s AND market=%s AND pair=%s "
                                 "AND timeframe=%s AND candle_type='futures' "
                                 "AND timestamp BETWEEN %s AND %s ORDER BY timestamp" +
                                 (" FOR UPDATE" if apply else ""))
                        rows = conn.execute(query, (exchange, market, pair, timeframe,
                                                    begin, end)).fetchall()
                        existing = {int(row["timestamp"]): row for row in rows}
                        updates = []
                        for timestamp, incoming in batch:
                            current = existing.get(timestamp)
                            if current is None:
                                dataset["missing_target"] += 1
                                counts["missing_target"] += 1
                                continue
                            original = dict(current["extras"] or {})
                            merged = merge_missing(original, incoming)
                            if merged is None:
                                dataset["unchanged"] += 1
                                counts["unchanged"] += 1
                                continue
                            dataset["updated"] += 1
                            counts["updated"] += 1
                            journal.write({"exchange": exchange, "market": market,
                                           "pair": pair, "timeframe": timeframe,
                                           "candle_type": "futures", "timestamp": timestamp,
                                           "extras": original})
                            updates.append((Jsonb(merged), exchange, market, pair,
                                            timeframe, "futures", timestamp))
                        if apply and updates:
                            with conn.cursor() as cursor:
                                cursor.executemany(
                                    "UPDATE candles SET extras=%s WHERE exchange=%s AND market=%s "
                                    "AND pair=%s AND timeframe=%s AND candle_type=%s AND timestamp=%s",
                                    updates)
                report["datasets"].append(dataset)
        return report
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--schema", default="quant")
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        from data_store import DataStore
        root = args.root.resolve()
        source_dir = (args.source_dir or root / "bot" / "user_data" / "data" / "orderflow").resolve()
        dsn = os.getenv("QUANT_DATABASE_URL", "")
        if not dsn:
            dsn = DataStore(root=root).dsn
        if not dsn:
            raise ValueError("QUANT_DATABASE_URL is not configured")
        result = run(root=root, source_dir=source_dir, dsn=dsn, schema=args.schema,
                     pairs=set(args.pairs or []), batch_size=args.batch_size,
                     apply=args.apply, backup=args.backup)
        print(json.dumps(_safe_json(result), ensure_ascii=False))
        return 1 if result["counts"].get("missing_target", 0) else 0
    except Exception as exc:
        print(f"Orderflow field backfill failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
