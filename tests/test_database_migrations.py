"""显式全局迁移的真实隔离 PostgreSQL 合同；普通检查绝不隐式安装。"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import uuid

import pytest

from runtime_config import load_environment

load_environment()
ROOT = Path(__file__).resolve().parents[1]


def capability():
    assert importlib.util.find_spec('database_migrations') is not None, (
        'explicit global migration capability missing')
    return importlib.import_module('database_migrations')


def test_frozen_migration_bundle_cannot_silently_lose_baseline(tmp_path):
    m = capability()
    with pytest.raises(m.MigrationError, match='bundle'):
        m.migration_bundle(tmp_path)


def test_baseline_bytes_are_pinned_independently():
    bundle = capability().migration_bundle()
    assert bundle[0].migration_id == '0001_global_baseline'
    assert bundle[0].checksum == 'daf64f37478c24274d922efbdeafb181b37cdd1bf1e951c8dd9f2f877c9d5718'
    assert hashlib.sha256(bundle[0].source.encode()).hexdigest() == bundle[0].checksum


@pytest.mark.parametrize('name,source', [
    ('0001_global_baseline.sql', 'SELECT 1'),
    ('0002_gap.sql', 'SELECT 1'),
    ('bad.sql', 'SELECT 1'),
])
def test_invalid_or_damaged_bundle_fails_closed(tmp_path, name, source):
    (tmp_path / name).write_text(source)
    with pytest.raises(capability().MigrationError):
        capability().migration_bundle(tmp_path)


@pytest.fixture
def database():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    dsn = os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test required')
    fields = conninfo_to_dict(dsn)
    assert fields.get('dbname') == 'quant_test', 'only quant_test allowed'
    production = os.getenv('QUANT_DATABASE_URL')
    if production:
        actual = conninfo_to_dict(production)
        keys = ('host', 'port', 'dbname')
        assert tuple(fields.get(k, '') for k in keys) != tuple(actual.get(k, '') for k in keys)
    schema = 'global_migration_' + uuid.uuid4().hex
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        try:
            yield conn, schema
        finally:
            conn.rollback()
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
            conn.commit()


def table_exists(conn, schema, name):
    return conn.execute('SELECT to_regclass(%s) AS name', (schema + '.' + name,)).fetchone()['name']


def baseline(conn, schema):
    from psycopg import sql
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    conn.execute((ROOT / 'database/schema.sql').read_text(), prepare=False)


def test_check_missing_schema_is_readonly_and_creates_nothing(database):
    conn, schema = database
    m = capability()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='missing'):
            m.check_schema(conn, schema=schema)
        assert table_exists(conn, schema, 'store_metadata') is None


def test_explicit_install_is_idempotent_and_check_is_readonly(database):
    conn, schema = database
    m = capability()
    receipt = m.migrate_schema(conn, schema=schema)
    conn.commit()
    assert receipt['applied'] == ['0001_global_baseline']
    assert receipt['adopted'] is False
    assert m.migrate_schema(conn, schema=schema)['applied'] == []
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        report = m.check_schema(conn, schema=schema)
        assert report['status'] == 'compatible'
        assert report['versions'] == ['0001_global_baseline']
    assert table_exists(conn, schema, 'ticks') is not None


@pytest.mark.parametrize('statement', [
    'UPDATE global_schema_migrations SET checksum=checksum',
    'DELETE FROM global_schema_migrations',
    'TRUNCATE global_schema_migrations',
    'TRUNCATE global_schema_migrations RESTART IDENTITY CASCADE',
])
def test_migration_history_cannot_be_rewritten(database, statement):
    import psycopg
    from psycopg import sql
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
            conn.execute(statement)
    conn.rollback()
    assert m.check_schema(conn, schema=schema)['status'] == 'compatible'


def test_existing_unversioned_schema_is_never_silently_adopted(database):
    conn, schema = database
    baseline(conn, schema)
    conn.commit()
    m = capability()
    with pytest.raises(m.MigrationError, match='adopt'):
        m.migrate_schema(conn, schema=schema)
    conn.rollback()
    assert table_exists(conn, schema, 'global_schema_migrations') is None


@pytest.mark.parametrize('backup', [None, '', {'status': 'verified'},
    {'reference': 'backup:1', 'archive_sha256': 'a' * 64, 'business_validation': 'pending'}])
def test_adoption_requires_explicit_verified_restore_evidence(database, backup):
    conn, schema = database
    baseline(conn, schema)
    conn.commit()
    m = capability()
    with pytest.raises(m.MigrationError, match='backup'):
        m.migrate_schema(conn, schema=schema, adopt=True, backup_evidence=backup)
    conn.rollback()
    assert table_exists(conn, schema, 'global_schema_migrations') is None


def backup_evidence():
    return {'reference': 'backup:independent-restore-1', 'archive_sha256': 'a' * 64,
            'business_validation': 'passed'}


def test_adoption_verifies_physical_shape_and_preserves_old_facts(database):
    conn, schema = database
    baseline(conn, schema)
    conn.execute("INSERT INTO documents(key,payload) VALUES('frozen','{\"source\":\"old\"}')")
    conn.commit()
    m = capability()
    report = m.migrate_schema(conn, schema=schema, adopt=True, backup_evidence=backup_evidence())
    conn.commit()
    assert report['adopted'] is True
    assert report['applied'] == ['0001_global_baseline']
    from psycopg import sql
    assert conn.execute(sql.SQL("SELECT payload FROM {}.documents WHERE key='frozen'").format(sql.Identifier(schema))).fetchone()['payload'] == {'source': 'old'}
    assert m.check_schema(conn, schema=schema)['status'] == 'compatible'


@pytest.mark.parametrize('damage', [
    'ALTER TABLE ticks ALTER COLUMN price TYPE DOUBLE PRECISION',
    'ALTER TABLE orderflow_price_candidates DROP COLUMN source_id',
    'DROP INDEX ticks_pair_time_numeric_id',
    'ALTER TABLE research_ledger_events DISABLE TRIGGER research_ledger_events_immutable',
    'DROP TRIGGER strategy_sim_account_identity ON strategy_sim_accounts',
])
def test_adoption_rejects_physical_drift_before_registering(database, damage):
    conn, schema = database
    baseline(conn, schema)
    conn.execute(damage)
    conn.commit()
    m = capability()
    with pytest.raises(m.MigrationError, match='shape'):
        m.migrate_schema(conn, schema=schema, adopt=True, backup_evidence=backup_evidence())
    conn.rollback()
    assert table_exists(conn, schema, 'global_schema_migrations') is None


@pytest.mark.parametrize('damage', [
    'ALTER TABLE ticks ALTER COLUMN price TYPE DOUBLE PRECISION',
    'DROP INDEX ticks_pair_time_numeric_id',
    'ALTER TABLE research_ledger_events DISABLE TRIGGER research_ledger_events_immutable',
    'CREATE OR REPLACE FUNCTION research_ledger_immutable_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$',
])
def test_ordinary_check_detects_catalog_drift_without_repair(database, damage):
    from psycopg import sql
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    conn.execute(damage)
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='shape'):
            m.check_schema(conn, schema=schema)


def test_callback_failure_rolls_back_new_schema_and_history(database):
    conn, schema = database
    m = capability()
    def fail(connection):
        connection.execute("INSERT INTO documents(key,payload) VALUES('partial','{}')")
        raise RuntimeError('injected migration failure')
    with pytest.raises(RuntimeError, match='injected'):
        m.migrate_schema(conn, schema=schema, legacy_import=fail)
    conn.rollback()
    assert table_exists(conn, schema, 'documents') is None
    assert table_exists(conn, schema, 'global_schema_migrations') is None


def test_legacy_import_runs_only_in_explicit_successful_migration(database):
    conn, schema = database
    m = capability()
    def import_old(connection):
        connection.execute("INSERT INTO documents(key,payload) VALUES('legacy-import','{}')")
    m.migrate_schema(conn, schema=schema, legacy_import=import_old)
    conn.commit()
    assert m.migrate_schema(conn, schema=schema, legacy_import=import_old)['applied'] == []
    conn.commit()
    from psycopg import sql
    assert conn.execute(sql.SQL("SELECT count(*) AS n FROM {}.documents WHERE key='legacy-import'").format(sql.Identifier(schema))).fetchone()['n'] == 1


def test_unrelated_extra_demo_tables_do_not_invalidate_global_contract(database):
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    from psycopg import sql
    conn.execute(sql.SQL('CREATE TABLE {}.extra_demo_future(x TEXT)').format(sql.Identifier(schema)))
    conn.commit()
    assert m.check_schema(conn, schema=schema)['status'] == 'compatible'


def test_schema_identifier_is_escaped_and_previous_search_path_preserved(database):
    conn, _ = database
    schema = 'global_test_' + uuid.uuid4().hex + '-quoted'
    m = capability()
    try:
        before = conn.execute('SHOW search_path').fetchone()['search_path']
        m.migrate_schema(conn, schema=schema)
        conn.commit()
        assert conn.execute('SHOW search_path').fetchone()['search_path'] == before
        assert m.check_schema(conn, schema=schema)['status'] == 'compatible'
    finally:
        from psycopg import sql
        conn.rollback()
        conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
        conn.commit()


@pytest.mark.parametrize('damage', [
    'ALTER TABLE global_schema_migrations DISABLE TRIGGER global_schema_migrations_immutable',
    'CREATE OR REPLACE FUNCTION prevent_global_migration_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$',
])
def test_current_check_rejects_history_protection_drift(database, damage):
    from psycopg import sql
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    conn.execute(damage)
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='history shape'):
            m.check_schema(conn, schema=schema)


def test_failed_callback_does_not_leak_schema_even_if_caller_continues(database):
    conn, schema = database
    m = capability()
    conn.execute('SELECT 1')
    def fail(_connection):
        raise RuntimeError('injected')
    with pytest.raises(RuntimeError):
        m.migrate_schema(conn, schema=schema, legacy_import=fail)
    assert table_exists(conn, schema, 'global_schema_migrations') is None
    assert table_exists(conn, schema, 'documents') is None


def test_legacy_table_requires_importer_before_adoption_can_finish(database):
    conn, schema = database
    baseline(conn, schema)
    conn.execute('CREATE TABLE orderflow_surge_observations(event_id TEXT,payload JSONB)')
    conn.commit()
    m = capability()
    with pytest.raises(m.MigrationError, match='legacy'):
        m.migrate_schema(conn, schema=schema, adopt=True, backup_evidence=backup_evidence())
    conn.rollback()
    assert table_exists(conn, schema, 'global_schema_migrations') is None


def test_function_source_literals_are_not_erased_by_schema_normalization():
    m = capability()
    original = "BEGIN IF NEW.execution->>'execution_enabled'='false' THEN RETURN NEW; END IF; END;"
    damaged = original.replace("'execution_enabled'", "'quant.execution_enabled'")
    assert m._normal(original, 'quant') != m._normal(damaged, 'quant')
    assert m._normal("RAISE EXCEPTION 'two  spaces';", 'quant') != m._normal("RAISE EXCEPTION 'two spaces';", 'quant')


@pytest.mark.parametrize('damage', [
    'ALTER TABLE research_ledger_events ENABLE ROW LEVEL SECURITY',
    "CREATE FUNCTION drop_research_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$; CREATE TRIGGER drop_research_insert BEFORE INSERT ON research_ledger_events FOR EACH ROW EXECUTE FUNCTION drop_research_insert()",
])
def test_check_rejects_new_behavior_changing_objects_on_frozen_global_tables(database, damage):
    from psycopg import sql
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    conn.execute(damage, prepare=False)
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='shape'):
            m.check_schema(conn, schema=schema)


def test_unknown_appended_history_is_rejected_in_readonly_transaction(database):
    from psycopg import sql
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    conn.execute(sql.SQL("INSERT INTO {}.global_schema_migrations(migration_id,checksum,shape_sha256,adopted) VALUES('9999_unknown',%s,%s,false)").format(sql.Identifier(schema)), ('a' * 64, 'b' * 64))
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='unknown'):
            m.check_schema(conn, schema=schema)


def test_other_schema_history_cannot_satisfy_target_schema(database):
    conn, schema = database
    m = capability()
    m.migrate_schema(conn, schema=schema)
    conn.commit()
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        with pytest.raises(m.MigrationError, match='missing'):
            m.check_schema(conn, schema=schema + '_missing')


@pytest.mark.parametrize('name', ['', 'a\x00b', '中' * 22, None, True])
def test_invalid_schema_identity_fails_before_database_access(name):
    from unittest.mock import Mock
    m = capability()
    conn = Mock()
    with pytest.raises(m.MigrationError, match='schema identifier'):
        m.migrate_schema(conn, schema=name)
    conn.execute.assert_not_called()
