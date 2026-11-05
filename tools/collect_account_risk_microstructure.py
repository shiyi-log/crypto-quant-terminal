#!/usr/bin/env python3
"""Build a read-only microstructure coverage audit for account-risk windows.

The account-risk preflight needs synchronized trade/mark windows plus a
defensible execution model.  This tool only audits the first part: it reads
the public futures ``orderbooks`` and ``ticks`` tables and records which
aligned 15-minute windows have enough observations for a later execution
runner.  It never contacts an exchange, reads credentials, writes the
database, or labels a window as a passing risk scenario.

The resulting ``coverage_summary_sha256`` binds the row-count and continuity
summary used for the audit.  It is deliberately not a replacement for the
raw microstructure evidence hash required by ``ExecutionEvidence``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_config import load_environment


PAIRS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "TRX/USDT:USDT",
)
WINDOW_MS = 15 * 60 * 1000
DEFAULT_MIN_WINDOWS = 100
DEFAULT_MAX_ORDERBOOK_GAP_MS = 1000
DEFAULT_MAX_TICK_GAP_MS = 1000
SCHEMA_VERSION = "account-risk-microstructure-coverage-v1"


def _canonical(value: Any) -> Any:
    """Convert values to a stable JSON-safe shape without floats."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def aligned_window_start(timestamp_ms: int, window_ms: int = WINDOW_MS) -> int:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise ValueError("timestamp_ms must be an integer")
    if isinstance(window_ms, bool) or not isinstance(window_ms, int) or window_ms <= 0:
        raise ValueError("window_ms must be a positive integer")
    return timestamp_ms // window_ms * window_ms


def _positive_gaps(timestamps: Sequence[int]) -> list[int]:
    ordered = sorted(set(timestamps))
    return [current - previous for previous, current in zip(ordered, ordered[1:])
            if current > previous]


def sequence_quality(
    timestamps: Iterable[int],
    *,
    window_start_ms: int,
    window_end_ms: int,
    min_rows: int,
    max_gap_ms: int,
) -> dict[str, Any]:
    """Summarize one timestamp sequence inside a closed window.

    Rows are counted only when their exchange timestamp falls inside the
    window.  A sequence with no rows, a leading/trailing hole, or a gap over
    ``max_gap_ms`` is not eligible for a 1,000 ms execution stress.
    """
    if window_end_ms <= window_start_ms:
        raise ValueError("window_end_ms must be after window_start_ms")
    if min_rows < 1 or max_gap_ms < 1:
        raise ValueError("min_rows and max_gap_ms must be positive")
    values = sorted({
        int(timestamp) for timestamp in timestamps
        if window_start_ms <= int(timestamp) < window_end_ms
    })
    gaps = _positive_gaps(values)
    leading_gap = values[0] - window_start_ms if values else window_end_ms - window_start_ms
    trailing_gap = window_end_ms - values[-1] if values else window_end_ms - window_start_ms
    max_internal_gap = max(gaps, default=0)
    eligible = bool(
        len(values) >= min_rows
        and leading_gap <= max_gap_ms
        and trailing_gap <= max_gap_ms
        and max_internal_gap <= max_gap_ms
    )
    return {
        "row_count": len(values),
        "first_timestamp_ms": values[0] if values else None,
        "last_timestamp_ms": values[-1] if values else None,
        "leading_gap_ms": leading_gap,
        "trailing_gap_ms": trailing_gap,
        "max_internal_gap_ms": max_internal_gap,
        "max_gap_ms": max_gap_ms,
        "min_rows": min_rows,
        "eligible": eligible,
    }


