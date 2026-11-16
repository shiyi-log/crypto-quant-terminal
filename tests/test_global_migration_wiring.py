"""Global schema writes require an explicit operator command, never a read."""

from contextlib import contextmanager
import json
import os
from unittest.mock import MagicMock, patch
import uuid

import pytest

from data_store import DataStore
import database_migrations
import manage


class Connection:
    def __init__(self):
        self.statements = []
        self.transactions = 0
        self.closed = False

    def execute(self, statement, params=None, **kwargs):
        self.statements.append(statement.as_string() if hasattr(statement, 'as_string') else statement)
        return MagicMock()

    def commit(self):
        pass

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class Pool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.conn = Connection()
        self.closed = False

    def open(self, **kwargs):
        self.kwargs['configure'](self.conn)

    @contextmanager
    def connection(self):
        yield self.conn

    def close(self):
        self.closed = True


def test_initialize_checks_schema_without_ddl_advisory_lock_or_legacy_import():
    store = DataStore(dsn='postgresql://invalid/offline', schema='test schema')
    pools = []

    def pool_factory(**kwargs):
        pool = Pool(**kwargs)
        pools.append(pool)
        return pool

    def readonly_check(conn, *, schema):
        assert schema == 'test schema'
        conn.execute('SELECT compatible_schema')
        return {'status': 'compatible'}

    with patch('psycopg_pool.ConnectionPool', side_effect=pool_factory), \
            patch('database_migrations.check_schema', side_effect=readonly_check) as check, \
            patch.object(store, '_migrate_orderflow_surges',
                         side_effect=AssertionError('implicit business import')):
        assert store.initialize() is store
        assert store.initialize() is store
    assert len(pools) == 1
    assert check.call_count == 1
    assert not pools[0].closed
    statements = pools[0].conn.statements
    assert 'SELECT compatible_schema' in statements
    assert any('"test schema"' in text for text in statements)
    assert all(text.startswith(('SET ', 'SELECT ')) for text in statements)
    assert not any('advisory' in text.lower() for text in statements)


@pytest.mark.parametrize('error', [
    database_migrations.MigrationError('global migrations missing; explicit migrate required'),
    database_migrations.MigrationError('global migrations pending; explicit migrate required'),
    database_migrations.MigrationError('global baseline shape mismatch: columns'),
    RuntimeError('postgresql://user:do-not-leak@host/private'),
])
def test_failed_initialize_closes_pool_and_hides_backend_details(error):
    store = DataStore(dsn='postgresql://user:do-not-leak@host/private')
    pool = Pool(configure=lambda conn: None)
    with patch('psycopg_pool.ConnectionPool', return_value=pool), \
            patch('database_migrations.check_schema', side_effect=error):
        with pytest.raises(RuntimeError) as result:
            store.initialize()
    assert pool.closed
    assert store._pool is None
    assert 'do-not-leak' not in str(result.value)
    assert result.value.__suppress_context__


def test_failed_pool_open_is_sanitized_and_pool_closed():
    store = DataStore(dsn='postgresql://user:do-not-leak@host/private')
    pool = Pool(configure=lambda conn: None)
    with patch('psycopg_pool.ConnectionPool', return_value=pool), \
            patch.object(pool, 'open', side_effect=RuntimeError('do-not-leak')):
        with pytest.raises(RuntimeError) as result:
            store.initialize()
    assert pool.closed
    assert store._pool is None
    assert 'do-not-leak' not in str(result.value)


def test_pool_constructor_failure_cannot_leak_connection_details():
    store = DataStore(dsn='postgresql://user:do-not-leak@host/private')
    with patch('psycopg_pool.ConnectionPool', side_effect=RuntimeError('do-not-leak')):
        with pytest.raises(RuntimeError) as result:
            store.initialize()
    assert store._pool is None
    assert 'do-not-leak' not in str(result.value)
    assert result.value.__suppress_context__


@pytest.mark.parametrize('operation', ['migrate', 'check'])
def test_direct_operations_do_not_return_receipt_when_commit_fails(operation):
    store = DataStore(dsn='postgresql://user:do-not-leak@host/private')
    conn = Connection()

    @contextmanager
    def failed_commit():
        yield conn
        raise RuntimeError('do-not-leak')

    with patch('psycopg.connect', return_value=conn), \
            patch.object(conn, 'transaction', side_effect=failed_commit), \
            patch('database_migrations.check_schema', return_value={'status': 'compatible'}), \
            patch('database_migrations.migrate_schema', return_value={'status': 'migrated'}):
        with pytest.raises(RuntimeError) as result:
            getattr(store, operation + '_schema')()
    assert 'do-not-leak' not in str(result.value)
    assert conn.closed


