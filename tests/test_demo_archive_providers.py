"""归档桥接使用真实 DemoEvidenceStore 链与完成索引，SQL 边界只替换为内存。"""
from contextlib import contextmanager
from copy import deepcopy
import importlib
import importlib.util
from unittest.mock import Mock

import pytest

from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_round_trip_costs import reconcile_round_trip_costs
from tests.test_demo_round_trip_costs import evidence
from tests.test_demo_safety_runtime import context
from tests.test_demo_entry_decision import proposal

SCOPE = {'environment': 'binance_demo', 'account_id': 'account-a', 'stage_id': 'stage-1000',
         'strategy_version_id': 'strategy-v1', 'run_id': 'run-1'}
IDENTITY = dict(zip(HASHES, (str(i)*64 for i in range(1, 6))))


class Result:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def fetchall(self):
        return self.rows


class MemorySQL:
    def __init__(self):
        self.rows, self.index = [], []

    @staticmethod
    def _json(value):
        return deepcopy(value)

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, query, params=()):
        if 'pg_advisory' in query:
            return Result([])
        if query.startswith('SELECT * FROM demo_evidence_archive'):
            return Result(sorted([row for row in self.rows if (row['environment'], row['account_id']) == params],
                                 key=lambda row: row['account_sequence']))
        if query.startswith('SELECT * FROM demo_completion_records'):
            return Result([row for row in self.index if (row['environment'], row['account_id'], row['stage_id']) == params])
        if query.startswith('INSERT INTO demo_evidence_archive'):
            columns = ('environment', 'account_id', 'stage_id', 'account_sequence', 'ref', 'kind',
                       'capture_key', 'sha256', 'previous_hash', 'record_sha256', 'body')
            self.rows.append(dict(zip(columns, deepcopy(params))))
            return Result([])
        if query.startswith('INSERT INTO demo_completion_records'):
            columns = ('environment', 'account_id', 'stage_id', 'trade_id', 'proof_sha256',
                       'ref', 'correction_required', 'payload')
            self.index.append(dict(zip(columns, deepcopy(params))))
            return Result([])
        raise AssertionError(query)


def module():
    assert importlib.util.find_spec('demo_archive_providers'), '内部归档安全 provider 桥接尚未实现'
    return importlib.import_module('demo_archive_providers')


def semantic(proof, sources, *, purpose, binding):
    return {'verified': True, 'purpose': purpose, 'scope': binding['scope'],
            'identity_hashes': binding['identity_hashes'],
            'projection_record_sha256': proof['record_sha256'],
            'source_record_hashes': {ref: row['record_sha256'] for ref, row in sources.items()}}


def setup_bridge(**options):
    archive = DemoEvidenceStore(MemorySQL(), 'account-a', 'stage-1000')
    refs = {}
    for role, kind in {'account': 'account_snapshot', 'positions': 'positions', 'normal': 'normal_orders',
                       'conditional': 'conditional_orders', 'fills': 'fills', 'income': 'cashflows',
                       'clock': 'clock', 'stage_opening': 'boundary', 'day_opening': 'boundary',
                       'market': 'equity_reconciliation', 'exchange_rules': 'equity_reconciliation'}.items():
        refs[role] = archive.append(kind, SCOPE, {'identity_hashes': IDENTITY, 'as_of_ms': 1100, 'items': []}, {})['ref']

    def projection(purpose, frozen_value, roles, **changes):
        sources = {role: refs[role] for role in roles}
        payload = {'schema': 'demo-safety-provider-v1', 'provider': purpose,
            'identity_hashes': IDENTITY, 'as_of_ms': 1100, 'value': frozen_value, 'source_refs': sources,
            'source_record_hashes': {ref: archive.read(ref)['record_sha256'] for ref in sources.values()}, **changes}
        return archive.append('equity_reconciliation', SCOPE, payload, {})['ref']

    account_context = context('account-a')
    account_context['approvals']['strategy-v1'].update(strategy_hash=IDENTITY['strategy_hash'],
        risk_policy_hash=IDENTITY['risk_policy_hash'], cost_model_hash=IDENTITY['cost_model_hash'])
    source = proposal(account_id='account-a', strategy_hash=IDENTITY['strategy_hash'],
        risk_policy_hash=IDENTITY['risk_policy_hash'], cost_model_hash=IDENTITY['cost_model_hash'],
        exit_params_hash=IDENTITY['execution_hash'])
    values = {'context': account_context, 'entry': source, 'protection': [],
              'completion': {'completion_refs': [], 'scan_through_ms': 1100}}
    roles = {'context': ['account', 'positions', 'normal', 'conditional', 'fills', 'income', 'clock', 'stage_opening', 'day_opening'],
             'entry': ['market', 'exchange_rules', 'clock'],
             'protection': ['positions', 'normal', 'conditional', 'fills', 'clock'],
             'completion': ['account', 'normal', 'conditional', 'fills', 'income']}
    selected = {purpose: projection(purpose, values[purpose], roles[purpose]) for purpose in roles}
    selectors = {purpose: (lambda _purpose=purpose, **kw: selected[_purpose]) for purpose in selected}
    options = {'semantic_validator': semantic, **options}
    bridge = module().ArchiveProviderBridge(archive, scope=SCOPE, identity_hashes=IDENTITY,
        selectors=selectors, max_age_ms={'context': 500, 'entry': 500, 'protection': 500, 'completion': 500}, **options)
    return bridge, archive, refs, selected, projection


