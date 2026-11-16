"""账户级风险预占：真实独立 PostgreSQL 并发、原子性和未知保留。"""
from concurrent.futures import ThreadPoolExecutor
import importlib
import importlib.util
import os
from unittest.mock import patch
from threading import Event
import uuid

import pytest

from data_store import DataStore
from demo_store import DemoStore
from runtime_config import load_environment
from tests.test_demo_entry_decision import proposal, BTC, ETH, TRX

load_environment()


def cls():
    assert importlib.util.find_spec('demo_risk_reservation_store'), 'account risk reservation implementation missing'
    return importlib.import_module('demo_risk_reservation_store').DemoRiskReservationStore


def context(account, **changes):
    return {
        'context_id': 'ctx-1', 'sequence': 1, 'environment': 'binance_demo',
        'account_id': account, 'stage_id': 'stage-1000', 'stage_initial_usdt': '1000',
        'net_equity_usdt': '1000', 'account_at_ms': 1100, 'max_account_age_ms': 500,
        'account_snapshot_ref': 'account/snapshot/1', 'account_snapshot_hash': 'e'*64,
        'exposure_watermark': 'external-ledger/1', 'external_risk_usdt': '0',
        'external_margin_usdt': '0', 'external_symbols': [],
        'exposure_scope': 'external_only_excludes_local_reservations',
        'account_reconciled': True, 'exposure_complete': True,
        'margin_evidence_complete': True, 'risk_entries_enabled': True,
        'funding_complete': True, 'costs_complete': True, 'native_protection_verified': True,
        'approvals': {
            'strategy-v1': {'run_id': 'run-1', 'approved': True, 'strategy_hash': 'b'*64,
                            'risk_policy_hash': 'c'*64, 'cost_model_hash': 'd'*64},
        }, **changes,
    }


def request(account, **changes):
    values = dict(account_id=account, account_snapshot_hash='e'*64,
                  market_snapshot_hash='f'*64, exchange_rules_hash='1'*64,
                  exit_params_hash='2'*64, exposure_watermark='external-ledger/1')
    values.update(changes)
    return proposal(**values)


def order(**changes):
    return {'strategy_id':'strategy-v1','signal_id':'signal-1','rule_hash':'b'*64,
            'execution_hash':'2'*64,'input_fingerprint':'a'*64,
            'client_id':'qd-'+'a'*32,'intent_key':'entry','pair':BTC,'side':'buy',
            'type':'limit','quantity':'10','price':'100','time_in_force':'GTX',
            'reduce_only':False,'signal_ms':1000,'created_ms':1200,
            'raw':{'expires_ms':31000},**changes}


@pytest.fixture(scope='module')
def data():
    dsn=os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test required')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    conf=conninfo_to_dict(dsn)
    assert conf.get('dbname')=='quant_test'
    production=os.getenv('QUANT_DATABASE_URL')
    if production:
        other=conninfo_to_dict(production)
        keys=('host','port','dbname')
        assert tuple(conf.get(k,'') for k in keys)!=tuple(other.get(k,'') for k in keys)
    schema='risk_reserve_test_'+uuid.uuid4().hex
    store=DataStore(dsn=dsn,schema=schema,max_connections=6)
    try:
        store.migrate_schema()
        cls()(store,'setup','setup').ensure_schema()
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def ledger(data):
    account='acct-'+uuid.uuid4().hex
    result=cls()(data, account, 'strategy-'+uuid.uuid4().hex)
    result.install_context(context(account))
    return result


def reserve(ledger, **changes):
    return ledger.reserve_entry(request(ledger.account_scope, **changes), order(), context_id='ctx-1')


def test_atomic_entry_stores_frozen_decision_reservation_and_ordinary_intent(ledger):
    result=reserve(ledger)
    assert result['accepted'] is True
    assert result['decision']['planned_loss_usdt']=='10'
    assert result['decision']['account_risk_after_usdt']=='10'
    assert result['reservation']['status']=='reserved'
    assert result['order']['quantity']=='10.000000000000000000'
    assert result['order']['risk_reservation_account_scope']==ledger.account_scope
    assert result['order']['risk_reservation_decision_id']=='decision-1'
    assert ledger.exposure()['risk_usdt']=='10'
    assert ledger.exposure()['margin_usdt']=='100'


