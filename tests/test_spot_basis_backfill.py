"""Spot backfill and its independent watcher preserve exchange-final facts."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from tools.backfill_spot_basis_history import (
    DEFAULT_STATUS_KEY, INTERVALS, PAIRS, SPOT_REST_URL, Journal, apply_window,
    identity, iso_timestamp, main, normalized_rows, source_rows, tail_windows,
    watch_snapshot, watch_windows, windows,
)

BASE = iso_timestamp("2026-10-08T00:00:00Z")
HOUR = INTERVALS["1h"]


def payload(count=2, start=BASE, interval=300_000):
    return [[start + i * interval, "10", "12", "8", "11", "2",
             start + (i + 1) * interval - 1, "20", 4, "1", "10", "0"] for i in range(count)]


def official(count=2):
    return normalized_rows(payload(count), "BTC/USDT", "5m", BASE, BASE + count * 300_000,
                           BASE + count * 300_000 + 5_000, SPOT_REST_URL)


class CopyWriter:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def write_row(self, value):
        self.conn.inserted.append(value)


class Cursor:
    def __init__(self, conn):
        self.conn, self.rowcount = conn, 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def executemany(self, query, values):
        values = list(values)
        self.conn.commands.append((query, values))
        self.rowcount = len(values)

    def copy(self, query):
        self.conn.commands.append((query, None))
        return CopyWriter(self.conn)


class Connection:
    def __init__(self, rows=None):
        self.rows, self.commands, self.inserted = rows or [], [], []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return Cursor(self)

    def execute(self, query, params=None):
        self.commands.append((query, params))
        return SimpleNamespace(fetchall=lambda: self.rows, fetchone=lambda: {"rows": 10})

    def commit(self):
        self.commands.append(("COMMIT", None))


class Client:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, url):
        self.calls.append(url)
        return json.dumps(self.data).encode()


class SpotBackfillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_real_market_identity_is_separate_and_restricted_to_three_coins(self):
        for pair in PAIRS:
            self.assertEqual(identity(pair, "1h"), ("binance", "spot", pair, "1h", "spot"))
        for pair, timeframe in [("BTC/USDT:USDT", "1h"), ("SOL/USDT", "1h"), ("BTC/USDT", "15m")]:
            with self.subTest(pair=pair, timeframe=timeframe), self.assertRaises(ValueError):
                identity(pair, timeframe)

    def test_default_warmup_precedes_gap_and_pages_never_include_forming_bar(self):
        now = BASE + 10 * HOUR + 123_000
        parts = list(windows(BASE, now, "1h", "BTC/USDT"))
        self.assertEqual(parts[0]["start"], BASE + 10 * HOUR - 752 * HOUR)
        self.assertEqual(parts[-1]["end"], BASE + 10 * HOUR)
        self.assertEqual(sum(part["expected"] for part in parts), 752)
        parts = list(windows(BASE, now, "5m", "TRX/USDT"))
        self.assertEqual(parts[0]["start"], BASE)
        self.assertEqual(parts[-1]["end"], BASE + 10 * HOUR)

    def test_page_boundary_and_invalid_start_are_explicit(self):
        parts = list(windows(BASE, BASE + 2001 * HOUR, "1h", "BTC/USDT", warmup_bars=0))
        self.assertEqual([part["expected"] for part in parts], [1000, 1000, 1])
        self.assertEqual(parts[0]["end"], parts[1]["start"])
        for start, end, warmup in [(BASE + 1, BASE + HOUR, 750), (BASE, BASE + 1, 750),
                                   (BASE, BASE + HOUR, -1), (BASE, BASE + HOUR, True)]:
            with self.subTest(start=start, end=end, warmup=warmup), self.assertRaises(ValueError):
                list(windows(start, end, "1h", "BTC/USDT", warmup_bars=warmup))

    def test_spot_parser_retains_actual_flow_and_verified_closed_time(self):
        rows = official()
        for item in rows:
            self.assertEqual(tuple(item[name] for name in
                ("exchange", "market", "pair", "timeframe", "candle_type")), identity("BTC/USDT", "5m"))
            self.assertIs(item["extras"]["closed"], True)
            self.assertEqual(item["extras"]["quote_volume"], 20)
            self.assertEqual(item["extras"]["taker_buy_base"], 1)
            self.assertEqual(item["extras"]["num_trades"], 4)
            self.assertEqual(item["extras"]["live_snapshot_timestamp"], BASE + 605_000)
            self.assertNotIn("live_event_timestamp", item["extras"])
            self.assertEqual(item["extras"]["backfill_source"], SPOT_REST_URL)

    def test_gap_duplicate_partial_impossible_prices_or_flow_are_rejected(self):
        mutations = [lambda data: data.pop(), lambda data: data[1].__setitem__(0, BASE),
                     lambda data: data[0].__setitem__(6, BASE + 300_000),
                     lambda data: data[0].__setitem__(2, "9"),
                     lambda data: data[0].__setitem__(9, "3"),
                     lambda data: data[0].__setitem__(7, "1000"),
                     lambda data: data[0].__setitem__(1, "NaN")]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                values = payload()
                mutate(values)
                normalized_rows(values, "BTC/USDT", "5m", BASE, BASE + 600_000,
                                BASE + 600_000, SPOT_REST_URL)
        with self.assertRaises(ValueError):
            normalized_rows(payload(), "BTC/USDT", "5m", BASE, BASE + 600_000,
                            BASE + 599_999, SPOT_REST_URL)

    def test_verified_cache_resumes_without_network_and_detects_corruption(self):
        window = list(windows(BASE, BASE + 600_000, "5m", "BTC/USDT"))[0]
        client = Client(payload())
        rows, source = source_rows(client, self.directory, window, BASE + 600_000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0].startswith(SPOT_REST_URL))
        self.assertIn("symbol=BTCUSDT", client.calls[0])
        self.assertIn("endTime=" + str(BASE + 599_999), client.calls[0])
        next_client = Client(None)
        source_rows(next_client, self.directory, window, BASE + 600_000)
        self.assertEqual(next_client.calls, [])
        cached = self.directory / source["cache"]
        data = json.loads(cached.read_text())
        data["payload"][0][1] = "9"
        cached.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            source_rows(next_client, self.directory, window, BASE + 600_000)

    def test_preview_issues_no_mutation_lock_copy_or_journal(self):
        existing = deepcopy(official())
        existing[0]["extras"].pop("closed")
        existing[0]["extras"].pop("live_snapshot_timestamp")
        connection = Connection(existing[:1])
        result = apply_window(connection, official())
        self.assertEqual(result["counts"], {"mark_closed": 1, "insert_missing": 1})
        self.assertEqual(len(connection.commands), 1)
        self.assertTrue(connection.commands[0][0].startswith("SELECT * FROM candles"))
        self.assertEqual(connection.inserted, [])

    def test_apply_keeps_existing_finalized_ws_ohlcv_and_unknown_fields(self):
        existing = deepcopy(official())
        existing[0]["open"] = 999
        existing[0]["extras"].update(live_event_timestamp=BASE + HOUR, unknown="keep")
        existing[0]["source_key"] = "websocket"
        connection = Connection(existing)
        journal = Journal(self.directory / "rollback.jsonl")
        try:
            result = apply_window(connection, official(), True, journal)
        finally:
            journal.close()
        self.assertEqual(result["counts"], {"existing_closed_flag": 2})
        self.assertEqual(connection.inserted, [])
        self.assertFalse(any("UPDATE candles SET" in str(item[0]) for item in connection.commands))
        self.assertEqual(existing[0]["extras"]["unknown"], "keep")

    def test_unflagged_price_conflict_is_not_overwritten(self):
        existing = deepcopy(official())
        existing[0]["extras"].pop("closed")
        existing[0]["extras"].pop("live_snapshot_timestamp")
        existing[0]["close"] = 10.5
        result = apply_window(Connection(existing), official())
        self.assertEqual(result["counts"], {"ohlcv_mismatch": 1, "existing_closed_flag": 1})
        self.assertEqual(result["conflict_timestamps"], [BASE])

    def test_identical_marker_preserves_annotations_and_checks_prior_extras(self):
        existing = deepcopy(official())[:1]
        existing[0]["extras"] = {"unknown": "keep", "quote_volume": 20}
        existing[0]["source_key"] = "source.feather"
        connection = Connection(existing)
        journal = Journal(self.directory / "rollback.jsonl")
        try:
            result = apply_window(connection, official(1), True, journal)
        finally:
            journal.close()
        self.assertEqual(result["counts"], {"mark_closed": 1})
        query = next(item[0] for item in connection.commands if "UPDATE candles SET extras=" in str(item[0]))
        self.assertIn("AND extras=%s", query)
        self.assertIn("source_key IS NOT DISTINCT FROM", query)
        record = next(json.loads(line) for line in (self.directory / "rollback.jsonl").read_text().splitlines()
                      if json.loads(line).get("type") == "change")
        self.assertEqual(record["after"]["extras"], {"unknown": "keep", "quote_volume": 20, "closed": True})

    def test_explicit_partial_finalization_does_not_attach_old_flow(self):
        existing = deepcopy(official())[:1]
        existing[0]["extras"].update(closed=False, quote_volume=999, unknown="keep")
        existing[0]["close"] = 10
        connection = Connection(existing)
        journal = Journal(self.directory / "rollback.jsonl")
        try:
            result = apply_window(connection, official(1), True, journal)
        finally:
            journal.close()
        self.assertEqual(result["counts"], {"reconcile_partial": 1})
        record = next(json.loads(line) for line in (self.directory / "rollback.jsonl").read_text().splitlines()
                      if json.loads(line).get("type") == "change")
        self.assertEqual(record["after"]["extras"]["quote_volume"], 20)
        self.assertEqual(record["after"]["extras"]["unknown"], "keep")

    def test_apply_requires_journal_and_mixed_dataset_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_window(Connection(), official(), True)
        mixed = official()
        mixed[1]["market"] = "futures"
        with self.assertRaises(ValueError):
            apply_window(Connection(), mixed)

    def test_repeated_window_uses_unique_transaction_journal_ids(self):
        journal = Journal(self.directory / "rollback.jsonl")
        try:
            for _ in range(2):
                apply_window(Connection(official()), official(), True, journal)
        finally:
            journal.close()
        entries = [json.loads(line) for line in (self.directory / "rollback.jsonl").read_text().splitlines()]
        starts = [entry["dataset_id"] for entry in entries if entry["type"] == "dataset_started"]
        commits = [entry["dataset_id"] for entry in entries if entry["type"] == "dataset_committed"]
        self.assertEqual(len(set(starts)), 2)
        self.assertEqual(starts, commits)

    def test_watch_tail_is_only_three_closed_hours_and_three_authorized_coins(self):
        parts = list(tail_windows(BASE + 8 * HOUR + 15_000))
        self.assertEqual([part["pair"] for part in parts], list(PAIRS))
        for part in parts:
            self.assertEqual((part["start"], part["end"], part["expected"]),
                             (BASE + 5 * HOUR, BASE + 8 * HOUR, 3))

    def test_watch_catches_long_downtime_without_gaps_then_returns_to_small_tail(self):
        for hours in (10, 1501, 2501):
            with self.subTest(hours=hours):
                parts = list(watch_windows(BASE + hours * HOUR + 1, BASE))
                for pair in PAIRS:
                    selected = [part for part in parts if part["pair"] == pair]
                    self.assertEqual(selected[0]["start"], BASE)
                    self.assertEqual(selected[-1]["end"], BASE + hours * HOUR)
                    self.assertEqual(sum(part["expected"] for part in selected), hours)
                    self.assertTrue(all(part["expected"] <= 1000 for part in selected))
                    for previous, current in zip(selected, selected[1:]):
                        self.assertEqual(previous["end"], current["start"])
        self.assertEqual(list(watch_windows(BASE + 10 * HOUR, BASE + 10 * HOUR)),
                         list(tail_windows(BASE + 10 * HOUR)))

    def test_watch_status_does_not_hide_earlier_incomplete_catchup_page(self):
        results = []
        for pair in PAIRS:
            for index in range(2):
                results.append({"pair": pair, "expected": 3, "end": BASE + HOUR,
                                "before": {"complete": not (pair == PAIRS[0] and index == 0),
                                           "last": BASE, "closed": 3}})
        self.assertFalse(watch_snapshot(results, BASE + HOUR, apply=False)["ready"])
        self.assertFalse(watch_snapshot([], BASE, apply=True, error_type="TimeoutError")["ready"])

    def test_main_default_preview_sets_readonly_and_never_creates_journal(self):
        output = self.directory / "run"
        connection = Connection()
        store = SimpleNamespace(dsn="do-not-print", schema="quant")
        with patch("data_store.DataStore", return_value=store), \
                patch("psycopg.connect", return_value=connection), \
                patch("tools.backfill_spot_basis_history.run_windows", return_value=[]) as run, \
                patch("tools.backfill_spot_basis_history.Journal", side_effect=AssertionError("preview journal")):
            self.assertEqual(main(["--start", "2026-10-08T00:00:00Z", "--end", "2026-10-09T00:00:00Z",
                                   "--output", str(output)]), 0)
        self.assertIn(("SET default_transaction_read_only TO on", None), connection.commands)
        self.assertIs(run.call_args.kwargs["apply"], False)
        self.assertIs(run.call_args.kwargs["journal"], None)
        self.assertFalse(json.loads((output / "report.json").read_text())["apply"])

    def test_watch_status_key_cannot_overwrite_main_or_trx_collector(self):
        for key in ("market_stream_status.json", "market_stream_trx_status.json", "../private.json"):
            with self.subTest(key=key), self.assertRaises(SystemExit):
                main(["--status-document-key", key, "--output", str(self.directory / key.replace("/", "_"))])
        self.assertEqual(DEFAULT_STATUS_KEY, "spot_basis_status.json")


@unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "isolated quant_test database not configured")
class SpotBackfillDatabaseTests(unittest.TestCase):
    def test_apply_preserves_final_ws_and_perpetual_market_and_rollback_journal_is_private(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from data_store import DataStore
        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        self.assertEqual(conninfo_to_dict(dsn).get("dbname"), "quant_test")
        schema = "spot_basis_test_" + uuid.uuid4().hex[:12]
        store = DataStore(dsn=dsn, schema=schema)
        store.initialize()
        try:
            spot_rows = official()
            first = deepcopy(spot_rows[0])
            first["extras"].update(live_event_timestamp=BASE + HOUR, custom="keep")
            store.upsert_candles(*identity("BTC/USDT", "5m"), [first], source_key="websocket")
            perp = deepcopy(spot_rows)
            store.upsert_candles("binance", "futures", "BTC/USDT:USDT", "5m", "futures", perp)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "rollback.jsonl"
                journal = Journal(path)
                try:
                    with store._connection() as conn:
                        result = apply_window(conn, spot_rows, True, journal)
                finally:
                    journal.close()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result["counts"], {"existing_closed_flag": 1, "insert_missing": 1})
            actual = store.get_candles(*identity("BTC/USDT", "5m"), limit=2)
            self.assertEqual(actual[0]["extras"]["custom"], "keep")
            self.assertEqual(actual[0]["extras"]["live_event_timestamp"], BASE + HOUR)
            self.assertEqual(len(store.get_candles("binance", "futures", "BTC/USDT:USDT", "5m", "futures")), 2)
        finally:
            with psycopg.connect(dsn, autocommit=True) as connection:
                from psycopg import sql
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