def _window_rows(
    conn: Any,
    *,
    table: str,
    pair: str,
    start_ms: int,
    end_ms: int,
) -> list[int]:
    if table not in {"orderbooks", "ticks"}:
        raise ValueError("unsupported table")
    columns = {
        "orderbooks": "timestamp,last_update_id,bids,asks,extras,received_at",
        "ticks": "trade_id,timestamp,price,quantity,buyer_maker,extras,received_at",
    }[table]
    rows = conn.execute(
        f"""SELECT {columns} FROM quant.{table}
             WHERE exchange='binance' AND market='futures' AND pair=%s
               AND timestamp >= %s AND timestamp < %s
             ORDER BY timestamp""",
        (pair, start_ms, end_ms),
    ).fetchall()
    return rows


def _row_timestamps(rows: Sequence[Any]) -> list[int]:
    return [int(row[0] if len(row) else 0) for row in rows]


def _rows_sha256(rows: Sequence[Any]) -> str:
    payload = [_canonical(tuple(row)) for row in rows]
    return canonical_sha256(payload)


def _candidate_window(
    conn: Any,
    *,
    start_ms: int,
    window_ms: int,
    min_orderbooks: int,
    min_ticks: int,
    max_orderbook_gap_ms: int,
    max_tick_gap_ms: int,
) -> dict[str, Any]:
    end_ms = start_ms + window_ms
    pair_reports: dict[str, Any] = {}
    # A bounded orderbook sequence is the primary candidate for the frozen
    # 1,000 ms fill bound.  Trade rows are retained as an independent
    # diagnostic because sparse aggregate trades (especially TRX) naturally
    # have gaps over one second; rejecting a whole window solely for that
    # would hide usable book evidence.  The execution runner must still bind
    # its chosen trade/mark path explicitly before preflight.
    eligible = True
    for pair in PAIRS:
        orderbook_rows = _window_rows(
            conn, table="orderbooks", pair=pair, start_ms=start_ms, end_ms=end_ms,
        )
        tick_rows = _window_rows(
            conn, table="ticks", pair=pair, start_ms=start_ms, end_ms=end_ms,
        )
        orderbook_times = _row_timestamps(orderbook_rows)
        tick_times = [int(row[1]) for row in tick_rows]
        orderbook = sequence_quality(
            orderbook_times, window_start_ms=start_ms, window_end_ms=end_ms,
            min_rows=min_orderbooks, max_gap_ms=max_orderbook_gap_ms,
        )
        ticks = sequence_quality(
            tick_times, window_start_ms=start_ms, window_end_ms=end_ms,
            min_rows=min_ticks, max_gap_ms=max_tick_gap_ms,
        )
        pair_reports[pair] = {
            "orderbooks": {**orderbook, "rows_sha256": _rows_sha256(orderbook_rows)},
            "ticks": {**ticks, "rows_sha256": _rows_sha256(tick_rows)},
        }
        eligible = eligible and orderbook["eligible"]
    return {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "eligible": eligible,
        "pairs": pair_reports,
    }


