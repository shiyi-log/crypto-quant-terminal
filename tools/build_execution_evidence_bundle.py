#!/usr/bin/env python3
"""Bind historical trade candidates to the available mark/depth evidence.

This command produces a credential-free, auditable *evidence bundle*.  It is
deliberately separate from :mod:`core.execution.account_risk_preflight`:
one-minute mark OHLC and thirty-second aggregate ``bookDepth`` observations
can be useful conservative bounds, but they do not prove a one-second mark
trigger, an L2 queue fill, or an insurance/ADL outcome.  The output therefore
remains ``not_estimable`` until those missing observations are supplied.
"""

from __future__ import annotations

import argparse
import bisect
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZipFile


SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
MARK_MS = 60_000
DEPTH_MS = 30_000
MISSING_EXECUTION_EVIDENCE = (
    "historical_mark_price_series_at_execution_resolution",
    "top_of_book_or_L2_sequence_at_1000ms_after_trigger",
    "fees_slippage_and_1000ms_delay_binding",
    "insurance_fund_ADL_and_uncovered_deficit_observations",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _zip_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist()
                 if not Path(name).name.startswith("._")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV member in {path}")
        return list(csv.DictReader(io.StringIO(archive.read(names[0]).decode("utf-8"))))


def _timestamp_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _decimal(value: object) -> str:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("non-finite decimal")
    return format(parsed, "f")


def _load_mark(path: Path) -> dict[int, dict[str, str]]:
    rows = _zip_rows(path)
    required = {"open_time", "open", "high", "low", "close", "close_time"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} has an invalid mark-price kline header")
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        opened = int(row["open_time"])
        if opened in result:
            raise ValueError(f"duplicate mark minute {opened} in {path}")
        result[opened] = {
            "open_time_ms": opened,
            "close_time_ms": int(row["close_time"]),
            "open": _decimal(row["open"]),
            "high": _decimal(row["high"]),
            "low": _decimal(row["low"]),
            "close": _decimal(row["close"]),
        }
    return result


def _load_depth(path: Path) -> tuple[list[int], dict[int, int], list[int]]:
    rows = _zip_rows(path)
    required = {"timestamp", "percentage", "depth", "notional"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} has an invalid bookDepth header")
    counts: dict[int, int] = {}
    for row in rows:
        timestamp = _timestamp_ms(row["timestamp"])
        counts[timestamp] = counts.get(timestamp, 0) + 1
    times = sorted(counts)
    intervals = sorted({b - a for a, b in zip(times, times[1:]) if b > a})
    # Data Vision timestamps are second-rounded and can drift by a few
    # seconds around the nominal 30-second cadence.  Preserve the observed
    # intervals in the bundle instead of falsely claiming exact cadence.
    if not intervals or min(intervals) < 1_000 or max(intervals) > 60_000:
        raise ValueError(f"{path} has an invalid bookDepth cadence")
    return times, counts, intervals


def _load_exchange_symbols(capture: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in capture.get("exchange_info", {}).get("symbols", []):
        symbol = item.get("symbol")
        if symbol not in SYMBOLS:
            continue
        filters = {row.get("filterType"): row for row in item.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER") or {}
        fees = capture.get("commission_rates", {}).get(f"{symbol[:-4]}/USDT:USDT", {})
        config = (capture.get("symbol_configs", {}).get(f"{symbol[:-4]}/USDT:USDT")
                  or [{}])[0]
        result[symbol] = {
            "tick_size": _decimal(price_filter.get("tickSize")),
            "liquidation_fee": _decimal(item.get("liquidationFee")),
            "maker_commission_rate": _decimal(fees.get("makerCommissionRate")),
            "taker_commission_rate": _decimal(fees.get("takerCommissionRate")),
            "leverage": int(config.get("leverage")),
            "margin_mode": str(config.get("margin_type", "")).lower(),
            "source_symbol": symbol,
        }
    missing = [symbol for symbol in SYMBOLS if symbol not in result]
    if missing:
        raise ValueError(f"capture is missing exchangeInfo for {', '.join(missing)}")
    return result


def _window_symbol_evidence(
    prepared: Mapping[str, Any],
    symbol: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    mark_rows = prepared["mark_rows"]
    depth_times = prepared["depth_times"]
    depth_counts = prepared["depth_counts"]
    depth_intervals = prepared["depth_intervals"]
    start_ms = int(window["window_start_ms"])
    end_ms = int(window["window_end_ms"])
    minute_start = start_ms // MARK_MS * MARK_MS
    mark = mark_rows.get(minute_start)
    if mark is None:
        raise ValueError(
            f"missing 1m mark row for {window.get('day', '<unknown-day')}"
            f"/{symbol}/{minute_start}"
        )
    index = bisect.bisect_right(depth_times, end_ms) - 1
    depth_timestamp = depth_times[index] if index >= 0 else None
    depth_age = None if depth_timestamp is None else end_ms - depth_timestamp
    candidate = window.get("symbols", {}).get(symbol) or {}
    return {
        "trade_path": {
            "trade_count": int(candidate.get("trade_count", 0)),
            "first_trade_ms": candidate.get("first_trade_ms"),
            "last_trade_ms": candidate.get("last_trade_ms"),
            "first_trade_price": candidate.get("first_trade_price"),
            "last_trade_price": candidate.get("last_trade_price"),
            "min_trade_price": candidate.get("min_trade_price"),
            "max_trade_price": candidate.get("max_trade_price"),
            "source_role": "observed_aggregate_trades",
        },
        "mark_1m_ohlc": {
            **mark,
            "source_role": "conservative_minute_bound_only",
            "execution_resolution_sufficient_for_1000ms": False,
            "intrasecond_trigger_order_known": False,
        },
        "book_depth_30s": {
            "timestamp_ms": depth_timestamp,
            "age_ms": depth_age,
            "row_count": None if depth_timestamp is None else depth_counts[depth_timestamp],
            "nominal_interval_ms": DEPTH_MS,
            "observed_intervals_ms": depth_intervals,
            "timestamp_coverage_is_exact_30s": depth_intervals == [DEPTH_MS],
            "source_role": "aggregate_percentage_depth_bound",
            "source_shape": "aggregate_percentage_depth_not_top_of_book",
            "execution_resolution_sufficient_for_1000ms": False,
        },
        "source_files": {
            "mark_price_1m": prepared["mark_source"],
            "book_depth_30s": prepared["depth_source"],
        },
    }


def _prepare_symbol(root: Path, day: str, symbol: str) -> dict[str, Any]:
    base = root / f"microstructure-{day}" / symbol
    mark_path = base / "markPriceKlines1m.zip"
    depth_path = base / "bookDepth.zip"
    if not mark_path.exists() or not depth_path.exists():
        raise ValueError(f"missing mark/depth archives for {day}/{symbol}")
    depth_times, depth_counts, depth_intervals = _load_depth(depth_path)
    return {
        "mark_rows": _load_mark(mark_path),
        "depth_times": depth_times,
        "depth_counts": depth_counts,
        "depth_intervals": depth_intervals,
        "mark_source": {"path": str(mark_path), "sha256": sha256_file(mark_path)},
        "depth_source": {"path": str(depth_path), "sha256": sha256_file(depth_path)},
    }


def build_bundle(
    root: Path,
    *,
    candidate_path: Path,
    capture_path: Path,
    tier_snapshot_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if candidate.get("status") != "candidate_not_pass":
        raise ValueError("historical candidate must remain candidate_not_pass")
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    tier = json.loads(tier_snapshot_path.read_text(encoding="utf-8"))
    exchange = _load_exchange_symbols(capture)
    windows = candidate.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("candidate has no windows")
    prepared = {
        (day, symbol): _prepare_symbol(root, day, symbol)
        for day in sorted({str(item.get("day")) for item in windows})
        for symbol in SYMBOLS
    }
    enriched = []
    for window in windows:
        day = str(window.get("day"))
        enriched.append({
            "day": day,
            "bar_index": int(window["bar_index"]),
            "window_start_ms": int(window["window_start_ms"]),
            "window_end_ms": int(window["window_end_ms"]),
            "symbols": {
                symbol: _window_symbol_evidence(prepared[(day, symbol)], symbol, window)
                for symbol in SYMBOLS
            },
        })
    source_files = {
        "candidate": {"path": str(candidate_path), "sha256": sha256_file(candidate_path)},
        "capture": {"path": str(capture_path), "sha256": sha256_file(capture_path)},
        "tier_snapshot": {"path": str(tier_snapshot_path), "sha256": sha256_file(tier_snapshot_path)},
    }
    if manifest_path is not None:
        source_files["microstructure_manifest"] = {
            "path": str(manifest_path), "sha256": sha256_file(manifest_path),
        }
    window_payload = {
        "candidate_windows_sha256": candidate.get("windows_sha256"),
        "windows": enriched,
    }
    window_hash = canonical_sha256(window_payload)
    return {
        "schema_version": "account-risk-execution-evidence-bundle-v1",
        "status": "not_estimable",
        "account": {
            "environment": capture.get("environment"),
            "endpoint": capture.get("endpoint"),
            "account_scope": tier.get("account_scope"),
            "initial_equity_usdt": capture.get("balance", {}).get("available_balance"),
            "margin_mode": "cross",
            "per_symbol": exchange,
        },
        "tier_snapshot": {
            "snapshot_hash": tier.get("snapshot_hash") or tier.get("tier_snapshot_hash"),
            "account_scope": tier.get("account_scope"),
            "as_of_utc": tier.get("as_of_utc"),
            "schedule_count": len(tier.get("schedules", [])),
        },
        "execution_model": {
            "slippage_multiple": "2",
            "delay_ms": 1000,
            "liquidation_slippage_ticks": [1, 5],
            "trade_role": "forced_fill_and_realized_pnl",
            "mark_role": "liquidation_trigger_and_unrealized_margin",
            "unknowns_are_not_zero": True,
            "model_status": "candidate_bound_not_execution_verified",
        },
        "candidate_window_count": len(enriched),
        "required_window_count": 100,
        "window_data_sha256": window_hash,
        "windows": enriched,
        "missing_execution_evidence": list(MISSING_EXECUTION_EVIDENCE),
        "limitations": [
            "1m mark OHLC bounds do not recover the intrasecond liquidation trigger order",
            "30s bookDepth is aggregate percentage depth, not top-of-book or L2 queue data",
            "aggregate trades do not prove a delayed fill at 1000ms or queue position",
            "static fee/tick fields are bound to the capture snapshot, not each historical execution window",
            "no insurance-fund, ADL, or uncovered-deficit observation is inferred",
        ],
        "source_files": source_files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("artifacts"))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--tier-snapshot", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    bundle = build_bundle(
        args.root, candidate_path=args.candidate, capture_path=args.capture,
        tier_snapshot_path=args.tier_snapshot, manifest_path=args.manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": bundle["status"],
        "candidate_window_count": bundle["candidate_window_count"],
        "window_data_sha256": bundle["window_data_sha256"],
        "missing_execution_evidence": len(bundle["missing_execution_evidence"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
