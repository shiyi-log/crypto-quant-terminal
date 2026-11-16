"""恢复验证应发现静默丢失、精度变化与快照漂移；数据库测试仅使用独立测试库。"""
import copy
from contextlib import nullcontext
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

import pytest


def capability():
    assert importlib.util.find_spec('tools.database_restore_verification'), 'business restore verification missing'
    return importlib.import_module('tools.database_restore_verification')


def manifest():
    return {'schema': 'quant-business-manifest-v1', 'namespace': 'quant',
            'tables': {'demo_orders': {'row_count': 0, 'rows_sha256': 'a'*64,
                                      'columns': [], 'identity_sha256': 'b'*64}},
            'sequences': {}, 'catalog_sha256': 'c'*64}


def test_exact_compare_detects_missing_even_empty_extra_and_changed_content():
    tool = capability()
    expected = manifest()
    assert tool.compare_business_manifests(expected, copy.deepcopy(expected))['status'] == 'matched'
    for alteration in ('missing', 'extra', 'data', 'columns', 'sequence', 'catalog'):
        actual = copy.deepcopy(expected)
        if alteration == 'missing':
            del actual['tables']['demo_orders']
        elif alteration == 'extra':
            actual['tables']['demo_fills'] = copy.deepcopy(actual['tables']['demo_orders'])
        elif alteration == 'data':
            actual['tables']['demo_orders']['rows_sha256'] = 'd'*64
        elif alteration == 'columns':
            actual['tables']['demo_orders']['columns'] = [{'name': 'new_column'}]
        elif alteration == 'sequence':
            actual['sequences']['events_seq'] = {'last_value': '2', 'is_called': True}
        else:
            actual['catalog_sha256'] = 'e'*64
        result = tool.compare_business_manifests(expected, actual)
        assert result['status'] == 'mismatch', alteration
        assert result['differences']


@pytest.mark.parametrize('invalid', [{}, {'schema': 'unknown'}, None, []])
def test_malformed_manifest_never_compares_successfully(invalid):
    tool = capability()
    with pytest.raises(tool.VerificationError, match='manifest'):
        tool.compare_business_manifests(invalid, invalid)


def test_restore_verification_checks_archive_receipt_migrations_and_constraints(tmp_path, monkeypatch):
    tool = capability()
    expected = manifest()
    backup = tmp_path / 'backup'
    backup.mkdir()
    archive = backup / 'database.dump'
    archive.write_bytes(b'PGDMP-fixture')
    business_path = backup / 'business-manifest.json'
    business_path.write_text(json.dumps(expected))
    backup_record = {'schema': 'quant-database-backup-v1', 'status': 'verified',
                     'archive': {'file': 'database.dump', 'sha256': tool.backup._hash(archive),
                                 'size_bytes': archive.stat().st_size},
                     'business_manifest': {'file': business_path.name,
                                           'sha256': tool.backup._hash(business_path),
                                           'snapshot_consistent': True}}
    (backup / 'manifest.json').write_text(json.dumps(backup_record))
    receipt = tmp_path / 'restore.json'
    receipt.write_text(json.dumps({'status': 'restored', 'database': 'quant_restore_unit',
                                   'archive_sha256': backup_record['archive']['sha256'],
                                   'business_validation': 'pending', 'dsn': 'postgresql://u:synthetic-secret@host/db'}))
    conn = SimpleNamespace(info=SimpleNamespace(dbname='quant_restore_unit'))
    monkeypatch.setattr(tool, '_snapshot', lambda *a: nullcontext())
    monkeypatch.setattr(tool, 'capture_business_manifest', lambda *a, **kw: copy.deepcopy(expected))
    checks = []
    def check(*args, **kwargs):
        checks.append(True)
        return {'constraints': 'passed', 'migrations': 'current'}
    monkeypatch.setattr(tool, 'validate_database_invariants', check)
    verified = tool.verify_restored_database(conn, backup, receipt, schema='quant')
    assert verified['business_validation'] == 'passed'
    assert 'dsn' not in verified
    assert verified['verified_namespace'] == 'quant'
    assert verified['coverage'] == 'scoped_schema'
    assert verified['archive_sha256'] == backup_record['archive']['sha256']
    assert checks == [True]
    def fail(*args, **kwargs):
        raise tool.VerificationError('migration check failed')
    monkeypatch.setattr(tool, 'validate_database_invariants', fail)
    with pytest.raises(tool.VerificationError, match='migration'):
        tool.verify_restored_database(conn, backup, receipt, schema='quant')
    receipt.write_text(json.dumps({'status': 'restored', 'database': 'quant_restore_unit',
                                   'archive_sha256': '0'*64}))
    with pytest.raises(tool.VerificationError, match='receipt'):
        tool.verify_restored_database(conn, backup, receipt, schema='quant')