def test_duplicate_exact_request_returns_original_without_rebudgeting(ledger):
    first=reserve(ledger)
    assert reserve(ledger)==first
    assert ledger.exposure()['risk_usdt']=='10'
    changed=request(ledger.account_scope,stop_price='98',target_price='104')
    with pytest.raises(ValueError,match='immutable'):
        ledger.reserve_entry(changed, order(),context_id='ctx-1')
    assert len(ledger.decisions())==1


def test_failed_order_insert_rolls_back_decision_reservation_events(ledger):
    with patch.object(DemoStore,'_reserve_order_on_connection',side_effect=RuntimeError('injected write failure')):
        with pytest.raises(RuntimeError):
            reserve(ledger)
    assert ledger.decisions()==[]
    assert ledger.exposure()['risk_usdt']=='0'
    assert ledger.get_order('qd-'+'a'*32) is None
    with ledger.store._connection() as conn:
        count=conn.execute('SELECT count(*) AS n FROM demo_risk_reservation_events WHERE account_scope=%s',
                           (ledger.account_scope,)).fetchone()['n']
    assert count==0


def test_account_lock_serializes_different_strategy_namespaces(data):
    account='acct-'+uuid.uuid4().hex
    one,two=(cls()(data,account,'namespace-'+uuid.uuid4().hex) for _ in range(2))
    one.install_context(context(account,external_risk_usdt='10'))
    def perform(item):
        index,store=item
        source=request(account,decision_id='decision-'+str(index),signal_id='signal-'+str(index),pair=ETH if index else BTC)
        intent=order(client_id='qd-'+str(index+1)*32,signal_id=source['signal_id'],pair=source['pair'])
        return store.reserve_entry(source,intent,context_id='ctx-1')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(perform,enumerate((one,two))))
    assert sum(value['accepted'] for value in results)==1
    assert one.exposure()['risk_usdt']=='20'
    rejected=next(value for value in results if not value['accepted'])
    assert 'account_risk_budget_exhausted' in rejected['decision']['reason_codes']
    assert rejected['order'] is None


def test_durable_totals_override_stale_caller_used_fields(ledger):
    reserve(ledger)
    second=request(ledger.account_scope,decision_id='decision-2',signal_id='signal-2',used_risk_usdt='0',pair=ETH)
    result=ledger.reserve_entry(second,order(client_id='qd-'+'c'*32,signal_id='signal-2',pair=ETH),context_id='ctx-1')
    assert result['accepted'] is True
    assert result['decision']['account_risk_before_usdt']=='10'
    third=request(ledger.account_scope,decision_id='decision-3',signal_id='signal-3',pair=TRX)
    result=ledger.reserve_entry(third,order(client_id='qd-'+'d'*32,signal_id='signal-3',pair=TRX),context_id='ctx-1')
    assert result['accepted'] is False
    assert 'active_symbol_limit_exceeded' in result['decision']['reason_codes']


@pytest.mark.parametrize('changes',[{'quantity':'11'},{'price':'101'},{'side':'sell'},
                                    {'rule_hash':'z'*64},{'input_fingerprint':'x'*64},
                                    {'execution_hash':'3'*64},{'reduce_only':True},{'time_in_force':'IOC'}])
def test_order_identity_or_final_amount_mismatch_cannot_create_order(ledger,changes):
    result=ledger.reserve_entry(request(ledger.account_scope),order(**changes),context_id='ctx-1')
    assert result['accepted'] is False
    assert 'order_intent_mismatch' in result['decision']['reason_codes']
    assert result['order'] is None
    assert ledger.exposure()['risk_usdt']=='0'


