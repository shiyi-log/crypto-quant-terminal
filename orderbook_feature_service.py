"""Independent hourly orderbook feature publisher, never a trading process.

Preview is read only. Only --apply may publish append-only feature documents;
--watch additionally waits for full hours that begin after this process boots.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any

from orderbook_features import (ALLOWED_PAIRS, HOUR_MS, METHOD_SPEC, METHOD_VERSION,
                               compute_hour_features, selector_document_key, valid_record_identity)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("orderbook_features")
STATUS_KEY = "orderbook_feature_status.json"
DEFAULT_STATUS_PATH = ROOT / "database/runtime/orderbook_feature_status.json"
DEFAULT_LOCK_PATH = ROOT / "database/runtime/orderbook_features.lock"


class ReadOnlyFeatureStore:
    """Preview uses an enforced read-only transaction without DataStore's DDL."""
    def __init__(self, *, schema="quant"):
        from runtime_config import load_environment
        load_environment(ROOT)
        from psycopg import connect, sql
        from psycopg.rows import dict_row
        dsn = os.getenv("QUANT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("QUANT_DATABASE_URL is required")
        self.connection = connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
        self.connection.execute("SET default_transaction_read_only TO on")
        self.connection.execute("SET statement_timeout TO '30s'")
        self.connection.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(schema)))

    @contextmanager
    def _connection(self):
        with self.connection.transaction():
            yield self.connection

    def get_document(self, key, default=None):
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM documents WHERE key=%s", (key,)).fetchone()
        return row["payload"] if row is not None else default

    def close(self):
        self.connection.close()


class FeatureInstanceLock:
    """Own only this publisher's lock; collector processes use other locks."""
    def __init__(self, path: Path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise RuntimeError(f"orderbook feature publisher already owns {self.path}") from None
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + "\n")
        handle.flush()
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def fetch_hour_rows(store: Any, pair: str, bar_open_ms: int, *, max_rows: int = 150_000):
    """Use the existing exchange/market/pair/event-time index, bounded to one hour.

    received_at is intentionally read: filtering by event time alone would
    allow a delayed batch or replay to become a hindsight decision input.
    The original receive timestamp is projected without the large raw extras.
    """
    selector_document_key(pair, bar_open_ms)
    if not 100 <= max_rows <= 500_000:
        raise ValueError("max_rows must be within [100,500000]")
    with store._connection() as conn:
        rows = conn.execute("""SELECT timestamp,last_update_id,bids,asks,received_at,
            extras->>'received_timestamp' AS received_timestamp FROM orderbooks
            WHERE exchange=%s AND market=%s AND pair=%s AND timestamp >= %s AND timestamp < %s
            ORDER BY timestamp,length(last_update_id),last_update_id LIMIT %s""",
            ("binance", "futures", pair, bar_open_ms, bar_open_ms + HOUR_MS, max_rows + 1)).fetchall()
    return rows[:max_rows], len(rows) > max_rows


def publish_feature(store: Any, record: dict[str, Any], *, published_at_ms: int) -> dict[str, Any]:
    """Append evidence and claim the first accepted key in one transaction.

    A correction has a different source hash/evidence key; it can never replace
    an already accepted input. Historical and late computations are not
    published into this namespace at all.
    """
    if not valid_record_identity(record):
        return {"published": False, "reason": "invalid_or_historical_feature_identity",
                "feature_key": record.get("feature_key")}
    cutoff = int(record["cutoff_ms"])
    if (
            not cutoff <= int(record["observed_at_ms"]) <= published_at_ms <= cutoff + 90_000):
        return {"published": False, "reason": "historical_or_late_publication", "feature_key": record["feature_key"]}
    payload = copy.deepcopy(record)
    payload["published_at_ms"] = published_at_ms
    key = selector_document_key(payload["pair"], payload["candle_ms"])
    selector = {"feature_key": payload["feature_key"], "source_hash": payload["source_hash"],
                "method_version": METHOD_VERSION, "published_at_ms": published_at_ms}
    with store._connection() as conn:
        conn.execute("""INSERT INTO documents (key,payload) VALUES (%s,%s)
            ON CONFLICT (key) DO NOTHING""", (payload["feature_key"], store._json(payload)))
        claimed = conn.execute("""INSERT INTO documents (key,payload) VALUES (%s,%s)
            ON CONFLICT (key) DO NOTHING RETURNING key""", (key, store._json(selector))).fetchone()
        accepted = conn.execute("SELECT payload FROM documents WHERE key=%s", (key,)).fetchone()["payload"]
    return {"published": True, "first_acceptance": claimed is not None,
            "feature_key": payload["feature_key"], "accepted_feature_key": accepted["feature_key"],
            "quality_status": payload["quality"]["status"]}


