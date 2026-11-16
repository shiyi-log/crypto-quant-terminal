"""运行清单测试使用真实不可变归档，SQL 边界替换为不联网的内存实现。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import importlib.util

import pytest

from demo_account_collector import CollectionError, DemoAccountCollector
from demo_evidence_store import DemoEvidenceStore, HASHES
from tests.test_demo_account_collector import Adapter, SCOPE as LEGACY_SCOPE, envelope, row
from tests.test_demo_archive_providers import MemorySQL

SCOPE = {**LEGACY_SCOPE, 'strategy_version_id': 'strategy-v1', 'run_id': 'run-1'}
IDENTITY = dict(zip(HASHES, (str(i) * 64 for i in range(1, 6))))


def api():
    assert importlib.util.find_spec('demo_collection_run'), '冻结采集运行及持久清单尚未实现'
    return importlib.import_module('demo_collection_run')


class EvidenceAdapter(Adapter):
    def __init__(self, rows=()):
        super().__init__(rows)
        self.reads = []

    def fetch_open_orders(self, *, with_evidence):
        assert with_evidence is True
        self.reads.append('normal')
        return envelope([], 'openOrders')

    def fetch_open_conditional_orders(self, *, with_evidence):
        assert with_evidence is True
        self.reads.append('conditional')
        return envelope([], 'openAlgoOrders')


def setup(*, scope=None, hashes=None, collection_id='capture-1', sql=None, adapter=None):
    archive = DemoEvidenceStore(sql or MemorySQL(), 'exclusive-demo', 'stage-1000')
    binding = api().FrozenCollectionBinding(scope or SCOPE, hashes or IDENTITY)
    adapter = adapter or EvidenceAdapter()
    run = api().DemoCollectionRun(adapter, archive, binding, collection_id=collection_id,
                                  clock_ms=lambda: 60_000_000)
    return run, archive, adapter, binding


def manifest_rows(archive):
    return [value for value in archive.records()
            if value['payload'].get('schema') == 'demo-collection-manifest-v1']


def test_binding_cannot_change_after_creation_or_through_exported_dicts():
    scope, hashes = deepcopy(SCOPE), deepcopy(IDENTITY)
    binding = api().FrozenCollectionBinding(scope, hashes)
    scope['run_id'], hashes['code_hash'] = 'other', 'f' * 64
    binding.scope['run_id'] = 'other'
    binding.identity_hashes['code_hash'] = 'f' * 64
    assert binding.scope == SCOPE
    assert binding.identity_hashes == IDENTITY
    with pytest.raises((FrozenInstanceError, AttributeError)):
        binding._scope_items = ()


@pytest.mark.parametrize('field', list(SCOPE))
def test_missing_scope_cannot_be_inferred_from_archive_or_adapter(field):
    scope = {key: value for key, value in SCOPE.items() if key != field}
    with pytest.raises(ValueError):
        api().FrozenCollectionBinding(scope, IDENTITY)


@pytest.mark.parametrize('field', list(IDENTITY))
def test_missing_hash_cannot_be_inferred(field):
    hashes = {key: value for key, value in IDENTITY.items() if key != field}
    with pytest.raises(ValueError):
        api().FrozenCollectionBinding(SCOPE, hashes)


@pytest.mark.parametrize('scope,hashes', [
    ({**SCOPE, 'environment': 'binance_live'}, IDENTITY),
    ({**SCOPE, 'run_id': 'postgres://private'}, IDENTITY),
    ({**SCOPE, 'run_id': 'user@host'}, IDENTITY),
    ({**SCOPE, 'verified': True}, IDENTITY),
    (SCOPE, {**IDENTITY, 'code_hash': 'F' * 64}),
    (SCOPE, {**IDENTITY, 'verified': True}),
])
def test_untrusted_fields_or_unsafe_identity_fail_before_archive_write(scope, hashes):
    with pytest.raises(ValueError):
        api().FrozenCollectionBinding(scope, hashes)


def test_bound_capture_seals_exact_receipts_and_transport_without_semantic_claims():
    run, archive, adapter, binding = setup()
    result = run.capture_account()
    facts = [r for r in archive.records() if r['payload'].get('collection_component_index')]
    manifests = manifest_rows(archive)
    assert [r['payload']['event'] for r in manifests] == ['start'] + ['component'] * 7 + ['end']
    assert len(facts) == 7
    assert adapter.reads == ['normal', 'conditional']
    assert result['scope'] == SCOPE
    assert result['identity_hashes'] == IDENTITY
    assert result['collection_status'] == 'completed'
    assert result['capture_complete'] is True
    assert result['transport_evidence_complete'] is True
    assert result['as_of'] is None and result['as_of_ms'] is None
    assert all(result[key] is False for key in ('reconciled', 'boundary_verified', 'acceptance_eligible'))
    assert all(r['scope'] == SCOPE and r['payload']['identity_hashes'] == IDENTITY for r in archive.records())
    end = archive.read(result['manifest_ref'])['payload']
    assert end['binding_sha256'] == binding.sha256
    assert end['start_ref'] == manifests[0]['ref']
    assert [r['ref'] for r in end['components']] == [r['ref'] for r in facts]
    assert [r['record_sha256'] for r in end['components']] == [r['record_sha256'] for r in facts]
    assert [r['sha256'] for r in end['components']] == [r['sha256'] for r in facts]
    assert [r['sequence'] for r in end['components']] == [r['sequence'] for r in facts]
    positions = next(r for r in facts if r['kind'] == 'positions')
    assert positions['request_evidence'] == {}
    assert positions['payload']['data']['source_ref'] == result['source_refs']['account_snapshot']
    assert positions['payload']['data']['derived_from_account'] is True


def test_restart_replays_sealed_facts_without_new_requests_or_archive_writes():
    run, archive, adapter, binding = setup()
    first = run.capture_account()
    before = deepcopy(archive.records())
    class MustNotRead:
        def __getattr__(self, _name):
            raise AssertionError('重启后不允许重复远端采集')
    restarted = api().DemoCollectionRun(MustNotRead(), archive, binding, collection_id='capture-1')
    assert restarted.capture_account() == first
    assert archive.records() == before
    assert adapter.reads == ['normal', 'conditional']


def test_partial_failure_archives_bounded_failure_then_seals_remaining_facts(caplog):
    adapter = EvidenceAdapter()
    def fail(**_):
        raise RuntimeError('signature=private-secret-value')
    adapter.fetch_open_orders = fail
    run, archive, _, _ = setup(adapter=adapter)
    result = run.capture_account()
    assert result['collection_status'] == 'failed'
    assert result['capture_complete'] is False
    assert result['transport_evidence_complete'] is False
    assert result['reasons'] == ['normal_orders_capture_failed']
    assert 'normal_orders' not in result['source_refs']
    assert 'conditional_orders' in result['source_refs']
    failures = [r['payload'] for r in manifest_rows(archive) if r['payload']['event'] == 'failure']
    assert [r['reason_code'] for r in failures] == ['normal_orders_capture_failed']
    assert 'private-secret-value' not in caplog.text
    assert 'private-secret-value' not in str(archive.records())


def test_income_windows_are_bound_and_saturated_parent_receipts_remain_in_manifest():
    run, archive, _, _ = setup(adapter=EvidenceAdapter([row(1, 1), row(2, 2), row(3, 4)]))
    result = run.collect_income(since_ms=1, until_ms=4, limit=2)
    assert result['complete'] is True
    assert result['settlement_finality_verified'] is False
    facts = [r for r in archive.records() if r['kind'] == 'cashflows']
    assert len(facts) == 5
    assert len(archive.read(result['manifest_ref'])['payload']['components']) == 5
    assert all(r['scope'] == SCOPE for r in facts)
    assert result['acceptance_eligible'] is False


@pytest.mark.parametrize('changed', ['scope', 'hashes', 'operation', 'parameters'])
def test_collection_id_cannot_be_reused_for_other_binding_or_operation(changed):
    run, archive, adapter, binding = setup()
    run.collect_income(since_ms=1, until_ms=2)
    if changed == 'scope':
        binding = api().FrozenCollectionBinding({**SCOPE, 'run_id': 'run-2'}, IDENTITY)
    if changed == 'hashes':
        binding = api().FrozenCollectionBinding(SCOPE, {**IDENTITY, 'code_hash': 'f' * 64})
    restarted = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1')
    before = deepcopy(archive.records())
    with pytest.raises(CollectionError, match='collection_identity_conflict'):
        if changed == 'operation':
            restarted.capture_account()
        else:
            restarted.collect_income(since_ms=1, until_ms=3 if changed == 'parameters' else 2)
    assert archive.records() == before


def test_legacy_three_field_facts_are_not_backfilled_or_reused_by_bound_run():
    run, archive, adapter, _ = setup()
    legacy = DemoAccountCollector(adapter, archive, LEGACY_SCOPE, clock_ms=lambda: 60_000_000)
    old = legacy.capture_account()
    original = deepcopy(archive.records())
    result = run.capture_account()
    assert set(old['source_refs'].values()).isdisjoint(result['source_refs'].values())
    assert archive.records()[:len(original)] == original
    assert all(r['scope'] == LEGACY_SCOPE and 'identity_hashes' not in r['payload'] for r in original)


class FailManifestSQL(MemorySQL):
    def __init__(self, event):
        super().__init__()
        self.event = event

    def execute(self, query, params=()):
        if query.startswith('INSERT INTO demo_evidence_archive') and params[-1]['payload'].get('event') == self.event:
            raise RuntimeError('password=private-secret-value')
        return super().execute(query, params)


@pytest.mark.parametrize('event,minimum_facts', [('start', 0), ('component', 1), ('failure', 6), ('end', 7)])
def test_manifest_commit_failure_preserves_facts_and_restart_requires_new_collection_id(event, minimum_facts, caplog):
    sql = FailManifestSQL(event)
    adapter = EvidenceAdapter()
    if event == 'failure':
        adapter.fetch_open_orders = lambda **_: (_ for _ in ()).throw(RuntimeError('private-secret-value'))
    run, archive, _, binding = setup(sql=sql, adapter=adapter)
    with pytest.raises(CollectionError, match='collection_manifest_commit_failed'):
        run.capture_account()
    facts = [r for r in archive.records() if r['payload'].get('collection_component_index')]
    assert len(facts) >= minimum_facts
    assert not any(r['payload'].get('event') == 'end' for r in archive.records())
    if event != 'start':
        before = deepcopy(archive.records())
        restarted = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1')
        with pytest.raises(CollectionError, match='collection_incomplete_requires_new_id'):
            restarted.capture_account()
        assert archive.records() == before
    assert 'private-secret-value' not in caplog.text


def test_binding_requires_real_committed_archive_and_matching_account_before_network():
    binding = api().FrozenCollectionBinding(SCOPE, IDENTITY)
    with pytest.raises(ValueError):
        api().DemoCollectionRun(EvidenceAdapter(), object(), binding, collection_id='capture-1')
    wrong = DemoEvidenceStore(MemorySQL(), 'other-account', 'stage-1000')
    with pytest.raises(ValueError):
        api().DemoCollectionRun(EvidenceAdapter(), wrong, binding, collection_id='capture-1')


def test_tampered_archive_chain_never_replays_sealed_success():
    sql = MemorySQL()
    run, archive, adapter, binding = setup(sql=sql)
    run.capture_account()
    sql.rows[2]['body']['payload']['data'] = 'changed'
    restarted = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.capture_account()


def test_restart_replays_sealed_failure_without_retrying_missing_component():
    adapter = EvidenceAdapter()
    adapter.fetch_open_orders = lambda **_: (_ for _ in ()).throw(RuntimeError('不可重试'))
    run, archive, _, binding = setup(adapter=adapter)
    first = run.capture_account()
    adapter.fetch_open_orders = lambda **_: pytest.fail('失败封存也不能重发请求')
    restarted = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1')
    assert restarted.capture_account() == first


@pytest.mark.parametrize('change', ['components_hash', 'components_order', 'component_events',
                                  'start_hash', 'status', 'as_of', 'eligible', 'omitted_source'])
def test_valid_archive_chain_with_invalid_end_links_or_truth_flags_cannot_replay(change):
    class HoldEndSQL(MemorySQL):
        end_payload = None
        def execute(self, query, params=()):
            if query.startswith('INSERT INTO demo_evidence_archive') and params[-1]['payload'].get('event') == 'end':
                self.end_payload = deepcopy(params[-1]['payload'])
                raise RuntimeError('离线阻止最终清单以注入错误组装')
            return super().execute(query, params)
    sql = HoldEndSQL()
    run, archive, adapter, binding = setup(sql=sql)
    with pytest.raises(CollectionError, match='collection_manifest_commit_failed'):
        run.capture_account()
    end = deepcopy(sql.end_payload)
    if change == 'components_hash':
        end['components'][0]['record_sha256'] = 'f' * 64
    elif change == 'components_order':
        end['components'][0], end['components'][1] = end['components'][1], end['components'][0]
    elif change == 'component_events':
        end['component_events'].pop()
    elif change == 'start_hash':
        end['start_record_sha256'] = 'f' * 64
    elif change == 'status':
        end['status'] = 'failed'
    elif change == 'as_of':
        end['result']['as_of_ms'] = 60_000_000
    elif change == 'eligible':
        end['result']['acceptance_eligible'] = True
    elif change == 'omitted_source':
        end['result']['source_refs'].pop('normal_orders')
    sql.execute = lambda query, params=(): MemorySQL.execute(sql, query, params)
    archive.append('equity_reconciliation', SCOPE, end, {}, stable_key='invalid-end')
    assert archive.records()[-1]['payload'] == end
    restarted = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.capture_account()


def test_income_budget_failure_seals_no_component_as_failure_and_never_semantic_success():
    _, archive, adapter, binding = setup()
    run = api().DemoCollectionRun(adapter, archive, binding, collection_id='capture-1', max_requests=1)
    result = run.collect_income(since_ms=0, until_ms=604_800_000)
    assert result['complete'] is False
    assert result['collection_status'] == 'failed'
    assert result['reasons'] == ['income_request_budget_exhausted']
    assert archive.read(result['manifest_ref'])['payload']['components'] == []


def test_raw_commit_failure_seals_failure_without_turning_unread_snapshot_into_empty():
    class FailRawSQL(MemorySQL):
        def execute(self, query, params=()):
            if query.startswith('INSERT INTO demo_evidence_archive') and params[-1]['kind'] == 'normal_orders':
                raise RuntimeError('签名上游异常不记录')
            return super().execute(query, params)
    run, archive, _, _ = setup(sql=FailRawSQL())
    result = run.capture_account()
    assert result['collection_status'] == 'failed'
    assert result['reasons'] == ['archive_commit_failed']
    assert 'normal_orders' not in result['source_refs']
    assert len(archive.read(result['manifest_ref'])['payload']['components']) == 6


def rebuild_collection_rows(rows, archive, mutate):
    """通过真实 append 重建有效链，只更改指定错误组装字段。"""
    mapping = {}
    for index, original in enumerate(rows):
        def rewrite(value):
            if isinstance(value, dict):
                fields = ('ref', 'sha256', 'record_sha256', 'sequence', 'committed', 'kind', 'scope')
                if set(fields) <= set(value) and value['ref'] in mapping:
                    return deepcopy(mapping[value['ref']])
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, str) and value in mapping:
                return mapping[value]['ref']
            return value
        body = rewrite(deepcopy(original['payload']))
        if body.get('event') == 'end':
            body['start_record_sha256'] = mapping[rows[0]['ref']]['record_sha256']
        if original['kind'] == 'positions':
            source = mapping[original['payload']['data']['source_ref']]
            body['data']['source_record_sha256'] = source['record_sha256']
        kind, metadata = mutate(original['kind'], body, deepcopy(original['request_evidence']))
        mapping[original['ref']] = archive.append(kind, SCOPE, body, metadata, stable_key=f'rebuilt:{index}')


@pytest.mark.parametrize('change', ['result_false_integer', 'end_false_integer', 'start_false_integer'])
def test_zero_integer_cannot_substitute_fixed_false_semantic_flags(change):
    run, archive, adapter, binding = setup()
    run.capture_account()
    other = DemoEvidenceStore(MemorySQL(), 'exclusive-demo', 'stage-1000')
    def mutate(kind, body, metadata):
        if change == 'start_false_integer' and body.get('event') == 'start':
            body['acceptance_eligible'] = 0
        if change == 'end_false_integer' and body.get('event') == 'end':
            body['acceptance_eligible'] = 0
        if change == 'result_false_integer' and body.get('event') == 'end':
            body['result']['acceptance_eligible'] = 0
        return kind, metadata
    rebuild_collection_rows(archive.records(), other, mutate)
    restarted = api().DemoCollectionRun(adapter, other, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.capture_account()


@pytest.mark.parametrize('change', ['component_kind', 'mode_path', 'normal_params', 'derived_hash', 'derived_value'])
def test_semantically_unverified_but_structurally_wrong_component_cannot_be_replayed(change):
    run, archive, adapter, binding = setup()
    run.capture_account()
    other = DemoEvidenceStore(MemorySQL(), 'exclusive-demo', 'stage-1000')
    def mutate(kind, body, metadata):
        name = body.get('capture_component')
        if change == 'component_kind' and name == 'account_snapshot':
            kind = 'equity_reconciliation'
            metadata = {}
        if change == 'mode_path' and name == 'mode':
            metadata['path'] = 'positionSide/dual'
        if change == 'normal_params' and name == 'normal_orders':
            metadata['request_params'] = {'symbol': 'BTCUSDT'}
        if change == 'derived_hash' and name == 'positions':
            body['data']['source_record_sha256'] = 'f' * 64
        if change == 'derived_value' and name == 'positions':
            body['data']['positions'] = [{'symbol': 'XRPUSDT', 'positionAmt': '1'}]
        return kind, metadata
    rebuild_collection_rows(archive.records(), other, mutate)
    restarted = api().DemoCollectionRun(adapter, other, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.capture_account()


@pytest.mark.parametrize('change', ['flat_account', 'flat_environment', 'clock_bracket'])
def test_result_identity_and_clock_projection_must_come_from_linked_account_facts(change):
    run, archive, adapter, binding = setup()
    run.capture_account()
    other = DemoEvidenceStore(MemorySQL(), 'exclusive-demo', 'stage-1000')
    def mutate(kind, body, metadata):
        if body.get('event') == 'end':
            result = body['result']
            if change == 'flat_account':
                result['account_id'] = 'wrong-account'
            elif change == 'flat_environment':
                result['environment'] = 'binance_live'
            else:
                result['clock_bracket'] = {}
        return kind, metadata
    rebuild_collection_rows(archive.records(), other, mutate)
    restarted = api().DemoCollectionRun(adapter, other, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.capture_account()


@pytest.mark.parametrize('change', ['no_cashflows', 'rows', 'scans', 'window', 'flat_account'])
def test_income_replay_rebuilds_window_and_rows_from_actual_cashflow_sources(change):
    run, archive, adapter, binding = setup(adapter=EvidenceAdapter([row(1, 1)]))
    run.collect_income(since_ms=1, until_ms=2)
    rows = deepcopy(archive.records())
    other = DemoEvidenceStore(MemorySQL(), 'exclusive-demo', 'stage-1000')
    if change == 'no_cashflows':
        rows = [r for r in rows if r['payload'].get('event') in {'start', 'end'}]
    def mutate(kind, body, metadata):
        if body.get('event') == 'end':
            result = body['result']
            if change == 'no_cashflows':
                for key in ('components', 'component_events'):
                    body[key] = []
                for key in ('source_refs', 'income_scans', 'rows'):
                    result[key] = []
            elif change == 'rows':
                result['rows'] = []
            elif change == 'scans':
                result['income_scans'] = []
            elif change == 'window':
                result['end_ms'] = 3
            else:
                result['account_id'] = 'other-account'
        return kind, metadata
    rebuild_collection_rows(rows, other, mutate)
    restarted = api().DemoCollectionRun(adapter, other, binding, collection_id='capture-1')
    with pytest.raises(CollectionError, match='collection_archive_unverified'):
        restarted.collect_income(since_ms=1, until_ms=2)


@pytest.mark.parametrize('methods', [
    ['fetch_server_time'], ['fetch_account_snapshot'], ['fetch_multi_assets_mode'],
    ['fetch_open_conditional_orders'], ['fetch_account_snapshot', 'fetch_open_orders'],
])
def test_all_requested_component_failure_positions_remain_replayable_without_network(methods):
    adapter = EvidenceAdapter()
    def fail(**_):
        raise RuntimeError('固定离线失败')
    for name in methods:
        setattr(adapter, name, fail)
    run, archive, _, binding = setup(adapter=adapter)
    first = run.capture_account()
    assert first['collection_status'] == 'failed'
    restarted = api().DemoCollectionRun(object(), archive, binding, collection_id='capture-1')
    assert restarted.capture_account() == first


@pytest.mark.parametrize('rows,limit', [([row(1, 1), row(2, 1)], 1),
                                      ([row(1, 1, kind='UNKNOWN')], 1000),
                                      ([row(1, 1), row(1, 1)], 1000)])
def test_income_structural_failure_seals_and_replays_original_bounded_reasons(rows, limit):
    run, archive, _, binding = setup(adapter=EvidenceAdapter(rows))
    first = run.collect_income(since_ms=1, until_ms=1, limit=limit)
    assert first['collection_status'] == 'failed'
    restarted = api().DemoCollectionRun(object(), archive, binding, collection_id='capture-1')
    assert restarted.collect_income(since_ms=1, until_ms=1, limit=limit) == first


@pytest.mark.parametrize('attribute,value', [('_binding', None), ('collection_id', 'other'),
                                            ('max_requests', 1), ('archive', None), ('adapter', None)])
def test_run_collection_identity_and_resources_cannot_change_after_creation(attribute, value):
    run, _, _, _ = setup()
    with pytest.raises((AttributeError, FrozenInstanceError)):
        setattr(run, attribute, value)


def test_account_failure_then_transport_failure_keeps_each_exact_fixed_reason_on_replay():
    adapter = EvidenceAdapter()
    adapter.fetch_account_snapshot = lambda **_: (_ for _ in ()).throw(RuntimeError('账户读取失败'))
    bad = envelope([], 'openOrders')
    bad['request_evidence']['received_at_utc'] = 'bad'
    adapter.fetch_open_orders = lambda **_: bad
    run, archive, _, binding = setup(adapter=adapter)
    first = run.capture_account()
    assert first['reasons'] == ['account_snapshot_capture_failed', 'positions_capture_failed',
                                'request_evidence_unverified']
    restarted = api().DemoCollectionRun(object(), archive, binding, collection_id='capture-1')
    assert restarted.capture_account() == first


@pytest.mark.parametrize('kind', ['clock', 'positions', 'account_snapshot', 'conditional_orders', 'cashflows'])
def test_every_raw_commit_failure_kind_has_replayable_failed_manifest(kind):
    class RejectKindSQL(MemorySQL):
        def execute(self, query, params=()):
            if query.startswith('INSERT INTO demo_evidence_archive') and params[-1]['kind'] == kind:
                raise RuntimeError('固定归档失败')
            return super().execute(query, params)
    run, archive, _, binding = setup(sql=RejectKindSQL())
    if kind == 'cashflows':
        first = run.collect_income(since_ms=1, until_ms=2)
    else:
        first = run.capture_account()
        if kind == 'clock':
            assert first['clock_bracket'] == {}
    assert first['collection_status'] == 'failed'
    assert 'archive_commit_failed' in first['reasons']
    restarted = api().DemoCollectionRun(object(), archive, binding, collection_id='capture-1')
    if kind == 'cashflows':
        assert restarted.collect_income(since_ms=1, until_ms=2) == first
    else:
        assert restarted.capture_account() == first