def test_legacy_claim_cannot_bypass_reservation_gate(ledger):
    reserve(ledger)
    ordinary=DemoStore(ledger.store,ledger.virtual_account_key)
    assert ordinary.claim_submission('qd-'+'a'*32,claimed_ms=1201) is False
    assert ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=1201,context_id='ctx-1') is True
    assert ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=1202,context_id='ctx-1') is False


def test_unknown_restart_and_partial_cancel_keep_full_reservation(ledger):
    reserve(ledger)
    ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=1201,context_id='ctx-1')
    ledger.mark_reserved_unknown('qd-'+'a'*32,'RequestTimeout',observed_ms=1202)
    restarted=cls()(ledger.store,ledger.account_scope,ledger.virtual_account_key)
    assert restarted.exposure()['risk_usdt']=='10'
    assert restarted.claim_reserved_entry('qd-'+'a'*32,now_ms=99999,context_id='ctx-1') is False
    ledger.record_order_observation('qd-'+'a'*32,{'status':'canceled','filled_qty':'4','qty':'10',
                                               'order_id':'7001','updated_ms':1300,'observed_ms':1300})
    assert restarted.exposure()['risk_usdt']=='10'


def test_reduced_equity_does_not_clip_old_occupation_and_blocks_new_claim(ledger):
    reserve(ledger)
    ledger.install_context(context(ledger.account_scope,context_id='ctx-2',sequence=2,net_equity_usdt='400',
                                   account_at_ms=1250,account_snapshot_ref='account/snapshot/2',account_snapshot_hash='f'*64))
    assert ledger.exposure()['risk_usdt']=='10'
    assert ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=1251,context_id='ctx-2') is False
    result=ledger.reserve_entry(request(ledger.account_scope,decision_id='decision-2',signal_id='signal-2',
                                       account_snapshot_hash='f'*64,account_snapshot_ref='account/snapshot/2',account_at_ms=1250,
                                       decision_at_ms=1251),order(client_id='qd-'+'c'*32,signal_id='signal-2'),context_id='ctx-2')
    assert result['accepted'] is False
    assert 'account_risk_budget_exhausted' in result['decision']['reason_codes']


@pytest.mark.parametrize('changes',[{'external_risk_usdt':'0'},{'external_margin_usdt':'0'},
                                    {'external_symbols':[]},{'stage_initial_usdt':'2000'}])
def test_context_cannot_silently_drop_external_exposure_or_raise_stage(data,changes):
    account='acct-'+uuid.uuid4().hex
    store=cls()(data,account,'ns-'+uuid.uuid4().hex)
    store.install_context(context(account,external_risk_usdt='4',external_margin_usdt='25',external_symbols=[ETH]))
    updated=context(account,context_id='ctx-2',sequence=2,external_risk_usdt='4',
                    external_margin_usdt='25',external_symbols=[ETH])
    updated.update(changes)
    with pytest.raises(ValueError):
        store.install_context(updated)
    assert store.exposure()['risk_usdt']=='4'


def test_stale_context_and_withdrawn_approval_block_claim(ledger):
    reserve(ledger)
    assert not ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=2000,context_id='ctx-1')
    changed=context(ledger.account_scope,context_id='ctx-2',sequence=2,approvals={})
    ledger.install_context(changed)
    assert not ledger.claim_reserved_entry('qd-'+'a'*32,now_ms=1202,context_id='ctx-2')


def test_immutable_decision_context_and_event_tables_reject_mutation(ledger):
    import psycopg
    reserve(ledger)
    for table in ('demo_entry_decisions','demo_risk_contexts','demo_risk_reservation_events'):
        for verb in ('UPDATE','DELETE','TRUNCATE'):
            statement=f'UPDATE {table} SET account_scope=account_scope' if verb=='UPDATE' else f'{verb} {table}'
            if verb=='DELETE':
                statement='DELETE FROM '+table
            with pytest.raises(psycopg.Error),ledger.store._connection() as conn:
                conn.execute(statement)