class OrderbookFeatureService:
    def __init__(self, store: Any, *, pairs=ALLOWED_PAIRS, apply: bool = False,
                 started_at_ms: int | None = None, max_rows: int = 150_000,
                 clock=None, status_path: Path = DEFAULT_STATUS_PATH):
        self.store, self.apply = store, bool(apply)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.started_at_ms = self.clock() if started_at_ms is None else int(started_at_ms)
        self.forward_start_ms = ((self.started_at_ms + HOUR_MS - 1) // HOUR_MS) * HOUR_MS
        self.pairs = tuple(dict.fromkeys(pairs))
        if not 1 <= len(self.pairs) <= 3 or any(pair not in ALLOWED_PAIRS for pair in self.pairs):
            raise ValueError("only 1 to 3 BTC/ETH/TRX futures pairs are supported")
        if not 100 <= max_rows <= 500_000:
            raise ValueError("max_rows must be within [100,500000]")
        self.max_rows, self.status_path = max_rows, Path(status_path)
        self.processed = set()
        self._last_status_ms = -1
        self.status = {"running": False, "pid": os.getpid(), "method_version": METHOD_VERSION,
                       "pairs": list(self.pairs), "apply": self.apply, "started_at_ms": self.started_at_ms,
                       "forward_start_ms": self.forward_start_ms,
                       "first_publish_cutoff_ms": self.forward_start_ms + HOUR_MS,
                       "poll_interval_seconds": 15, "heartbeat_interval_seconds": 45,
                       "last_error": None, "last_observations": {}, "published_count": 0,
                       "state": "waiting_first_full_forward_hour", "no_historical_orders": True}

    def preview(self, *, bar_open_ms: int | None = None) -> list[dict[str, Any]]:
        observed = self.clock()
        bar_open = observed // HOUR_MS * HOUR_MS - HOUR_MS if bar_open_ms is None else bar_open_ms
        records = []
        for pair in self.pairs:
            rows, truncated = fetch_hour_rows(self.store, pair, bar_open, max_rows=self.max_rows)
            records.append(compute_hour_features(rows, pair=pair, bar_open_ms=bar_open,
                                                observed_at_ms=observed, forward_start_ms=self.forward_start_ms,
                                                query_truncated=truncated))
        return records

    def write_status(self, *, force=False):
        now = self.clock()
        if not force and self._last_status_ms >= 0 and now - self._last_status_ms < 45_000:
            return
        self.status["heartbeat_at_ms"] = now
        # A preview may not write any database document, lock, or local file.
        if self.apply:
            self.store.put_document(STATUS_KEY, self.status)
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.status_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(self.status, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            os.replace(temporary, self.status_path)
        self._last_status_ms = now

    def step(self):
        now = self.clock()
        cutoff = now // HOUR_MS * HOUR_MS
        bar_open = cutoff - HOUR_MS
        if bar_open < self.forward_start_ms:
            self.status["state"] = "waiting_first_full_forward_hour"
            self.write_status()
            return []
        if now < cutoff + 15_000:
            self.status["state"] = "waiting_ingest_grace"
            self.write_status()
            return []
        if now > cutoff + METHOD_SPEC["max_publication_lag_ms"]:
            self.status["state"] = "waiting_next_hour_after_publication_window"
            self.status["last_missed_cutoff_ms"] = cutoff
            self.write_status()
            return []
        results = []
        for pair in self.pairs:
            if (pair, bar_open) in self.processed:
                continue
            observed = self.clock()
            rows, truncated = fetch_hour_rows(self.store, pair, bar_open, max_rows=self.max_rows)
            record = compute_hour_features(rows, pair=pair, bar_open_ms=bar_open,
                                           observed_at_ms=observed, forward_start_ms=self.forward_start_ms,
                                           query_truncated=truncated)
            result = publish_feature(self.store, record, published_at_ms=self.clock()) if self.apply else {
                "published": False, "reason": "read_only_preview", "feature_key": record["feature_key"]}
            self.processed.add((pair, bar_open))
            self.status["last_observations"][pair] = {
                "candle_ms": bar_open, "cutoff_ms": cutoff, "feature_key": record["feature_key"],
                "observed_at_ms": observed, "quality": record["quality"], "publication": result}
            self.status["published_count"] += int(result.get("first_acceptance", False))
            results.append({"pair": pair, "quality": record["quality"], "publication": result})
        # Retain only the current hour; this is not a history replay queue.
        self.processed = {item for item in self.processed if item[1] == bar_open}
        self.status["state"] = "waiting_next_closed_hour"
        self.status["last_error"] = None
        self.write_status(force=bool(results))
        return results

    def run(self, stop: threading.Event):
        self.status["running"] = True
        self.write_status(force=True)
        try:
            while not stop.is_set():
                try:
                    results = self.step()
                    if results:
                        LOG.info("hourly observations: %s", json.dumps(results, ensure_ascii=False))
                except Exception as exc:
                    self.status["state"] = "error"
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                    LOG.exception("orderbook feature cycle failed")
                    self.write_status(force=True)
                stop.wait(15)
        finally:
            self.status["running"] = False
            self.status["state"] = "stopped"
            self.write_status(force=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", nargs="+", default=list(ALLOWED_PAIRS))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="preview latest closed hour (default)")
    mode.add_argument("--watch", action="store_true", help="watch future full hourly cutoffs")
    parser.add_argument("--apply", action="store_true", help="explicitly permit immutable publication and status writes")
    parser.add_argument("--bar-open-ms", type=int, help="select a closed hour for read-only preview")
    parser.add_argument("--max-rows", type=int, default=150_000)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    args = parser.parse_args(argv)
    if args.bar_open_ms is not None and (args.watch or args.apply):
        parser.error("--bar-open-ms is read-only preview only")
    if args.apply:
        from data_store import DataStore
        store = DataStore(root=ROOT, max_connections=2)
    else:
        store = ReadOnlyFeatureStore()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = OrderbookFeatureService(store, pairs=args.pairs, apply=args.apply,
                                     max_rows=args.max_rows, status_path=args.status_path)
    try:
        if args.watch:
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            if args.apply:
                with FeatureInstanceLock(args.lock_path):
                    service.run(stop)
            else:
                service.run(stop)
        else:
            records = service.preview(bar_open_ms=args.bar_open_ms)
            if args.apply:
                # A newly started once process has no complete forward hour.
                # This explicit diagnostic does not manufacture a past input.
                for record in records:
                    record["publication"] = publish_feature(store, record, published_at_ms=service.clock())
            print(json.dumps({"preview": True, "records": records}, ensure_ascii=False, allow_nan=False))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
