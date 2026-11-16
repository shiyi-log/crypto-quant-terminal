"""只用离线账户响应和内存提交回执，验证真实采集编排行为。"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json

import pytest

from demo_account_collector import DailyBoundarySlot, DemoAccountCollector
from demo_evidence_store import DemoEvidenceStore
from binance_demo_adapter import normalize_conditional_order, normalize_order
from tests.test_binance_demo_adapter import BTC, order
from tests.test_binance_demo_native_orders import algo

SCOPE = {'environment': 'binance_demo', 'account_id': 'exclusive-demo', 'stage_id': 'stage-1000'}
MIDNIGHT = 57_600_000


def row(identity, stamp, amount='1', kind='TRANSFER', asset='USDT', symbol=''):
    return {'tranId': identity, 'time': stamp, 'income': amount, 'incomeType': kind,
            'asset': asset, 'symbol': symbol}


def envelope(data, path, params=None):
    return {'data': deepcopy(data), 'request_evidence': {
        'api': 'fapiPublic' if path == 'time' else 'fapiPrivateV3' if path == 'account' else 'fapiPrivate',
        'path': path, 'method': 'GET', 'status': 'ok', 'request_params': params or {},
        'response_sha256': 'a' * 64, 'response_hash_basis': 'canonical_json',
        'started_at_utc': '1970-01-02T00:00:00Z', 'received_at_utc': '1970-01-02T00:00:00Z'}}


class Archive:
    def __init__(self):
        self.records = []
        self.by_ref = {}
        self.fail = False
        self.receipt_change = None

    def append(self, kind, scope, payload, request_evidence):
        if self.fail:
            raise RuntimeError('must never log signature=secret-token')
        body = {'kind': kind, 'scope': deepcopy(scope), 'payload': deepcopy(payload),
                'request_evidence': deepcopy(request_evidence)}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        receipt = {'ref': 'evidence:' + digest, 'sha256': digest, 'record_sha256': digest,
                   'sequence': len(self.records) + 1, 'committed': True, 'kind': kind, 'scope': deepcopy(scope)}
        if self.receipt_change:
            receipt.update(self.receipt_change)
        self.records.append(body)
        self.by_ref[receipt['ref']] = {**body, **receipt}
        return receipt

    def read(self, ref):
        return deepcopy(self.by_ref[ref])


class Adapter:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.clock = 60_000_000

    def fetch_income_window(self, *, since_ms, until_ms, limit, with_evidence):
        assert with_evidence is True
        self.calls.append((since_ms, until_ms, limit))
        values = [x for x in self.rows if since_ms <= x['time'] <= until_ms][:limit]
        return envelope(values, 'income', {'startTime': since_ms, 'endTime': until_ms, 'limit': limit})

    def fetch_server_time(self, *, with_evidence):
        assert with_evidence is True
        self.clock += 1
        return envelope(self.clock, 'time')

    def fetch_account_snapshot(self, *, with_evidence):
        assert with_evidence is True
        return envelope({'totalWalletBalance': '1000', 'totalUnrealizedProfit': '0',
                         'totalMarginBalance': '1000', 'assets': [{'asset': 'USDT',
                         'walletBalance': '1000', 'unrealizedProfit': '0', 'marginBalance': '1000'}],
                         'positions': [], 'updateTime': MIDNIGHT}, 'account')

    def fetch_multi_assets_mode(self, *, with_evidence):
        assert with_evidence is True
        return envelope(False, 'multiAssetsMargin')

    def fetch_open_orders(self, *, with_evidence):
        assert with_evidence is True
        return envelope([], 'openOrders')

    def fetch_open_conditional_orders(self, *, with_evidence):
        assert with_evidence is True
        return envelope([], 'openAlgoOrders')


def collector(rows=(), **kwargs):
    adapter, archive = Adapter(rows), Archive()
    return DemoAccountCollector(adapter, archive, SCOPE, clock_ms=lambda: 60_000_000, **kwargs), adapter, archive


def test_replay_diagnostics_are_instance_local_and_live_capture_keeps_scoped_log(caplog):
    import logging
    live, _, _ = collector()
    replay, _, _ = collector(readonly_replay=True)
    with caplog.at_level(logging.INFO):
        replay.capture_account()
    assert '离线采集重放诊断' in caplog.text
    assert 'exclusive-demo' not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        live.capture_account()
    assert '账户=exclusive-demo' in caplog.text and '阶段=stage-1000' in caplog.text


def test_full_windows_split_into_contiguous_short_leaves_and_parent_is_not_counted_twice():
    c, adapter, archive = collector([row(1, 1), row(2, 2), row(3, 4)])
    result = c.collect_income(since_ms=1, until_ms=4, limit=2)
    assert result['complete'] is True
    assert [(x['start_ms'], x['end_ms']) for x in result['income_scans']] == [(1, 1), (2, 2), (3, 4)]
    assert [x['tranId'] for x in result['rows']] == [1, 2, 3]
    assert len(archive.records) == 5
    assert len(adapter.calls) == 5
    assert result['acceptance_eligible'] is False
    assert all(x['pages'][0]['page'] == 1 and x['pages'][0]['has_more'] is False for x in result['income_scans'])


def test_single_millisecond_saturation_cannot_skip_rows_or_invent_empty_next_page():
    c, adapter, _ = collector([row(1, 1), row(2, 1), row(3, 1)])
    result = c.collect_income(since_ms=1, until_ms=1, limit=2)
    assert result['complete'] is False
    assert result['reasons'] == ['income_single_millisecond_saturated']
    assert result['income_scans'] == []
    assert adapter.calls == [(1, 1, 2)]


def test_long_window_uses_strictly_less_than_seven_day_contiguous_chunks():
    c, adapter, _ = collector()
    result = c.collect_income(since_ms=1, until_ms=604_800_001, limit=1000)
    assert result['complete'] is True
    assert adapter.calls == [(1, 604_800_000, 1000), (604_800_001, 604_800_001, 1000)]


def test_unknown_income_asset_and_external_symbols_are_archived_and_never_discarded():
    c, _, archive = collector([row(1, 1, kind='NEW_BONUS', asset='BNB', symbol='XRPUSDT')])
    result = c.collect_income(since_ms=1, until_ms=2)
    assert result['complete'] is False
    assert set(result['reasons']) == {'income_type_unclassified', 'income_asset_unsupported'}
    assert result['rows'][0]['symbol'] == 'XRPUSDT'
    assert archive.records[0]['payload']['rows'][0]['incomeType'] == 'NEW_BONUS'


@pytest.mark.parametrize('rows, reason', [
    ([row(1, 1), row(1, 1)], 'income_transaction_duplicate'),
    ([row(1, 1), row(1, 1, amount='2')], 'income_transaction_conflict'),
])
def test_duplicate_transaction_identity_is_not_double_counted(rows, reason):
    c, _, _ = collector(rows)
    result = c.collect_income(since_ms=1, until_ms=2)
    assert result['complete'] is False
    assert reason in result['reasons']
    assert len(result['rows']) == 1


def test_same_transaction_id_in_two_income_types_is_distinct():
    c, _, _ = collector([row(1, 1), row(1, 2, kind='COMMISSION', amount='-1')])
    assert c.collect_income(since_ms=1, until_ms=2)['complete'] is True


def test_parent_observed_transaction_cannot_disappear_from_all_leaf_windows():
    c, adapter, _ = collector()
    def changing(*, since_ms, until_ms, limit, with_evidence):
        values = [row(1, 1), row(2, 2)] if (since_ms, until_ms) == (1, 4) else []
        return envelope(values, 'income', {'startTime': since_ms, 'endTime': until_ms, 'limit': limit})
    adapter.fetch_income_window = changing
    result = c.collect_income(since_ms=1, until_ms=4, limit=2)
    assert result['complete'] is False
    assert result['reasons'] == ['income_window_changed_during_capture']


def test_duplicate_identity_even_in_saturated_parent_is_not_accepted_as_consistent():
    c, adapter, _ = collector([row(1, 1), row(2, 2)])
    original = adapter.fetch_income_window
    def duplicated(**kwargs):
        value = original(**kwargs)
        if kwargs['since_ms'] == 1 and kwargs['until_ms'] == 4:
            value['data'] = [row(1, 1), row(1, 1)]
        return value
    adapter.fetch_income_window = duplicated
    result = c.collect_income(since_ms=1, until_ms=4, limit=2)
    assert result['complete'] is False
    assert 'income_transaction_duplicate' in result['reasons']


@pytest.mark.parametrize('change', [{'committed': False}, {'kind': 'positions'},
                                  {'scope': {**SCOPE, 'account_id': 'other'}}, {'sha256': 'bad'}])
def test_uncommitted_or_wrong_scope_receipt_never_supplies_income_evidence(change):
    c, _, archive = collector()
    archive.receipt_change = change
    result = c.collect_income(since_ms=1, until_ms=2)
    assert result['complete'] is False
    assert result['income_scans'] == []
    assert 'archive_commit_unverified' in result['reasons']


def test_archive_failure_retains_block_and_safe_reason_without_exception_text(caplog):
    c, _, archive = collector()
    archive.fail = True
    result = c.collect_income(since_ms=1, until_ms=2)
    assert result['complete'] is False
    assert result['reasons'] == ['archive_commit_failed']
    assert 'secret-token' not in caplog.text


def test_request_budget_bounds_full_window_recursion_without_false_success():
    c, adapter, _ = collector([row(1, 1), row(2, 2)], max_requests=1)
    result = c.collect_income(since_ms=1, until_ms=4, limit=1)
    assert result['complete'] is False
    assert 'income_request_budget_exhausted' in result['reasons']
    assert len(adapter.calls) == 1


def test_unbounded_long_window_cannot_allocate_millions_of_pending_chunks():
    c, adapter, _ = collector(max_requests=2)
    result = c.collect_income(since_ms=0, until_ms=1_814_400_000)
    assert result['complete'] is False
    assert result['reasons'] == ['income_request_budget_exhausted']
    assert adapter.calls == []


@pytest.mark.parametrize('start,end,limit', [(True, 2, 1), (2, 1, 1), (0, 1, 0), (0, 1, 1001), (-1, 2, 1)])
def test_invalid_windows_fail_before_network(start, end, limit):
    c, adapter, archive = collector()
    with pytest.raises(ValueError):
        c.collect_income(since_ms=start, until_ms=end, limit=limit)
    assert adapter.calls == []
    assert archive.records == []


def test_rest_capture_archives_all_components_but_never_invents_exact_midnight_asof():
    c, _, archive = collector()
    result = c.capture_account()
    assert set(result['source_refs']) == {'clock_before', 'clock_after', 'account_snapshot',
                                         'positions', 'mode', 'normal_orders', 'conditional_orders'}
    assert result['capture_complete'] is True
    assert result['reconciled'] is False
    assert result['as_of_ms'] is None
    assert result['boundary_verified'] is False
    assert result['transport_evidence_complete'] is True
    assert result['acceptance_eligible'] is False
    assert [r['kind'] for r in archive.records].count('clock') == 2


def test_capture_partial_failure_preserves_committed_refs_without_turning_missing_orders_into_empty():
    c, adapter, _ = collector()
    def fail(*, with_evidence):
        assert with_evidence is True
        raise RuntimeError('signature=secret-token')
    adapter.fetch_open_orders = fail
    result = c.capture_account()
    assert result['capture_complete'] is False
    assert 'normal_orders' not in result['source_refs']
    assert 'conditional_orders' in result['source_refs']
    assert result['reasons'] == ['normal_orders_capture_failed']


def test_malformed_order_list_cannot_be_archived_as_successful_empty_read():
    c, adapter, _ = collector()
    adapter.fetch_open_orders = lambda **_: envelope(None, 'openOrders')
    result = c.capture_account()
    assert result['capture_complete'] is False
    assert 'normal_orders' not in result['source_refs']
    assert result['reasons'] == ['normal_orders_capture_failed']


def proof(at=MIDNIGHT):
    return {'kind': 'boundary', 'scope': SCOPE, 'payload': {
        'method': 'direct_exchange_asof', 'boundary_ms': at, 'state_at_ms': at,
        'valuation_policy_hash': 'c' * 64, 'coverage_complete': True,
        'source_refs': ['evidence:anchor'], 'source_record_hashes': {'evidence:anchor': 'e' * 64},
        'wallet_balance_usdt': '1000',
        'unrealized_pnl_usdt': '0', 'net_equity_usdt': '1000'},
        'receipt': {'committed': True, 'ref': 'evidence:boundary', 'scope': SCOPE,
                    'kind': 'boundary', 'sha256': 'b' * 64, 'record_sha256': 'd' * 64, 'sequence': 8}}


def anchor():
    return {'kind': 'account_snapshot', 'scope': SCOPE, 'payload': {'trusted_test_anchor': True},
            'ref': 'evidence:anchor', 'committed': True, 'sha256': 'f' * 64,
            'record_sha256': 'e' * 64, 'sequence': 1}


def resolve_proof(value):
    return lambda ref: anchor() if ref == 'evidence:anchor' else deepcopy(value)


def semantic_validator(value, sources):
    # 仅受控离线假资料使用；生产须注入真正的时点、覆盖和估值验证器。
    receipt = value.get('receipt', value)
    payload = value['payload']
    return {'verified': True, 'scope': value['scope'],
            'proof_record_sha256': receipt['record_sha256'],
            'source_record_hashes': {ref: item.get('receipt', item)['record_sha256'] for ref, item in sources.items()},
            'method': payload['method'], 'state_at_ms': payload['state_at_ms'],
            'valuation_policy_hash': payload['valuation_policy_hash']}


def test_boundary_missing_nearby_rest_and_http_proof_flags_cannot_unlock_entries():
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    assert slot.status(now_ms=MIDNIGHT - 1)['status'] == 'pending'
    assert slot.status(now_ms=MIDNIGHT)['status'] == 'missing'
    with pytest.raises(ValueError):
        slot.fulfill(proof(), resolver=lambda _: proof())
    assert slot.status(now_ms=MIDNIGHT + 1)['entries_allowed'] is False


@pytest.mark.parametrize('change', [
    {'state_at_ms': MIDNIGHT + 1}, {'coverage_complete': False}, {'method': 'nearest_rest'},
    {'net_equity_usdt': '1001'}, {'valuation_policy_hash': 'bad'}, {'source_refs': []},
])
def test_resolved_boundary_requires_exact_state_and_complete_immutable_proof(change):
    value = proof()
    value['payload'].update(change)
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    with pytest.raises(ValueError):
        slot.fulfill('evidence:boundary', resolver=resolve_proof(value), proof_validator=semantic_validator)
    assert slot.status(now_ms=MIDNIGHT)['entries_allowed'] is False


def test_trusted_resolver_boundary_is_idempotent_and_changed_proof_cannot_overwrite():
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    value = proof()
    first = slot.fulfill('evidence:boundary', resolver=resolve_proof(value), proof_validator=semantic_validator)
    assert first['status'] == 'verified'
    assert slot.fulfill('evidence:boundary', resolver=resolve_proof(value), proof_validator=semantic_validator) == first
    value['payload']['wallet_balance_usdt'] = '1001'
    value['payload']['net_equity_usdt'] = '1001'
    with pytest.raises(ValueError):
        slot.fulfill('evidence:boundary', resolver=resolve_proof(value), proof_validator=semantic_validator)
    assert slot.status(now_ms=MIDNIGHT)['status'] == 'conflict'
    assert slot.status(now_ms=MIDNIGHT)['entries_allowed'] is False


def test_real_archive_flat_read_receipt_contract_can_supply_verified_boundary():
    nested = proof()
    flattened = {key: value for key, value in nested.items() if key != 'receipt'}
    flattened.update(nested['receipt'])
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    result = slot.fulfill('evidence:boundary', resolver=resolve_proof(flattened), proof_validator=semantic_validator)
    assert result['status'] == 'verified'


def test_scope_and_midnight_validation_precedes_any_collection_or_fulfillment():
    for scope in ({**SCOPE, 'environment': 'live'}, {**SCOPE, 'account_id': ''}):
        with pytest.raises(ValueError):
            DemoAccountCollector(Adapter(), Archive(), scope)
    with pytest.raises(ValueError):
        DailyBoundarySlot(SCOPE, MIDNIGHT + 1)


def risk_snapshot(at, source_ref):
    return {**SCOPE, 'timestamp_ms': at, 'source': 'exchange_account_snapshot',
            'source_ref': source_ref, 'reconciled': True, 'positions_complete': True,
            'mark_prices_complete': True, 'multi_assets_margin': False, 'asset': 'USDT',
            'wallet_balance_usdt': '1000', 'unrealized_pnl_usdt': '0', 'net_equity_usdt': '1000'}


def test_risk_projection_requires_verified_archive_refs_not_rest_capture_flags():
    c, _, archive = collector()
    income = c.collect_income(since_ms=1, until_ms=60_000_000)
    resolved = {}
    for ref, at, kind in (('evidence:start', 0, 'equity_reconciliation'),
                          ('evidence:day', MIDNIGHT, 'boundary'),
                          ('evidence:current', 60_000_000, 'equity_reconciliation')):
        resolved[ref] = {'kind': kind, 'scope': SCOPE,
            'payload': {**risk_snapshot(at, 'evidence:anchor'), 'coverage_complete': True,
                        'method': 'direct_exchange_asof', 'state_at_ms': at,
                        'source_refs': ['evidence:anchor'], 'source_record_hashes': {'evidence:anchor': 'e' * 64},
                        'valuation_policy_hash': 'c' * 64},
            'ref': ref, 'committed': True, 'sha256': 'a' * 64, 'record_sha256': 'b' * 64, 'sequence': 1}
    result = c.build_risk_evidence(stage_start_ms=0, stage_opening_ref='evidence:start',
        day_opening_ref='evidence:day', current_ref='evidence:current', income=income,
        resolver=lambda ref: anchor() if ref == 'evidence:anchor' else resolved[ref] if ref in resolved else archive.read(ref),
        proof_validator=semantic_validator,
        now_ms=60_000_000, max_snapshot_age_ms=5000)
    assert result['data_complete'] is True
    assert result['margin_balance'] == '1000'
    resolved['evidence:day']['payload']['method'] = 'nearest_rest'
    result = c.build_risk_evidence(stage_start_ms=0, stage_opening_ref='evidence:start',
        day_opening_ref='evidence:day', current_ref='evidence:current', income=income,
        resolver=lambda ref: anchor() if ref == 'evidence:anchor' else resolved[ref] if ref in resolved else archive.read(ref),
        proof_validator=semantic_validator,
        now_ms=60_000_000, max_snapshot_age_ms=5000)
    assert result['data_complete'] is False
    assert result['reasons'] == ['archived_risk_projection_unverified']


def test_replayed_boundary_is_not_relabelled_as_raw_exchange_snapshot_for_old_reducer():
    c, _, _ = collector()
    income = c.collect_income(since_ms=1, until_ms=60_000_000)
    value = {'kind': 'boundary', 'scope': SCOPE, 'payload': {
        **risk_snapshot(MIDNIGHT, 'evidence:day'), 'method': 'replayed_boundary_projection',
        'coverage_complete': True, 'state_at_ms': MIDNIGHT}, 'committed': True,
        'ref': 'evidence:day', 'sha256': 'a' * 64, 'record_sha256': 'b' * 64, 'sequence': 1}
    result = c.build_risk_evidence(stage_start_ms=0, stage_opening_ref='evidence:day',
        day_opening_ref='evidence:day', current_ref='evidence:day', income=income,
        resolver=lambda _: value, now_ms=60_000_000, max_snapshot_age_ms=5000)
    assert result['data_complete'] is False
    assert result['reasons'] == ['archived_risk_projection_unverified']


class SqlResult:
    def __init__(self, values):
        self.values = deepcopy(values)

    def fetchall(self):
        return self.values


class MemoryArchiveSql:
    """实际DemoEvidenceStore事务所需的有界内存SQL边界，不连接数据库。"""
    def __init__(self):
        self.rows = []

    def _json(self, value):
        return deepcopy(value)

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, query, params=()):
        if 'pg_advisory' in query:
            return SqlResult([])
        if query.startswith('SELECT * FROM demo_evidence_archive'):
            return SqlResult(sorted([item for item in self.rows
                if (item['environment'], item['account_id']) == params], key=lambda item: item['account_sequence']))
        if query.startswith('INSERT INTO demo_evidence_archive'):
            columns = ('environment', 'account_id', 'stage_id', 'account_sequence', 'ref', 'kind',
                       'capture_key', 'sha256', 'previous_hash', 'record_sha256', 'body')
            self.rows.append(dict(zip(columns, deepcopy(params))))
            return SqlResult([])
        raise AssertionError('unexpected archive SQL')


def real_archive():
    return DemoEvidenceStore(MemoryArchiveSql(), SCOPE['account_id'], SCOPE['stage_id'])


def test_real_archive_boundary_rejects_missing_cross_scope_or_changed_link_and_plain_read():
    archive = real_archive()
    value = proof()['payload']
    receipt = archive.append('boundary', SCOPE, value, {})
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    with pytest.raises(ValueError):
        slot.fulfill(receipt['ref'], resolver=archive.read)
    with pytest.raises(ValueError):
        slot.fulfill(receipt['ref'], resolver=archive.read, proof_validator=semantic_validator)
    assert slot.status(now_ms=MIDNIGHT)['entries_allowed'] is False


@pytest.mark.parametrize('response', [True, {'verified': True}, False])
def test_boundary_validator_boolean_or_unbound_declaration_cannot_unlock_entries(response):
    value = proof()
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    with pytest.raises(ValueError):
        slot.fulfill('evidence:boundary', resolver=resolve_proof(value),
                     proof_validator=lambda *_: response)
    assert slot.status(now_ms=MIDNIGHT)['entries_allowed'] is False


@pytest.mark.parametrize('change', ['scope', 'hash', 'kind', 'commit'])
def test_boundary_every_link_must_match_frozen_hash_scope_type_and_commit(change):
    value, source = proof(), anchor()
    if change == 'scope':
        source['scope'] = {**SCOPE, 'account_id': 'other'}
    elif change == 'hash':
        source['record_sha256'] = '9' * 64
    elif change == 'kind':
        source['kind'] = 'acceptance'
    else:
        source['committed'] = False
    slot = DailyBoundarySlot(SCOPE, MIDNIGHT)
    with pytest.raises(ValueError):
        slot.fulfill('evidence:boundary', resolver=lambda ref: source if ref == 'evidence:anchor' else value,
                     proof_validator=semantic_validator)
    assert slot.status(now_ms=MIDNIGHT)['entries_allowed'] is False


def test_real_content_addressed_archive_projection_injects_receipt_ref_after_read_without_mutation():
    archive = real_archive()
    c = DemoAccountCollector(Adapter(), archive, SCOPE, clock_ms=lambda: 60_000_000)
    income = c.collect_income(since_ms=1, until_ms=60_000_000)
    source = archive.append('account_snapshot', SCOPE, {'test_only_asof_anchor': True}, {})
    refs, original = {}, {}
    for label, at, kind in (('start', 0, 'equity_reconciliation'),
                            ('day', MIDNIGHT, 'boundary'),
                            ('current', 60_000_000, 'equity_reconciliation')):
        value = {**risk_snapshot(at, source['ref']), 'method': 'direct_exchange_asof',
                 'coverage_complete': True, 'state_at_ms': at, 'valuation_policy_hash': 'c' * 64,
                 'source_refs': [source['ref']], 'source_record_hashes': {source['ref']: source['record_sha256']}}
        refs[label] = archive.append(kind, SCOPE, value, {})['ref']
        original[label] = archive.read(refs[label])
        assert value['source_ref'] != refs[label]
    result = c.build_risk_evidence(stage_start_ms=0, stage_opening_ref=refs['start'],
        day_opening_ref=refs['day'], current_ref=refs['current'], income=income, resolver=archive.read,
        proof_validator=semantic_validator, now_ms=60_000_000, max_snapshot_age_ms=5000)
    assert result['data_complete'] is True
    assert {label: archive.read(ref) for label, ref in refs.items()} == original


def test_real_nonempty_adapter_orders_archive_latency_as_exact_metadata_without_amount_float():
    archive, adapter = real_archive(), Adapter()
    ordinary = normalize_order(order(), BTC, 1.25)
    conditional = normalize_conditional_order(algo(), BTC, 2.75)
    adapter.fetch_open_orders = lambda **_: envelope([ordinary], 'openOrders')
    adapter.fetch_open_conditional_orders = lambda **_: envelope([conditional], 'openAlgoOrders')
    result = DemoAccountCollector(adapter, archive, SCOPE).capture_account()
    assert result['capture_complete'] is True
    saved = archive.read(result['source_refs']['normal_orders'])['payload']['data'][0]
    assert saved['request_latency_ms'] == '1.25'
    assert saved['qty'] == '0.010'
    assert ordinary['request_latency_ms'] == 1.25
    saved = archive.read(result['source_refs']['conditional_orders'])['payload']['data'][0]
    assert saved['request_latency_ms'] == '2.75'
    assert saved['quantity'] == '0.002'


def test_non_metadata_float_order_amount_remains_rejected_by_real_archive():
    archive, adapter = real_archive(), Adapter()
    adapter.fetch_open_orders = lambda **_: envelope([{'quantity': 0.01, 'request_latency_ms': 1.25}], 'openOrders')
    result = DemoAccountCollector(adapter, archive, SCOPE).capture_account()
    assert result['capture_complete'] is False
    assert 'normal_orders' not in result['source_refs']


@pytest.mark.parametrize('identity', ['postgresql://alice:hunter2@private-db:5432/trades',
                                      'alice:hunter2@private-db', 'https://server/path'])
def test_scope_rejects_connection_and_userinfo_before_network_or_sensitive_log(identity, caplog):
    adapter, archive = Adapter(), Archive()
    for field in ('account_id', 'stage_id'):
        scope = {**SCOPE, field: identity}
        with pytest.raises(ValueError):
            DemoAccountCollector(adapter, archive, scope)
        with pytest.raises(ValueError):
            DailyBoundarySlot(scope, MIDNIGHT)
    assert adapter.calls == []
    assert archive.records == []
    assert 'hunter2' not in caplog.text


@pytest.mark.parametrize('component,path', [('fetch_open_orders', 'openOrders'),
                                           ('fetch_open_conditional_orders', 'openAlgoOrders')])
@pytest.mark.parametrize('change', [
    {'started_at_utc': 'missing-timezone'}, {'received_at_utc': '1969-01-01T00:00:00Z'},
    {'extra': 'not-allowed'}, {'response_sha256': 'bad'}, {'latency_ms': '-1'},
    {'request_params': {'symbol': 'BTCUSDT'}},
])
def test_invalid_order_transport_metadata_never_becomes_complete_even_with_legacy_archive(component, path, change):
    c, adapter, archive = collector()
    value = envelope([], path)
    value['request_evidence'].update(change)
    setattr(adapter, component, lambda **_: value)
    result = c.capture_account()
    assert result['capture_complete'] is False
    assert result['transport_evidence_complete'] is False
    assert result['reasons'] == ['request_evidence_unverified']
    assert not any(r['request_evidence'].get('path') == path for r in archive.records)


def test_adapter_collection_error_text_is_never_returned_or_persisted_as_reason(caplog):
    from demo_account_collector import CollectionError
    c, adapter, _ = collector()
    def fail(**_):
        raise CollectionError('signature=private-upstream-value')
    adapter.fetch_open_orders = fail
    result = c.capture_account()
    assert result['reasons'] == ['normal_orders_capture_failed']
    assert 'private-upstream-value' not in str(result)
    assert 'private-upstream-value' not in caplog.text
