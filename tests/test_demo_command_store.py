"""被动命令账本的拒绝边界与持久恢复；仅独立 quant_test 随机 schema。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import importlib.util
import os
from unittest.mock import Mock
import uuid

import pytest

from data_store import DataStore
from demo_evidence_store import DemoEvidenceStore
from demo_store import DemoStore
from runtime_config import load_environment

load_environment()
OWNER = 'writer-' + '1' * 32
OTHER = 'writer-' + '2' * 32


def api():
    assert importlib.util.find_spec('demo_command_store'), 'passive command store missing'
    return importlib.import_module('demo_command_store')


def scope(account='command-account'):
    return {'environment': 'binance_demo', 'account_id': account, 'stage_id': 'stage-1000',
            'strategy_version_id': 'strategy-v1', 'run_id': 'run-1'}


def hashes():
    return dict(zip(('code_hash', 'strategy_hash', 'execution_hash', 'risk_policy_hash', 'cost_model_hash'),
                    (char * 64 for char in 'abcde'), strict=True))


def binding(account='command-account', **changes):
    return api().FrozenCommandBinding('binding-1', scope(account), hashes(), 'demo_default', **changes)


def submit(store, **changes):
    return store.enqueue(binding_id='binding-1', command_id='command-1', kind='pause_entries',
                         actor='operator-1', reason_code='operator_pause', submitted_ms=1000, **changes)


def test_offline_frozen_binding_keeps_deep_copies_and_never_claims_authenticity():
    original_scope, original_hashes = scope(), hashes()
    value = api().FrozenCommandBinding('binding-1', original_scope, original_hashes, 'demo_default')
    original_scope['run_id'] = 'changed'
    original_hashes['code_hash'] = 'f' * 64
    exported = value.to_dict()
    exported['scope']['run_id'] = 'changed-again'
    assert value.to_dict()['scope']['run_id'] == 'run-1'
    assert value.to_dict()['identity_hashes']['code_hash'] == 'a' * 64
    assert value.sha256 == api().FrozenCommandBinding('binding-1', scope(), hashes(), 'demo_default').sha256
    with pytest.raises((AttributeError, TypeError)):
        value.namespace = 'changed'


@pytest.mark.parametrize('changed', [
    {'environment': 'live'}, {'account_id': ''}, {'run_id': None},
    {'stage_id': 'https://x.invalid?token=x'}, {'strategy_version_id': 'user@host'},
    {'account_id': '../outside'}, {'acceptance_eligible': True},
])
def test_offline_invalid_binding_scope_cannot_create_authority(changed):
    with pytest.raises(ValueError):
        api().FrozenCommandBinding('binding-1', {**scope(), **changed}, hashes(), 'demo_default')


@pytest.mark.parametrize('changed', [{'code_hash': True}, {'risk_policy_hash': 'x'}, {'secret': 'x'}])
def test_offline_invalid_five_hashes_are_rejected(changed):
    with pytest.raises(ValueError):
        api().FrozenCommandBinding('binding-1', scope(), {**hashes(), **changed}, 'demo_default')


@pytest.mark.parametrize('changed', [
    {'command_id': ''}, {'command_id': 'https://x.invalid'}, {'binding_id': '../secret'},
    {'kind': 'start'}, {'kind': []}, {'kind': 'flatten'}, {'actor': 'Bearer secret'},
    {'actor': 'user@host'}, {'reason_code': 'secret=x'}, {'reason_code': 'restart_reconciliation'},
    {'submitted_ms': True}, {'submitted_ms': -1}, {'submitted_ms': 1.1}, {'submitted_ms': 2**63},
])
def test_offline_invalid_enqueue_never_opens_database(changed):
    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    values = dict(binding_id='binding-1', command_id='command-1', kind='pause_entries',
                  actor='operator-1', reason_code='operator_pause', submitted_ms=1000)
    values.update(changed)
    with pytest.raises(ValueError):
        store.enqueue(**values)
    data._connection.assert_not_called()


@pytest.mark.parametrize('owner', [123, '', '1234', 'pid-100', 'writer-' + 'a' * 31,
                                   'writer-' + 'A' * 32, {'lease': True}])
def test_offline_claim_rejects_pid_lease_or_malformed_owner_before_database(owner):
    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    with pytest.raises(ValueError):
        store.claim('command-1', owner_id=owner, claimed_ms=1001)
    data._connection.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'event_type': 'completed'}, {'event_type': 'claimed'}, {'event_type': []},
    {'reason_code': 'upstream secret'}, {'observed_ms': True},
    {'evidence_refs': [{'ref': 'https://x.invalid', 'record_sha256': 'a' * 64}]},
    {'evidence_refs': [{'ref': 'evidence:' + 'a' * 64, 'record_sha256': 'b' * 64, 'secret': 'x'}]},
    {'evidence_refs': []},
])
def test_offline_invalid_applied_event_never_opens_database(changes):
    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    values = dict(event_id='event-1', event_type='applied', owner_id=OWNER,
                  observed_ms=1002, reason_code='control_applied',
                  evidence_refs=[{'ref': 'evidence:' + 'a' * 64, 'record_sha256': 'b' * 64}])
    values.update(changes)
    with pytest.raises(ValueError):
        store.append_event('command-1', **values)
    data._connection.assert_not_called()


def test_offline_wrong_account_binding_never_opens_database():
    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    with pytest.raises(ValueError):
        store.register_binding(binding('other-account'))
    data._connection.assert_not_called()


@pytest.mark.parametrize('mutation', ['payload', 'hash', 'identity'])
def test_offline_binding_subclass_cannot_commit_unreplayable_facts(mutation):
    class Forged(api().FrozenCommandBinding):
        def to_dict(self):
            body = super().to_dict()
            if mutation == 'payload':
                body['unexpected_payload'] = {'request_url': 'https://x.invalid'}
            elif mutation == 'identity':
                body['binding_id'] = 'other-binding'
            return body

        @property
        def sha256(self):
            return 'f' * 64 if mutation == 'hash' else super().sha256

    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    forged = Forged('binding-1', scope(), hashes(), 'demo_default')
    with pytest.raises(ValueError):
        store.register_binding(forged)
    data._connection.assert_not_called()


def test_offline_damaged_frozen_binding_is_revalidated_before_connection():
    data = Mock()
    value = binding()
    object.__setattr__(value, '_scope', (('environment', 'live'),))
    with pytest.raises(ValueError):
        api().DemoCommandStore(data, 'command-account').register_binding(value)
    data._connection.assert_not_called()


def test_offline_malformed_archive_payload_has_bounded_rejection():
    from unittest.mock import patch
    data = Mock()
    store = api().DemoCommandStore(data, 'command-account')
    refs = [{'ref': 'evidence:' + 'a' * 64, 'record_sha256': 'b' * 64}]
    row = {'ref': refs[0]['ref'], 'record_sha256': 'b' * 64,
           'body': {'scope': scope(), 'payload': []}}
    with patch.object(DemoEvidenceStore, '_chain', return_value=[row]):
        with pytest.raises(ValueError, match='evidence'):
            store._evidence(object(), binding().to_dict(), refs)


class AccountCommandSQL:
    """只替换 SQL 传输；所有写入、摘要、状态与重放均使用真实存储。"""
    def __new__(cls):
        from tests.test_demo_writer import CommandSQL, One

        class SQL(CommandSQL):
            def __init__(self):
                super().__init__()
                self.statements = []

            def execute(self, query, params=()):
                self.statements.append((query, deepcopy(params)))
                if query.startswith('SELECT command_id FROM demo_command_intents') and 'FOR UPDATE' not in query:
                    rows = [row for row in self.intents
                            if (row['environment'], row['account_id']) == params[:2]]
                    rows.sort(key=lambda row: row['command_id'])
                    return One([{'command_id': row['command_id']} for row in rows[:params[2]]])
                return super().execute(query, params)

        return SQL()


def offline_account_ledger():
    data = AccountCommandSQL()
    store = api().DemoCommandStore(data, 'command-account')
    store.register_binding(binding())
    return store, data


def enqueue_offline(store, command_id, binding_id='binding-1'):
    return store.enqueue(binding_id=binding_id, command_id=command_id, kind='pause_entries',
                         actor='operator-1', reason_code='operator_pause', submitted_ms=1000)


def test_offline_unresolved_commands_replay_account_wide_across_bindings_after_restart():
    store, data = offline_account_ledger()
    other_binding = api().FrozenCommandBinding('binding-2', {**scope(), 'run_id': 'run-2'},
                                              hashes(), 'demo_other')
    store.register_binding(other_binding)
    enqueue_offline(store, 'z-claimed')
    enqueue_offline(store, 'a-unknown', 'binding-2')
    enqueue_offline(store, 'accepted-only')
    enqueue_offline(store, 'rejected-only')
    store.claim('z-claimed', owner_id=OWNER, claimed_ms=1001)
    store.claim('a-unknown', owner_id=OTHER, claimed_ms=1001)
    store.append_event('a-unknown', event_id='unknown-1', event_type='unknown', owner_id=OTHER,
                       observed_ms=1002, reason_code='outcome_unknown', evidence_refs=[])
    store.claim('rejected-only', owner_id=OWNER, claimed_ms=1001)
    store.append_event('rejected-only', event_id='reject-1', event_type='rejected', owner_id=OWNER,
                       observed_ms=1002, reason_code='command_refused', evidence_refs=[])
    foreign = api().DemoCommandStore(data, 'other-account')
    foreign.register_binding(binding('other-account'))
    enqueue_offline(foreign, 'foreign-claimed')
    foreign.claim('foreign-claimed', owner_id=OWNER, claimed_ms=1001)
    before = deepcopy((data.bindings, data.intents, data.events, data.rows))
    data.statements.clear()

    restarted = api().DemoCommandStore(data, 'command-account')
    result = restarted.unresolved_commands()

    assert [(row['command_id'], row['status']) for row in result] == [
        ('a-unknown', 'unknown'), ('z-claimed', 'claimed')]
    assert [row['binding']['namespace'] for row in result] == ['demo_other', 'demo_default']
    assert all(row['submission_attempts'] == 1 and row['execution_complete'] is False
               and row['source_authenticated'] is False and row['acceptance_eligible'] is False
               and row['production_authorized'] is False for row in result)
    assert before == (data.bindings, data.intents, data.events, data.rows)
    assert all(query.startswith('SELECT ') for query, _ in data.statements)
    assert data.statements[0][0].startswith('SELECT pg_advisory_xact_lock')
    result[0]['binding']['namespace'] = 'mutated-copy'
    assert restarted.unresolved_commands()[0]['binding']['namespace'] == 'demo_other'


def test_offline_unresolved_commands_empty_and_accepted_are_not_unresolved():
    store, _ = offline_account_ledger()
    assert store.unresolved_commands() == []
    enqueue_offline(store, 'accepted-only')
    assert store.unresolved_commands() == []


def test_offline_pending_commands_includes_accepted_claimed_and_unknown_only():
    store, _ = offline_account_ledger()
    for command_id in ('z-claimed', 'a-unknown', 'accepted-only', 'rejected-only'):
        enqueue_offline(store, command_id)
    store.claim('z-claimed', owner_id=OWNER, claimed_ms=1001)
    store.claim('a-unknown', owner_id=OTHER, claimed_ms=1001)
    store.append_event('a-unknown', event_id='unknown-1', event_type='unknown', owner_id=OTHER,
                       observed_ms=1002, reason_code='outcome_unknown', evidence_refs=[])
    store.claim('rejected-only', owner_id=OWNER, claimed_ms=1001)
    store.append_event('rejected-only', event_id='reject-1', event_type='rejected', owner_id=OWNER,
                       observed_ms=1002, reason_code='command_refused', evidence_refs=[])

    pending = store.pending_commands()

    assert [(row['command_id'], row['status']) for row in pending] == [
        ('a-unknown', 'unknown'), ('accepted-only', 'accepted'), ('z-claimed', 'claimed')]
    assert all(row['execution_complete'] is False and row['production_authorized'] is False
               for row in pending)


def test_offline_accepted_commands_are_limited_to_exact_frozen_binding():
    store, _ = offline_account_ledger()
    other = api().FrozenCommandBinding('binding-2', {**scope(), 'run_id': 'run-2'},
                                      hashes(), 'demo_other')
    store.register_binding(other)
    enqueue_offline(store, 'z-current')
    enqueue_offline(store, 'a-other', 'binding-2')
    enqueue_offline(store, 'b-current')

    accepted = store.accepted_commands('binding-1')

    assert [row['command_id'] for row in accepted] == ['b-current', 'z-current']
    assert all(row['binding']['binding_id'] == 'binding-1' and row['status'] == 'accepted'
               for row in accepted)


@pytest.mark.parametrize('corruption', ['binding', 'intent', 'event_hash', 'transition',
                                        'orphan_event', 'missing_accepted'])
def test_offline_unresolved_commands_refuse_corruption_even_in_other_terminal_command(corruption):
    store, data = offline_account_ledger()
    enqueue_offline(store, 'terminal-command')
    store.claim('terminal-command', owner_id=OWNER, claimed_ms=1001)
    store.append_event('terminal-command', event_id='reject-1', event_type='rejected', owner_id=OWNER,
                       observed_ms=1002, reason_code='command_refused', evidence_refs=[])
    if corruption == 'binding':
        data.bindings[0]['payload']['namespace'] = 'changed'
    elif corruption == 'intent':
        data.intents[0]['payload']['actor'] = 'changed'
    elif corruption == 'event_hash':
        data.events[-1]['payload']['reason_code'] = 'changed'
    elif corruption == 'transition':
        row = data.events[-1]
        row['payload']['reason_code'] = 'control_applied'
        row['payload_sha256'] = api()._hash(row['payload'])
        row['record_sha256'] = api()._hash({'sequence': row['account_sequence'],
            'previous_hash': row['previous_hash'], 'payload_sha256': row['payload_sha256']})
    elif corruption == 'orphan_event':
        data.intents.clear()
    else:
        data.events.clear()
    with pytest.raises(ValueError, match='integrity'):
        store.unresolved_commands()


def test_offline_unresolved_commands_refuse_account_replay_limit_without_partial_list(monkeypatch):
    store, _ = offline_account_ledger()
    for command_id in ('first', 'second', 'third'):
        enqueue_offline(store, command_id)
    monkeypatch.setattr(api(), 'MAX_EVENTS', 2)
    with pytest.raises(ValueError, match='limit'):
        store.unresolved_commands()


@pytest.fixture(scope='module')
def database():
    dsn = os.getenv('QUANT_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('independent quant_test required')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    parsed = conninfo_to_dict(dsn)
    assert parsed.get('dbname') == 'quant_test'
    production = os.getenv('QUANT_DATABASE_URL')
    if production:
        live = conninfo_to_dict(production)
        keys = ('host', 'port', 'dbname')
        assert tuple(parsed.get(k, '') for k in keys) != tuple(live.get(k, '') for k in keys)
    schema = 'command_test_' + uuid.uuid4().hex
    data = DataStore(dsn=dsn, schema=schema, max_connections=6)
    try:
        data.migrate_schema()
        DemoStore(data).ensure_schema()
        with data._connection() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'] == 'quant_test'
        yield data
    finally:
        data.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
            assert conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone() is None


@pytest.fixture
def ledger(database):
    account = 'command-' + uuid.uuid4().hex
    store = api().DemoCommandStore(database, account)
    store.register_binding(binding(account))
    return store


def evidence(store):
    archive = DemoEvidenceStore(store.store, store.account_id, 'stage-1000')
    receipt = archive.append('equity_reconciliation', scope(store.account_id),
                             {'schema': 'demo-control-observation-v1', 'identity_hashes': hashes(),
                              'entries_enabled': False}, {}, stable_key='control-1')
    return [{'ref': receipt['ref'], 'record_sha256': receipt['record_sha256']}]


def test_persistence_accepted_is_not_execution_and_exact_retry_is_one_event(ledger):
    first = submit(ledger)
    assert first['committed'] is True
    assert first['action_ticket'] is False
    assert first['command']['status'] == 'accepted'
    assert first['command']['acceptance_eligible'] is False
    assert first['command']['execution_complete'] is False
    assert submit(ledger) == first
    assert len(ledger.events('command-1')) == 1
    assert ledger.enqueue(binding_id='binding-1', command_id='command-1', kind='pause_entries',
                          actor='operator-1', reason_code='operator_pause', submitted_ms=9999) == first
    first['command']['binding']['scope']['run_id'] = 'tampered'
    assert ledger.get('command-1')['binding']['scope']['run_id'] == 'run-1'
    with pytest.raises(ValueError, match='immutable'):
        ledger.enqueue(binding_id='binding-1', command_id='command-1', kind='pause_entries',
                       actor='operator-2', reason_code='operator_pause', submitted_ms=1000)
    assert len(ledger.events('command-1')) == 1


def test_persistence_reconcile_command_keeps_kind_reason_and_does_not_execute(ledger):
    result = ledger.enqueue(binding_id='binding-1', command_id='reconcile-1', kind='reconcile_owned',
        actor='operator-1', reason_code='restart_reconciliation', submitted_ms=1000)
    assert result['command']['intent']['kind'] == 'reconcile_owned'
    assert result['command']['status'] == 'accepted'
    assert result['action_ticket'] is False
    assert result['command']['execution_complete'] is False


@pytest.mark.parametrize('event_type,reason', [('unknown', 'outcome_unknown'),
                                             ('applied', 'control_applied'),
                                             ('rejected', 'command_refused')])
def test_persistence_terminal_or_unknown_requires_prior_claim(ledger, event_type, reason):
    submit(ledger)
    refs = evidence(ledger) if event_type == 'applied' else []
    with pytest.raises(ValueError, match='integrity'):
        ledger.append_event('command-1', event_id='early-1', event_type=event_type, owner_id=OWNER,
            observed_ms=1001, reason_code=reason, evidence_refs=refs)
    assert ledger.get('command-1')['status'] == 'accepted'
    assert len(ledger.events('command-1')) == 1


def test_persistence_regressed_event_rolls_back_and_other_owner_cannot_claim_success(ledger):
    submit(ledger)
    ledger.claim('command-1', owner_id=OWNER, claimed_ms=1001)
    refs = evidence(ledger)
    for event_id, owner, now in [('regressed', OWNER, 999), ('other-owner', OTHER, 1002)]:
        with pytest.raises(ValueError, match='integrity'):
            ledger.append_event('command-1', event_id=event_id, event_type='applied', owner_id=owner,
                observed_ms=now, reason_code='control_applied', evidence_refs=refs)
    assert ledger.get('command-1')['status'] == 'claimed'
    assert len(ledger.events('command-1')) == 2


def test_persistence_binding_retry_and_changed_binding_conflict(ledger):
    first = ledger.register_binding(binding(ledger.account_id))
    assert ledger.register_binding(binding(ledger.account_id)) == first
    changed = scope(ledger.account_id)
    changed['run_id'] = 'run-2'
    with pytest.raises(ValueError, match='immutable'):
        ledger.register_binding(api().FrozenCommandBinding('binding-1', changed, hashes(), 'demo_default'))
    assert ledger.get_binding('binding-1')['scope']['run_id'] == 'run-1'


def test_persistence_concurrent_claim_commits_exactly_one_ticket(ledger):
    submit(ledger)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: ledger.claim('command-1', owner_id=owner, claimed_ms=1001),
                                (OWNER, OTHER)))
    assert sum(row['action_ticket'] is True for row in results) == 1
    assert [row['event_type'] for row in ledger.events('command-1')] == ['accepted', 'claimed']
    assert ledger.get('command-1')['status'] == 'claimed'
    assert ledger.claim('command-1', owner_id=OWNER, claimed_ms=1002)['action_ticket'] is False


def test_persistence_unknown_restart_cannot_reclaim_or_renew_with_age(ledger):
    submit(ledger)
    assert ledger.claim('command-1', owner_id=OWNER, claimed_ms=1001)['action_ticket'] is True
    ledger.append_event('command-1', event_id='unknown-1', event_type='unknown', owner_id=OTHER,
                        observed_ms=1002, reason_code='outcome_unknown', evidence_refs=[])
    restarted = api().DemoCommandStore(ledger.store, ledger.account_id)
    assert restarted.get('command-1')['status'] == 'unknown'
    assert restarted.claim('command-1', owner_id=OTHER, claimed_ms=10**15)['action_ticket'] is False
    assert len(restarted.events('command-1')) == 3
    observed = restarted.append_event('command-1', event_id='observed-1', event_type='observed',
        owner_id=OTHER, observed_ms=1003, reason_code='recovery_observed', evidence_refs=[])
    assert observed['status'] == 'unknown'
    assert observed['submission_attempts'] == 1


def test_persistence_applied_requires_real_same_binding_archive_and_does_not_certify_exchange(ledger):
    submit(ledger)
    ledger.claim('command-1', owner_id=OWNER, claimed_ms=1001)
    refs = evidence(ledger)
    result = ledger.append_event('command-1', event_id='applied-1', event_type='applied', owner_id=OWNER,
        observed_ms=1002, reason_code='control_applied', evidence_refs=refs)
    assert result['status'] == 'applied'
    assert result['execution_complete'] is False
    assert result['acceptance_eligible'] is False
    assert result['production_authorized'] is False
    assert ledger.append_event('command-1', event_id='applied-1', event_type='applied', owner_id=OWNER,
        observed_ms=1002, reason_code='control_applied', evidence_refs=refs) == result
    with pytest.raises(ValueError, match='immutable'):
        ledger.append_event('command-1', event_id='applied-1', event_type='applied', owner_id=OWNER,
            observed_ms=1003, reason_code='control_applied', evidence_refs=refs)
    assert ledger.claim('command-1', owner_id=OTHER, claimed_ms=1003)['action_ticket'] is False


@pytest.mark.parametrize('mutation', ['missing', 'hash', 'scope', 'hashes'])
def test_persistence_applied_cannot_borrow_or_invent_evidence(ledger, mutation):
    submit(ledger)
    ledger.claim('command-1', owner_id=OWNER, claimed_ms=1001)
    refs = evidence(ledger)
    if mutation == 'missing':
        refs[0]['ref'] = 'evidence:' + 'f' * 64
    elif mutation == 'hash':
        refs[0]['record_sha256'] = 'f' * 64
    else:
        archive = DemoEvidenceStore(ledger.store, ledger.account_id, 'stage-1000')
        altered = scope(ledger.account_id)
        altered_hashes = hashes()
        if mutation == 'scope':
            altered['run_id'] = 'other-run'
        else:
            altered_hashes['code_hash'] = 'f' * 64
        row = archive.append('equity_reconciliation', altered, {'identity_hashes': altered_hashes}, {})
        refs = [{'ref': row['ref'], 'record_sha256': row['record_sha256']}]
    with pytest.raises(ValueError, match='command state replay integrity failed'):
        ledger.append_event('command-1', event_id='applied-1', event_type='applied', owner_id=OWNER,
            observed_ms=1002, reason_code='control_applied', evidence_refs=refs)
    assert ledger.get('command-1')['status'] == 'claimed'
    assert [row['event_type'] for row in ledger.events('command-1')] == ['accepted', 'claimed']


def test_persistence_commit_failure_returns_no_ticket_and_leaves_claim_available(ledger):
    import psycopg
    submit(ledger)
    with ledger.store._connection() as conn:
        conn.execute("CREATE FUNCTION command_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
                     "BEGIN IF NEW.account_id='" + ledger.account_id + "' AND NEW.event_type='claimed' THEN "
                     "RAISE EXCEPTION 'injected command failure'; END IF; RETURN NEW; END; $$")
        conn.execute('CREATE CONSTRAINT TRIGGER command_commit_failure AFTER INSERT ON demo_command_events '
                     'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION command_commit_failure()')
    try:
        with pytest.raises(psycopg.Error, match='injected command failure'):
            ledger.claim('command-1', owner_id=OWNER, claimed_ms=1001)
    finally:
        with ledger.store._connection() as conn:
            conn.execute('DROP TRIGGER command_commit_failure ON demo_command_events')
            conn.execute('DROP FUNCTION command_commit_failure()')
    assert ledger.get('command-1')['status'] == 'accepted'
    assert ledger.claim('command-1', owner_id=OTHER, claimed_ms=1002)['action_ticket'] is True


@pytest.mark.parametrize('table', ['demo_command_bindings', 'demo_command_intents', 'demo_command_events'])
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE', 'TRUNCATE'])
def test_persistence_all_facts_have_always_immutable_guards(ledger, table, operation):
    import psycopg
    from psycopg import sql
    submit(ledger)
    template = {'UPDATE': 'UPDATE {} SET account_id=account_id',
                'DELETE': 'DELETE FROM {}', 'TRUNCATE': 'TRUNCATE {} CASCADE'}[operation]
    statement = sql.SQL(template).format(sql.Identifier(table))
    with pytest.raises(psycopg.Error, match='immutable') as raised:
        with ledger.store._connection() as conn:
            conn.execute("SET LOCAL session_replication_role='replica'")
            conn.execute(statement)
    assert raised.value.sqlstate == '55000'
    assert ledger.get('command-1')['status'] == 'accepted'


def test_persistence_replay_detects_event_content_hash_tampering(ledger):
    submit(ledger)
    # 仅自有随机 schema 暂停触发器制造损坏；恢复后读取必须拒绝。此用例不操作生产。
    with ledger.store._connection() as conn:
        conn.execute('ALTER TABLE demo_command_events DISABLE TRIGGER demo_command_events_immutable')
        conn.execute("UPDATE demo_command_events SET payload=jsonb_set(payload,'{reason_code}',%s::jsonb) "
                     'WHERE account_id=%s', ('"control_applied"', ledger.account_id))
        conn.execute('ALTER TABLE demo_command_events ENABLE ALWAYS TRIGGER demo_command_events_immutable')
    with pytest.raises(ValueError, match='integrity'):
        ledger.get('command-1')
