#!/usr/bin/env python3
"""Verify BTC/ETH/TRX official spot history and optionally sustain basis feeds.

Preview is the default.  --apply conservatively inserts missing exchange-final
facts, marks identical unflagged OHLCV, and finalizes explicit partial rows.
Existing finalized rows are never replaced.  --watch polls only the last three
closed 1h candles and uses independent status files/documents; no existing
market collector or strategy/account state is changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import uuid
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.backfill_trx_history import OfficialClient, atomic_json, insert_rows, iso_timestamp
from tools.repair_closed_candles import (
    IDENTITY, OHLCV, WHERE_DATASET, Journal, dataset_lock, mark_rows,
    parse_rest_window, reconcile_rows, reconciled_partial, rest_action,
)

SPOT_REST_URL = "https://api.binance.com/api/v3/klines"
PAIRS = ("BTC/USDT", "ETH/USDT", "TRX/USDT")
INTERVALS = {"5m": 300_000, "1h": 3_600_000}
REST_LIMIT = 1000
DEFAULT_START = "2026-10-08T00:00:00Z"
DEFAULT_STATUS_KEY = "spot_basis_status.json"


def identity(pair, timeframe):
    if pair not in PAIRS or timeframe not in INTERVALS:
        raise ValueError("only BTC/ETH/TRX spot 5m/1h inputs are supported")
    return "binance", "spot", pair, timeframe, "spot"


def windows(start, observed_at, timeframe, pair, *, warmup_bars=750):
    """Bounded exact REST pages; 1h includes independently verified warmup."""
    identity(pair, timeframe)
    interval = INTERVALS[timeframe]
    boundary = observed_at // interval * interval
    if (isinstance(warmup_bars, bool) or not isinstance(warmup_bars, int)
            or warmup_bars < 0 or start % interval or start >= boundary):
        raise ValueError("window must be aligned, closed and use a valid warmup size")
    if timeframe == "1h":
        start = min(start, max(0, boundary - (warmup_bars + 2) * interval))
    for begin in range(start, boundary, REST_LIMIT * interval):
        end = min(boundary, begin + REST_LIMIT * interval)
        yield {"pair": pair, "timeframe": timeframe, "start": begin, "end": end,
               "expected": (end - begin) // interval}


def normalized_rows(payload, pair, timeframe, start, end, observed_at, source_url):
    identity(pair, timeframe)
    interval = INTERVALS[timeframe]
    count = (end - start) // interval
    if (start % interval or end % interval or not 1 <= count <= REST_LIMIT
            or start >= end or end > observed_at):
        raise ValueError("spot page must be an exact observed, already closed window")
    # Binance spot and perpetual klines share a structural format. Reuse only
    # its strict timestamp/OHLCV/flow validation; assign actual spot identity
    # and provenance before returning any rows.
    rows = parse_rest_window(payload, pair + ":USDT", timeframe, end, count)
    for row in rows:
        row.update(zip(IDENTITY, identity(pair, timeframe)))
        extras = row["extras"]
        extras.pop("repair_source")
        extras.pop("repair_requested_at")
        extras.update(live_snapshot_timestamp=observed_at,
                      backfill_source=source_url, backfill_observed_at=observed_at)
    return rows


def source_rows(client, cache, window, observed_at):
    pair, timeframe = window["pair"], window["timeframe"]
    identity(pair, timeframe)
    url = SPOT_REST_URL + "?" + urlencode({"symbol": pair.replace("/", ""),
        "interval": timeframe, "startTime": window["start"], "endTime": window["end"] - 1,
        "limit": window["expected"]})
    name = f"{pair.replace('/', '')}-{timeframe}-{window['start']}-{window['end']}.json"
    path = Path(cache) / name
    if not path.exists():
        payload = json.loads(client.get(url))
        normalized_rows(payload, pair, timeframe, window["start"], window["end"],
                        observed_at, url)
        digest = hashlib.sha256(json.dumps(payload, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        atomic_json(path, {"payload": payload, "sha256": digest})
    cached = json.loads(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(json.dumps(cached["payload"], separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    if digest != cached.get("sha256"):
        raise ValueError("cached spot REST payload SHA256 mismatch")
    rows = normalized_rows(cached["payload"], pair, timeframe, window["start"],
                           window["end"], observed_at, url)
    return rows, {"kind": "official_spot_rest", "url": url, "cache": name,
                  "payload_sha256": digest}


def coverage(conn, window):
    interval = INTERVALS[window["timeframe"]]
    row = conn.execute("SELECT count(*) AS total,"
        "count(*) FILTER (WHERE extras->'closed'='true'::jsonb) AS closed,"
        "count(*) FILTER (WHERE timestamp %% %s<>0) AS misaligned,"
        "min(timestamp) AS first,max(timestamp) AS last FROM candles WHERE "
        + WHERE_DATASET + " AND timestamp>=%s AND timestamp<%s",
        (interval, *identity(window["pair"], window["timeframe"]),
         window["start"], window["end"])).fetchone()
    return {**row, "expected": window["expected"],
            "complete": (row["total"] == row["closed"] == window["expected"]
                         and row["misaligned"] == 0)}


def apply_window(conn, rows, apply=False, journal=None):
    """Use the shared dataset lock and exact prior-row guards; preserve finals."""
    if not rows:
        raise ValueError("spot window must not be empty")
    dataset = tuple(rows[0][key] for key in IDENTITY)
    if dataset != identity(dataset[2], dataset[3]) or any(
            tuple(row[key] for key in IDENTITY) != dataset for row in rows):
        raise ValueError("spot window contains mixed or unsupported market identities")
    dataset_id = ("spot-basis:" + ":".join(dataset) + ":" + str(rows[0]["timestamp"])
                  + ":" + uuid.uuid4().hex)
    counts, conflicts = Counter(), []
    with conn.transaction():
        if apply:
            if journal is None:
                raise ValueError("apply requires a private write-ahead journal")
            dataset_lock(conn, dataset)
            journal.write({"type": "dataset_started", "dataset_id": dataset_id,
                           "identity": dataset})
        previous = conn.execute("SELECT * FROM candles WHERE " + WHERE_DATASET
            + " AND timestamp>=%s AND timestamp<=%s" + (" FOR UPDATE" if apply else ""),
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
                bounds = (min(row["timestamp"] for row in insertions),
                          max(row["timestamp"] for row in insertions))
                updated = conn.execute("UPDATE candle_datasets SET rows=rows+%s,"
                    "first_timestamp=LEAST(first_timestamp,%s),last_timestamp=GREATEST(last_timestamp,%s),"
                    "updated_at=CURRENT_TIMESTAMP WHERE " + WHERE_DATASET + " RETURNING rows",
                    (len(insertions), *bounds, *dataset)).fetchone()
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


def tail_windows(observed_at, bars=3):
    """Small current 1h windows, never an open candle or a paused coin."""
    interval = INTERVALS["1h"]
    boundary = observed_at // interval * interval
    if isinstance(bars, bool) or not isinstance(bars, int) or not 1 <= bars <= REST_LIMIT:
        raise ValueError("tail size must be between 1 and 1000 closed bars")
    for pair in PAIRS:
        yield {"pair": pair, "timeframe": "1h", "start": boundary - bars * interval,
               "end": boundary, "expected": bars}


def watch_windows(observed_at, last_success_boundary):
    """Catch up interruptions, then return to the small three-candle tail."""
    interval = INTERVALS["1h"]
    boundary = observed_at // interval * interval
    start = min(last_success_boundary, max(0, boundary - 3 * interval))
    for pair in PAIRS:
        for begin in range(start, boundary, REST_LIMIT * interval):
            end = min(boundary, begin + REST_LIMIT * interval)
            yield {"pair": pair, "timeframe": "1h", "start": begin, "end": end,
                   "expected": (end - begin) // interval}


def run_windows(conn, client, cache, selected_windows, observed_at, *, apply=False,
                journal=None, on_progress=None):
    results = []
    for window in selected_windows:
        before = coverage(conn, window)
        conn.commit()
        if before["complete"]:
            result = {"counts": {"existing_finalized_window": before["total"]}, "source": None}
        else:
            rows, source = source_rows(client, cache, window, observed_at)
            result = {**apply_window(conn, rows, apply=apply, journal=journal), "source": source}
        if apply:
            result["after"] = coverage(conn, window)
            conn.commit()
        results.append({**window, "before": before, **result})
        if on_progress:
            on_progress(results)
    return results


def watch_snapshot(results, observed_at, *, apply, error_type=None):
    coverage_by_pair = {}
    for window in results:
        current = window.get("after", window["before"])
        prior = coverage_by_pair.get(window["pair"])
        coverage_by_pair[window["pair"]] = {"ready": current["complete"] and (
            prior is None or prior["ready"]),
            "last_timestamp": current["last"], "closed": current["closed"],
            "expected": window["expected"], "end": window["end"]}
    return {"service": "spot-basis", "running": True, "apply": apply,
            "pairs": list(PAIRS), "timeframe": "1h", "last_poll_at": observed_at,
            "ready": (not error_type and len(coverage_by_pair) == len(PAIRS)
                      and all(item["ready"] for item in coverage_by_pair.values())),
            "by_pair": coverage_by_pair, "last_error": error_type}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--start", default=DEFAULT_START, help="inclusive aligned UTC boundary")
    parser.add_argument("--end", help="fixed initial observation bound; default current UTC time")
    parser.add_argument("--warmup-bars", type=int, default=750, help="verify at least this many 1h bars")
    parser.add_argument("--timeframes", nargs="+", choices=sorted(INTERVALS), default=["5m", "1h"])
    parser.add_argument("--output", type=Path, help="private cache/journal/status directory")
    parser.add_argument("--resume", action="store_true", help="reuse fixed initial manifest and cache")
    parser.add_argument("--apply", action="store_true", help="perform explicit DB writes; default preview")
    parser.add_argument("--watch", action="store_true", help="after initial run, poll current closed 1h tail")
    parser.add_argument("--interval", type=float, default=45, help="watch polling seconds, 10 to 300")
    parser.add_argument("--status-document-key", default=DEFAULT_STATUS_KEY)
    args = parser.parse_args(argv)
    root, now = args.root.resolve(), int(time.time() * 1000)
    try:
        start = iso_timestamp(args.start)
        end = iso_timestamp(args.end) if args.end else now
        if end > now or end <= start or not 10 <= args.interval <= 300:
            raise ValueError("observation must be in the past and interval between 10 and 300 seconds")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", args.status_document_key):
            raise ValueError("status document key must be an independent JSON filename")
        if args.status_document_key in {"market_stream_status.json", "market_stream_trx_status.json"}:
            raise ValueError("spot status must not overwrite another collector's heartbeat")
        selected = [part for timeframe in args.timeframes for pair in PAIRS
                    for part in windows(start, end, timeframe, pair, warmup_bars=args.warmup_bars)]
    except ValueError as exc:
        parser.error(str(exc))
    output = (args.output or root / "bot" / "user_data" / "backfill" /
              ("spot-basis-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))).resolve()
    if args.resume:
        if not output.is_dir():
            parser.error("--resume requires an existing private output directory")
    else:
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
    lock_fd = os.open(output / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        parser.error("another spot backfill/collector owns this output directory")
    manifest_path = output / "manifest.json"
    manifest = {"version": 1, "pairs": list(PAIRS), "start": start, "observed_at": end,
                "warmup_bars": args.warmup_bars, "timeframes": args.timeframes}
    if args.resume:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(previous.get(key) != value for key, value in manifest.items()
               if key != "observed_at") or (args.end and previous.get("observed_at") != end):
            os.close(lock_fd)
            parser.error("resume differs from the frozen initial manifest")
        end = previous["observed_at"]
        if isinstance(end, bool) or not isinstance(end, int) or not start < end <= now:
            os.close(lock_fd)
            parser.error("frozen observation must be an actual past millisecond timestamp")
        selected = [part for timeframe in args.timeframes for pair in PAIRS
                    for part in windows(start, end, timeframe, pair, warmup_bars=args.warmup_bars)]
    else:
        atomic_json(manifest_path, manifest)
    cache = output / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    journal = Journal(output / ("rollback-" + run_id + ".jsonl")) if args.apply else None
    if journal:
        journal.write({"type": "rollback_comparison", "columns": [*IDENTITY, "timestamp",
                       *OHLCV, "extras", "source_key"], "instruction":
            "Compare every recorded after column exactly, excluding generated updated_at. "
            "Restore prior columns only for the unique dataset_committed transaction ID. "
            "A started transaction without commit is uncertain and requires DB verification."})
        journal.flush()
    report = {"version": 1, "pairs": list(PAIRS), "start": start, "observed_at": end,
              "apply": args.apply, "status": "running", "windows": [], "run_id": run_id}
    client, started, store = OfficialClient(), time.monotonic(), None
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from data_store import DataStore
        store = DataStore(root=root)
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(store.schema)))
            conn.execute("SET statement_timeout TO '120s'")
            conn.execute("SET lock_timeout TO '15s'")
            if not args.apply:
                conn.execute("SET default_transaction_read_only TO on")
            conn.commit()

            def progress(results):
                report.update(windows=results, requests=client.requests,
                              downloaded_bytes=client.bytes,
                              elapsed_seconds=round(time.monotonic() - started, 3))
                atomic_json(output / "report.json", report)
                item = results[-1]
                print(json.dumps({"pair": item["pair"], "timeframe": item["timeframe"],
                                  "apply": args.apply, "counts": item["counts"]}), flush=True)

            report["windows"] = run_windows(conn, client, cache, selected, end,
                apply=args.apply, journal=journal, on_progress=progress)
            incomplete = any(item.get("conflict_timestamps") or (args.apply and not
                item.get("after", item["before"])["complete"]) for item in report["windows"])
            report["status"] = "incomplete" if incomplete else "completed"
            atomic_json(output / "report.json", report)
            if not incomplete and args.watch:
                last_success_boundary = end // INTERVALS["1h"] * INTERVALS["1h"]
                signal.signal(signal.SIGTERM, lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
                while True:
                    observed_at = int(time.time() * 1000)
                    try:
                        recent = run_windows(conn, client, cache,
                                             watch_windows(observed_at, last_success_boundary),
                                             observed_at, apply=args.apply, journal=journal)
                        snapshot = watch_snapshot(recent, observed_at, apply=args.apply)
                        if snapshot["ready"]:
                            last_success_boundary = observed_at // INTERVALS["1h"] * INTERVALS["1h"]
                    except Exception as exc:
                        conn.rollback()
                        snapshot = watch_snapshot([], observed_at, apply=args.apply,
                                                  error_type=type(exc).__name__)
                    atomic_json(output / "status.json", snapshot)
                    if args.apply:
                        store.put_document(args.status_document_key, snapshot)
                    print(json.dumps({"service": "spot-basis", "ready": snapshot["ready"],
                                      "last_poll_at": observed_at,
                                      "last_error": snapshot["last_error"]}), flush=True)
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        report["status"] = "stopped"
    except Exception as exc:
        report.update(status="error", error_type=type(exc).__name__)
    finally:
        if args.watch:
            stopped = {"service": "spot-basis", "running": False, "apply": args.apply,
                       "pairs": list(PAIRS), "timeframe": "1h",
                       "last_poll_at": int(time.time() * 1000),
                       "ready": False, "last_error": report.get("error_type")}
            atomic_json(output / "status.json", stopped)
            if args.apply and store is not None:
                try:
                    store.put_document(args.status_document_key, stopped)
                except Exception:
                    pass
        if journal:
            journal.close()
        report.update(requests=client.requests, downloaded_bytes=client.bytes,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        atomic_json(output / ("report-" + run_id + ".json"), report)
        atomic_json(output / "report.json", report)
        os.close(lock_fd)
        print(json.dumps({"status": report["status"], "apply": args.apply,
                          "report": str(output / "report.json"), "requests": client.requests,
                          "downloaded_bytes": client.bytes,
                          "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
    return 0 if report["status"] in {"completed", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
