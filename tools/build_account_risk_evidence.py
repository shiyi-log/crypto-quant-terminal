#!/usr/bin/env python3
"""Build a read-only account-risk evidence diagnostic from frozen 15m data.

The archived trade/mark candles are useful for synchronized window coverage and
gap statistics, but they do not prove a 1,000 ms fill or liquidation path.  This
tool therefore emits a diagnostic document with all frozen scenario ids and
stable model/assumption hashes.  It never creates a research-ledger event and
never labels candle-only evidence as executable ``pass``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from decimal import Decimal
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.account_risk_preflight import REQUIRED_SCENARIO_EVIDENCE


SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
INTERVAL_MS = 900_000
MODEL_VERSION = "account-risk-candle-diagnostic-v1"
MISSING_EXECUTION_EVIDENCE = (
    "fees_slippage_and_1000ms_delay",
    "symbol_tick_sizes_and_liquidation_fee",
    "bankruptcy_insurance_adl_and_deficit_rules",
    "historical_1s_or_orderbook_fill_bound",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _read_trade_zip(raw: bytes) -> dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]:
    result: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError("trade archive must contain exactly one CSV")
        lines = archive.read(names[0]).decode("utf-8").splitlines()
    if not lines or not lines[0].startswith("open_time,"):
        raise ValueError("trade CSV header is invalid")
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) < 5:
            raise ValueError("trade CSV row is incomplete")
        timestamp = int(fields[0])
        row = tuple(Decimal(fields[index]) for index in (1, 2, 3, 4))
        if timestamp in result:
            raise ValueError("duplicate trade candle timestamp")
        if any(value <= 0 or not value.is_finite() for value in row):
            raise ValueError("trade candle contains an invalid price")
        result[timestamp] = row  # type: ignore[assignment]
    return result


def _read_mark_json(raw: bytes) -> dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]:
    payload = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=int)
    if not isinstance(payload, list):
        raise ValueError("mark response must be an array")
    result: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for row in payload:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("mark response row is incomplete")
        timestamp = int(row[0])
        values = tuple(Decimal(str(row[index])) for index in (1, 2, 3, 4))
        if timestamp in result:
            raise ValueError("duplicate mark candle timestamp")
        if any(value <= 0 or not value.is_finite() for value in values):
            raise ValueError("mark candle contains an invalid price")
        result[timestamp] = values  # type: ignore[assignment]
    return result


def load_series(archive_path: Path) -> tuple[
    dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
    dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
    str,
]:
    archive_bytes = archive_path.read_bytes()
    trade: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]] = {
        symbol: {} for symbol in SYMBOLS
    }
    mark: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]] = {
        symbol: {} for symbol in SYMBOLS
    }
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = name.split("/")
            if len(parts) < 4 or parts[0] != "raw" or parts[1] not in SYMBOLS:
                continue
            if Path(name).name.startswith("._"):
                continue
            symbol = parts[1]
            if not member.isfile():
                continue
            raw = archive.extractfile(member).read()  # type: ignore[union-attr]
            if parts[2] == "trade_ohlc" and name.endswith(".zip"):
                trade[symbol].update(_read_trade_zip(raw))
            elif parts[2] == "mark_ohlc" and name.endswith(".json"):
                mark[symbol].update(_read_mark_json(raw))
    if any(not trade[symbol] or not mark[symbol] for symbol in SYMBOLS):
        raise ValueError("archive is missing one or more symbol series")
    return trade, mark, _sha256(archive_bytes)


def _quantile(values: list[Decimal], required: int = 100) -> dict[str, Any]:
    values = sorted(value for value in values if value > 0)
    rank = (99 * len(values) + 99) // 100 if values else None
    return {
        "status": "pass" if len(values) >= required else "not_estimable",
        "p99": str(values[rank - 1]) if rank is not None and len(values) >= required else None,
        "maximum": str(values[-1]) if values and len(values) >= required else None,
        "sample_size": len(values),
        "required_sample_size": required,
        "rank": rank if len(values) >= required else None,
        "excluded_count": 0,
    }


def gap_summary(
    trade: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
    mark: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for side in ("long", "short"):
            item: dict[str, Any] = {"symbol": symbol, "side": side}
            for name, series in (("trade", trade[symbol]), ("mark", mark[symbol])):
                timestamps = sorted(series)
                values: list[Decimal] = []
                for previous, current in zip(timestamps, timestamps[1:]):
                    if current - previous != INTERVAL_MS:
                        continue
                    prior_close = series[previous][3]
                    next_open = series[current][0]
                    gap = (next_open - prior_close) / prior_close
                    adverse = -gap if side == "long" else gap
                    if adverse > 0:
                        values.append(adverse)
                item[name] = _quantile(values)
            rows.append(item)
    return rows


def synchronized_windows(
    trade: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
    mark: dict[str, dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]],
) -> tuple[list[int], dict[str, int]]:
    per_symbol = {
        symbol: set(trade[symbol]).intersection(mark[symbol])
        for symbol in SYMBOLS
    }
    common = sorted(set.intersection(*(per_symbol[symbol] for symbol in SYMBOLS)))
    usable = [
        timestamp for timestamp in common
        if timestamp + INTERVAL_MS in common
        and all(timestamp + INTERVAL_MS in trade[symbol] and timestamp + INTERVAL_MS in mark[symbol]
                for symbol in SYMBOLS)
    ]
    return usable, {symbol: len(per_symbol[symbol]) for symbol in SYMBOLS}


def build_diagnostic(archive_path: Path, *, bracket_path: Path | None = None) -> dict[str, Any]:
    trade, mark, archive_sha256 = load_series(archive_path)
    windows, per_symbol_rows = synchronized_windows(trade, mark)
    model_spec = {
        "version": MODEL_VERSION,
        "window": "t_close_to_t_plus_1_open_15m",
        "mark_role": "unrealized_margin_liquidation_trigger",
        "trade_role": "forced_fill_realized_pnl",
        "slippage_multiple": "2",
        "delay_ms": 1000,
        "liquidation_slippage_ticks": [1, 5],
        "requires_historical_microstructure": True,
    }
    assumptions = {
        "source_archive_sha256": archive_sha256,
        "synchronized_window_count": len(windows),
        "data_granularity": "15m_ohlc",
        "microstructure_evidence": "absent",
        "account_scope": "not_supplied",
    }
    model_sha256 = _sha256(_canonical(model_spec))
    assumption_sha256 = _sha256(_canonical(assumptions))
    bracket_sha256 = None
    if bracket_path is not None:
        bracket_sha256 = _sha256(bracket_path.read_bytes())
    scenarios = {
        scenario_id: {
            "status": "not_estimable",
            "window_count": len(windows),
            "model_sha256": model_sha256,
            "assumption_sha256": assumption_sha256,
            "tier_snapshot_input_sha256": bracket_sha256,
            "reason": "historical 1s/orderbook fill evidence is absent; 15m OHLC cannot prove 1000ms delay or 1/5 tick liquidation execution",
        }
        for scenario_id in REQUIRED_SCENARIO_EVIDENCE
    }
    return {
        "schema_version": "account-risk-evidence-diagnostic-v1",
        "status": "not_estimable",
        "model_sha256": model_sha256,
        "assumption_sha256": assumption_sha256,
        "archive_sha256": archive_sha256,
        "bracket_input_sha256": bracket_sha256,
        "symbols": list(SYMBOLS),
        "series_rows": per_symbol_rows,
        "synchronized_window_count": len(windows),
        "required_window_count": 100,
        "gap_p99s": gap_summary(trade, mark),
        "missing_execution_evidence": list(MISSING_EXECUTION_EVIDENCE),
        "scenarios": scenarios,
        "strategy_signal_or_return_accessed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--bracket", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    diagnostic = build_diagnostic(args.archive, bracket_path=args.bracket)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": diagnostic["status"],
        "synchronized_window_count": diagnostic["synchronized_window_count"],
        "scenario_count": len(diagnostic["scenarios"]),
        "model_sha256": diagnostic["model_sha256"],
        "assumption_sha256": diagnostic["assumption_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