def invoke(bridge, purpose='context', **extra):
    provider = getattr(bridge, purpose + '_provider' if purpose != 'entry' else 'entry_source_provider')
    return provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=1200, **extra)


def test_default_bridge_without_internal_configuration_returns_precise_unavailability():
    archive = DemoEvidenceStore(MemorySQL(), 'account-a', 'stage-1000')
    bridge = module().ArchiveProviderBridge(archive, scope=SCOPE, identity_hashes=IDENTITY)
    result = invoke(bridge)
    assert result['committed'] is False
    assert result['reason_codes'] == ['context_selector_unavailable']
    assert result['acceptance_eligible'] is False


def test_real_archive_linked_context_entry_and_protection_are_frozen_and_scoped():
    bridge, archive, _, selected, _ = setup_bridge()
    for purpose, key in [('context', 'context'), ('entry', 'source'), ('protection', 'items')]:
        result = invoke(bridge, purpose)
        assert result['committed'] is True
        assert result['archive_ref'] == selected[purpose]
        assert result['account_scope'] == 'account-a'
        assert result[key] == archive.read(selected[purpose])['payload']['value']
        assert result['acceptance_eligible'] is False
    result['items'].append('changed')
    assert invoke(bridge, 'protection')['items'] == []


def test_record_exists_and_self_declared_verified_bool_do_not_replace_semantic_authority():
    bridge, _, _, _, _ = setup_bridge(semantic_validator=None)
    assert invoke(bridge)['reason_codes'] == ['semantic_validator_unavailable']
    bridge, _, _, _, _ = setup_bridge(semantic_validator=lambda *a, **k: True)
    assert invoke(bridge)['reason_codes'] == ['semantic_proof_unverified']


@pytest.mark.parametrize('change', [
    {'identity_hashes': {**IDENTITY, 'code_hash': '9'*64}}, {'as_of_ms': 1}, {'as_of_ms': 1300},
    {'provider': 'entry'}, {'schema': 'http-caller-verified'}, {'value': None},
])
def test_incomplete_stale_or_mismatched_projection_cannot_become_authoritative(change):
    bridge, _, _, selected, projection = setup_bridge()
    selected['context'] = projection('context', context('account-a'),
        ['account', 'positions', 'normal', 'conditional', 'fills', 'income', 'clock', 'stage_opening', 'day_opening'], **change)
    assert invoke(bridge)['committed'] is False


def test_missing_or_wrong_kind_link_and_hash_binding_fail_closed():
    bridge, archive, refs, selected, projection = setup_bridge()
    selected['context'] = projection('context', context('account-a'), ['clock'])
    assert invoke(bridge)['reason_codes'] == ['context_dependencies_incomplete']
    roles = ['account', 'positions', 'normal', 'conditional', 'fills', 'income', 'clock', 'stage_opening', 'day_opening']
    selected['context'] = projection('context', context('account-a'), roles,
        source_record_hashes={ref: '9'*64 for ref in refs.values()})
    assert invoke(bridge)['committed'] is False
    selected['context'] = projection('context', context('account-a'), roles,
        source_refs={role: 'evidence:missing' if role == 'account' else refs[role] for role in roles})
    assert invoke(bridge)['committed'] is False
    assert archive.records()


