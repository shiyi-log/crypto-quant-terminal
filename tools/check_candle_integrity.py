#!/usr/bin/env python3
"""Read-only candle completeness and exchange reconciliation audit.

The normal invocation reads PostgreSQL in a repeatable-read, read-only
transaction.  An offline JSON snapshot can be supplied for CI and for audits
that must not touch the production database.  REST reconciliation is opt-in;
the default run never sends an exchange request.  Reports are written locally
with ``--output``.  ``--persist`` is also opt-in and writes the finished report
to the ``documents`` table without changing candle data.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


METHOD = "candle_integrity_check_v1"
REST_URL = "https://fapi.binance.com/fapi/v1/klines"
DEFAULT_REQUIRED_FIELDS = (
    "quote_volume",
    "num_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "flow_imbal",
    "flow_imbal_base",
)
DEFAULT_REST_FIELDS = (
    "num_trades",
    "quote_volume",
    "taker_buy_base",
    "taker_buy_quote",
)
TIMEFRAME_MS = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}
IDENTITY_FIELDS = ("exchange", "market", "pair", "timeframe", "candle_type")


def timeframe_milliseconds(timeframe: str) -> int:
    """Return a fixed-width timeframe in milliseconds."""
    match = re.fullmatch(r"([1-9]\d*)([smhdw])", str(timeframe))
    if match is None:
        raise ValueError(f"unsupported fixed-width timeframe: {timeframe!r}")
    count, unit = int(match.group(1)), match.group(2)
    return count * TIMEFRAME_MS[unit]


def _finite_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _nonnegative_integer(value: Any) -> bool:
    if not _finite_number(value):
        return False
    number = float(value)
    return number >= 0 and number.is_integer()


def _field_present(extras: dict[str, Any], name: str) -> bool:
    value = extras.get(name)
    if name == "num_trades":
        return _nonnegative_integer(value)
    if name in {"quote_volume", "taker_buy_base", "taker_buy_quote"}:
        return _finite_number(value) and float(value) >= 0
    if name in {"flow_imbal", "flow_imbal_base", "flow_imbal_q"}:
        return _finite_number(value) and -1 <= float(value) <= 1
    return _finite_number(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _normalize_dataset(value: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"dataset {index} must be an object")
    identity = {}
    for name in IDENTITY_FIELDS:
        item = value.get(name)
        if not isinstance(item, str) or not item:
            raise ValueError(f"dataset {index} has invalid {name}")
        identity[name] = item
    rows = value.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"dataset {index} rows must be a list")
    normalized = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"dataset {index} row {row_index} must be an object")
        try:
            timestamp = int(row["timestamp"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"dataset {index} row {row_index} has invalid timestamp") from exc
        if timestamp < 0 or float(row["timestamp"]) != timestamp:
            raise ValueError(f"dataset {index} row {row_index} has invalid timestamp")
        extras = row.get("extras", {})
        if not isinstance(extras, dict):
            raise ValueError(f"dataset {index} row {row_index} extras must be an object")
        normalized_row = {"timestamp": timestamp, "extras": extras}
        # ``volume`` is a candle column rather than an extra in PostgreSQL.
        # Keep it available for the base-volume imbalance formula while still
        # accepting offline snapshots that place it inside ``extras``.
        if "volume" in row:
            normalized_row["volume"] = row["volume"]
        normalized.append(normalized_row)
    return {**identity, "rows": normalized}


def normalize_snapshot(payload: Any) -> list[dict[str, Any]]:
    """Accept ``{"datasets": [...]}`` or a bare dataset list."""
    if isinstance(payload, list):
        datasets = payload
    elif isinstance(payload, dict) and isinstance(payload.get("datasets"), list):
        datasets = payload["datasets"]
    else:
        raise ValueError("input must contain a datasets list")
    normalized = [_normalize_dataset(dataset, index) for index, dataset in enumerate(datasets)]
    identities = [tuple(dataset[name] for name in IDENTITY_FIELDS) for dataset in normalized]
    if len(set(identities)) != len(identities):
        raise ValueError("input contains duplicate dataset identities")
    return normalized


def _gap_records(timestamps: list[int], interval: int, limit: int = 100) -> tuple[list[dict], int]:
    gaps: list[dict[str, Any]] = []
    for previous, current in zip(timestamps, timestamps[1:]):
        difference = current - previous
        if difference == interval:
            continue
        missing = difference // interval - 1 if difference > interval and difference % interval == 0 else None
        gaps.append({
            "after_timestamp": previous,
            "timestamp": current,
            "observed_interval_ms": difference,
            "expected_interval_ms": interval,
            "missing_bars": missing,
        })
    return gaps[:limit], len(gaps)


def inspect_dataset(
    dataset: dict[str, Any],
    *,
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS,
    min_coverage: float = 1.0,
    max_age_bars: int | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    """Inspect one normalized or raw dataset without I/O."""
    dataset = _normalize_dataset(dataset, 0)
    if (max_age_bars is not None
            and (isinstance(max_age_bars, bool) or max_age_bars < 0)):
        raise ValueError("max_age_bars must be non-negative")
    if as_of_ms is not None and (isinstance(as_of_ms, bool) or as_of_ms < 0):
        raise ValueError("as_of_ms must be non-negative")
    rows = dataset["rows"]
    timestamps = [row["timestamp"] for row in rows]
    counts = Counter(timestamps)
    unique_timestamps = sorted(counts)
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    result: dict[str, Any] = {
        **{name: dataset[name] for name in IDENTITY_FIELDS},
        "row_count": len(rows),
        "first_timestamp": unique_timestamps[0] if unique_timestamps else None,
        "last_timestamp": unique_timestamps[-1] if unique_timestamps else None,
        "duplicate_timestamp_count": duplicates,
        "gap_count": 0,
        "gaps": [],
        "fields": {},
        "flow_formula": {"status": "unknown", "checked": 0, "mismatch_count": 0, "mismatches": []},
        "rest_reconciliation": {"status": "not_requested"},
        "errors": [],
        "warnings": [],
    }
    try:
        interval = timeframe_milliseconds(dataset["timeframe"])
        result["expected_interval_ms"] = interval
        result["gaps"], result["gap_count"] = _gap_records(unique_timestamps, interval)
        if result["gap_count"]:
            result["errors"].append("timestamp_gaps")
        if max_age_bars is not None and unique_timestamps:
            observed_at = (as_of_ms if as_of_ms is not None
                           else int(datetime.now(timezone.utc).timestamp() * 1000))
            if observed_at < 0:
                raise ValueError("as_of_ms must be non-negative")
            tail_age_ms = max(0, observed_at - (unique_timestamps[-1] + interval))
            result["tail"] = {
                "as_of_ms": observed_at,
                "last_closed_end_ms": unique_timestamps[-1] + interval,
                "age_ms": tail_age_ms,
                "age_bars": tail_age_ms / interval,
                "max_age_bars": max_age_bars,
                "passes": tail_age_ms <= max_age_bars * interval,
            }
            if not result["tail"]["passes"]:
                result["errors"].append("stale_tail")
        elif max_age_bars is not None:
            result["tail"] = {
                "as_of_ms": as_of_ms,
                "last_closed_end_ms": None,
                "age_ms": None,
                "age_bars": None,
                "max_age_bars": max_age_bars,
                "passes": False,
            }
            # Empty datasets already have a more specific error.
            if "empty_dataset" not in result["errors"]:
                result["errors"].append("stale_tail")
    except ValueError:
        result["expected_interval_ms"] = None
        result["errors"].append("unsupported_timeframe")
    if duplicates:
        result["errors"].append("duplicate_timestamps")
    total = len(rows)
    if not rows:
        result["errors"].append("empty_dataset")
    for field in required_fields:
        present = sum(_field_present(row["extras"], field) for row in rows)
        coverage = present / total if total else 0.0
        passes = coverage >= min_coverage
        result["fields"][field] = {
            "present": present,
            "missing": total - present,
            "coverage": coverage,
            "min_coverage": min_coverage,
            "passes": passes,
        }
        if not passes:
            result["errors"].append(f"field_coverage:{field}")
    # ``flow_imbal`` is the canonical quote-volume convention.  The base
    # convention is kept in ``flow_imbal_base`` so consumers can select it
    # explicitly.  ``flow_imbal_q`` is an optional legacy alias for the quote
    # convention and is checked only on datasets that provide it.
    formula_specs = (
        ("flow_imbal", "quote_volume", "taker_buy_quote", "flow_imbal_formula", True),
        ("flow_imbal_base", "volume", "taker_buy_base", "flow_imbal_base_formula", True),
        ("flow_imbal_q", "quote_volume", "taker_buy_quote", "flow_imbal_q_formula", False),
    )
    formula_reports: dict[str, dict[str, Any]] = {}
    all_mismatches: list[dict[str, Any]] = []
    canonical_checked = 0
    canonical_unavailable = 0
    total_unavailable = 0
    for flow_name, denominator_name, taker_name, error_name, required in formula_specs:
        checked = 0
        unavailable = 0
        mismatches: list[dict[str, Any]] = []
        present_rows = 0
        for row in rows:
            extras = row["extras"]
            actual = extras.get(flow_name)
            if actual is None and not required:
                continue
            denominator = extras.get(denominator_name)
            if denominator is None and denominator_name == "volume":
                denominator = row.get("volume", extras.get("volume"))
            taker_value = extras.get(taker_name)
            if not (_finite_number(denominator) and _finite_number(taker_value)):
                continue
            present_rows += 1
            if float(denominator) <= 0:
                unavailable += 1
                continue
            if not _finite_number(actual):
                mismatch = {"timestamp": row["timestamp"], "actual": actual, "expected": None}
                mismatches.append(mismatch)
                all_mismatches.append({"formula": flow_name, **mismatch})
                continue
            checked += 1
            expected = 2 * float(taker_value) / float(denominator) - 1
            if not math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-9):
                mismatch = {
                    "timestamp": row["timestamp"], "actual": actual,
                    "expected": expected,
                }
                mismatches.append(mismatch)
                all_mismatches.append({"formula": flow_name, **mismatch})
        status = "passed" if checked and not mismatches else "failed" if mismatches else "unknown"
        if not required and not present_rows:
            status = "not_present"
        formula_reports[flow_name] = {
            "status": status,
            "checked": checked,
            "zero_denominator_count": unavailable,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches[:100],
            "denominator": denominator_name,
            "taker_field": taker_name,
            "required": required,
        }
        if flow_name == "flow_imbal":
            canonical_checked = checked
            canonical_unavailable = unavailable
        total_unavailable += unavailable
        if mismatches:
            result["errors"].append(error_name)
    flow_status = "failed" if all_mismatches else "passed" if canonical_checked else "unknown"
    result["flow_formula"] = {
        "status": flow_status,
        "checked": canonical_checked,
        "zero_quote_volume_count": canonical_unavailable,
        "zero_denominator_count": total_unavailable,
        "mismatch_count": len(all_mismatches),
        "mismatches": all_mismatches[:100],
        "canonical": "flow_imbal",
        "formulas": formula_reports,
    }
    if total_unavailable and not all_mismatches:
        result["warnings"].append("flow_formula_unavailable_for_zero_denominator")
    result["ok"] = not result["errors"]
    return result


def pair_to_symbol(pair: str) -> str:
    """Convert ccxt futures pair notation to Binance REST symbol."""
    match = re.fullmatch(r"([A-Za-z0-9]+)/(USDT|USDC)(?::(?:USDT|USDC))?", pair)
    if match is None:
        raise ValueError(f"unsupported REST pair: {pair!r}")
    return "".join(match.groups())


def fetch_rest_klines(
    pair: str,
    timeframe: str,
    *,
    limit: int = 10,
    now_ms: int | None = None,
    opener: Callable[..., Any] = urlopen,
    url: str = REST_URL,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1500:
        raise ValueError("REST sample limit must be between 1 and 1500")
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative")
    symbol = pair_to_symbol(pair)
    # Bound endTime to the last completed interval. This keeps the response at
    # exactly ``limit`` closed bars even when the exchange includes the current
    # forming candle by default.
    interval = timeframe_milliseconds(timeframe)
    end_time = (now_ms // interval) * interval - 1
    query = urlencode({"symbol": symbol, "interval": timeframe, "limit": limit,
                       "endTime": end_time})
    request = Request(f"{url}?{query}", headers={"User-Agent": "quant-candle-integrity/1"})
    with opener(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Binance REST returned a non-list payload")
    rows = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, list) or len(raw) < 12:
            raise ValueError(f"Binance REST kline {index} is malformed")
        close_time = int(raw[6])
        if close_time >= now_ms:
            continue
        rows.append({
            "timestamp": int(raw[0]),
            "quote_volume": float(raw[7]),
            "num_trades": int(raw[8]),
            "taker_buy_base": float(raw[9]),
            "taker_buy_quote": float(raw[10]),
        })
    if len(rows) != limit:
        raise ValueError(f"Binance REST returned {len(rows)} closed rows; expected {limit}")
    return rows


def reconcile_rest(
    dataset: dict[str, Any],
    *,
    rest_rows: list[dict[str, Any]],
    fields: tuple[str, ...] = DEFAULT_REST_FIELDS,
    tolerance: float = 0.0,
    expected_count: int | None = None,
) -> dict[str, Any]:
    local = {row["timestamp"]: row["extras"] for row in dataset["rows"]}
    compared = 0
    mismatches = []
    missing_local = []
    for remote in rest_rows:
        timestamp = remote["timestamp"]
        if timestamp not in local:
            missing_local.append(timestamp)
            continue
        compared += 1
        extras = local[timestamp]
        for field in fields:
            left, right = extras.get(field), remote.get(field)
            if field == "num_trades":
                matches = _nonnegative_integer(left) and int(left) == int(right)
            else:
                matches = _finite_number(left) and math.isclose(float(left), float(right), rel_tol=tolerance, abs_tol=tolerance)
            if not matches:
                mismatches.append({"timestamp": timestamp, "field": field, "local": left, "rest": right})
    count_mismatch = (expected_count is not None and len(rest_rows) != expected_count)
    status = "passed" if (compared and not mismatches and not missing_local
                           and not count_mismatch) else "failed"
    return {
        "status": status,
        "sample_count": len(rest_rows),
        "expected_sample_count": expected_count,
        "sample_count_matches": not count_mismatch,
        "compared_count": compared,
        "missing_local_count": len(missing_local),
        "missing_local_timestamps": missing_local[:100],
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "fields": list(fields),
    }


def inspect_snapshot(
    payload: Any,
    *,
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS,
    min_coverage: float = 1.0,
    rest_fetcher: Callable[[str, str], list[dict[str, Any]]] | None = None,
    max_age_bars: int | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    datasets = normalize_snapshot(payload)
    reports = []
    for dataset in datasets:
        report = inspect_dataset(
            dataset,
            required_fields=required_fields,
            min_coverage=min_coverage,
            max_age_bars=max_age_bars,
            as_of_ms=as_of_ms,
        )
        if rest_fetcher is not None:
            try:
                report["rest_reconciliation"] = reconcile_rest(
                    dataset, rest_rows=rest_fetcher(dataset["pair"], dataset["timeframe"])
                )
            except Exception as exc:  # report the dataset, never hide an audit failure
                report["rest_reconciliation"] = {"status": "error", "error_type": type(exc).__name__}
            if report["rest_reconciliation"]["status"] != "passed":
                report["errors"].append("rest_reconciliation")
            report["ok"] = not report["errors"]
        reports.append(report)
    snapshot_errors = ["empty_snapshot"] if not reports else []
    errors = sum(len(report["errors"]) for report in reports) + len(snapshot_errors)
    return {
        "method": METHOD,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": {"required_fields": list(required_fields), "min_coverage": min_coverage,
                   "max_age_bars": max_age_bars, "as_of_ms": as_of_ms},
        "dataset_count": len(reports),
        "ok": errors == 0,
        "error_count": errors,
        "errors": snapshot_errors,
        "datasets": reports,
    }


def read_database_snapshot(*, dsn: str, schema: str, pairs: list[str] | None = None,
                           timeframes: list[str] | None = None) -> dict[str, Any]:
    """Read candles without DataStore.initialize (which can run migrations)."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    conditions = ["exchange='binance'", "market='futures'"]
    params: list[Any] = []
    if pairs:
        conditions.append("pair = ANY(%s)")
        params.append(pairs)
    if timeframes:
        conditions.append("timeframe = ANY(%s)")
        params.append(timeframes)
    query = ("SELECT exchange,market,pair,timeframe,candle_type,timestamp,volume,extras "
             "FROM candles WHERE " + " AND ".join(conditions) + " "
             "ORDER BY exchange,market,pair,timeframe,candle_type,timestamp")
    datasets: dict[tuple[str, ...], dict[str, Any]] = {}
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            conn.execute("SET LOCAL statement_timeout TO '120s'")
            for row in conn.execute(query, params):
                identity = tuple(row[name] for name in IDENTITY_FIELDS)
                key = identity
                if key not in datasets:
                    datasets[key] = {name: row[name] for name in IDENTITY_FIELDS} | {"rows": []}
                datasets[key]["rows"].append({
                    "timestamp": row["timestamp"],
                    "volume": row["volume"],
                    "extras": row["extras"] or {},
                })
    # A requested dataset with no rows is still an integrity failure.  Keep it
    # in the snapshot so the report cannot confuse "absent" with "complete".
    if pairs and timeframes:
        for pair in pairs:
            for timeframe in timeframes:
                identity = ("binance", "futures", pair, timeframe, "futures")
                datasets.setdefault(identity, {
                    "exchange": identity[0], "market": identity[1], "pair": identity[2],
                    "timeframe": identity[3], "candle_type": identity[4], "rows": [],
                })
    return {"datasets": list(datasets.values())}


