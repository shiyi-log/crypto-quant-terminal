#!/usr/bin/env python3
"""Resume an official TRXUSDT futures 5m/15m history backfill from 2024.

Dry-run by default. Completed months use SHA256-verified Binance Vision
archives; the current partial month uses bounded REST pages. Download caches
and a fixed observation boundary survive interruptions. Re-running with
--resume rechecks DB coverage, skips finalized windows and preserves all final
rows. --apply writes a private, exact write-ahead rollback journal, commits one
source window at a time, and never changes strategy/account/service state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repair_closed_candles import (
    IDENTITY, Journal, OHLCV, REST_URL, WHERE_DATASET, dataset_lock,
    journal_change, mark_rows, parse_rest_window, reconcile_rows,
    reconciled_partial, rest_action,
)

PAIR = "TRX/USDT:USDT"
SYMBOL = "TRXUSDT"
INTERVALS = {"5m": 300_000, "15m": 900_000}
ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
DEFAULT_START = "2024-01-01T00:00:00Z"
MAX_DOWNLOAD_BYTES = 8_000_000
MAX_CSV_BYTES = 12_000_000
REST_LIMIT = 1500
HEADER = ["open_time", "open", "high", "low", "close", "volume", "close_time",
          "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def iso_timestamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include UTC timezone")
    stamp = result.timestamp() * 1000
    if stamp < 0 or stamp != int(stamp):
        raise ValueError("invalid millisecond timestamp")
    return int(stamp)


def month_bounds(stamp):
    value = datetime.fromtimestamp(stamp / 1000, timezone.utc)
    first = datetime(value.year, value.month, 1, tzinfo=timezone.utc)
    last = datetime(value.year + (value.month == 12), value.month % 12 + 1,
                    1, tzinfo=timezone.utc)
    return int(first.timestamp() * 1000), int(last.timestamp() * 1000)


def windows(start, observed_at, timeframe):
    """Calendar months plus a partial tail; end is exclusive and frozen."""
    interval = INTERVALS[timeframe]
    boundary = observed_at // interval * interval
    if start % interval or start >= boundary:
        raise ValueError("start must be aligned and precede the last closed boundary")
    current = start
    while current < boundary:
        first, next_month = month_bounds(current)
        end = min(next_month, boundary)
        yield {"timeframe": timeframe, "start": current, "end": end,
               "month_start": first, "month_end": next_month,
               "month": datetime.fromtimestamp(first / 1000, timezone.utc).strftime("%Y-%m"),
               "archive": next_month <= boundary,
               "expected": (end - current) // interval}
        current = end


def atomic_json(path, payload):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class OfficialClient:
    def __init__(self, timeout=25, tries=4, opener=urlopen, sleeper=time.sleep):
        self.timeout, self.tries = timeout, tries
        self.opener, self.sleeper = opener, sleeper
        self.requests, self.bytes = 0, 0

    def get(self, url):
        for attempt in range(self.tries):
            self.requests += 1
            try:
                with self.opener(url, timeout=self.timeout) as response:
                    data = response.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("official response exceeds size limit")
                self.bytes += len(data)
                return data
            except HTTPError as exc:
                if exc.code == 404 or exc.code not in {429, 500, 502, 503, 504}:
                    raise
                retry = float(exc.headers.get("Retry-After", 2 ** attempt))
                # Do not violate an exchange ban by shortening Retry-After.
                if not 0 <= retry <= 60 or attempt + 1 == self.tries:
                    raise
                self.sleeper(retry)
            except (URLError, TimeoutError, ConnectionError):
                if attempt + 1 == self.tries:
                    raise
                self.sleeper(min(2 ** attempt, 10))
        raise RuntimeError("official download exhausted retries")


def archive_url(timeframe, month):
    name = f"{SYMBOL}-{timeframe}-{month}.zip"
    return f"{ARCHIVE_ROOT}/{SYMBOL}/{timeframe}/{name}", name


def verified_archive(client, cache, timeframe, month):
    url, name = archive_url(timeframe, month)
    path = Path(cache) / name
    checksum_path = path.with_name(name + ".CHECKSUM")
    if not checksum_path.exists():
        checksum = client.get(url + ".CHECKSUM")
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?" + re.escape(name) + r"\s*",
                             checksum.decode("ascii"))
        if match is None:
            raise ValueError("archive checksum must name the exact TRX source")
        checksum_path.write_bytes(checksum)
        checksum_path.chmod(0o600)
    checksum = checksum_path.read_bytes()
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?" + re.escape(name) + r"\s*",
                         checksum.decode("ascii"))
    if match is None:
        raise ValueError("cached archive checksum is invalid")
    if not path.exists():
        payload = client.get(url)
        if hashlib.sha256(payload).hexdigest() != match[1].lower():
            raise ValueError("official archive SHA256 mismatch")
        temp = path.with_suffix(".tmp")
        temp.write_bytes(payload)
        temp.chmod(0o600)
        temp.replace(path)
    payload = path.read_bytes()
    if len(payload) > MAX_DOWNLOAD_BYTES or hashlib.sha256(payload).hexdigest() != match[1].lower():
        raise ValueError("cached archive SHA256 mismatch")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if (len(entries) != 1 or entries[0].filename != name.replace(".zip", ".csv")
                or entries[0].file_size > MAX_CSV_BYTES):
            raise ValueError("unexpected Binance archive member")
        with archive.open(entries[0]) as handle:
            records = list(csv.reader(io.TextIOWrapper(handle, encoding="utf-8")))
    if records and records[0] == HEADER:
        records = records[1:]
    return records, {"kind": "monthly_archive", "url": url,
                     "sha256": match[1].lower(), "cache": name, "bytes": len(payload)}


def normalized_rows(payload, timeframe, start, end, observed_at, provenance):
    interval = INTERVALS[timeframe]
    expected = (end - start) // interval
    if (start % interval or end % interval or end > observed_at
            or expected <= 0 or len(payload) != expected):
        raise ValueError("official history window must be exact, aligned and closed")
    # Reuse the repair tool's strict OHLCV, taker volume and timestamp checks.
    rows = []
    for offset in range(0, expected, REST_LIMIT):
        page = payload[offset:offset + REST_LIMIT]
        page_end = start + (offset + len(page)) * interval
        rows.extend(parse_rest_window(page, PAIR, timeframe, page_end, len(page)))
    for row in rows:
        extras = row["extras"]
        extras.pop("repair_source")
        extras.pop("repair_requested_at")
        # This is the source observation time, not a fabricated WS event. The
        # DataStore guard uses it to prevent a later unversioned file import
        # from replacing verified final OHLCV/taker fields. Final WS still wins.
        extras["live_snapshot_timestamp"] = observed_at
        extras.update(backfill_source=provenance["url"], backfill_observed_at=observed_at)
        if "sha256" in provenance:
            extras["backfill_source_sha256"] = provenance["sha256"]
    return rows


def rest_rows(client, cache, window, observed_at):
    interval = INTERVALS[window["timeframe"]]
    rows, sources = [], []
    for start in range(window["start"], window["end"], REST_LIMIT * interval):
        end = min(start + REST_LIMIT * interval, window["end"])
        count = (end - start) // interval
        name = f"{SYMBOL}-{window['timeframe']}-{start}-{end}.json"
        path = Path(cache) / name
        url = REST_URL + "?" + urlencode({"symbol": SYMBOL, "interval": window["timeframe"],
                                          "startTime": start, "endTime": end - 1, "limit": count})
        if not path.exists():
            payload = client.get(url)
            parsed = json.loads(payload)
            # Validate before accepting this cache as resumable evidence.
            normalized_rows(parsed, window["timeframe"], start, end, observed_at, {"url": url})
            atomic_json(path, {"payload": parsed, "sha256": hashlib.sha256(
                json.dumps(parsed, separators=(",", ":"), allow_nan=False).encode()).hexdigest()})
        cached = json.loads(path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(json.dumps(cached["payload"], separators=(",", ":"),
                                            allow_nan=False).encode()).hexdigest()
        if digest != cached["sha256"]:
            raise ValueError("cached REST SHA256 mismatch")
        provenance = {"kind": "rest", "url": url, "cache": name,
                      "payload_sha256": cached["sha256"]}
        rows.extend(normalized_rows(cached["payload"], window["timeframe"], start, end,
                                    observed_at, provenance))
        sources.append(provenance)
    return rows, sources


def source_rows(client, cache, window, observed_at):
    if window["archive"]:
        try:
            payload, provenance = verified_archive(client, cache, window["timeframe"], window["month"])
        except HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            full = normalized_rows(payload, window["timeframe"], window["month_start"],
                                   window["month_end"], observed_at, provenance)
            return [row for row in full if window["start"] <= row["timestamp"] < window["end"]], [provenance]
    return rest_rows(client, cache, window, observed_at)


def identity(timeframe):
    return "binance", "futures", PAIR, timeframe, "futures"


def coverage(conn, window):
    interval = INTERVALS[window["timeframe"]]
    row = conn.execute("SELECT count(*) AS total,count(*) FILTER (WHERE extras->'closed'='true'::jsonb) AS closed,"
                       "count(*) FILTER (WHERE timestamp %% %s<>0) AS misaligned,"
                       "min(timestamp) AS first,max(timestamp) AS last FROM candles WHERE "
                       + WHERE_DATASET + " AND timestamp>=%s AND timestamp<%s",
                       (interval, *identity(window["timeframe"]), window["start"], window["end"])).fetchone()
    return {**row, "expected": window["expected"],
            "complete": (row["closed"] == row["total"] == window["expected"]
                         and row["misaligned"] == 0)}


def insert_rows(conn, rows, journal, dataset_id):
    from psycopg.types.json import Jsonb
    if not rows:
        return
    for row in rows:
        journal_change(journal, dataset_id, "insert_missing", None, row)
    journal.flush()
    columns = (*IDENTITY, "timestamp", *OHLCV, "extras", "source_key")
    # COPY is efficient for a multi-year history. The shared advisory lock and
    # FOR UPDATE below cover existing rows; a rogue concurrent insert raises a
    # primary-key error and rolls back this entire source window.
    with conn.cursor().copy("COPY candles (" + ",".join(columns) + ") FROM STDIN") as writer:
        for row in rows:
            writer.write_row(tuple(Jsonb(row[key]) if key == "extras" else row[key] for key in columns))


def apply_window(conn, rows, apply=False, journal=None):
    """Only missing/partial/matching unflagged facts can change."""
    dataset = tuple(rows[0][key] for key in IDENTITY)
    dataset_id = "trx-backfill:" + ":".join(dataset) + ":" + str(rows[0]["timestamp"])
    counts, conflicts = Counter(), []
    with conn.transaction():
        if apply:
            if journal is None:
                raise ValueError("apply requires write-ahead journal")
            dataset_lock(conn, dataset)
            journal.write({"type": "dataset_started", "dataset_id": dataset_id, "identity": dataset})
        previous = conn.execute("SELECT * FROM candles WHERE " + WHERE_DATASET
                                + " AND timestamp>=%s AND timestamp<=%s"
                                + (" FOR UPDATE" if apply else ""),
                                (*dataset, rows[0]["timestamp"], rows[-1]["timestamp"])).fetchall()
        existing = {row["timestamp"]: row for row in previous}
        markers, insertions, partials = [], [], []
        for official in rows:
            before = existing.get(official["timestamp"])
            action = rest_action(before, official)
            counts[action] += 1
            if action == "mark_closed":
                markers.append(before)
            elif action == "insert_missing":
                insertions.append(official)
            elif action == "reconcile_partial":
                partials.append((before, reconciled_partial(before, official)))
            elif action in {"ohlcv_mismatch", "invalid_extras", "live_provenance"}:
                conflicts.append(official["timestamp"])
        if apply:
            mark_rows(conn, markers, journal, dataset_id)
            reconcile_rows(conn, partials, journal, dataset_id)
            insert_rows(conn, insertions, journal, dataset_id)
            if insertions:
                bounds = min(row["timestamp"] for row in insertions), max(row["timestamp"] for row in insertions)
                updated = conn.execute("UPDATE candle_datasets SET rows=rows+%s,first_timestamp=LEAST(first_timestamp,%s),"
                                       "last_timestamp=GREATEST(last_timestamp,%s),updated_at=CURRENT_TIMESTAMP WHERE "
                                       + WHERE_DATASET + " RETURNING rows", (len(insertions), *bounds, *dataset)).fetchone()
                if updated is None:
                    conn.execute("INSERT INTO candle_datasets "
                                 "(exchange,market,pair,timeframe,candle_type,rows,first_timestamp,last_timestamp) "
                                 "SELECT %s,%s,%s,%s,%s,COUNT(*),MIN(timestamp),MAX(timestamp) FROM candles WHERE "
                                 + WHERE_DATASET, (*dataset, *dataset))
            journal.flush()
    if apply:
        journal.write({"type": "dataset_committed", "dataset_id": dataset_id})
        journal.flush()
    return {"counts": dict(counts), "conflict_timestamps": conflicts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--start", default=DEFAULT_START, help="inclusive UTC ISO timestamp")
    parser.add_argument("--end", help="exclusive upper observation bound; default current UTC time")
    parser.add_argument("--timeframes", nargs="+", choices=sorted(INTERVALS), default=["5m", "15m"])
    parser.add_argument("--output", type=Path, help="private persistent cache/report directory")
    parser.add_argument("--resume", action="store_true", help="reuse fixed range/cache and recheck committed DB windows")
    parser.add_argument("--apply", action="store_true", help="perform DB mutations; default dry-run")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    now = int(time.time() * 1000)
    try:
        start = iso_timestamp(args.start)
        end = iso_timestamp(args.end) if args.end else now
        if end > now or end <= start:
            raise ValueError("end must be observed, and later than start")
        for timeframe in args.timeframes:
            list(windows(start, end, timeframe))
    except ValueError as exc:
        parser.error(str(exc))
    output = (args.output or root / "bot" / "user_data" / "backfill" /
              ("trx-history-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))).resolve()
    if args.resume:
        if not output.is_dir():
            parser.error("--resume requires an existing output directory")
    else:
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
    lock_fd = os.open(output / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        parser.error("another backfill owns this output directory")
    manifest_path = output / "manifest.json"
    if args.resume:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("version") != 1 or manifest.get("pair") != PAIR
                or manifest.get("start") != start
                or manifest.get("timeframes") != args.timeframes
                or (args.end and manifest.get("observed_at") != end)):
            os.close(lock_fd)
            parser.error("resume range differs from the frozen manifest")
        end = manifest["observed_at"]
    else:
        atomic_json(manifest_path, {"version": 1, "pair": PAIR, "start": start,
                                   "observed_at": end, "timeframes": args.timeframes})
    if (isinstance(end, bool) or not isinstance(end, int) or end > now or end <= start):
        os.close(lock_fd)
        parser.error("frozen manifest end must be an observed millisecond boundary")
    cache = output / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    journal = Journal(output / ("rollback-" + run_id + ".jsonl")) if args.apply else None
    if journal:
        journal.write({"type": "rollback_comparison", "columns": [*IDENTITY, "timestamp", *OHLCV,
                                                                       "extras", "source_key"],
                       "instruction": "Compare every recorded after column exactly, excluding generated updated_at. Restore prior recorded columns only for dataset_committed entries. A started window without commit is uncertain and requires DB verification; never blindly revert it."})
        journal.flush()
    report = {"version": 1, "pair": PAIR, "start": start, "observed_at": end,
              "apply": args.apply, "status": "running", "windows": [], "run_id": run_id}
    client, started = OfficialClient(), time.monotonic()
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from data_store import DataStore
        # Loading configuration is read-only; never call initialize() in this
        # utility, so a dry-run cannot create schema or run service migrations.
        store = DataStore(root=root)
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(store.schema)))
            conn.execute("SET statement_timeout TO '120s'")
            conn.execute("SET lock_timeout TO '15s'")
            conn.commit()
            for timeframe in args.timeframes:
                for window in windows(start, end, timeframe):
                    report["active_window"] = {"timeframe": timeframe, "month": window["month"],
                                               "start": window["start"], "end": window["end"]}
                    current = coverage(conn, window)
                    conn.commit()
                    if current["complete"]:
                        result = {"counts": {"existing_finalized_window": current["total"]}, "sources": []}
                    else:
                        rows, sources = source_rows(client, cache, window, end)
                        result = {**apply_window(conn, rows, args.apply, journal), "sources": sources}
                        if args.apply:
                            result["after"] = coverage(conn, window)
                            conn.commit()
                    report["windows"].append({**window, "before": current, **result})
                    report.update(requests=client.requests, downloaded_bytes=client.bytes,
                                  elapsed_seconds=round(time.monotonic() - started, 3))
                    atomic_json(output / "report.json", report)
                    print(json.dumps({"timeframe": timeframe, "month": window["month"],
                                      "apply": args.apply, "counts": result["counts"]}), flush=True)
        incomplete = any(window.get("conflict_timestamps") or
                         (args.apply and not window.get("after", window["before"])["complete"])
                         for window in report["windows"])
        report["status"] = "incomplete" if incomplete else "completed"
        report.pop("active_window", None)
    except Exception as exc:
        # DSNs and HTTP exception bodies may contain private configuration.
        report.update(status="error", error_type=type(exc).__name__)
    finally:
        if journal:
            journal.close()
        report.update(requests=client.requests, downloaded_bytes=client.bytes,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        atomic_json(output / ("report-" + run_id + ".json"), report)
        atomic_json(output / "report.json", report)
        os.close(lock_fd)
        print(json.dumps({"status": report["status"], "apply": args.apply,
                          "report": str(output / "report.json"), "requests": client.requests,
                          "downloaded_bytes": client.bytes, "elapsed_seconds": report["elapsed_seconds"]}))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
