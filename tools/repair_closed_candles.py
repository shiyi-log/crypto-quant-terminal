#!/usr/bin/env python3
"""Conservatively repair historical close flags and recent Binance gaps.

Dry-run is the default. --apply requires a private rollback journal written
before each dataset commits. Finalized OHLCV is never replaced; an explicitly
partial snapshot can only be finalized by a verified official closed candle.
No account/strategy/service state is changed by this utility.
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
import time
from urllib.parse import urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_sync import candle_close_timestamp, candle_identity, public_source

IDENTITY = ("exchange", "market", "pair", "timeframe", "candle_type")
OHLCV = ("open", "high", "low", "close", "volume")
REST_URL = "https://fapi.binance.com/fapi/v1/klines"
# 1d 必须与其它周期走同一条已审计路径：日线策略族（s13/s22-s30）的预热
# 只能由官方收盘日线满足，5m 重采样需要 216,000 根才能覆盖 750 天。
REST_INTERVALS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}
ALLOWED_PAIRS = {coin + "/USDT:USDT" for coin in ("BTC", "ETH", "TRX")}
FLOW_FIELDS = {"quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote",
               "avg_trade_sz", "buy_ratio", "sell_volume", "flow_imbal", "flow_imbal_q"}
WHERE_KEY = " AND ".join(name + "=%s" for name in (*IDENTITY, "timestamp"))
WHERE_DATASET = " AND ".join(name + "=%s" for name in IDENTITY)


def integer(value):
    if isinstance(value, bool):
        raise ValueError("invalid integer")
    result = int(value)
    if float(value) != result or result < 0:
        raise ValueError("invalid integer")
    return result


def finite(value):
    if isinstance(value, bool):
        raise ValueError("invalid numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid numeric value")
    return result


def valid_ohlcv(row):
    try:
        opened, high, low, closed, volume = (finite(row[key]) for key in OHLCV)
        integer(row["timestamp"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (min(opened, high, low, closed) > 0 and volume >= 0
            and low <= min(opened, closed) <= max(opened, closed) <= high)


def live_provenance(extras):
    # Even a closed flag is not inferred for an incomplete live snapshot.
    return (any(str(key).startswith("live_") or "websocket" in str(key).lower()
                for key in extras)
            or extras.get("live") is True
            or str(extras.get("source")) in {"ws", "websocket", "market_stream", "market-live"})


def trusted_source(root, source, observed_at):
    """Resolve a successful, unchanged, public local candle source only."""
    root = Path(root).resolve()
    key = source.get("key", "")
    if not isinstance(key, str):
        raise ValueError("untrusted source")
    path = root / key
    data_dir = root / "bot" / "user_data" / "data"
    if (not isinstance(key, str) or Path(key).is_absolute() or ".." in Path(key).parts
            or source.get("kind") != "feather" or source.get("error")
            or path.suffix != ".feather" or not path.is_file()
            or not public_source(path, root / "bot" / "user_data")):
        raise ValueError("untrusted source")
    source_path = Path(str(source.get("path", "")))
    if not source_path.is_absolute():
        source_path = root / source_path
    if source_path.resolve() != path.resolve():
        raise ValueError("source path mismatch")
    identity = candle_identity(path, data_dir)
    if identity[4] in {"funding_rate", "oi"}:
        raise ValueError("not a candle source")
    stamp = path.stat()
    if (stamp.st_mtime_ns, stamp.st_size) != (source.get("mtime_ns"), source.get("size")):
        raise ValueError("source fingerprint mismatch")
    return {"key": key, "path": path, "identity": identity,
            "mtime_ns": stamp.st_mtime_ns, "size": stamp.st_size,
            "cutoff": min(integer(observed_at), stamp.st_mtime_ns // 1_000_000)}


def unchanged_source(source):
    stamp = source["path"].stat()
    return (stamp.st_mtime_ns, stamp.st_size) == (source["mtime_ns"], source["size"])


def local_action(row, source):
    if tuple(row.get(name) for name in IDENTITY) != source["identity"]:
        return "identity_mismatch"
    if row.get("source_key") != source["key"]:
        return "source_mismatch"
    extras = row.get("extras")
    if not isinstance(extras, dict):
        return "invalid_extras"
    if "closed" in extras:
        return "existing_closed_flag"
    if live_provenance(extras):
        return "live_provenance"
    if not valid_ohlcv(row):
        return "invalid_ohlcv"
    try:
        closes_at = candle_close_timestamp(integer(row["timestamp"]), row["timeframe"])
    except (ValueError, TypeError, OverflowError):
        return "invalid_timeframe"
    if closes_at > source["cutoff"]:
        return "forming_or_future"
    return "mark_closed"


def parse_rest_window(payload, pair, timeframe, request_started_at, limit):
    """Validate an exact continuous, already closed official kline window."""
    if pair not in ALLOWED_PAIRS or timeframe not in REST_INTERVALS:
        raise ValueError("unsupported REST dataset")
    limit = integer(limit)
    if not 1 <= limit <= 1500 or not isinstance(payload, list) or len(payload) != limit:
        raise ValueError("unexpected REST window size")
    interval = REST_INTERVALS[timeframe]
    end_boundary = integer(request_started_at) // interval * interval
    first = end_boundary - interval * limit
    rows = []
    for index, values in enumerate(payload):
        if not isinstance(values, list) or len(values) < 11:
            raise ValueError("invalid REST kline shape")
        stamp, close_time = integer(values[0]), integer(values[6])
        if (stamp != first + index * interval or stamp % interval
                or close_time != stamp + interval - 1 or close_time >= request_started_at):
            raise ValueError("REST window is not aligned, continuous and closed")
        row = dict(zip(OHLCV, (finite(value) for value in values[1:6])))
        row["timestamp"] = stamp
        if not valid_ohlcv(row):
            raise ValueError("invalid REST OHLCV")
        quote, trades = finite(values[7]), integer(values[8])
        buy_base, buy_quote = finite(values[9]), finite(values[10])
        # Relative tolerance only absorbs binary floating conversion noise.
        tolerance = 1e-10
        if (quote < 0 or buy_base < 0 or buy_quote < 0
                or buy_base > row["volume"] + max(1e-12, row["volume"] * tolerance)
                or buy_quote > quote + max(1e-12, quote * tolerance)
                or (row["volume"] == 0 and (quote != 0 or buy_base != 0 or buy_quote != 0))):
            raise ValueError("invalid REST quote/taker volumes")
        for base, quoted in ((row["volume"], quote), (buy_base, buy_quote)):
            if base and not (row["low"] * base * (1 - tolerance) <= quoted
                             <= row["high"] * base * (1 + tolerance)):
                raise ValueError("REST quote volume falls outside candle range")
            if not base and quoted:
                raise ValueError("REST quote volume without base volume")
        row.update(zip(IDENTITY, ("binance", "futures", pair, timeframe, "futures")))
        row["source_key"] = None
        row["extras"] = {"closed": True, "quote_volume": quote, "num_trades": trades,
                         "taker_buy_base": buy_base, "taker_buy_quote": buy_quote,
                         "close_time": close_time, "repair_source": REST_URL,
                         "repair_requested_at": request_started_at,
                         "live_snapshot_timestamp": request_started_at}
        rows.append(row)
    return rows


def rest_action(existing, official):
    if existing is None:
        return "insert_missing"
    extras = existing.get("extras")
    if not isinstance(extras, dict):
        return "invalid_extras"
    if extras.get("closed") is False:
        # Elapsed time is insufficient for a partial snapshot. A freshly
        # validated exchange-final row provides the missing evidence instead.
        return "reconcile_partial"
    if "closed" in extras:
        return "existing_closed_flag"
    if live_provenance(extras):
        return "live_provenance"
    if not valid_ohlcv(existing):
        return "ohlcv_mismatch"
    if any(not math.isclose(float(existing[name]), official[name], rel_tol=1e-10, abs_tol=1e-10)
           for name in OHLCV):
        return "ohlcv_mismatch"
    return "mark_closed"


def reconciled_partial(existing, official):
    """Retain unknown annotations; never attach old flow to revised prices."""
    extras = {key: value for key, value in existing["extras"].items()
              if key not in FLOW_FIELDS and not str(key).startswith("live_")
              and key not in {"closed", "source", "live", "close_time",
                              "repair_source", "repair_requested_at"}}
    extras.update(official["extras"])
    return {**official, "extras": extras}


class Journal:
    """Write-ahead JSONL rollback inventory; no connection information is saved."""
    def __init__(self, path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.handle = os.fdopen(fd, "w", encoding="utf-8")
        self.write({"type": "metadata", "version": 1,
                    "rollback": "Only revert records for dataset_committed entries. Restore exact before row for mark_closed/reconcile_partial if current row still equals after; delete insert_missing only if current row equals after. Keep uncertain commits for manual review. Refresh candle_datasets summary after any deletion."})

    def write(self, record):
        self.handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False, default=str) + "\n")

    def flush(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self):
        self.flush()
        self.handle.close()


def row_key(row):
    return tuple(row[name] for name in (*IDENTITY, "timestamp"))


def journal_change(journal, dataset_id, action, before, after):
    journal.write({"type": "change", "dataset_id": dataset_id, "action": action,
                   "key": row_key(after), "before": before, "after": after})


def mark_rows(conn, rows, journal, dataset_id):
    from psycopg.types.json import Jsonb
    if not rows:
        return
    for row in rows:
        after = {**row, "extras": {**row["extras"], "closed": True}}
        journal_change(journal, dataset_id, "mark_closed", row, after)
    journal.flush()
    # The dataset is locked, and matching the prior extras also guards direct
    # writers that do not honor the shared advisory lock. Never set OHLCV.
    query = ("UPDATE candles SET extras=extras || '{\"closed\":true}'::jsonb WHERE "
             + WHERE_KEY + " AND extras=%s AND source_key IS NOT DISTINCT FROM %s")
    with conn.cursor() as cursor:
        cursor.executemany(query, [(*row_key(row), Jsonb(row["extras"]), row["source_key"])
                                  for row in rows])
        if cursor.rowcount != len(rows):
            raise RuntimeError("concurrent marker change")


def reconcile_rows(conn, changes, journal, dataset_id):
    from psycopg.types.json import Jsonb
    if not changes:
        return
    for before, after in changes:
        journal_change(journal, dataset_id, "reconcile_partial", before, after)
    journal.flush()
    query = ("UPDATE candles SET " + ",".join(name + "=%s" for name in OHLCV)
             + ",extras=%s,source_key=NULL WHERE " + WHERE_KEY + " AND extras=%s")
    with conn.cursor() as cursor:
        cursor.executemany(query, [(*(after[name] for name in OHLCV), Jsonb(after["extras"]),
                                   *row_key(before), Jsonb(before["extras"]))
                                  for before, after in changes])
        if cursor.rowcount != len(changes):
            raise RuntimeError("concurrent partial change; dataset rolled back")


def dataset_lock(conn, identity):
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("candles:" + encoded,))


def repair_local(conn, root, pairs, timeframes, now_ms, apply, journal, reports=None):
    sources = conn.execute("SELECT key,path,kind,mtime_ns,size,error FROM sources WHERE kind='feather' ORDER BY key").fetchall()
    conn.commit()
    reports = [] if reports is None else reports
    for entry in sources:
        try:
            source = trusted_source(root, entry, now_ms)
        except (ValueError, OSError) as exc:
            reports.append({"source_key": entry["key"], "skipped": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
            continue
        identity = source["identity"]
        if identity[2] not in pairs or identity[3] not in timeframes:
            continue
        counts = Counter()
        dataset_id = "local:" + source["key"]
        with conn.transaction():
            if apply:
                dataset_lock(conn, identity)
                journal.write({"type": "dataset_started", "dataset_id": dataset_id,
                               "identity": identity, "source_mtime_ns": source["mtime_ns"],
                               "closed_cutoff": source["cutoff"]})
            query = ("SELECT * FROM candles WHERE " + WHERE_DATASET
                     + " AND source_key=%s AND NOT (extras ? 'closed') ORDER BY timestamp"
                     + (" FOR UPDATE" if apply else ""))
            with conn.cursor(name="closed_repair_scan") as cursor:
                cursor.execute(query, (*identity, source["key"]))
                while batch := cursor.fetchmany(2000):
                    changes = []
                    for row in batch:
                        action = local_action(row, source)
                        counts[action] += 1
                        if action == "mark_closed":
                            changes.append(row)
                    if apply:
                        mark_rows(conn, changes, journal, dataset_id)
            if not unchanged_source(source):
                raise RuntimeError("source changed during repair; dataset rolled back")
            if apply:
                journal.flush()
        if apply:
            journal.write({"type": "dataset_committed", "dataset_id": dataset_id})
            journal.flush()
        reports.append({"source_key": source["key"], "identity": identity,
                        "closed_cutoff": source["cutoff"], "counts": dict(counts)})
    return reports


def fetch_rest(pair, timeframe, limit):
    requested_at = int(time.time() * 1000)
    boundary = requested_at // REST_INTERVALS[timeframe] * REST_INTERVALS[timeframe]
    query = urlencode({"symbol": pair.split("/")[0] + "USDT", "interval": timeframe,
                       "limit": limit, "endTime": boundary - 1})
    with urlopen(REST_URL + "?" + query, timeout=20) as response:
        payload = json.load(response)
    return parse_rest_window(payload, pair, timeframe, requested_at, limit), requested_at


def repair_rest(conn, pairs, timeframes, window, apply, journal, fetch=fetch_rest, reports=None):
    from psycopg.types.json import Jsonb
    reports = [] if reports is None else reports
    for pair in sorted(pairs):
        for timeframe in timeframes:
            if timeframe not in REST_INTERVALS:
                continue
            rows, requested_at = fetch(pair, timeframe, window)
            identity = tuple(rows[0][name] for name in IDENTITY)
            dataset_id = "rest:" + ":".join(identity)
            counts, conflicts = Counter(), []
            with conn.transaction():
                if apply:
                    dataset_lock(conn, identity)
                    journal.write({"type": "dataset_started", "dataset_id": dataset_id,
                                   "identity": identity, "request_started_at": requested_at})
                existing_rows = conn.execute(
                    "SELECT * FROM candles WHERE " + WHERE_DATASET
                    + " AND timestamp>=%s AND timestamp<=%s" + (" FOR UPDATE" if apply else ""),
                    (*identity, rows[0]["timestamp"], rows[-1]["timestamp"])).fetchall()
                existing = {row["timestamp"]: row for row in existing_rows}
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
                    elif action == "ohlcv_mismatch":
                        conflicts.append(official["timestamp"])
                if apply:
                    mark_rows(conn, markers, journal, dataset_id)
                    reconcile_rows(conn, partials, journal, dataset_id)
                    for row in insertions:
                        journal_change(journal, dataset_id, "insert_missing", None, row)
                    journal.flush()
                    columns = (*IDENTITY, "timestamp", *OHLCV, "extras", "source_key")
                    query = ("INSERT INTO candles (" + ",".join(columns) + ") VALUES ("
                             + ",".join(["%s"] * len(columns)) + ") ON CONFLICT DO NOTHING")
                    with conn.cursor() as cursor:
                        cursor.executemany(query, [tuple(Jsonb(row[name]) if name == "extras" else row[name]
                                                        for name in columns) for row in insertions])
                        if cursor.rowcount != len(insertions):
                            raise RuntimeError("concurrent candle insertion; dataset rolled back")
                    if insertions:
                        # Metadata is refreshed in the same dataset transaction.
                        conn.execute("""INSERT INTO candle_datasets
                            (exchange,market,pair,timeframe,candle_type,rows,first_timestamp,last_timestamp)
                            SELECT %s,%s,%s,%s,%s,COUNT(*),MIN(timestamp),MAX(timestamp) FROM candles
                            WHERE """ + WHERE_DATASET + """ ON CONFLICT (exchange,market,pair,timeframe,candle_type)
                            DO UPDATE SET rows=EXCLUDED.rows,first_timestamp=EXCLUDED.first_timestamp,
                            last_timestamp=EXCLUDED.last_timestamp,updated_at=CURRENT_TIMESTAMP""", (*identity, *identity))
                    journal.flush()
            if apply:
                journal.write({"type": "dataset_committed", "dataset_id": dataset_id})
                journal.flush()
            reports.append({"identity": identity, "request_started_at": requested_at,
                            "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
                            "counts": dict(counts), "conflict_timestamps": conflicts})
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pairs", nargs="+", default=["BTC", "ETH", "TRX"],
                        choices=["BTC", "ETH", "TRX"])
    parser.add_argument("--timeframes", nargs="+", default=["5m", "15m", "1h", "4h", "1d"])
    parser.add_argument("--rest-window", type=int, metavar="N", help="also validate/repair recent N official closed candles (1..1500)")
    parser.add_argument("--rest-only", action="store_true", help="skip trusted local close-flag repair")
    parser.add_argument("--apply", action="store_true", help="perform reviewed mutations; default is dry-run")
    parser.add_argument("--output", type=Path, help="new report directory; must not exist")
    args = parser.parse_args(argv)
    if args.rest_window is not None and not 1 <= args.rest_window <= 1500:
        parser.error("--rest-window must be between 1 and 1500")
    if args.rest_only and args.rest_window is None:
        parser.error("--rest-only requires --rest-window")
    for timeframe in args.timeframes:
        try:
            candle_close_timestamp(0, timeframe)
        except ValueError:
            parser.error("unsupported timeframe")
    from data_store import DataStore
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    root = args.root.resolve()
    now_ms = int(time.time() * 1000)
    output = (args.output or root / "artifacts" / ("closed-candle-repair-"
              + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    journal = Journal(output / "rollback.jsonl") if args.apply else None
    report = {"version": 1, "apply": args.apply, "observed_at": now_ms,
              "pairs": args.pairs, "timeframes": args.timeframes,
              "rest_window": args.rest_window, "status": "running", "local": [], "rest": []}
    try:
        # Constructor loads the project's private environment. Do not initialize
        # the DataStore: a dry-run must not perform migrations/schema writes.
        store = DataStore(root=root)
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(store.schema)))
            conn.execute("SET statement_timeout TO '120s'")
            conn.execute("SET lock_timeout TO '15s'")
            conn.commit()
            pairs = {pair + "/USDT:USDT" for pair in args.pairs}
            if not args.rest_only:
                repair_local(conn, root, pairs, args.timeframes, now_ms, args.apply, journal,
                             reports=report["local"])
            if args.rest_window is not None:
                repair_rest(conn, pairs, args.timeframes, args.rest_window, args.apply, journal,
                            reports=report["rest"])
        report["status"] = "completed"
    except Exception as exc:
        # Exception strings may contain connection DSNs; never serialize them.
        report.update(status="error", error_type=type(exc).__name__)
    finally:
        if journal:
            journal.close()
        report_path = output / "report.json"
        fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"status": report["status"], "apply": args.apply,
                          "report": str(report_path),
                          "rollback": str(journal.path) if journal else None}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
