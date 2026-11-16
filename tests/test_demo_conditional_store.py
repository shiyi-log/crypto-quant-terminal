"""原生条件单持久化合同；数据库测试仅允许独立 quant_test 随机 schema。"""
from concurrent.futures import ThreadPoolExecutor
import importlib
import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from data_store import DataStore
from runtime_config import load_environment

load_environment()
CLIENT = 'qd-' + 'a' * 32
PARENT = 'qd-' + 'b' * 32
BTC = 'BTC/USDT:USDT'


def store_class():
    assert importlib.util.find_spec('demo_conditional_store') is not None, (
        'missing native conditional ledger implementation')
    return importlib.import_module('demo_conditional_store').DemoConditionalStore


def intent(**changes):
    return {
        'strategy_id': 'frozen-strategy', 'run_id': 'round-1',
        'entry_decision_id': 'decision-1', 'parent_entry_client_id': PARENT,
        'pair': BTC, 'side': 'sell', 'order_type': 'STOP_MARKET',
        'quantity': '0.002', 'trigger_price': '59000.10',
        'working_type': 'MARK_PRICE', 'price_protect': False, 'reduce_only': True,
        'client_algo_id': CLIENT, 'revision': 1, 'created_ms': 1000,
        'supersedes_client_algo_id': None, **changes,
    }


def observation(**changes):
    return {
        'algo_id': '7001', 'client_algo_id': CLIENT, 'algo_status': 'NEW',
        'algo_type': 'CONDITIONAL',
        'pair': BTC, 'side': 'sell', 'order_type': 'STOP_MARKET',
        'position_side': 'BOTH', 'quantity': '0.002', 'trigger_price': '59000.10',
        'working_type': 'MARK_PRICE', 'price_protect': False, 'reduce_only': True,
        'close_position': False, 'created_ms': 1001, 'updated_ms': 1002,
        'trigger_ms': 0, 'actual_order_id': None, 'execution_complete': False,
        'raw': {'algoId': 7001, 'algoStatus': 'NEW'}, **changes,
    }


@pytest.mark.parametrize('changes', [
    {'environment': 'live'}, {'endpoint_host': 'fapi.binance.com'},
    {'pair': 'SOL/USDT:USDT'}, {'reduce_only': False}, {'reduce_only': 'true'},
    {'quantity': True}, {'quantity': '0'}, {'quantity': '-1'}, {'quantity': 'NaN'},
    {'trigger_price': 'Infinity'}, {'trigger_price': '0'}, {'side': 'SELL'},
    {'position_side': 'LONG'}, {'working_type': 'LAST_PRICE'},
    {'price_protect': 'false'}, {'order_type': 'LIMIT'},
    {'client_algo_id': 'random'}, {'revision': True}, {'revision': 0},
    {'strategy_id': ''}, {'run_id': ''}, {'entry_decision_id': ''},
    {'parent_entry_client_id': 'foreign'}, {'created_ms': True},
    {'created_ms': -1}, {'created_ms': 1.5}, {'close_position': True},
    {'quantity': '1E+1000'}, {'quantity': '1E-1000'},
])
def test_invalid_frozen_conditional_intent_never_connects(changes):
    data = Mock()
    ledger = store_class()(data, 'offline-test')
    with pytest.raises(ValueError):
        ledger.reserve_conditional(intent(**changes))
    data._connection.assert_not_called()


@pytest.fixture(scope='module')
def isolated_data():
    dsn = os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test database required')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    config = conninfo_to_dict(dsn)
    assert config.get('dbname') == 'quant_test', 'only independent quant_test allowed'
    production = os.getenv('QUANT_DATABASE_URL')
    if production:
        other = conninfo_to_dict(production)
        fields = ('host', 'port', 'dbname')
        assert tuple(config.get(k, '') for k in fields) != tuple(other.get(k, '') for k in fields)
    schema = 'conditional_test_' + uuid.uuid4().hex
    data = DataStore(dsn=dsn, schema=schema, max_connections=6)
    try:
        data.migrate_schema()
        store_class()(data, 'setup').ensure_schema()
        yield data
    finally:
        data.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def ledger(isolated_data):
    return store_class()(isolated_data, 'conditional-' + uuid.uuid4().hex)