def test_partial_or_boolean_only_release_facts_are_refused(ledger):
    reserve(ledger)
    with pytest.raises(ValueError):
        ledger.record_flat_reconciliation({'account_id':ledger.account_scope,'flat':True})
    with pytest.raises(ValueError):
        ledger.release_with_reconciliation('qd-'+'a'*32,'unknown-fact')
    assert ledger.exposure()['risk_usdt']=='10'


def archive_flat(ledger, *, fact_id='flat-1', observed_ms=1400):
    import demo_risk_reservation_store as module
    refs = {}
    hashes = ledger.local_fact_hashes()
    for kind in ('positions', 'orders', 'conditional_orders', 'fills', 'cashflows'):
        key = 'risk-flat/' + ledger.account_scope + '/' + fact_id + '/' + kind
        payload = {'kind': kind, 'environment': ledger.environment, 'account_id': ledger.account_scope,
                   'stage_id': 'stage-1000', 'observed_ms': observed_ms, 'watermark': fact_id,
                   'complete': True, 'items': []}
        if kind in hashes:
            payload['local_ledger_hash'] = hashes[kind]
        ledger.store.put_document(key, payload)
        refs[kind] = {'key': key, 'sha256': module._hash(payload)}
    return {'fact_id': fact_id, 'environment': ledger.environment, 'account_id': ledger.account_scope,
            'stage_id': 'stage-1000', 'client_id': 'qd-'+'a'*32, 'observed_ms': observed_ms,
            'exposure_watermark': fact_id, 'source_refs': refs,
            'local_ledger_hashes': hashes}


def test_valid_archived_flat_proof_releases_and_expires_unclaimed_intent(ledger):
    reserve(ledger)
    fact = archive_flat(ledger)
    assert ledger.record_flat_reconciliation(fact) == 'flat-1'
    assert ledger.record_flat_reconciliation(fact) == 'flat-1'
    assert ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1') is True
    assert ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1') is False
    assert ledger.exposure()['risk_usdt'] == '0'
    assert ledger.get_order('qd-'+'a'*32)['status'] == 'expired'
    assert not ledger.claim_reserved_entry('qd-'+'a'*32, now_ms=1401, context_id='ctx-1')


def test_flat_proof_cannot_release_later_local_order_observation(ledger):
    reserve(ledger)
    fact = archive_flat(ledger)
    ledger.record_flat_reconciliation(fact)
    ledger.record_order_observation('qd-'+'a'*32, {'status':'canceled','qty':'10','filled_qty':'0',
                                                'order_id':'7001','updated_ms':1500})
    with pytest.raises(ValueError, match='cover|changed'):
        ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1')
    assert ledger.exposure()['risk_usdt'] == '10'


def test_archived_flat_sources_are_immutable_after_document_replacement(ledger):
    reserve(ledger)
    fact = archive_flat(ledger)
    ledger.record_flat_reconciliation(fact)
    ledger.store.put_document(fact['source_refs']['positions']['key'], {'mutable':'replacement'})
    assert ledger.record_flat_reconciliation(fact) == 'flat-1'
    with ledger.store._connection() as conn:
        row = conn.execute('SELECT payload FROM demo_risk_flat_reconciliations WHERE account_scope=%s',
                           (ledger.account_scope,)).fetchone()
    assert row['payload']['archived_sources']['positions']['items'] == []
    assert ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1')


def test_direct_sql_claim_without_risk_event_is_refused(ledger):
    import psycopg
    reserve(ledger)
    with pytest.raises(psycopg.Error), ledger.store._connection() as conn:
        conn.execute("UPDATE demo_orders SET status='submitting',submission_attempts=1 WHERE virtual_account_key=%s",
                     (ledger.virtual_account_key,))
    assert ledger.get_order('qd-'+'a'*32)['submission_attempts'] == 0


def test_same_account_cross_namespace_client_identity_collision_is_rejected(ledger):
    reserve(ledger)
    sibling = cls()(ledger.store, ledger.account_scope, 'sibling-'+uuid.uuid4().hex)
    with pytest.raises(ValueError, match='identity conflict'):
        sibling.reserve_entry(request(ledger.account_scope, decision_id='decision-2', pair=ETH),
                              order(pair=ETH), context_id='ctx-1')
    assert sibling.get_order('qd-'+'a'*32) is None


