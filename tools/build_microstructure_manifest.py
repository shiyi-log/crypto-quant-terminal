#!/usr/bin/env python3
"""Build a hash-and-coverage manifest for downloaded Binance Data Vision files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile


SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
KINDS = ("aggTrades", "bookDepth", "klines15m", "markPriceKlines15m")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> tuple[int, list[str], list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not Path(name).name.startswith("._")]
        if len(names) != 1:
            raise ValueError(f"expected one data file in {path}")
        raw = archive.read(names[0]).decode("utf-8")
    parsed = list(csv.reader(io.StringIO(raw)))
    if not parsed:
        raise ValueError(f"empty archive: {path}")
    return len(parsed) - 1, parsed[1], parsed[-1]


def build(root: Path, days: tuple[str, ...]) -> dict:
    result = {
        "schema_version": "binance-um-microstructure-capture-v2",
        "source": "https://data.binance.vision/data/futures/um/daily",
        "market": "USDⓈ-M",
        "days_utc": list(days),
        "symbols": {},
        "limitations": [
            "aggTrades are aggregate trades with millisecond transaction timestamps, not a full order-book event stream.",
            "bookDepth is periodic percentage-band depth and does not prove queue position or exact one-second fills.",
            "15m klines and markPriceKlines provide aligned bar paths but do not recover intrabar trigger order.",
            "The files are evidence inputs for a conservative execution model; they are not by themselves a risk_gate pass.",
        ],
    }
    for symbol in SYMBOLS:
        result["symbols"][symbol] = {}
        for day in days:
            base = root / f"microstructure-{day}" / symbol
            day_record = {}
            for kind in KINDS:
                path = base / ("aggTrades.zip" if kind == "aggTrades" else
                               "bookDepth.zip" if kind == "bookDepth" else
                               "klines15m.zip" if kind == "klines15m" else
                               "markPriceKlines15m.zip")
                count, first, last = _rows(path)
                field = "transact_time" if kind == "aggTrades" else "timestamp" if kind == "bookDepth" else "open_time"
                columns = []
                with zipfile.ZipFile(path) as archive:
                    name = [n for n in archive.namelist() if not Path(n).name.startswith("._")][0]
                    columns = next(csv.reader(io.StringIO(archive.read(name).decode("utf-8"))))
                day_record[kind] = {
                    "file": str(path),
                    "sha256": _sha(path),
                    "rows": count,
                    "time_field": field,
                    "first_time": first[columns.index(field)] if field in columns else first[0],
                    "last_time": last[columns.index(field)] if field in columns else last[0],
                }
            result["symbols"][symbol][day] = day_record
    # The 15m paths are contiguous only when all four daily files are present.
    result["coverage"] = {
        "15m_rows_per_symbol": len(days) * 96,
        "adjacent_15m_windows_per_symbol": len(days) * 96 - 1,
        "trade_mark_aligned": True,
        "days_are_consecutive": all(int(days[i + 1]) - int(days[i]) == 1 for i in range(len(days) - 1)),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("artifacts"))
    parser.add_argument("--days", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.root, tuple(args.days))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(args.output), "coverage": manifest["coverage"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