def persist_report(*, dsn: str, schema: str, key: str, report: dict[str, Any]) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            conn.execute("INSERT INTO documents (key,payload) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=CURRENT_TIMESTAMP",
                         (key, Jsonb(report)))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(payload), handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="offline JSON snapshot; otherwise read PostgreSQL")
    parser.add_argument("--schema", default="quant")
    parser.add_argument("--pair", action="append", dest="pairs")
    parser.add_argument("--timeframe", action="append", dest="timeframes")
    parser.add_argument("--required-field", action="append", dest="required_fields",
                        help="extras field required on every row; may be repeated")
    parser.add_argument("--min-coverage", type=float, default=1.0)
    parser.add_argument("--max-age-bars", type=int,
                        help="fail datasets whose final closed bar is older than this many bars")
    parser.add_argument("--as-of-ms", type=int,
                        help="observation time for deterministic tail checks")
    parser.add_argument("--rest", action="store_true", help="explicitly request Binance REST samples")
    parser.add_argument("--rest-limit", type=int, default=10,
                        help="number of closed REST bars to reconcile (1-1500)")
    parser.add_argument("--output", type=Path, help="write report JSON locally")
    parser.add_argument("--persist", action="store_true", help="write report to PostgreSQL documents")
    parser.add_argument("--document-key", default="candle_integrity/latest.json")
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.min_coverage <= 1:
            raise ValueError("--min-coverage must be between 0 and 1")
        if args.max_age_bars is not None and args.max_age_bars < 0:
            raise ValueError("--max-age-bars must be non-negative")
        if args.as_of_ms is not None and args.as_of_ms < 0:
            raise ValueError("--as-of-ms must be non-negative")
        if not 1 <= args.rest_limit <= 1500:
            raise ValueError("--rest-limit must be between 1 and 1500")
        required = tuple(args.required_fields or DEFAULT_REQUIRED_FIELDS)
        dsn = os.getenv("QUANT_DATABASE_URL", "")
        if args.input:
            payload = json.loads(args.input.read_text(encoding="utf-8"))
        else:
            if not dsn:
                raise ValueError("QUANT_DATABASE_URL is not configured")
            payload = read_database_snapshot(dsn=dsn, schema=args.schema, pairs=args.pairs, timeframes=args.timeframes)
        rest_fetcher = (lambda pair, timeframe: fetch_rest_klines(
            pair, timeframe, limit=args.rest_limit,
        )) if args.rest else None
        report = inspect_snapshot(
            payload,
            required_fields=required,
            min_coverage=args.min_coverage,
            rest_fetcher=rest_fetcher,
            max_age_bars=args.max_age_bars,
            as_of_ms=args.as_of_ms,
        )
        if args.output:
            _write_json(args.output, report)
        if args.persist:
            if not dsn:
                raise ValueError("--persist requires QUANT_DATABASE_URL")
            persist_report(dsn=dsn, schema=args.schema, key=args.document_key, report=report)
        print(json.dumps({"method": METHOD, "ok": report["ok"],
                          "dataset_count": report["dataset_count"], "error_count": report["error_count"],
                          "output": str(args.output) if args.output else None}, ensure_ascii=False))
        return 0 if report["ok"] else 1
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Candle integrity audit failed ({type(exc).__name__}); verify local input/configuration.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Candle integrity audit failed ({type(exc).__name__}); no candle writes were attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