def test_explicit_migrate_uses_direct_transaction_and_supplies_legacy_import():
    store = DataStore(dsn='postgresql://invalid/offline', schema='test schema')
    conn = Connection()
    evidence = {'reference': 'verified-receipt', 'archive_sha256': 'a' * 64,
                'business_validation': 'passed'}

    def migrate(connection, *, schema, adopt, backup_evidence, legacy_import):
        assert connection is conn and conn.transactions == 1
        assert schema == 'test schema' and adopt is True and backup_evidence == evidence
        assert legacy_import.__self__ is store
        assert store._sql.Identifier('legacy').as_string() == '"legacy"'
        assert store._json({'imported': 0}) is not None
        return {'status': 'migrated', 'adopted': True, 'applied': ['0001_global_baseline']}

    with patch('psycopg.connect', return_value=conn) as connect, \
            patch('database_migrations.migrate_schema', side_effect=migrate), \
            patch.object(store, 'initialize', side_effect=AssertionError('bootstrap recursion')):
        result = store.migrate_schema(adopt=True, backup_evidence=evidence)
    assert result['adopted'] is True
    assert connect.call_args.args == ('postgresql://invalid/offline',)
    assert conn.closed and store._pool is None


def test_explicit_check_is_readonly_direct_connection_and_never_bootstraps():
    store = DataStore(dsn='postgresql://invalid/offline', schema='test schema')
    conn = Connection()
    with patch('psycopg.connect', return_value=conn), \
            patch.object(store, 'initialize', side_effect=AssertionError('implicit pool')), \
            patch('database_migrations.migrate_schema', side_effect=AssertionError('implicit migration')), \
            patch('database_migrations.check_schema', return_value={'status': 'compatible'}) as check:
        assert store.check_schema() == {'status': 'compatible'}
    check.assert_called_once_with(conn, schema='test schema')
    assert conn.statements[0] == 'SET TRANSACTION READ ONLY'
    assert all(text.startswith('SET ') for text in conn.statements)
    assert conn.closed and store._pool is None


@pytest.mark.parametrize('operation', ['migrate', 'check'])
def test_direct_database_operations_sanitize_connection_errors(operation):
    store = DataStore(dsn='postgresql://user:do-not-leak@host/private')
    with patch('psycopg.connect', side_effect=RuntimeError('do-not-leak')):
        with pytest.raises(RuntimeError) as result:
            getattr(store, operation + '_schema')()
    assert 'do-not-leak' not in str(result.value)
    assert result.value.__suppress_context__


@pytest.mark.parametrize('operation,method', [('check', 'check_schema'), ('migrate', 'migrate_schema')])
def test_manage_explicit_commands_never_change_service_lifecycle(operation, method, capsys):
    store = MagicMock()
    getattr(store, method).return_value = {'status': 'compatible' if operation == 'check' else 'migrated'}
    with patch('manage.load_environment'), patch('data_store.DataStore', return_value=store), \
            patch('manage.postgres_start', side_effect=AssertionError('database start')), \
            patch('manage.postgres_initialize', side_effect=AssertionError('database init')), \
            patch('manage.run_lifecycle', side_effect=AssertionError('application restart')):
        assert manage.main(['db', operation]) == 0
    assert json.loads(capsys.readouterr().out)['status'] in {'compatible', 'migrated'}
    if operation == 'migrate':
        store.migrate_schema.assert_called_once_with(adopt=False, backup_evidence=None)
    else:
        store.check_schema.assert_called_once_with()
    store.close.assert_called_once_with()


@pytest.mark.parametrize('document', [None, {}, {'business_validation': 'pending'},
    [], 'passed',
    {'business_validation': True, 'archive_sha256': 'a' * 64, 'reference': 'receipt'},
    {'business_validation': 'passed', 'archive_sha256': 'bad', 'reference': 'receipt'},
    {'business_validation': 'passed', 'archive_sha256': 'a' * 64},
])
def test_adopt_rejects_missing_unverified_or_incomplete_receipt_before_database(tmp_path, document):
    args = ['db', 'migrate', '--adopt']
    if document is not None:
        receipt = tmp_path / 'receipt.json'
        receipt.write_text(json.dumps(document))
        args += ['--backup-receipt', str(receipt)]
    with patch('manage.load_environment'), \
            patch('data_store.DataStore', side_effect=AssertionError('unverified database adoption')):
        assert manage.main(args) == 1