def test_exact_reserve_retry_is_durable_but_changed_facts_conflict(ledger):
    first, created = ledger.reserve_conditional(intent())
    assert created is True
    assert first['status'] == 'intent'
    assert first['submission_attempts'] == 0
    assert len(first['intent_hash']) == 64
    assert first['quantity'] == '0.002'
    restarted = store_class()(ledger.store, ledger.virtual_account_key)
    same, created = restarted.reserve_conditional(intent())
    assert created is False
    assert same == first
    for change in ({'quantity': '0.003'}, {'run_id': 'round-2'},
                   {'trigger_price': '58000.1'}, {'revision': 2}):
        with pytest.raises(ValueError, match='frozen intent'):
            restarted.reserve_conditional(intent(**change))
    assert len(ledger.list_conditionals()) == 1
    assert len(ledger.conditional_events(CLIENT)) == 1


def test_concurrent_reserve_and_claim_have_one_persisted_winner(ledger):
    def reserve(_):
        return ledger.reserve_conditional(intent())[1]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1
    def claim(_):
        fresh = store_class()(ledger.store, ledger.virtual_account_key)
        return fresh.claim_conditional_submission(CLIENT, claimed_ms=1001)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    row = ledger.get_conditional(CLIENT)
    assert row['status'] == 'submitting'
    assert row['submission_attempts'] == 1
    assert len(ledger.conditional_events(CLIENT)) == 2


def test_unknown_absent_restart_cannot_reclaim_or_assume_canceled(ledger):
    ledger.reserve_conditional(intent())
    assert ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.mark_conditional_unknown(CLIENT, 'RequestTimeout', observed_ms=1003)
    row = ledger.record_conditional_observation(CLIENT, None, observed_ms=2000)
    assert row['status'] == 'unknown'
    assert row['algo_id'] is None
    assert row['execution_complete'] is False
    restarted = store_class()(ledger.store, ledger.virtual_account_key)
    assert restarted.claim_conditional_submission(CLIENT, claimed_ms=999999) is False
    assert restarted.get_conditional(CLIENT)['submission_attempts'] == 1


@pytest.mark.parametrize('changes', [
    {'client_algo_id': 'qd-' + 'c' * 32}, {'pair': 'ETH/USDT:USDT'},
    {'quantity': '0.001'}, {'trigger_price': '59000.2'}, {'side': 'buy'},
    {'order_type': 'TAKE_PROFIT_MARKET'}, {'position_side': 'LONG'},
    {'reduce_only': False}, {'close_position': True},
    {'working_type': 'CONTRACT_PRICE'}, {'price_protect': True},
    {'algo_id': None}, {'algo_id': '0'}, {'algo_type': 'WRONG'},
    {'algo_status': 'FILLED'}, {'updated_ms': True},
])
def test_observation_mismatch_cannot_certify_protection(ledger, changes):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    with pytest.raises(ValueError):
        ledger.record_conditional_observation(CLIENT, observation(**changes), observed_ms=1010)
    row = ledger.get_conditional(CLIENT)
    assert row['status'] == 'submitting'
    assert row['algo_id'] is None
    assert len(ledger.conditional_events(CLIENT)) == 2


def test_unknown_fact_on_never_claimed_intent_is_rejected(ledger):
    ledger.reserve_conditional(intent())
    with pytest.raises(ValueError, match='claim'):
        ledger.mark_conditional_unknown(CLIENT, 'RequestTimeout', observed_ms=1001)
    assert ledger.get_conditional(CLIENT)['status'] == 'intent'


