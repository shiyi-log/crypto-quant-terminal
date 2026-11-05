#!/usr/bin/env python3
"""Audit official microstructure archives against the frozen execution stress.

This command is intentionally diagnostic.  Binance Data Vision ``aggTrades``
is useful for a trade-price path, while ``bookDepth`` is a 30-second,
percentage-bucket aggregate and is not a top-of-book/L2 event stream.  The
tool hashes and counts both sources, records their coverage, and emits a
credential-free pressure bundle.  It never turns 30-second depth into a
1,000 ms fill model and never creates a risk-gate event.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any, Mapping
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
SCENARIOS = (
    "A_long", "A_short", "B", "C", "joint_marginal_p99", "synthetic_max_envelope",
    "A_long_trade_only", "A_long_mark_only", "A_short_trade_only", "A_short_mark_only",
    "B_trade_only", "B_mark_only", "C_trade_only", "C_mark_only",
    "A_long_liquidation_1tick", "A_long_liquidation_5tick",
    "A_short_liquidation_1tick", "A_short_liquidation_5tick",
    "B_liquidation_1tick", "B_liquidation_5tick",
    "C_liquidation_1tick", "C_liquidation_5tick",
)
MODEL_SPEC = {
    "version": "account-risk-execution-pressure-v1",
    "slippage_multiple": "2",
    "delay_ms": 1000,
    "liquidation_slippage_ticks": [1, 5],
    "mark_role": "liquidation_trigger_and_unrealized_margin",
    "trade_role": "forced_fill_and_realized_pnl",
    "unknowns_are_not_zero": True,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_trade(path: Path) -> dict[str, Any]:
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError(f"{path} must contain one CSV")
        raw = archive.read(names[0])
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    required = {"transact_time", "price", "quantity", "is_buyer_maker"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} has an invalid aggTrades header")
    timestamps = [int(row["transact_time"]) for row in rows]
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    return {
        "rows": len(rows),
        "first_timestamp_ms": min(timestamps),
        "last_timestamp_ms": max(timestamps),
        "max_gap_ms": max(gaps, default=0),
        "p95_gap_ms": sorted(gaps)[max(0, int(len(gaps) * 0.95) - 1)] if gaps else 0,
        "gaps_over_1000ms": sum(gap > 1000 for gap in gaps),
        "timestamps": timestamps,
        "sha256": sha256_file(path),
    }


def _parse_depth(path: Path) -> dict[str, Any]:
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError(f"{path} must contain one CSV")
        raw = archive.read(names[0])
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    required = {"timestamp", "percentage", "depth", "notional"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} has an invalid bookDepth header")
    timestamps = [
        int(datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc).timestamp() * 1000)
        for row in rows
    ]
    unique_timestamps = sorted(set(timestamps))
    intervals = [b - a for a, b in zip(unique_timestamps, unique_timestamps[1:]) if b > a]
    percentages = sorted({row["percentage"] for row in rows}, key=Decimal)
    rows_per_timestamp = {
        timestamp: timestamps.count(timestamp) for timestamp in set(timestamps)
    }
    return {
        "rows": len(rows),
        "unique_timestamps": len(unique_timestamps),
        "first_timestamp_ms": min(timestamps),
        "last_timestamp_ms": max(timestamps),
        "intervals_ms": sorted(set(intervals)),
        "rows_per_timestamp": sorted(set(rows_per_timestamp.values())),
        "percentages": percentages,
        "sha256": sha256_file(path),
        "timestamp_coverage_is_30s": sorted(set(intervals)) == [30_000],
        "source_shape": "aggregate_percentage_depth_not_top_of_book",
    }


def _load_microstructure_manifest(path: Path) -> dict[str, Any]:
    """Load and validate the immutable 15m coverage manifest.

    The manifest is a coverage and provenance binding only.  Its 15m bars do
    not upgrade the 30-second ``bookDepth`` archive into execution-resolution
    evidence, so the diagnostic remains ``not_estimable``.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    coverage = raw.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError(f"{path} has no coverage object")
    required = {
        "15m_rows_per_symbol",
        "adjacent_15m_windows_per_symbol",
        "days_are_consecutive",
        "trade_mark_aligned",
    }
    missing = sorted(required - set(coverage))
    if missing:
        raise ValueError(f"{path} is missing coverage fields: {', '.join(missing)}")
    rows = coverage["15m_rows_per_symbol"]
    adjacent = coverage["adjacent_15m_windows_per_symbol"]
    if not isinstance(rows, int) or rows < 1:
        raise ValueError(f"{path} has invalid 15m_rows_per_symbol")
    if not isinstance(adjacent, int) or adjacent != rows - 1 or adjacent < 0:
        raise ValueError(f"{path} has invalid adjacent_15m_windows_per_symbol")
    days = raw.get("days_utc")
    if not isinstance(days, list) or not days:
        raise ValueError(f"{path} has no days_utc coverage")
    if not all(isinstance(day, str) and len(day) == 8 and day.isdigit() for day in days):
        raise ValueError(f"{path} has invalid days_utc coverage")
    return {
        "path": str(path),
        "path_sha256": sha256_file(path),
        "schema_version": raw.get("schema_version"),
        "source": raw.get("source"),
        "market": raw.get("market"),
        "days_utc": days,
        "coverage": {
            "15m_rows_per_symbol": rows,
            "adjacent_15m_windows_per_symbol": adjacent,
            "days_are_consecutive": bool(coverage["days_are_consecutive"]),
            "trade_mark_aligned": bool(coverage["trade_mark_aligned"]),
        },
        "limitations": list(raw.get("limitations", [])),
    }