def test_account_pair_ownership_is_preserved_across_strategy_namespaces(ledger):
    reserve(ledger)
    sibling = cls()(ledger.store, ledger.account_scope, 'sibling-'+uuid.uuid4().hex)
    result = sibling.reserve_entry(request(ledger.account_scope, decision_id='decision-2', signal_id='signal-2'),
                                   order(client_id='qd-'+'b'*32, signal_id='signal-2'), context_id='ctx-1')
    assert not result['accepted']
    assert 'account_pair_ownership_conflict' in result['decision']['reason_codes']
    assert sibling.get_order('qd-'+'b'*32) is None


def test_unknown_exact_observation_is_idempotent_and_retains_risk(ledger):
    reserve(ledger)
    assert ledger.claim_reserved_entry('qd-'+'a'*32, now_ms=1201, context_id='ctx-1')
    ledger.mark_reserved_unknown('qd-'+'a'*32, 'RequestTimeout', observed_ms=1202)
    ledger.mark_reserved_unknown('qd-'+'a'*32, 'RequestTimeout', observed_ms=1202)
    assert ledger.exposure()['risk_usdt'] == '10'


def test_unsupported_values_cannot_collapse_to_same_immutable_request_hash(ledger):
    source = request(ledger.account_scope)
    source['untrusted_object'] = object()
    with pytest.raises(TypeError, match='unsupported'):
        ledger.reserve_entry(source, order(), context_id='ctx-1')


@pytest.mark.parametrize('changes', [{'net_equity_usdt':'800'}, {'external_symbols':[ETH,TRX]}])
def test_claim_rechecks_current_single_entry_and_symbol_bounds(ledger, changes):
    reserve(ledger)
    ledger.install_context(context(ledger.account_scope, context_id='ctx-2', sequence=2, **changes))
    assert not ledger.claim_reserved_entry('qd-'+'a'*32, now_ms=1201, context_id='ctx-2')
    assert ledger.exposure()['risk_usdt'] == '10'


def test_unresolved_local_unknown_blocks_new_reservation_and_other_claim(ledger):
    reserve(ledger)
    second = ledger.reserve_entry(request(ledger.account_scope, decision_id='decision-2', signal_id='signal-2', pair=ETH),
                                  order(client_id='qd-'+'b'*32, signal_id='signal-2', pair=ETH), context_id='ctx-1')
    assert second['accepted']
    assert ledger.claim_reserved_entry('qd-'+'a'*32, now_ms=1201, context_id='ctx-1')
    ledger.mark_reserved_unknown('qd-'+'a'*32, 'RequestTimeout', observed_ms=1202)
    assert not ledger.claim_reserved_entry('qd-'+'b'*32, now_ms=1203, context_id='ctx-1')
    third = ledger.reserve_entry(request(ledger.account_scope, decision_id='decision-3', signal_id='signal-3', pair=TRX),
                                 order(client_id='qd-'+'c'*32, signal_id='signal-3', pair=TRX), context_id='ctx-1')
    assert not third['accepted']
    assert 'unresolved_account_submission' in third['decision']['reason_codes']


def test_order_frozen_identity_cannot_change_after_risk_binding(ledger):
    import psycopg
    reserve(ledger)
    with pytest.raises(psycopg.Error), ledger.store._connection() as conn:
        conn.execute("UPDATE demo_orders SET quantity=11 WHERE virtual_account_key=%s", (ledger.virtual_account_key,))
    assert ledger.get_order('qd-'+'a'*32)['quantity'] == '10.000000000000000000'