def _candidate_starts(conn: Any, *, window_ms: int) -> list[int]:
    row = conn.execute(
        """SELECT min(timestamp), max(timestamp) FROM quant.orderbooks
             WHERE exchange='binance' AND market='futures'
               AND pair = ANY(%s)""",
        (list(PAIRS),),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return []
    first = aligned_window_start(int(row[0]), window_ms)
    last = aligned_window_start(int(row[1]), window_ms)
    return list(range(first, last + 1, window_ms))


def build_coverage_report(
    conn: Any,
    *,
    as_of_ms: int | None = None,
    window_ms: int = WINDOW_MS,
    min_windows: int = DEFAULT_MIN_WINDOWS,
    min_orderbooks: int = 100,
    min_ticks: int = 1,
    max_orderbook_gap_ms: int = DEFAULT_MAX_ORDERBOOK_GAP_MS,
    max_tick_gap_ms: int = DEFAULT_MAX_TICK_GAP_MS,
) -> dict[str, Any]:
    """Return a reproducible, read-only coverage report."""
    if window_ms != WINDOW_MS:
        raise ValueError("risk windows are frozen at 15 minutes")
    if min_windows < 1 or min_orderbooks < 1 or min_ticks < 1:
        raise ValueError("sample and row thresholds must be positive")
    if as_of_ms is None:
        row = conn.execute(
            """SELECT max(timestamp) FROM quant.orderbooks
                 WHERE exchange='binance' AND market='futures'
                   AND pair = ANY(%s)""",
            (list(PAIRS),),
        ).fetchone()
        as_of_ms = None if row is None or row[0] is None else int(row[0])
    if as_of_ms is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_estimable",
            "reason": "no futures orderbook rows",
            "as_of_ms": None,
            "window_count": 0,
            "eligible_window_count": 0,
            "min_windows": min_windows,
            "windows": [],
            "coverage_summary_sha256": canonical_sha256([]),
            "execution_evidence_ready": False,
        }
    as_of_ms = int(as_of_ms)
    latest_complete_start = aligned_window_start(as_of_ms - window_ms, window_ms)
    starts = [start for start in _candidate_starts(conn, window_ms=window_ms)
              if start + window_ms <= as_of_ms and start <= latest_complete_start]
    windows = [
        _candidate_window(
            conn, start_ms=start, window_ms=window_ms,
            min_orderbooks=min_orderbooks, min_ticks=min_ticks,
            max_orderbook_gap_ms=max_orderbook_gap_ms,
            max_tick_gap_ms=max_tick_gap_ms,
        )
        for start in starts
    ]
    eligible_windows = [item for item in windows if item["eligible"]]
    # The hash covers only the auditable summary, not raw book/tick payloads.
    # A later execution runner must add a raw-window hash before preflight.
    summary_hash = canonical_sha256(windows)
    status = "pass" if len(eligible_windows) >= min_windows else "not_estimable"
    reason = None if status == "pass" else (
        f"eligible 15m windows {len(eligible_windows)} < required {min_windows}"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "as_of_ms": as_of_ms,
        "as_of_utc": datetime.fromtimestamp(as_of_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_ms": window_ms,
        "window_count": len(windows),
        "eligible_window_count": len(eligible_windows),
        "min_windows": min_windows,
        "min_orderbooks": min_orderbooks,
        "min_ticks": min_ticks,
        "max_orderbook_gap_ms": max_orderbook_gap_ms,
        "max_tick_gap_ms": max_tick_gap_ms,
        "pairs": list(PAIRS),
        "windows": windows,
        "coverage_summary_sha256": summary_hash,
        # Coverage is necessary, but never sufficient for account risk pass.
        "execution_evidence_ready": False,
        "execution_evidence_reason": (
            "coverage summary is not a frozen execution model; mark-price windows, "
            "fees, liquidation, insurance/ADL and deficit evidence remain required"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of-ms", type=int)
    parser.add_argument("--min-windows", type=int, default=DEFAULT_MIN_WINDOWS)
    parser.add_argument("--min-orderbooks", type=int, default=100)
    parser.add_argument("--min-ticks", type=int, default=1)
    parser.add_argument("--max-orderbook-gap-ms", type=int, default=DEFAULT_MAX_ORDERBOOK_GAP_MS)
    parser.add_argument("--max-tick-gap-ms", type=int, default=DEFAULT_MAX_TICK_GAP_MS)
    args = parser.parse_args(argv)
    load_environment(ROOT)
    dsn = os.getenv("QUANT_DATABASE_URL")
    if not dsn:
        parser.error("QUANT_DATABASE_URL is required")
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET default_transaction_read_only TO on")
            conn.execute("SET statement_timeout TO '120s'")
            report = build_coverage_report(
                conn, as_of_ms=args.as_of_ms, min_windows=args.min_windows,
                min_orderbooks=args.min_orderbooks, min_ticks=args.min_ticks,
                max_orderbook_gap_ms=args.max_orderbook_gap_ms,
                max_tick_gap_ms=args.max_tick_gap_ms,
            )
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["status"],
        "eligible_window_count": report["eligible_window_count"],
        "min_windows": report["min_windows"],
        "coverage_summary_sha256": report["coverage_summary_sha256"],
        "execution_evidence_ready": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
