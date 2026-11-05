#!/usr/bin/env python3
"""Extract complete first-second trade windows from public Binance archives.

The output is an auditable candidate input for a conservative execution model.
It deliberately records that mark prices are 15m observations; it never labels
the result as a completed risk preflight or invents missing order-book events.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from decimal import Decimal
import zipfile

SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
BAR_MS = 900_000
DAY_MS = 86_400_000


def _zip_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if not Path(n).name.startswith("._")]
        if len(names) != 1:
            raise ValueError(f"expected one data member in {path}")
        text = archive.read(names[0]).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(value: str) -> str:
    return format(Decimal(value), "f")


def build(root: Path, days: tuple[str, ...]) -> dict:
    per_day: dict[str, dict[str, dict]] = {}
    source_hashes: dict[str, str] = {}
    for day in days:
        day_start = int(__import__("datetime").datetime.strptime(day, "%Y%m%d").replace(tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
        per_day[day] = {}
        for symbol in SYMBOLS:
            base = root / f"microstructure-{day}" / symbol
            agg_path = base / "aggTrades.zip"
            kline_path = base / "klines15m.zip"
            mark_path = base / "markPriceKlines15m.zip"
            for path in (agg_path, kline_path, mark_path):
                source_hashes[str(path)] = _sha(path)
            trades = _zip_rows(agg_path)
            bars = _zip_rows(kline_path)[0:96]
            marks = _zip_rows(mark_path)[0:96]
            if len(bars) != 96 or len(marks) != 96:
                raise ValueError(f"{day}/{symbol}: expected 96 15m bars")
            if [row["open_time"] for row in bars] != [row["open_time"] for row in marks]:
                raise ValueError(f"{day}/{symbol}: trade/mark 15m timestamps do not align")
            normalized = []
            for row in trades:
                normalized.append((int(row["transact_time"]), row["price"], row["quantity"]))
            normalized.sort(key=lambda item: item[0])
            per_day[day][symbol] = {"day_start": day_start, "trades": normalized, "bars": bars, "marks": marks}

    windows = []
    for day in days:
        day_start = per_day[day][SYMBOLS[0]]["day_start"]
        for index in range(96):
            start = day_start + index * BAR_MS
            end = start + 1000
            symbols = {}
            complete = True
            for symbol in SYMBOLS:
                data = per_day[day][symbol]
                selected = [row for row in data["trades"] if start <= row[0] < end]
                if not selected:
                    complete = False
                    symbols[symbol] = {"trade_count": 0, "trade_prices": [], "first_trade_ms": None, "last_trade_ms": None}
                    continue
                prices = [Decimal(row[1]) for row in selected]
                bar = data["bars"][index]
                mark = data["marks"][index]
                symbols[symbol] = {
                    "trade_count": len(selected),
                    "first_trade_ms": selected[0][0],
                    "last_trade_ms": selected[-1][0],
                    "first_trade_price": _decimal(selected[0][1]),
                    "last_trade_price": _decimal(selected[-1][1]),
                    "min_trade_price": _decimal(str(min(prices))),
                    "max_trade_price": _decimal(str(max(prices))),
                    "trade_open": _decimal(bar["open"]),
                    "trade_close": _decimal(bar["close"]),
                    "mark_open": _decimal(mark["open"]),
                    "mark_close": _decimal(mark["close"]),
                }
            if complete:
                windows.append({"day": day, "bar_index": index, "window_start_ms": start, "window_end_ms": end, "symbols": symbols})
    canonical = json.dumps({"source_hashes": source_hashes, "windows": windows}, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": "historical-1s-execution-window-candidate-v1",
        "status": "candidate_not_pass",
        "execution_resolution": "first_1000ms_aggregate_trade_with_15m_mark_observation",
        "symbols": list(SYMBOLS),
        "days_utc": list(days),
        "bar_ms": BAR_MS,
        "delay_ms": 1000,
        "complete_window_count": len(windows),
        "required_window_count": 100,
        "source_hashes": source_hashes,
        "windows_sha256": hashlib.sha256(canonical).hexdigest(),
        "windows": windows,
        "limitations": [
            "Aggregate trades are observed fills, not a full L2 queue model.",
            "Mark values are 15m open/close observations; intrasecond mark trigger order is unknown.",
            "No liquidation fee, insurance fund, ADL or deficit observation is inferred here.",
            "The candidate must remain separate from ExecutionEvidence until the frozen model is independently reviewed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("artifacts"))
    parser.add_argument("--days", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, tuple(args.days))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "complete_window_count": result["complete_window_count"], "windows_sha256": result["windows_sha256"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
