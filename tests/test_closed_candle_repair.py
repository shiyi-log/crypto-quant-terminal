"""Close repair must retain final facts, and require evidence for partial rows."""
from contextlib import contextmanager
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid
from tools.repair_closed_candles import IDENTITY, Journal, OHLCV, local_action, main, parse_rest_window, reconciled_partial, repair_local, repair_rest, rest_action, trusted_source
PAIR = 'BTC/USDT:USDT'
BASE = 1700000100000
IDENT = ('binance', 'futures', PAIR, '5m', 'futures')

def candle(stamp=BASE, extras=None, identity=IDENT):
    return {**dict(zip(IDENTITY, identity)), 'timestamp': stamp, 'open': 10.0, 'high': 12.0, 'low': 8.0, 'close': 11.0, 'volume': 2.0, 'extras': {} if extras is None else extras, 'source_key': 'local-key'}

def rest_payload(stamps):
    return [[stamp, '10', '12', '8', '11', '2', stamp + 299999, '20', 4, '1', '10', '0'] for stamp in stamps]

def official_rows(limit=2):
    now = BASE + limit * 300000 + 1234
    return (parse_rest_window(rest_payload([BASE + index * 300000 for index in range(limit)]), PAIR, '5m', now, limit), now)

def source_file(tmp_path, timeframe='5m'):
    key = f'bot/user_data/data/binance/futures/BTC_USDT-{timeframe}-futures.feather'
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'test public snapshot')
    os.utime(path, ns=((BASE + 600000) * 1000000,) * 2)
    stat = path.stat()
    return {'key': key, 'path': str(path), 'kind': 'feather', 'error': None, 'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size}

class Cursor:

    def __init__(self, conn):
        self.conn, self.position, self.rowcount = (conn, 0, 0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, query, params):
        self.conn.commands.append((query, params))

    def fetchmany(self, size):
        result = self.conn.rows[self.position:self.position + size]
        self.position += size
        return result

    def executemany(self, query, params):
        params = list(params)
        self.conn.commands.append((query, params))
        self.rowcount = len(params)

class Connection:

    def __init__(self, rows=None, sources=None):
        self.rows = rows or []
        self.sources = sources or []
        self.commands, self.transactions = ([], 0)

    def execute(self, query, params=None):
        self.commands.append((query, params))
        result = self.sources if query.startswith('SELECT key,path') else self.rows
        return SimpleNamespace(fetchall=lambda: result)

    def cursor(self, **_kwargs):
        return Cursor(self)

    def commit(self):
        pass

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

class ClosedCandleRepairTests(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)

    def test_local_cutoff_respects_source_mtime_and_identity(self):
        tmp_path = self.tmp_path
        entry = source_file(tmp_path)
        source = trusted_source(tmp_path, entry, BASE + 9000000)
        row = candle(BASE + 300000, {'flow_imbal': 0.5, 'annotation': 'keep'})
        row['source_key'] = entry['key']
        assert source['cutoff'] == BASE + 600000
        assert local_action(row, source) == 'mark_closed'
        row['timestamp'] += 300000
        assert local_action(row, source) == 'forming_or_future'
        row['timestamp'] = BASE
        row['candle_type'] = 'mark'
        assert local_action(row, source) == 'identity_mismatch'

    def test_local_never_infers_a_partial_or_live_fact(self):
        tmp_path = self.tmp_path
        for extras, reason in [({'closed': False}, 'existing_closed_flag'), ({'closed': True}, 'existing_closed_flag'), ({'closed': None}, 'existing_closed_flag'), ({'live_event_timestamp': BASE}, 'live_provenance'), ({'live_snapshot_timestamp': BASE}, 'live_provenance'), ({'source': 'websocket'}, 'live_provenance')]:
            with self.subTest(extras=extras, reason=reason):
                entry = source_file(tmp_path)
                source = trusted_source(tmp_path, entry, BASE + 1000000)
                row = candle(extras=extras)
                row['source_key'] = entry['key']
                assert local_action(row, source) == reason

    def test_local_rejects_untrusted_or_changed_source(self):
        tmp_path = self.tmp_path
        for change in [{'kind': 'pickle'}, {'mtime_ns': 1}, {'size': 1}, {'error': 'bad import'}, {'path': '/untrusted/another.feather'}, {'key': '../escape.feather'}]:
            with self.subTest(change=change):
                entry = {**source_file(tmp_path), **change}
                with self.assertRaises(ValueError):
                    trusted_source(tmp_path, entry, BASE + 1000000)

    def test_local_refuses_symlink(self):
        tmp_path = self.tmp_path
        entry = source_file(tmp_path)
        path = Path(entry['path'])
        other = path.with_name('backup')
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(ValueError):
            trusted_source(tmp_path, entry, BASE + 1000000)

    def test_local_calendar_month_closure(self):
        tmp_path = self.tmp_path
        entry = source_file(tmp_path, '1M')
        path = Path(entry['path'])
        closed_at = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        os.utime(path, ns=(closed_at * 1000000,) * 2)
        entry.update(mtime_ns=path.stat().st_mtime_ns)
        source = trusted_source(tmp_path, entry, closed_at)
        row = candle(int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000), identity=source['identity'])
        row['source_key'] = entry['key']
        assert local_action(row, source) == 'mark_closed'
        source['cutoff'] -= 1
        assert local_action(row, source) == 'forming_or_future'

    def test_local_rejects_invalid_prices(self):
        tmp_path = self.tmp_path
        for field, value in [('open', 0), ('close', float('nan')), ('low', 13), ('volume', -1)]:
            with self.subTest(field=field, value=value):
                entry = source_file(tmp_path)
                source = trusted_source(tmp_path, entry, BASE + 1000000)
                row = candle()
                row['source_key'] = entry['key']
                row[field] = value
                assert local_action(row, source) == 'invalid_ohlcv'

    def test_rest_exact_closed_continuous_window_and_exchange_fields(self):
        rows, now = official_rows()
        assert rows[-1]['timestamp'] + 300000 <= now
        assert rows[0]['extras']['taker_buy_base'] == 1.0
        assert rows[0]['extras']['quote_volume'] == 20.0
        assert rows[0]['extras']['closed'] is True
        assert tuple((rows[0][key] for key in IDENTITY)) == IDENT

    def test_rest_rejects_invalid_or_incomplete_payload(self):
        for mutator in [lambda data: data.pop(), lambda data: data[0].__setitem__(0, BASE + 1), lambda data: data[0].__setitem__(6, BASE + 300000), lambda data: data[0].__setitem__(2, '9'), lambda data: data[0].__setitem__(5, 'NaN'), lambda data: data[0].__setitem__(7, '100'), lambda data: data[0].__setitem__(8, 1.5), lambda data: data[0].__setitem__(9, '3'), lambda data: data[0].__setitem__(10, '30')]:
            with self.subTest(mutator=mutator):
                payload = rest_payload([BASE, BASE + 300000])
                mutator(payload)
                with self.assertRaises(ValueError):
                    parse_rest_window(payload, PAIR, '5m', BASE + 601234, 2)

    def test_rest_supports_maximum_1500_window(self):
        rows, _now = official_rows(1500)
        assert len(rows) == 1500

    def test_rest_preserves_finalized_ws_and_marks_only_matching_unflagged(self):
        rows, _now = official_rows()
        official = rows[0]
        assert rest_action(None, official) == 'insert_missing'
        existing = candle(extras={'flow_imbal': 0.1})
        assert rest_action(existing, official) == 'mark_closed'
        existing['close'] = 10.5
        assert rest_action(existing, official) == 'ohlcv_mismatch'
        existing['extras'] = {'closed': True, 'live_event_timestamp': BASE}
        assert rest_action(existing, official) == 'existing_closed_flag'

    def test_official_finalization_replaces_explicit_partial_without_stale_flow(self):
        official = official_rows()[0][0]
        before = candle(extras={'closed': False, 'live_event_timestamp': BASE, 'flow_imbal': -0.9, 'buy_ratio': 0.01, 'note': 'retain'})
        before['close'] = 10.5
        original = deepcopy(before)
        assert rest_action(before, official) == 'reconcile_partial'
        after = reconciled_partial(before, official)
        assert after['close'] == 11
        assert after['extras']['closed'] is True
        assert after['extras']['note'] == 'retain'
        assert after['extras']['taker_buy_base'] == 1
        assert 'flow_imbal' not in after['extras']
        assert 'buy_ratio' not in after['extras']
        assert 'live_event_timestamp' not in after['extras']
        assert after['source_key'] is None
        assert before == original

    def test_rest_dry_run_performs_no_writes(self):
        rows, now = official_rows()
        before = candle(extras={'closed': False})
        connection = Connection([before])
        report = repair_rest(connection, {PAIR}, ['5m'], 2, False, None, fetch=lambda *_args: (rows, now))
        assert report[0]['counts'] == {'reconcile_partial': 1, 'insert_missing': 1}
        assert connection.transactions == 1
        assert not any((query.startswith(('INSERT', 'UPDATE', 'DELETE')) for query, _ in connection.commands))

    def test_rest_apply_records_exact_before_for_all_changes(self):
        tmp_path = self.tmp_path
        rows, now = official_rows(3)
        unflagged = candle(extras={'annotation': 'unchanged'})
        partial = candle(BASE + 300000, {'closed': False, 'flow_imbal': -0.5})
        partial['close'] = 10.5
        connection = Connection([unflagged, partial])
        journal = Journal(tmp_path / 'rollback.jsonl')
        report = repair_rest(connection, {PAIR}, ['5m'], 3, True, journal, fetch=lambda *_args: (rows, now))
        journal.close()
        records = [json.loads(line) for line in journal.path.read_text().splitlines()]
        changes = [record for record in records if record['type'] == 'change']
        assert report[0]['counts'] == {'mark_closed': 1, 'reconcile_partial': 1, 'insert_missing': 1}
        assert [item['action'] for item in changes] == ['mark_closed', 'reconcile_partial', 'insert_missing']
        assert changes[0]['before'] == unflagged
        assert changes[1]['before'] == partial
        assert changes[2]['before'] is None
        assert records[-1]['type'] == 'dataset_committed'
        marker_sql = next((query for query, _ in connection.commands if query.startswith('UPDATE candles SET extras')))
        assert 'SET extras=extras ||' in marker_sql
        assert 'SET open' not in marker_sql
        assert journal.path.stat().st_mode & 511 == 384

    def test_local_apply_only_changes_extras_and_preserves_backup(self):
        tmp_path = self.tmp_path
        entry = source_file(tmp_path)
        before = candle(extras={'flow_imbal': 0.25, 'quote_volume': 20})
        before['source_key'] = entry['key']
        connection = Connection([before], [entry])
        journal = Journal(tmp_path / 'rollback.jsonl')
        report = repair_local(connection, tmp_path, {PAIR}, ['5m'], BASE + 1000000, True, journal)
        journal.close()
        records = [json.loads(line) for line in journal.path.read_text().splitlines()]
        change = next((record for record in records if record['type'] == 'change'))
        assert report[0]['counts'] == {'mark_closed': 1}
        assert change['before'] == before
        assert change['after']['extras'] == {'flow_imbal': 0.25, 'quote_volume': 20, 'closed': True}
        assert all((change['after'][key] == before[key] for key in OHLCV))
        assert records[-1]['type'] == 'dataset_committed'

    def test_cli_rejects_applyless_invalid_rest_arguments(self):
        with self.assertRaises(SystemExit):
            main(['--rest-window', '1501'])
        with self.assertRaises(SystemExit):
            main(['--rest-only'])

    @unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "isolated PostgreSQL test URL not configured")
    def test_postgres_final_facts_rollback_inventory_and_dataset_summary(self):
        """Exercise actual SQL in an expendable schema, never the live schema."""
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        schema = "test_close_repair_" + uuid.uuid4().hex
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"], row_factory=dict_row) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(schema)))
            conn.commit()
            try:
                conn.execute("""CREATE TABLE candles (
                    exchange TEXT,market TEXT,pair TEXT,timeframe TEXT,candle_type TEXT,
                    timestamp BIGINT,open FLOAT8,high FLOAT8,low FLOAT8,close FLOAT8,volume FLOAT8,
                    extras JSONB,source_key TEXT,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(exchange,market,pair,timeframe,candle_type,timestamp))""")
                conn.execute("""CREATE TABLE candle_datasets (
                    exchange TEXT,market TEXT,pair TEXT,timeframe TEXT,candle_type TEXT,
                    rows BIGINT,first_timestamp BIGINT,last_timestamp BIGINT,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(exchange,market,pair,timeframe,candle_type))""")
                before = [candle(extras={"flow_imbal": 0.25}),
                          candle(BASE + 300_000, {"closed": False, "flow_imbal": -0.9, "note": "retain"}),
                          candle(BASE + 600_000, {"closed": True, "live_event_timestamp": BASE + 899_999})]
                before[1]["close"] = 10.5
                before[2]["close"] = 9.5  # even an official mismatch must preserve a final WS row
                for row in before:
                    conn.execute("""INSERT INTO candles (exchange,market,pair,timeframe,candle_type,
                        timestamp,open,high,low,close,volume,extras,source_key)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                 (*(row[key] for key in (*IDENTITY, "timestamp", *OHLCV)),
                                  Jsonb(row["extras"]), row["source_key"]))
                conn.commit()
                rows, now = official_rows(4)
                repair_rest(conn, {PAIR}, ["5m"], 4, False, None, fetch=lambda *_args: (rows, now))
                unchanged = conn.execute("SELECT * FROM candles ORDER BY timestamp").fetchall()
                conn.commit()
                journal = Journal(self.tmp_path / "postgres-rollback.jsonl")
                try:
                    report = repair_rest(conn, {PAIR}, ["5m"], 4, True, journal,
                                         fetch=lambda *_args: (rows, now))
                finally:
                    journal.close()
                after = conn.execute("SELECT * FROM candles ORDER BY timestamp").fetchall()
                assert len(after) == 4
                assert after[0]["extras"] == {"flow_imbal": 0.25, "closed": True}
                assert after[0]["updated_at"] == unchanged[0]["updated_at"]
                assert after[1]["close"] == 11
                assert after[1]["extras"]["note"] == "retain"
                assert "flow_imbal" not in after[1]["extras"]
                assert after[2] == unchanged[2]
                assert after[3]["extras"]["closed"] is True
                summary = conn.execute("SELECT rows FROM candle_datasets").fetchone()
                assert summary["rows"] == 4
                assert report[0]["counts"] == {"mark_closed": 1, "reconcile_partial": 1,
                                                "existing_closed_flag": 1, "insert_missing": 1}
                conn.commit()
            finally:
                conn.rollback()
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                conn.commit()
