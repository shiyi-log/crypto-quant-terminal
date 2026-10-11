"""Official TRX backfill validates full facts and never rewrites final rows."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import uuid
import zipfile

from tools.backfill_trx_history import (
    HEADER, IDENTITY, INTERVALS, Journal, OfficialClient, apply_window,
    archive_url, coverage, identity, iso_timestamp, main, normalized_rows,
    source_rows, verified_archive, windows,
)

BASE = iso_timestamp("2024-01-01T00:00:00Z")


def payload(count=2, start=BASE, interval=300_000):
    return [[start + i * interval, "10", "12", "8", "11", "2",
             start + (i + 1) * interval - 1, "20", 4, "1", "10", "0"] for i in range(count)]


def official(count=2):
    return normalized_rows(payload(count), "5m", BASE, BASE + count * 300_000,
                           BASE + count * 300_000, {"url": "official.test", "sha256": "abc"})


class CopyWriter:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def write_row(self, row):
        self.conn.inserted.append(row)


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
    def __init__(self, rows=None, summary=True):
        self.rows, self.summary = rows or [], summary
        self.commands, self.inserted = [], []

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return Cursor(self)

    def execute(self, query, params=None):
        self.commands.append((query, params))
        return SimpleNamespace(fetchall=lambda: self.rows,
                               fetchone=lambda: {"rows": 10} if self.summary else None)


class DownloadClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def archive_bytes(records, name="TRXUSDT-5m-2024-01.csv"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        csv = "\n".join(",".join(map(str, row)) for row in [HEADER, *records]) + "\n"
        zipped.writestr(name, csv)
    return stream.getvalue()


class TrxBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_monthly_windows_are_contiguous_and_freeze_closed_tail(self):
        end = iso_timestamp("2024-03-02T12:01:32Z")
        for timeframe, interval in INTERVALS.items():
            with self.subTest(timeframe=timeframe):
                parts = list(windows(BASE, end, timeframe))
                self.assertEqual([part["month"] for part in parts], ["2024-01", "2024-02", "2024-03"])
                self.assertEqual([part["archive"] for part in parts], [True, True, False])
                self.assertEqual(parts[1]["end"] - parts[1]["start"], 29 * 86_400_000)
                self.assertEqual(parts[-1]["end"], end // interval * interval)
                self.assertEqual(parts[0]["start"], BASE)
                for previous, next_part in zip(parts, parts[1:]):
                    self.assertEqual(previous["end"], next_part["start"])

    def test_partial_first_month_and_december_boundary(self):
        start = iso_timestamp("2024-12-14T01:30:00Z")
        end = iso_timestamp("2025-01-01T00:00:00Z")
        parts = list(windows(start, end, "15m"))
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0]["archive"])
        self.assertEqual(parts[0]["month_start"], iso_timestamp("2024-12-01T00:00:00Z"))

    def test_invalid_and_forming_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            iso_timestamp("2024-01-01")
        for start, end in [(BASE + 1, BASE + 600_000), (BASE, BASE + 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                list(windows(start, end, "5m"))

    def test_exchange_parser_handles_full_month_and_keeps_taker_fields(self):
        count = 31 * 24 * 12
        rows = normalized_rows(payload(count), "5m", BASE, BASE + count * 300_000,
                               BASE + count * 300_000, {"url": "official.test", "sha256": "abc"})
        self.assertEqual(len(rows), count)
        self.assertEqual(rows[-1]["timestamp"], BASE + (count - 1) * 300_000)
        extras = rows[0]["extras"]
        self.assertIs(extras["closed"], True)
        self.assertEqual(extras["taker_buy_base"], 1.0)
        self.assertEqual(extras["taker_buy_quote"], 10.0)
        self.assertEqual(extras["num_trades"], 4)
        self.assertEqual(extras["live_snapshot_timestamp"], BASE + count * 300_000)
        self.assertNotIn("live_event_timestamp", extras)
        self.assertEqual(tuple(rows[0][name] for name in IDENTITY), identity("5m"))

    def test_rejects_gaps_duplicates_partial_and_impossible_flow(self):
        for mutate in [lambda data: data.pop(), lambda data: data[1].__setitem__(0, BASE),
                       lambda data: data[0].__setitem__(6, BASE + 300_000),
                       lambda data: data[0].__setitem__(9, "3"),
                       lambda data: data[0].__setitem__(2, "9")]:
            with self.subTest(mutate=mutate):
                data = payload()
                mutate(data)
                with self.assertRaises(ValueError):
                    normalized_rows(data, "5m", BASE, BASE + 600_000, BASE + 600_000, {"url": "test"})
        with self.assertRaises(ValueError):
            normalized_rows(payload(), "5m", BASE, BASE + 600_000, BASE + 599_999, {"url": "test"})

    def test_verified_archive_resumes_without_network_and_detects_corruption(self):
        url, name = archive_url("5m", "2024-01")
        data = archive_bytes(payload())
        checksum = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
        client = DownloadClient({url: data, url + ".CHECKSUM": checksum})
        records, provenance = verified_archive(client, self.directory, "5m", "2024-01")
        self.assertEqual(len(records), 2)
        self.assertEqual(provenance["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(len(client.calls), 2)
        cached_client = DownloadClient({})
        self.assertEqual(verified_archive(cached_client, self.directory, "5m", "2024-01")[0], records)
        self.assertEqual(cached_client.calls, [])
        (self.directory / name).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            verified_archive(cached_client, self.directory, "5m", "2024-01")

    def test_checksum_must_match_exact_archive_name_and_content(self):
        url, name = archive_url("5m", "2024-01")
        data = archive_bytes(payload())
        for checksum in [b"0" * 64 + b"  other.zip\n", b"0" * 64 + f"  {name}\n".encode()]:
            with self.subTest(checksum=checksum), tempfile.TemporaryDirectory() as tmp:
                client = DownloadClient({url: data, url + ".CHECKSUM": checksum})
                with self.assertRaises(ValueError):
                    verified_archive(client, tmp, "5m", "2024-01")

    def test_archive_with_unexpected_zip_entry_is_rejected(self):
        url, name = archive_url("5m", "2024-01")
        data = archive_bytes(payload(), "../escape.csv")
        checksum = f"{hashlib.sha256(data).hexdigest()}  {name}".encode()
        with self.assertRaises(ValueError):
            verified_archive(DownloadClient({url: data, url + ".CHECKSUM": checksum}),
                             self.directory, "5m", "2024-01")

    def test_unpublished_archive_falls_back_to_exact_rest_page(self):
        window = list(windows(BASE, BASE + 600_000, "5m"))[0]
        window["archive"] = True
        url, _name = archive_url("5m", "2024-01")
        class Client:
            calls = []
            def get(self, request):
                self.calls.append(request)
                if request == url + ".CHECKSUM":
                    raise HTTPError(request, 404, "missing", {}, None)
                self.assert_request(request)
                return json.dumps(payload()).encode()
            def assert_request(self, request):
                assert "startTime=1704067200000" in request
                assert "endTime=1704067799999" in request
                assert "limit=2" in request
        client = Client()
        rows, sources = source_rows(client, self.directory, window, BASE + 600_000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sources[0]["kind"], "rest")
        resumed = DownloadClient({})
        window["archive"] = False
        self.assertEqual(source_rows(resumed, self.directory, window, BASE + 600_000)[0], rows)
        self.assertEqual(resumed.calls, [])
        cached_path = next(self.directory.glob("*.json"))
        cache = json.loads(cached_path.read_text())
        cache["payload"][0][4] = "10.5"  # Valid OHLCV, but altered official evidence.
        cached_path.write_text(json.dumps(cache))
        with self.assertRaisesRegex(ValueError, "cached REST SHA256 mismatch"):
            source_rows(resumed, self.directory, window, BASE + 600_000)

    def test_dry_run_performs_no_database_or_journal_writes(self):
        rows = official()
        before = deepcopy(rows[0])
        before["extras"] = {"closed": True, "live_event_timestamp": BASE, "note": "retain"}
        before["close"] = 9.0
        connection = Connection([before])
        report = apply_window(connection, rows)
        self.assertEqual(report["counts"], {"existing_closed_flag": 1, "insert_missing": 1})
        self.assertEqual(connection.inserted, [])
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "DELETE", "COPY")) for query, _ in connection.commands))
        self.assertNotIn("FOR UPDATE", connection.commands[0][0])
        self.assertEqual(before["extras"]["note"], "retain")

    def test_apply_preserves_final_ws_and_journals_exact_missing_row(self):
        rows = official()
        before = deepcopy(rows[0])
        before["extras"] = {"closed": True, "live_event_timestamp": BASE, "note": "retain"}
        connection = Connection([before])
        journal = Journal(self.directory / "rollback.jsonl")
        result = apply_window(connection, rows, True, journal)
        journal.close()
        self.assertEqual(result["counts"], {"existing_closed_flag": 1, "insert_missing": 1})
        self.assertEqual(len(connection.inserted), 1)
        records = [json.loads(line) for line in journal.path.read_text().splitlines()]
        change = next(record for record in records if record["type"] == "change")
        self.assertIsNone(change["before"])
        self.assertEqual(change["after"], rows[1])
        self.assertEqual(records[-1]["type"], "dataset_committed")
        self.assertIn("pg_advisory_xact_lock", connection.commands[0][0])
        self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)

    def test_partial_finalization_retains_annotations_and_removes_stale_flow(self):
        rows = official(1)
        before = deepcopy(rows[0])
        before["close"] = 10.0
        before["extras"] = {"closed": False, "live_event_timestamp": BASE,
                            "flow_imbal": -0.8, "taker_buy_base": 0, "annotation": "retain"}
        connection = Connection([before])
        journal = Journal(self.directory / "rollback.jsonl")
        result = apply_window(connection, rows, True, journal)
        journal.close()
        self.assertEqual(result["counts"], {"reconcile_partial": 1})
        changes = [json.loads(line) for line in journal.path.read_text().splitlines() if '"type": "change"' in line]
        self.assertEqual(changes[0]["before"], before)
        extras = changes[0]["after"]["extras"]
        self.assertEqual(extras["annotation"], "retain")
        self.assertEqual(extras["taker_buy_base"], 1.0)
        self.assertNotIn("live_event_timestamp", extras)
        self.assertNotIn("flow_imbal", extras)

    def test_matching_unflagged_only_marks_closed_and_conflicts_stay_intact(self):
        rows = official(2)
        matching, mismatch = deepcopy(rows[0]), deepcopy(rows[1])
        matching["extras"] = {"annotation": "keep", "flow_imbal": 0.25}
        mismatch["extras"] = {}
        mismatch["close"] = 10.0
        journal = Journal(self.directory / "rollback.jsonl")
        connection = Connection([matching, mismatch])
        result = apply_window(connection, rows, True, journal)
        journal.close()
        self.assertEqual(result["counts"], {"mark_closed": 1, "ohlcv_mismatch": 1})
        self.assertEqual(result["conflict_timestamps"], [mismatch["timestamp"]])
        query = next(query for query, _ in connection.commands if query.startswith("UPDATE candles"))
        self.assertIn("extras=extras ||", query)
        self.assertNotIn("SET open", query)

    def test_rate_limit_honors_retry_after_without_retrying_long_ban(self):
        sleeps, attempts = [], []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read(self, _limit):
                return b"official"
        def opener(_url, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise HTTPError("test", 429, "retry", {"Retry-After": "7"}, None)
            return Response()
        client = OfficialClient(opener=opener, sleeper=sleeps.append)
        self.assertEqual(client.get("test"), b"official")
        self.assertEqual(sleeps, [7])
        def banned(_url, **_kwargs):
            raise HTTPError("test", 429, "retry", {"Retry-After": "120"}, None)
        client = OfficialClient(opener=banned, sleeper=sleeps.append)
        with self.assertRaises(HTTPError):
            client.get("test")
        self.assertEqual(client.requests, 1)

    def test_cli_default_dry_run_cannot_initialize_database(self):
        output = self.directory / "preview"
        with patch("data_store.DataStore") as store, patch("psycopg.connect", side_effect=RuntimeError("redacted")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--output", str(output), "--end", "2024-01-01T00:10:00Z", "--timeframes", "5m"]), 1)
        report = json.loads((output / "report.json").read_text())
        self.assertFalse(report["apply"])
        store.return_value.initialize.assert_not_called()
        self.assertEqual(list(output.glob("rollback*")), [])

    def test_cli_resume_reuses_frozen_boundary_and_skips_finalized_downloads(self):
        output = self.directory / "resume"
        complete = {"total": 2, "closed": 2, "misaligned": 0, "first": BASE,
                    "last": BASE + 300_000, "expected": 2, "complete": True}
        with patch("data_store.DataStore") as store, patch("psycopg.connect"), \
                patch("tools.backfill_trx_history.coverage", return_value=complete), \
                patch("tools.backfill_trx_history.source_rows") as download, \
                patch("sys.stdout", new_callable=io.StringIO):
            store.return_value.schema = "quant"
            self.assertEqual(main(["--output", str(output), "--end", "2024-01-01T00:10:00Z", "--timeframes", "5m"]), 0)
            self.assertEqual(main(["--resume", "--output", str(output), "--timeframes", "5m"]), 0)
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["observed_at"], BASE + 600_000)
        self.assertEqual(len(report["windows"]), 1)
        self.assertEqual(report["requests"], 0)
        download.assert_not_called()
        store.return_value.initialize.assert_not_called()

    def test_cli_apply_reports_unresolved_coverage_as_incomplete(self):
        output = self.directory / "incomplete"
        incomplete = {"total": 1, "closed": 1, "misaligned": 0, "first": BASE,
                      "last": BASE, "expected": 2, "complete": False}
        with patch("data_store.DataStore") as store, patch("psycopg.connect"), \
                patch("tools.backfill_trx_history.coverage", return_value=incomplete), \
                patch("tools.backfill_trx_history.source_rows", return_value=(official(), [])), \
                patch("tools.backfill_trx_history.apply_window", return_value={"counts": {"ohlcv_mismatch": 1}, "conflict_timestamps": [BASE]}), \
                patch("sys.stdout", new_callable=io.StringIO):
            store.return_value.schema = "quant"
            self.assertEqual(main(["--apply", "--output", str(output), "--end", "2024-01-01T00:10:00Z", "--timeframes", "5m"]), 1)
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["windows"][0]["after"]["complete"])

    def test_resume_rejects_changed_range_before_connecting(self):
        output = self.directory / "manifest"
        output.mkdir()
        (output / "manifest.json").write_text(json.dumps({"version": 1, "pair": "TRX/USDT:USDT",
                                                         "start": BASE, "observed_at": BASE + 600_000,
                                                         "timeframes": ["5m"]}))
        with patch("psycopg.connect") as connect, patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(["--resume", "--start", "2024-01-02T00:00:00Z", "--timeframes", "5m", "--output", str(output)])
        connect.assert_not_called()

    @unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "isolated PostgreSQL test URL not configured")
    def test_postgres_copy_resume_preserves_ws_and_summary(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        schema = "trx_backfill_test_" + uuid.uuid4().hex[:12]
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            self.addCleanup(lambda: self.drop_schema(schema))
            conn.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(schema)))
            conn.execute((Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(), prepare=False)
            rows = official(2)
            final = deepcopy(rows[0])
            final["extras"] = {"closed": True, "live_event_timestamp": BASE + 1, "flow_imbal": 0.3}
            columns = (*IDENTITY, "timestamp", "open", "high", "low", "close", "volume", "extras", "source_key")
            conn.execute("INSERT INTO candles (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(columns)) + ")",
                         [Jsonb(final[key]) if key == "extras" else final[key] for key in columns])
            conn.execute("INSERT INTO candle_datasets VALUES (%s,%s,%s,%s,%s,1,%s,%s,CURRENT_TIMESTAMP)", (*identity("5m"), BASE, BASE))
            journal = Journal(self.directory / "integration-rollback.jsonl")
            result = apply_window(conn, rows, True, journal)
            journal.close()
            self.assertEqual(result["counts"]["insert_missing"], 1)
            stored = conn.execute("SELECT timestamp,extras FROM candles ORDER BY timestamp").fetchall()
            self.assertEqual(stored[0]["extras"], final["extras"])
            self.assertEqual(stored[1]["extras"], rows[1]["extras"])
            summary = conn.execute("SELECT rows,first_timestamp,last_timestamp FROM candle_datasets").fetchone()
            self.assertEqual(summary["rows"], 2)
            # A later unversioned file cannot erase either a WS fact or the
            # official source/taker evidence of the new historical row.
            from data_store import DataStore
            store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=schema)
            store._sql = sql
            stale = deepcopy(rows)
            for row in stale:
                row["extras"] = {"closed": True}
                row["close"] = 9.0
            stale_values = [[Jsonb(row[key]) if key == "extras" else row[key] for key in columns] for row in stale]
            blocked = store._copy_upsert(conn, "candles", list(columns), [*IDENTITY, "timestamp"], stale_values)
            self.assertEqual(blocked["written"], 0)
            stored_again = conn.execute("SELECT timestamp,extras FROM candles ORDER BY timestamp").fetchall()
            self.assertEqual(stored_again, stored)
            window = list(windows(BASE, BASE + 600_000, "5m"))[0]
            self.assertTrue(coverage(conn, window)["complete"])
            again = apply_window(conn, rows)
            self.assertEqual(again["counts"], {"existing_closed_flag": 2})

    @staticmethod
    def drop_schema(schema):
        import psycopg
        from psycopg import sql
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