def test_adopt_reads_local_verified_receipt_and_normalizes_audit_evidence(tmp_path):
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'reference': 'drill/verified-20261116',
        'archive_sha256': 'a' * 64, 'business_validation': 'passed', 'unrelated': 'not forwarded'}))
    store = MagicMock()
    store.migrate_schema.return_value = {'status': 'migrated', 'adopted': True}
    with patch('manage.load_environment'), patch('data_store.DataStore', return_value=store):
        assert manage.main(['db', 'migrate', '--adopt', '--backup-receipt', str(receipt)]) == 0
    store.migrate_schema.assert_called_once_with(adopt=True, backup_evidence={
        'reference': 'drill/verified-20261116', 'archive_sha256': 'a' * 64,
        'business_validation': 'passed'})


@pytest.mark.parametrize('content', ['{invalid', '{"archive_sha256": "a"}', ' ' * 1_048_577])
def test_adopt_rejects_malformed_or_oversized_receipt_without_file_contents_in_error(tmp_path, content, capsys):
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(content)
    with patch('manage.load_environment'), \
            patch('data_store.DataStore', side_effect=AssertionError('invalid receipt')):
        assert manage.main(['db', 'migrate', '--adopt', '--backup-receipt', str(receipt)]) == 1
    assert content not in capsys.readouterr().err


@pytest.mark.parametrize('args', [
    ['db', 'check', '--adopt'], ['db', 'init', '--adopt'],
    ['db', 'migrate', '--backup-receipt', 'untrusted.json'],
])
def test_adoption_flags_cannot_be_silently_ignored_on_other_commands(args):
    with patch('manage.load_environment'), \
            patch('data_store.DataStore', side_effect=AssertionError('invalid operation')), \
            patch('manage.postgres_initialize', side_effect=AssertionError('invalid operation')):
        assert manage.main(args) == 1


def test_database_init_remains_cluster_only():
    with patch('manage.load_environment'), patch('manage.postgres_initialize', return_value=0) as init, \
            patch('data_store.DataStore', side_effect=AssertionError('implicit schema migration')):
        assert manage.main(['db', 'init']) == 0
    init.assert_called_once_with()


def test_database_status_missing_schema_fails_without_installing_anything(capsys):
    store = DataStore(dsn='postgresql://invalid/offline')
    pool = Pool(configure=lambda conn: None)
    with patch('manage.load_environment'), patch('manage.uses_local_postgres', return_value=False), \
            patch('data_store.DataStore', return_value=store), \
            patch('psycopg_pool.ConnectionPool', return_value=pool), \
            patch('database_migrations.check_schema', side_effect=database_migrations.MigrationError(
                'global migrations missing; explicit migrate required')), \
            patch('database_migrations.migrate_schema', side_effect=AssertionError('status migrated')):
        assert manage.main(['db', 'status']) == 1
    assert pool.closed and store._pool is None
    assert all(text.startswith(('SET ', 'SELECT ')) for text in pool.conn.statements)
    assert not capsys.readouterr().out


@pytest.fixture
def isolated_postgres_store():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from runtime_config import load_environment

    load_environment()
    dsn = os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test required')
    config = conninfo_to_dict(dsn)
    assert config.get('dbname') == 'quant_test', 'only quant_test allowed'
    production = os.getenv('QUANT_DATABASE_URL')
    if production:
        current = conninfo_to_dict(production)
        keys = ('host', 'port', 'dbname')
        assert tuple(config.get(key, '') for key in keys) != tuple(current.get(key, '') for key in keys)
    schema = 'migration_wiring_' + uuid.uuid4().hex
    store = DataStore(dsn=dsn, schema=schema)
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def test_real_postgres_missing_check_creates_nothing_then_explicit_migration_opens_business_pool(isolated_postgres_store):
    import psycopg

    store = isolated_postgres_store
    with pytest.raises(RuntimeError):
        store.check_schema()
    with pytest.raises(RuntimeError):
        store.initialize()
    assert store._pool is None
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute('SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)',
                            (store.schema,)).fetchone() == (False,)
    receipt = store.migrate_schema()
    assert receipt['status'] == 'migrated' and receipt['adopted'] is False
    assert receipt['applied'] == ['0001_global_baseline']
    assert store.check_schema()['status'] == 'compatible'
    assert store.initialize() is store
    assert store.stats()['counts']['documents'] == 0
    with store._connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM store_metadata WHERE key='orderflow_surge_migration_v3'").fetchone()['n'] == 1
    assert store.migrate_schema()['applied'] == []