def test_foreign_run_or_hash_in_raw_dependency_is_not_rebound_by_projection():
    bridge, archive, refs, selected, projection = setup_bridge()
    refs['account'] = archive.append('account_snapshot', {**SCOPE, 'run_id': 'other'},
                                     {'identity_hashes': IDENTITY}, {})['ref']
    selected['context'] = projection('context', context('account-a'),
        ['account', 'positions', 'normal', 'conditional', 'fills', 'income', 'clock', 'stage_opening', 'day_opening'])
    assert invoke(bridge)['committed'] is False


def test_archive_chain_damage_blocks_even_previously_selected_receipt():
    bridge, archive, _, _, _ = setup_bridge()
    archive.store.rows[-1]['record_sha256'] = '0'*64
    assert invoke(bridge)['reason_codes'] == ['archive_chain_or_reference_invalid']


def test_http_override_and_credential_scope_cannot_select_an_archive():
    bridge, _, _, _, _ = setup_bridge()
    assert invoke(bridge, ref='caller-ref')['reason_codes'] == ['provider_call_contract_invalid']
    with pytest.raises(ValueError):
        module().ArchiveProviderBridge(DemoEvidenceStore(MemorySQL(), 'account-a', 'stage-1000'),
            scope={**SCOPE, 'account_id': 'postgresql://alice:secret@private/db'}, identity_hashes=IDENTITY)


def completed(archive, *, change=None):
    proof = evidence()
    if change:
        change(proof)
    dependencies = {}
    for role, kind in {'account': 'account_snapshot', 'normal': 'normal_orders',
                       'conditional': 'conditional_orders', 'fills': 'fills', 'income': 'cashflows'}.items():
        dependencies[role] = archive.append(kind, SCOPE, {'identity_hashes': IDENTITY, 'items': []}, {})['ref']
    source = archive.append('equity_reconciliation', SCOPE,
        {'identity_hashes': IDENTITY, 'cost_evidence': proof, 'source_refs': dependencies,
         'source_record_hashes': {ref: archive.read(ref)['record_sha256'] for ref in dependencies.values()}}, {})
    trip = reconcile_round_trip_costs(proof).to_dict()['trips'][0]
    receipt = archive.append_completion(SCOPE, trip, [source['ref']])
    return source, trip, receipt


def test_completion_reads_real_index_and_rechecks_cost_proof_returning_stable_replays():
    bridge, archive, _, selected, projection = setup_bridge()
    _, trip, receipt = completed(archive)
    selected['completion'] = projection('completion', {'completion_refs': [receipt['ref']], 'scan_through_ms': 4000},
        ['account', 'normal', 'conditional', 'fills', 'income'], as_of_ms=4000)
    first = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=4100)
    second = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=4100)
    assert first['committed'] is True
    assert first['events'] == second['events']
    assert first['events'][0] == {**trip['risk_event'], 'complete': True, 'funding_complete': True}
    assert first['completion_sequence'] > 0


def test_completion_correction_blocks_without_silently_keeping_old_profitable_fact():
    bridge, archive, _, selected, projection = setup_bridge()
    completed(archive)
    def correction(proof):
        proof['income_rows'][-1]['amount'] = '-0.8'
    completed(archive, change=correction)
    selected['completion'] = projection('completion',
        {'completion_refs': [row['ref'] for row in archive.completion_records()], 'scan_through_ms': 4000},
        ['account', 'normal', 'conditional', 'fills', 'income'], as_of_ms=4000)
    result = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=4100)
    assert result['committed'] is False
    assert result['reason_codes'] == ['completion_correction_requires_review']


def test_empty_completion_index_without_fresh_semantic_coverage_cannot_claim_complete_costs():
    bridge, _, _, _, _ = setup_bridge()
    bridge.selectors.pop('completion')
    result = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=1200)
    assert result['committed'] is False
    assert result['reason_codes'] == ['completion_selector_unavailable']