def test_namespace_cannot_be_reused_for_different_account(data):
    namespace = 'namespace-'+uuid.uuid4().hex
    one = cls()(data, 'acct-'+uuid.uuid4().hex, namespace)
    one.install_context(context(one.account_scope))
    reserve(one)
    two = cls()(data, 'acct-'+uuid.uuid4().hex, namespace)
    two.install_context(context(two.account_scope))
    with pytest.raises(ValueError, match='namespace.*account'):
        two.reserve_entry(request(two.account_scope, decision_id='decision-2', signal_id='signal-2', pair=ETH),
                          order(client_id='qd-'+'d'*32, signal_id='signal-2', pair=ETH), context_id='ctx-1')


def test_conflicting_order_type_alias_cannot_save_market_intent(ledger):
    result = ledger.reserve_entry(request(ledger.account_scope), order(order_type='market'), context_id='ctx-1')
    assert not result['accepted']
    assert 'order_intent_mismatch' in result['decision']['reason_codes']
    assert ledger.get_order('qd-'+'a'*32) is None


def test_claim_decimal_boundary_uses_exact_current_equity(ledger):
    reserve(ledger)
    ledger.install_context(context(ledger.account_scope, context_id='ctx-2', sequence=2,
                                   net_equity_usdt='999.99999999999999999999999999'))
    assert not ledger.claim_reserved_entry('qd-'+'a'*32, now_ms=1201, context_id='ctx-2')


def test_late_fact_after_release_invalidates_available_budget(ledger):
    reserve(ledger)
    ledger.record_flat_reconciliation(archive_flat(ledger))
    assert ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1')
    ledger.record_order_observation('qd-'+'a'*32, {'status':'canceled','qty':'10','filled_qty':'0',
                                                'order_id':'7001','updated_ms':1500})
    with pytest.raises(RuntimeError, match='released.*facts changed'):
        ledger.exposure()


def test_release_prevents_mid_snapshot_legacy_observation_write(ledger):
    import psycopg
    reserve(ledger)
    ledger.record_flat_reconciliation(archive_flat(ledger))
    checked = Event()
    proceed = Event()
    original = ledger._local_fact_hashes
    def pause_after_snapshot(conn):
        value = original(conn)
        checked.set()
        assert proceed.wait(5), 'release race test timed out'
        return value
    def legacy_writer():
        try:
            with ledger.store._connection() as conn:
                conn.execute("SET LOCAL lock_timeout='250ms'")
                conn.execute("UPDATE demo_orders SET audit_observations=audit_observations || '[{\"late\":true}]'::jsonb "
                             'WHERE virtual_account_key=%s', (ledger.virtual_account_key,))
            return True
        except psycopg.errors.LockNotAvailable:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool, patch.object(ledger, '_local_fact_hashes', side_effect=pause_after_snapshot):
        release = pool.submit(ledger.release_with_reconciliation, 'qd-'+'a'*32, 'flat-1')
        assert checked.wait(5), 'release did not reach snapshot'
        try:
            written = pool.submit(legacy_writer).result(timeout=3)
            assert written is False, 'legacy writer changed facts inside release snapshot'
        finally:
            proceed.set()
            release.result(timeout=5)
    assert legacy_writer() is True
    with pytest.raises(RuntimeError, match='released.*facts changed'):
        ledger.exposure()


def test_release_table_lock_conflict_rolls_back_and_preserves_risk(ledger):
    import psycopg
    reserve(ledger)
    ledger.record_flat_reconciliation(archive_flat(ledger))
    held = Event()
    done = Event()
    def existing_writer():
        with ledger.store._connection() as conn:
            conn.execute('LOCK TABLE demo_conditional_events IN ROW EXCLUSIVE MODE')
            held.set()
            assert done.wait(5), 'writer release test timed out'
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(existing_writer)
        assert held.wait(5)
        try:
            try:
                ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1')
            except psycopg.errors.LockNotAvailable:
                pass
            else:
                raise AssertionError('conflicting writer must preserve reserved risk')
        finally:
            done.set()
            writer.result(timeout=5)
    assert ledger.exposure()['risk_usdt'] == '10'
    assert ledger.exposure()['reservations'][0]['status'] == 'reserved'
    assert ledger.release_with_reconciliation('qd-'+'a'*32, 'flat-1') is True