def build_diagnostic(
    root: Path,
    *,
    tier_snapshot: Path | None = None,
    mark_archive: Path | None = None,
    microstructure_manifest: Path | None = None,
) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for symbol in SYMBOLS:
        trade_path = root / symbol / "aggTrades.zip"
        depth_path = root / symbol / "bookDepth.zip"
        if not trade_path.exists() or not depth_path.exists():
            raise ValueError(f"missing microstructure archive for {symbol}")
        symbols[symbol] = {
            "agg_trades": _parse_trade(trade_path),
            "book_depth": _parse_depth(depth_path),
        }
        # Do not duplicate all timestamps in the output bundle.
        symbols[symbol]["agg_trades"].pop("timestamps", None)
    tier = None
    if tier_snapshot is not None:
        raw = json.loads(tier_snapshot.read_text(encoding="utf-8"))
        tier = {
            "path_sha256": sha256_file(tier_snapshot),
            "snapshot_hash": raw.get("snapshot_hash"),
            "account_scope": raw.get("account_scope"),
            "as_of_utc": raw.get("as_of_utc"),
            "all_schedules_verified": all(
                bool(item.get("verified")) for item in raw.get("schedules", [])
            ),
        }
    mark = None
    if mark_archive is not None:
        mark = {
            "path_sha256": sha256_file(mark_archive),
            "size_bytes": mark_archive.stat().st_size,
            "granularity": "15m_ohlc",
            "role": "historical_mark_and_trade_candle_alignment_only",
            "execution_resolution_sufficient_for_1000ms": False,
        }
    manifest = None
    if microstructure_manifest is not None:
        manifest = _load_microstructure_manifest(microstructure_manifest)
    missing = [
        "historical_mark_price_series_at_execution_resolution",
        "top_of_book_or_L2_sequence_at_1000ms_after_trigger",
        "tick_size_and_liquidation_fee_evidence_bound_to_each_window",
        "insurance_fund_ADL_and_uncovered_deficit_observations",
    ]
    source_hashes = [
        symbols[symbol][kind]["sha256"]
        for symbol in SYMBOLS for kind in ("agg_trades", "book_depth")
    ]
    bundle_hash = hashlib.sha256("".join(sorted(source_hashes)).encode()).hexdigest()
    return {
        "schema_version": "account-risk-execution-pressure-diagnostic-v1",
        "status": "not_estimable",
        "model_spec": MODEL_SPEC,
        "model_sha256": hashlib.sha256(json.dumps(
            MODEL_SPEC, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "source_bundle_sha256": bundle_hash,
        "tier_snapshot": tier,
        "mark_source": mark,
        "microstructure_manifest": manifest,
        "symbols": symbols,
        "scenario_status": {
            scenario: {
                "status": "not_estimable",
                "missing_evidence": missing,
            } for scenario in SCENARIOS
        },
        "missing_evidence": missing,
        "limitations": [
            "the bound manifest reports 383 adjacent 15m windows per symbol across four consecutive UTC days, but 15m continuity does not recover intrabar trigger order",
            "bookDepth is a 30-second aggregate percentage-depth file, not a 100ms top-of-book/L2 event stream",
            "aggTrades has exchange trade timestamps but does not provide mark price or liquidation trigger order",
            "a 30-second observation cannot establish a 1,000ms delayed fill or a 1/5 tick liquidation fill",
            "no deficit observation is inferred from a candle or aggregate depth file",
        ],
        "strategy_signal_or_return_accessed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tier-snapshot", type=Path)
    parser.add_argument("--mark-archive", type=Path)
    parser.add_argument(
        "--microstructure-manifest",
        type=Path,
        help="immutable 15m coverage/provenance manifest to bind into the report",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_diagnostic(
        args.root,
        tier_snapshot=args.tier_snapshot,
        mark_archive=args.mark_archive,
        microstructure_manifest=args.microstructure_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["status"],
        "source_bundle_sha256": report["source_bundle_sha256"],
        "missing_evidence_count": len(report["missing_evidence"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