def test_identical_event_retry_does_not_duplicate_and_changed_event_conflicts(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    fact = observation(event_id='exchange-fact-1')
    row = ledger.record_conditional_observation(CLIENT, fact, observed_ms=1010)
    assert row['status'] == 'confirmed'
    assert ledger.record_conditional_observation(CLIENT, fact, observed_ms=1010) == row
    assert len(ledger.conditional_events(CLIENT)) == 3
    with pytest.raises(ValueError, match='different event'):
        ledger.record_conditional_observation(CLIENT, {**fact, 'updated_ms': 1003}, observed_ms=1010)
    assert len(ledger.conditional_events(CLIENT)) == 3


def test_late_and_same_time_conflicting_facts_cannot_restore_old_protection(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(CLIENT, observation(), observed_ms=1010)
    closed = observation(algo_status='CANCELED', updated_ms=1004)
    ledger.record_conditional_observation(CLIENT, closed, observed_ms=1020)
    late = ledger.record_conditional_observation(CLIENT, observation(updated_ms=1003), observed_ms=1030)
    assert late['algo_status'] == 'CANCELED'
    assert late['last_exchange_observed_ms'] == 1020
    assert len(ledger.conditional_events(CLIENT)) == 5
    conflict = ledger.record_conditional_observation(CLIENT, observation(updated_ms=1004), observed_ms=1040)
    assert conflict['status'] == 'reconciliation_conflict'
    assert conflict['algo_status'] == 'CANCELED'


@pytest.mark.parametrize('status', ['TRIGGERING', 'TRIGGERED', 'FINISHED'])
def test_trigger_or_finished_never_proves_execution_or_releases_claim(ledger, status):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    row = ledger.record_conditional_observation(
        CLIENT, observation(algo_status=status, actual_order_id='8001'), observed_ms=1010)
    assert row['algo_status'] == status
    assert row['actual_order_id'] == '8001'
    assert row['execution_complete'] is False
    assert row['submission_attempts'] == 1
    assert not ledger.claim_conditional_submission(CLIENT, claimed_ms=1020)


def test_cancel_ack_does_not_erase_confirmed_new_fact(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(CLIENT, observation(), observed_ms=1010)
    row = ledger.record_conditional_observation(CLIENT, {
        **observation(), 'cancel_acknowledged': True, 'cancellation_confirmed': False,
        'cancel_raw': {'algoId': 7001, 'clientAlgoId': CLIENT, 'code': 200},
    }, observed_ms=1020)
    assert row['algo_status'] == 'NEW'
    assert row['cancellation_confirmed'] is False
    assert row['execution_complete'] is False
    assert ledger.conditional_events(CLIENT)[-1]['event_type'] == 'cancel_ack'


def test_replay_recovers_projection_without_resending_after_local_corruption(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(
        CLIENT, observation(algo_status='TRIGGERED', actual_order_id='8001'), observed_ms=1010)
    before = ledger.get_conditional(CLIENT)
    with ledger.store._connection() as conn:
        conn.execute('UPDATE demo_conditional_current SET status=%s,submission_attempts=0,algo_id=NULL '
                     'WHERE environment=%s AND virtual_account_key=%s AND client_algo_id=%s',
                     ('intent', *ledger._identity(), CLIENT))
    assert ledger.rebuild_conditional_projection(CLIENT) == before
    assert ledger.claim_conditional_submission(CLIENT, claimed_ms=1030) is False


def test_immutable_intents_and_events_refuse_update_delete_truncate(ledger):
    import psycopg
    ledger.reserve_conditional(intent())
    for table in ('demo_conditional_intents', 'demo_conditional_events'):
        for mutation in (f'UPDATE {table} SET client_algo_id=client_algo_id',
                         f'DELETE FROM {table}', f'TRUNCATE {table} CASCADE'):
            with pytest.raises(psycopg.Error):
                with ledger.store._connection() as conn:
                    conn.execute(mutation)
    assert len(ledger.conditional_events(CLIENT)) == 1
    with ledger.store._connection() as conn:
        rows = conn.execute(
            'SELECT tgname,tgenabled FROM pg_trigger WHERE tgname IN '
            "('demo_conditional_intents_immutable','demo_conditional_events_immutable')").fetchall()
    assert len(rows) == 2
    assert all(row['tgenabled'] == 'A' for row in rows)


def test_account_namespaces_cannot_observe_or_claim_each_others_orders(ledger):
    ledger.reserve_conditional(intent())
    foreign = store_class()(ledger.store, 'another-' + uuid.uuid4().hex)
    assert foreign.get_conditional(CLIENT) is None
    assert foreign.list_conditionals() == []
    assert foreign.claim_conditional_submission(CLIENT, claimed_ms=1001) is False
    with pytest.raises(ValueError, match='unknown conditional'):
        foreign.record_conditional_observation(CLIENT, observation(), observed_ms=1010)


def test_sensitive_raw_fields_are_excluded_from_persistent_audit(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    value = observation(raw={'algoId': 7001, 'algoStatus': 'NEW',
                             'apiKey': 'SYNTHETIC_KEY', 'secret': 'SYNTHETIC_SECRET',
                             'headers': {'Authorization': 'SYNTHETIC_AUTH'},
                             'unexpected': 'SYNTHETIC_UNTRUSTED'})
    ledger.record_conditional_observation(CLIENT, value, observed_ms=1010)
    text = str(ledger.conditional_events(CLIENT))
    assert 'SYNTHETIC_' not in text
    assert 'algoId' in text


def test_schema_check_is_readonly_and_rejects_pending_native_migration(ledger):
    from contextlib import contextmanager
    @contextmanager
    def readonly():
        with ledger.store._connection() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            yield conn
    reading = store_class()(SimpleNamespace(_connection=readonly), ledger.virtual_account_key)
    assert reading.check_schema() is True
    with ledger.store._connection() as conn:
        rows = conn.execute('DELETE FROM demo_schema_migrations WHERE migration_id>=%s RETURNING *',
                            ('0003_native_conditional_orders',)).fetchall()
    try:
        with pytest.raises(RuntimeError, match='pending'):
            reading.check_schema()
    finally:
        with ledger.store._connection() as conn:
            for row in rows:
                conn.execute('INSERT INTO demo_schema_migrations(migration_id,checksum,applied_at) VALUES(%s,%s,%s)',
                             (row['migration_id'], row['checksum'], row['applied_at']))


def test_decimal_intent_never_rounds_at_python_context_or_database_scale(ledger):
    amount = '0.00200000000000000000000000000000000001'
    row, _ = ledger.reserve_conditional(intent(quantity=amount))
    assert row['quantity'] == amount
    with ledger.store._connection() as conn:
        value = conn.execute('SELECT quantity::text AS value FROM demo_conditional_intents WHERE '
                             'environment=%s AND virtual_account_key=%s AND client_algo_id=%s',
                             (*ledger._identity(), CLIENT)).fetchone()['value']
    assert value == amount


@pytest.mark.parametrize('changes', [
    {'algoId': 7002}, {'clientAlgoId': 'external'}, {'code': True}, {'code': 500},
])
def test_cancel_ack_requires_receipt_identity_and_success_code(ledger, changes):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(CLIENT, observation(), observed_ms=1010)
    with pytest.raises(ValueError, match='cancel'):
        ledger.record_conditional_observation(CLIENT, {
            **observation(), 'cancel_acknowledged': True, 'cancellation_confirmed': False,
            'cancel_raw': {'algoId': 7001, 'clientAlgoId': CLIENT, 'code': 200, **changes},
        }, observed_ms=1020)
    assert len(ledger.conditional_events(CLIENT)) == 3


def test_known_child_identity_is_preserved_and_changed_child_blocks_reconciliation(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(
        CLIENT, observation(algo_status='TRIGGERED', actual_order_id='8001'), observed_ms=1010)
    row = ledger.record_conditional_observation(
        CLIENT, observation(algo_status='FINISHED', actual_order_id=None, updated_ms=1003), observed_ms=1020)
    assert row['actual_order_id'] == '8001'
    row = ledger.record_conditional_observation(
        CLIENT, observation(algo_status='FINISHED', actual_order_id='8002', updated_ms=1004), observed_ms=1030)
    assert row['actual_order_id'] == '8001'
    assert row['status'] == 'reconciliation_conflict'
    assert row['execution_complete'] is False


def test_superseded_revision_must_belong_to_same_round_parent_and_semantics(ledger):
    ledger.reserve_conditional(intent())
    next_id = 'qd-' + 'c' * 32
    for change in ({'run_id': 'other'}, {'side': 'buy'}, {'order_type': 'TAKE_PROFIT_MARKET'},
                   {'parent_entry_client_id': 'qd-' + 'd' * 32}, {'revision': 1}):
        with pytest.raises(ValueError, match='superseded'):
            ledger.reserve_conditional(intent(**{
                'client_algo_id': next_id, 'revision': 2,
                'supersedes_client_algo_id': CLIENT, **change}))
    row, created = ledger.reserve_conditional(intent(
        client_algo_id=next_id, revision=2, supersedes_client_algo_id=CLIENT))
    assert created
    assert row['status'] == 'intent'
    assert row['supersedes_client_algo_id'] == CLIENT


def test_projection_write_failure_rolls_back_intent_event_and_claim(ledger):
    import psycopg
    with ledger.store._connection() as conn:
        conn.execute("CREATE FUNCTION conditional_test_reject_projection() RETURNS trigger LANGUAGE plpgsql "
                     "AS $$ BEGIN RAISE EXCEPTION 'injected projection failure'; END; $$")
        conn.execute('CREATE TRIGGER conditional_test_projection BEFORE INSERT OR UPDATE ON '
                     'demo_conditional_current FOR EACH STATEMENT EXECUTE FUNCTION conditional_test_reject_projection()')
    try:
        with pytest.raises(psycopg.Error):
            ledger.reserve_conditional(intent())
    finally:
        with ledger.store._connection() as conn:
            conn.execute('DROP TRIGGER conditional_test_projection ON demo_conditional_current')
            conn.execute('DROP FUNCTION conditional_test_reject_projection()')
    assert ledger.get_conditional(CLIENT) is None
    assert ledger.conditional_events(CLIENT) == []


def test_triggered_cannot_regress_to_triggering_even_at_later_fact_time(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(
        CLIENT, observation(algo_status='TRIGGERED', actual_order_id='8001'), observed_ms=1010)
    row = ledger.record_conditional_observation(
        CLIENT, observation(algo_status='TRIGGERING', updated_ms=1003), observed_ms=1020)
    assert row['algo_status'] == 'TRIGGERED'
    assert row['status'] == 'reconciliation_conflict'
    assert row['actual_order_id'] == '8001'


def test_claim_projection_failure_rolls_back_claim_event_and_allows_only_later_first_attempt(ledger):
    import psycopg
    ledger.reserve_conditional(intent())
    with ledger.store._connection() as conn:
        conn.execute("CREATE FUNCTION conditional_test_fail_claim() RETURNS trigger LANGUAGE plpgsql "
                     "AS $$ BEGIN RAISE EXCEPTION 'injected claim projection failure'; END; $$")
        conn.execute('CREATE TRIGGER conditional_test_claim BEFORE INSERT OR UPDATE ON '
                     'demo_conditional_current FOR EACH STATEMENT EXECUTE FUNCTION conditional_test_fail_claim()')
    try:
        with pytest.raises(psycopg.Error):
            ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    finally:
        with ledger.store._connection() as conn:
            conn.execute('DROP TRIGGER conditional_test_claim ON demo_conditional_current')
            conn.execute('DROP FUNCTION conditional_test_fail_claim()')
    assert len(ledger.conditional_events(CLIENT)) == 1
    assert ledger.get_conditional(CLIENT)['submission_attempts'] == 0
    assert ledger.claim_conditional_submission(CLIENT, claimed_ms=1002)
    assert not ledger.claim_conditional_submission(CLIENT, claimed_ms=1003)


def test_corrupt_projection_never_authorizes_second_claim_even_without_manual_rebuild(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    with ledger.store._connection() as conn:
        conn.execute('UPDATE demo_conditional_current SET status=%s,submission_attempts=0 '
                     'WHERE environment=%s AND virtual_account_key=%s AND client_algo_id=%s',
                     ('intent', *ledger._identity(), CLIENT))
    restarted = store_class()(ledger.store, ledger.virtual_account_key)
    assert restarted.claim_conditional_submission(CLIENT, claimed_ms=2000) is False
    assert restarted.get_conditional(CLIENT)['submission_attempts'] == 1


def test_decimal_equivalent_retry_reuses_exact_frozen_intent(ledger):
    first, _ = ledger.reserve_conditional(intent())
    retry, created = ledger.reserve_conditional(intent(quantity='0.002000', trigger_price='59000.1000'))
    assert created is False
    assert retry['intent_hash'] == first['intent_hash']
    assert len(ledger.conditional_events(CLIENT)) == 1


def test_exact_string_cancel_success_is_compatible_and_remains_pending(ledger):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    ledger.record_conditional_observation(CLIENT, observation(), observed_ms=1010)
    row = ledger.record_conditional_observation(CLIENT, {
        **observation(), 'cancel_acknowledged': True, 'cancellation_confirmed': False,
        'cancel_raw': {'algoId': 7001, 'clientAlgoId': CLIENT, 'code': '200'},
    }, observed_ms=1020)
    assert row['status'] == 'cancel_pending'
    assert row['cancel_pending'] is True
    assert row['last_cancel_ack_ms'] == 1020
    assert row['last_exchange_observed_ms'] == 1010
    assert row['last_observed_ms'] == 1020
    row = ledger.record_conditional_observation(
        CLIENT, observation(algo_status='CANCELED', updated_ms=1021), observed_ms=1030)
    assert row['cancel_pending'] is False
    assert row['cancellation_confirmed'] is True


@pytest.mark.parametrize('fact,observed', [
    (observation(updated_ms=999999999), 1010), (observation(), 1000),
    (observation(created_ms=1003, updated_ms=1002), 1010),
])
def test_future_or_preclaim_observations_cannot_poison_latest_fact(ledger, fact, observed):
    ledger.reserve_conditional(intent())
    ledger.claim_conditional_submission(CLIENT, claimed_ms=1001)
    with pytest.raises(ValueError, match='time'):
        ledger.record_conditional_observation(CLIENT, fact, observed_ms=observed)
    assert len(ledger.conditional_events(CLIENT)) == 2
    assert ledger.get_conditional(CLIENT)['status'] == 'submitting'