@pytest.mark.parametrize('database', ['quant_terminal', 'quant_test', 'postgres', 'quant_restore_'])
def test_verify_refuses_non_isolated_database_before_reading_files(database, tmp_path):
    tool = capability()
    conn = SimpleNamespace(info=SimpleNamespace(dbname=database))
    with pytest.raises(tool.VerificationError, match='isolated'):
        tool.verify_restored_database(conn, tmp_path, tmp_path / 'receipt.json', schema='quant')


def test_cli_rejects_dsn_arguments_and_uses_environment_only(capsys):
    tool = capability()
    with pytest.raises(SystemExit) as failure:
        tool.main(['capture', '--dsn', 'postgresql://private:secret@localhost/private'])
    assert failure.value.code != 0
    assert 'private:secret' not in capsys.readouterr().out


@pytest.fixture
def database():
    from runtime_config import load_environment
    load_environment()
    dsn = os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test required')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    assert conninfo_to_dict(dsn).get('dbname') == 'quant_test'
    schema = 'restore_fingerprint_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        conn.execute('''CREATE TABLE demo_orders(id bigint PRIMARY KEY,
            amount numeric, payload jsonb, original json, recorded_at timestamptz,
            secret_text text, sequence_id bigint GENERATED ALWAYS AS IDENTITY)''')
        conn.execute('''INSERT INTO demo_orders(id,amount,payload,original,recorded_at,secret_text)
            VALUES(1,12345678901234567890.123456789012345678901,
                   '{"precise":12345678901234567890.123456789012345678901}'::jsonb,
                   '{"b": 2, "a": 1}'::json, '2026-11-16T01:02:03.123456+00', 'secret-never-output')''')
        conn.execute('CREATE TABLE demo_fills(id bigint)')
        try:
            yield conn, schema, dsn
        finally:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_capture_is_readonly_stable_exact_and_contains_no_raw_facts(database):
    conn, schema, _ = database
    tool = capability()
    first = tool.capture_business_manifest(conn, schema)
    assert first == tool.capture_business_manifest(conn, schema)
    assert first['tables']['demo_orders']['row_count'] == 1
    assert first['tables']['demo_fills']['row_count'] == 0
    assert first['sequences']
    serialized = json.dumps(first)
    assert 'secret-never-output' not in serialized
    assert '12345678901234567890' not in serialized
    conn.execute('UPDATE demo_orders SET amount=amount+0.000000000000000000001')
    second = tool.capture_business_manifest(conn, schema)
    assert tool.compare_business_manifests(first, second)['status'] == 'mismatch'


def test_capture_preserves_json_numeric_precision_original_json_and_timestamp(database):
    conn, schema, _ = database
    tool = capability()
    baseline = tool.capture_business_manifest(conn, schema)
    updates = [
        '''UPDATE demo_orders SET payload='{"precise":12345678901234567890.123456789012345678902}'::jsonb''',
        '''UPDATE demo_orders SET original='{"b":2,"a":1}'::json''',
        "UPDATE demo_orders SET recorded_at=recorded_at+interval '1 microsecond'",
    ]
    for statement in updates:
        conn.execute(statement)
        after = tool.capture_business_manifest(conn, schema)
        assert after['tables']['demo_orders']['rows_sha256'] != baseline['tables']['demo_orders']['rows_sha256']
        baseline = after


def test_missing_empty_table_extra_row_and_catalog_changes_are_detected(database):
    conn, schema, _ = database
    tool = capability()
    baseline = tool.capture_business_manifest(conn, schema)
    conn.execute('DROP TABLE demo_fills')
    assert tool.compare_business_manifests(baseline, tool.capture_business_manifest(conn, schema))['status'] == 'mismatch'
    conn.execute('CREATE TABLE demo_fills(id bigint)')
    assert tool.compare_business_manifests(baseline, tool.capture_business_manifest(conn, schema))['status'] == 'matched'
    conn.execute('INSERT INTO demo_fills VALUES(1),(1)')
    after = tool.capture_business_manifest(conn, schema)
    assert after['tables']['demo_fills']['row_count'] == 2
    assert tool.compare_business_manifests(baseline, after)['status'] == 'mismatch'
    conn.execute('ALTER TABLE demo_orders ADD CONSTRAINT valid_amount CHECK(amount>0) NOT VALID')
    with pytest.raises(tool.VerificationError, match='constraint'):
        tool.validate_database_invariants(conn, schema=schema)


def test_capture_rejects_existing_readwrite_transaction_without_committing_it(database):
    conn, schema, _ = database
    tool = capability()
    with conn.transaction():
        conn.execute('INSERT INTO demo_fills VALUES(99)')
        with pytest.raises(tool.VerificationError, match='transaction'):
            tool.capture_business_manifest(conn, schema)
        assert conn.execute('SELECT count(*) FROM demo_fills').fetchone()[0] == 1


def test_backup_and_manifest_share_exported_snapshot_during_concurrent_insert(database, tmp_path, monkeypatch):
    conn, schema, dsn = database
    tool = capability()
    calls = []
    def command(name, arguments, environment, container, *, source=None, output=None):
        calls.append((name, arguments))
        if name != 'pg_dump':
            return
        exported = next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--snapshot='))
        assert re.fullmatch(r'[0-9A-Fa-f-]+', exported)
        conn.execute('INSERT INTO demo_fills VALUES(7)')
        import psycopg
        from psycopg import sql
        with psycopg.connect(dsn) as reader:
            reader.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            reader.execute(sql.SQL('SET TRANSACTION SNAPSHOT {}').format(sql.Literal(exported)))
            assert reader.execute(sql.SQL('SELECT count(*) FROM {}.demo_fills').format(sql.Identifier(schema))).fetchone()[0] == 0
        output.write(b'PGDMP-fixture')
    monkeypatch.setattr(tool.backup, '_run', command)
    destination = tmp_path / 'snapshot-backup'
    result = tool.backup_with_business_manifest(dsn, destination, schema=schema)
    record = json.loads(result.read_text())
    business = json.loads((destination / 'business-manifest.json').read_text())
    assert record['business_manifest']['snapshot_consistent'] is True
    assert business['tables']['demo_fills']['row_count'] == 0
    assert conn.execute('SELECT count(*) FROM demo_fills').fetchone()[0] == 1
    from psycopg.conninfo import conninfo_to_dict
    password = conninfo_to_dict(dsn).get('password')
    assert not password or all(password not in str(arguments) for _, arguments in calls)


def test_non_mvcc_sequence_drift_never_publishes_success_manifest(database, tmp_path, monkeypatch):
    conn, schema, dsn = database
    tool = capability()
    def command(name, arguments, environment, container, *, source=None, output=None):
        if name == 'pg_dump':
            conn.execute("SELECT nextval('demo_orders_sequence_id_seq')")
            output.write(b'PGDMP-fixture')
    monkeypatch.setattr(tool.backup, '_run', command)
    destination = tmp_path / 'drifted-backup'
    with pytest.raises(tool.VerificationError, match='sequence'):
        tool.backup_with_business_manifest(dsn, destination, schema=schema)
    assert not (destination / 'manifest.json').exists()


def test_cli_parse_error_never_echoes_connection_argument(capsys):
    tool = capability()
    with pytest.raises(SystemExit):
        tool.main(['capture', '--output', '/tmp/not-created', '--dsn', 'postgresql://private:secret@localhost/private'])
    captured = capsys.readouterr()
    assert 'private:secret' not in captured.out + captured.err


def test_invariant_check_requires_real_global_migrations_and_detects_history_tamper(database):
    conn, schema, _ = database
    tool = capability()
    with pytest.raises(tool.VerificationError, match='migration history missing'):
        tool.validate_database_invariants(conn, schema=schema)
    conn.execute('DROP TABLE demo_orders,demo_fills')
    import database_migrations
    database_migrations.migrate_schema(conn, schema=schema)
    result = tool.validate_database_invariants(conn, schema=schema)
    assert result['constraints'] == 'passed'
    assert result['migrations'] == 'current'
    conn.execute('DROP TABLE global_schema_migrations')
    with pytest.raises(tool.VerificationError, match='migration history missing'):
        tool.validate_database_invariants(conn, schema=schema)
    result = tool.validate_database_invariants(conn, schema=schema, allow_legacy_baseline=True)
    assert result['migrations'] == 'legacy_baseline_compatible_adoption_pending'
    conn.execute('ALTER TABLE strategy_sim_accounts DISABLE TRIGGER strategy_sim_account_identity')
    with pytest.raises(tool.VerificationError, match='migration compatibility'):
        tool.validate_database_invariants(conn, schema=schema, allow_legacy_baseline=True)


def test_legacy_restore_distinguishes_known_pending_demo_migrations_from_current(database):
    conn, schema, _ = database
    tool = capability()
    conn.execute('DROP TABLE demo_orders,demo_fills')
    import database_migrations
    from demo_store import _demo_migration_bundle
    database_migrations.migrate_schema(conn, schema=schema)
    conn.execute(Path('database/demo_schema.sql').read_text(), prepare=False)
    conn.execute('CREATE TABLE demo_schema_migrations(migration_id text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    identity, (checksum, source) = next(iter(_demo_migration_bundle().items()))
    conn.execute(source, prepare=False)
    conn.execute('INSERT INTO demo_schema_migrations(migration_id,checksum) VALUES(%s,%s)', (identity, checksum))
    with pytest.raises(tool.VerificationError, match='migration compatibility'):
        tool.validate_database_invariants(conn, schema=schema)
    result = tool.validate_database_invariants(conn, schema=schema, allow_legacy_baseline=True)
    assert result['demo_migrations'] == 'known_prefix_pending_explicit_migration'
    conn.execute('UPDATE demo_schema_migrations SET checksum=%s', ('0'*64,))
    with pytest.raises(tool.VerificationError, match='migration compatibility'):
        tool.validate_database_invariants(conn, schema=schema, allow_legacy_baseline=True)


def test_capture_duplicate_multiset_and_dict_rows_are_stable(database):
    conn, schema, dsn = database
    tool = capability()
    conn.execute('INSERT INTO demo_fills VALUES(NULL),(1),(1)')
    first = tool.capture_business_manifest(conn, schema)
    conn.execute('DELETE FROM demo_fills WHERE id=1 AND ctid=(SELECT min(ctid) FROM demo_fills WHERE id=1)')
    after = tool.capture_business_manifest(conn, schema)
    assert first['tables']['demo_fills']['row_count'] == 3
    assert after['tables']['demo_fills']['row_count'] == 2
    assert after['tables']['demo_fills']['rows_sha256'] != first['tables']['demo_fills']['rows_sha256']
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as dictionary_conn:
        assert tool.capture_business_manifest(dictionary_conn, schema) == after


def test_business_manifest_cannot_be_replaced_without_updating_trusted_archive_manifest(tmp_path, monkeypatch):
    tool = capability()
    backup = tmp_path / 'backup'
    backup.mkdir()
    archive = backup / 'database.dump'
    archive.write_bytes(b'PGDMP-fixture')
    business = backup / 'business-manifest.json'
    business.write_text(json.dumps(manifest()))
    archive_hash = tool.backup._hash(archive)
    (backup / 'manifest.json').write_text(json.dumps({
        'schema': 'quant-database-backup-v1', 'status': 'verified',
        'archive': {'file': 'database.dump', 'size_bytes': archive.stat().st_size, 'sha256': archive_hash},
        'business_manifest': {'file': business.name, 'sha256': tool.backup._hash(business), 'snapshot_consistent': True}}))
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'status': 'restored', 'database': 'quant_restore_unit', 'archive_sha256': archive_hash}))
    changed = manifest()
    changed['tables']['demo_orders']['row_count'] = 10
    business.write_text(json.dumps(changed))
    conn = SimpleNamespace(info=SimpleNamespace(dbname='quant_restore_unit'))
    with pytest.raises(tool.VerificationError, match='manifest hash'):
        tool.verify_restored_database(conn, backup, receipt)
    business.unlink()
    business.symlink_to(receipt)
    with pytest.raises(tool.VerificationError, match='manifest invalid'):
        tool.verify_restored_database(conn, backup, receipt)


def test_partition_bounds_and_inheritance_are_part_of_business_fingerprint(database):
    conn, schema, _ = database
    tool = capability()
    conn.execute('CREATE TABLE partitioned_orders(x integer) PARTITION BY RANGE(x)')
    conn.execute('CREATE TABLE partitioned_orders_0 PARTITION OF partitioned_orders FOR VALUES FROM (0) TO (10)')
    before = tool.capture_business_manifest(conn, schema)
    conn.execute('ALTER TABLE partitioned_orders DETACH PARTITION partitioned_orders_0')
    conn.execute('ALTER TABLE partitioned_orders ATTACH PARTITION partitioned_orders_0 FOR VALUES FROM (10) TO (20)')
    after = tool.capture_business_manifest(conn, schema)
    assert before['tables'] == after['tables']
    assert before['catalog_sha256'] != after['catalog_sha256']


def test_domain_and_enum_semantics_are_part_of_business_fingerprint(database):
    conn, schema, _ = database
    tool = capability()
    conn.execute("CREATE TYPE order_phase AS ENUM('new','filled')")
    conn.execute('CREATE DOMAIN positive_units AS numeric CHECK(VALUE>0)')
    conn.execute('CREATE TABLE typed_orders(phase order_phase,units positive_units)')
    before = tool.capture_business_manifest(conn, schema)
    conn.execute("ALTER TYPE order_phase ADD VALUE 'unknown'")
    after_enum = tool.capture_business_manifest(conn, schema)
    assert before['catalog_sha256'] != after_enum['catalog_sha256']
    conn.execute('ALTER DOMAIN positive_units DROP CONSTRAINT positive_units_check')
    conn.execute('ALTER DOMAIN positive_units ADD CONSTRAINT positive_units_check CHECK(VALUE>=0)')
    after_domain = tool.capture_business_manifest(conn, schema)
    assert after_enum['catalog_sha256'] != after_domain['catalog_sha256']


def install_real_demo_prefix(conn, schema, count=None):
    from pathlib import Path
    from psycopg import sql
    import database_migrations
    from demo_store import _demo_migration_bundle
    conn.execute('DROP TABLE demo_orders,demo_fills')
    database_migrations.migrate_schema(conn, schema=schema)
    conn.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    conn.execute(Path('database/demo_schema.sql').read_text(), prepare=False)
    conn.execute('''CREATE TABLE demo_schema_migrations(migration_id text PRIMARY KEY,
        checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    bundle = _demo_migration_bundle()
    for identity, (checksum, source) in list(bundle.items())[:count]:
        conn.execute(source, prepare=False)
        conn.execute('INSERT INTO demo_schema_migrations(migration_id,checksum) VALUES(%s,%s)', (identity, checksum))
    return bundle


def test_history_without_physical_demo_objects_never_claims_migrations_current(database):
    conn, schema, _ = database
    tool = capability()
    conn.execute('DROP TABLE demo_orders,demo_fills')
    import database_migrations
    from demo_store import _demo_migration_bundle
    database_migrations.migrate_schema(conn, schema=schema)
    conn.execute('''CREATE TABLE demo_schema_migrations(migration_id text PRIMARY KEY,
        checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    for identity, (checksum, _) in _demo_migration_bundle().items():
        conn.execute('INSERT INTO demo_schema_migrations(migration_id,checksum) VALUES(%s,%s)', (identity, checksum))
    with pytest.raises(tool.VerificationError, match='Demo physical'):
        tool.validate_database_invariants(conn, schema=schema)


@pytest.mark.parametrize('mutation', [
    'ALTER TABLE demo_risk_events DISABLE TRIGGER demo_risk_events_immutable',
    'DROP TRIGGER demo_risk_events_immutable ON demo_risk_events',
    "CREATE OR REPLACE FUNCTION prevent_demo_risk_event_mutation() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NULL; END'",
    'ALTER TABLE demo_conditional_events DROP CONSTRAINT demo_conditional_events_client_algo_id_fkey',
])
def test_real_demo_current_requires_physical_constraints_and_immutability(database, mutation):
    conn, schema, _ = database
    tool = capability()
    install_real_demo_prefix(conn, schema)
    assert tool.validate_database_invariants(conn, schema=schema)['demo_migrations'] == 'current'
    if 'demo_conditional_events_client_algo_id_fkey' in mutation:
        foreign = conn.execute("SELECT conname FROM pg_constraint WHERE conrelid='demo_conditional_events'::regclass AND contype='f'").fetchone()[0]
        from psycopg import sql
        conn.execute(sql.SQL('ALTER TABLE demo_conditional_events DROP CONSTRAINT {}').format(sql.Identifier(foreign)))
    else:
        conn.execute(mutation)
    with pytest.raises(tool.VerificationError, match='Demo physical'):
        tool.validate_database_invariants(conn, schema=schema)


def test_empty_column_collation_change_is_part_of_business_fingerprint(database):
    conn, schema, _ = database
    tool = capability()
    conn.execute('CREATE TABLE collated_orders(label text COLLATE "C")')
    before = tool.capture_business_manifest(conn, schema)
    conn.execute('ALTER TABLE collated_orders ALTER COLUMN label TYPE text COLLATE "POSIX"')
    after = tool.capture_business_manifest(conn, schema)
    assert before['catalog_sha256'] != after['catalog_sha256']