def test_completion_index_reference_cannot_be_omitted_from_fresh_coverage_projection():
    bridge, archive, _, selected, projection = setup_bridge()
    completed(archive)
    selected['completion'] = projection('completion', {'completion_refs': [], 'scan_through_ms': 4000},
        ['account', 'normal', 'conditional', 'fills', 'income'], as_of_ms=4000)
    result = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=4100)
    assert result['committed'] is False
    assert result['reason_codes'] == ['completion_coverage_index_mismatch']


def test_fresh_empty_coverage_is_explicit_internal_proof_and_returns_zero_stable_events():
    bridge, _, _, _, _ = setup_bridge()
    result = bridge.completion_provider(account_scope='account-a', strategy_id='strategy-v1', now_ms=1200)
    assert result['committed'] is True
    assert result['events'] == []
    assert result['completion_sequence'] == 0
    assert result['acceptance_eligible'] is False


def acceptance_round():
    return {'round_id': 'round-1', 'environment': 'binance_demo', 'account_scope': 'account-a',
            'strategy_id': 'strategy-v1', 'rule_hash': IDENTITY['strategy_hash'],
            'execution_hash': IDENTITY['execution_hash'], 'status': 'active',
            'approval_id': 'approval-1', 'approved_by': 'operator-1'}


def test_archive_factory_requires_a_complete_acceptance_round_contract():
    bridge, archive, _, _, _ = setup_bridge()
    factory = module().archive_runtime_factory(bridge)
    ledger = Mock(store=archive.store, virtual_account_key='ns', environment='binance_demo')
    import threading
    runtime = factory(account_scope='account-a', ledger=ledger, adapter=Mock(), account_lock=threading.RLock(),
                      strategy_id='strategy-v1', acceptance_round=acceptance_round())
    assert runtime.account_scope == 'account-a'
    assert runtime.context_provider.__self__ is bridge


@pytest.mark.parametrize('missing', [
    'round_id', 'environment', 'account_scope', 'strategy_id', 'rule_hash',
    'execution_hash', 'status', 'approval_id', 'approved_by',
])
def test_archive_factory_rejects_acceptance_round_missing_required_identity(missing):
    bridge, archive, _, _, _ = setup_bridge()
    factory = module().archive_runtime_factory(bridge)
    ledger = Mock(store=archive.store, virtual_account_key='ns', environment='binance_demo')
    candidate = acceptance_round()
    candidate.pop(missing)
    import threading
    with pytest.raises(ValueError):
        factory(account_scope='account-a', ledger=ledger, adapter=Mock(), account_lock=threading.RLock(),
                strategy_id='strategy-v1', acceptance_round=candidate)


@pytest.mark.parametrize('change', [
    {'environment': 'production'}, {'account_scope': 'other-account'},
    {'strategy_id': 'other-strategy'}, {'rule_hash': '0' * 64},
    {'execution_hash': '0' * 64}, {'status': 'approved'},
    {'round_id': ''}, {'round_id': ' round-1'}, {'round_id': 'https://approval.example/round'},
    {'approval_id': 'https://approval.example/id'}, {'approved_by': 'operator\nforged'},
    {'approved_by': 'x' * 513},
])
def test_archive_factory_rejects_mismatched_or_malformed_acceptance_identity(change):
    bridge, archive, _, _, _ = setup_bridge()
    factory = module().archive_runtime_factory(bridge)
    ledger = Mock(store=archive.store, virtual_account_key='ns', environment='binance_demo')
    import threading
    with pytest.raises(ValueError):
        factory(account_scope='account-a', ledger=ledger, adapter=Mock(), account_lock=threading.RLock(),
                strategy_id='strategy-v1', acceptance_round={**acceptance_round(), **change})


def test_archive_factory_never_constructs_runtime_from_http_boolean_authority():
    bridge, archive, _, _, _ = setup_bridge()
    factory = module().archive_runtime_factory(bridge)
    ledger = Mock(store=archive.store, virtual_account_key='ns', environment='binance_demo')
    import threading
    with pytest.raises(ValueError):
        factory(account_scope='other', ledger=ledger, adapter=Mock(), account_lock=threading.RLock(),
                strategy_id='strategy-v1', acceptance_round=acceptance_round())
