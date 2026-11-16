"""通过真实证据链与离线采集原文验证观察投影，不授予午夜或交易资格。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import importlib.util
import logging

import pytest

from demo_collection_run import FrozenCollectionBinding
from demo_evidence_store import DemoEvidenceStore
from tests.test_demo_archive_providers import MemorySQL
from tests.test_demo_collection_run import EvidenceAdapter, IDENTITY, SCOPE, setup
from tests.test_demo_collection_run import rebuild_collection_rows
from tests.test_demo_account_collector import envelope


def module():
    assert importlib.util.find_spec('demo_equity_observation'), '账户观察生产器尚未实现'
    return importlib.import_module('demo_equity_observation')


def policy_spec(**changes):
    return {'schema': 'demo-equity-observation-policy-v1', 'account_api': 'fapiPrivateV3',
        'account_path': 'account', 'asset': 'USDT', 'multi_assets_margin': False,
        'equation': 'wallet_plus_unrealized_v1', 'money_scale': 24,
        'max_integer_digits': 18, 'max_assets': 64, 'max_positions': 2048, **changes}


def account():
    return {'totalWalletBalance': '999.123456789012345678901234',
        'totalUnrealizedProfit': '-3.000000000000000000000001',
        'totalMarginBalance': '996.123456789012345678901233',
        'assets': [{'asset': 'USDT', 'walletBalance': '999.123456789012345678901234',
            'unrealizedProfit': '-3.000000000000000000000001',
            'marginBalance': '996.123456789012345678901233'}],
        'positions': [{'symbol': 'BTCUSDT', 'positionSide': 'BOTH', 'positionAmt': '0.01',
            'unrealizedProfit': '-3.000000000000000000000001', 'isolatedWallet': '50',
            'isolatedMargin': '47', 'entryPrice': '60000'}], 'updateTime': 57_600_000}


class AccountAdapter(EvidenceAdapter):
    def __init__(self, value=None, mode=False):
        super().__init__()
        self.value, self.mode = deepcopy(account() if value is None else value), mode

    def fetch_account_snapshot(self, *, with_evidence):
        return envelope(self.value, 'account')

    def fetch_multi_assets_mode(self, *, with_evidence):
        return envelope(self.mode, 'multiAssetsMargin')


def observed(value=None, mode=False, *, spec=None):
    run, archive, _, binding = setup(adapter=AccountAdapter(value, mode))
    captured = run.capture_account()
    api = module()
    policy = api.FrozenObservationPolicy(binding, policy_spec() if spec is None else spec)
    producer = api.DemoEquityObservation(archive, binding, policy)
    return producer, archive, captured, binding, policy


def test_exact_observation_keeps_24_decimals_and_never_adds_isolated_collateral_or_costs():
    producer, archive, captured, _, policy = observed()
    before = deepcopy(archive.records())
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'computed'
    assert result['response_consistent'] is True
    assert result['amounts'] == {'wallet_balance_usdt': '999.123456789012345678901234',
        'unrealized_pnl_usdt': '-3.000000000000000000000001',
        'net_equity_usdt': '996.123456789012345678901233'}
    assert result['positions'][0]['isolatedWallet'] == '50'
    assert result['policy_sha256'] == policy.sha256
    assert result['scope'] == SCOPE and result['identity_hashes'] == IDENTITY
    assert result['source_refs']['manifest'] == captured['manifest_ref']
    assert archive.records() == before
    assert result['as_of'] is None and result['state_at_ms'] is None
    assert result['clock_bracket'] == {'clock_before': 60_000_001, 'clock_after': 60_000_002}
    for name in ('data_complete', 'reconciled', 'funding_finality_verified',
                 'boundary_verified', 'acceptance_eligible', 'production_authorized'):
        assert result[name] is False


def test_calculation_restarts_from_real_archive_and_does_not_write_or_access_any_adapter():
    producer, archive, captured, binding, policy = observed()
    expected = producer.calculate(captured['manifest_ref'])
    before = deepcopy(archive.records())
    def forbidden(*_args, **_kwargs):
        raise AssertionError('观察计算不允许写归档')
    archive.append = forbidden
    again = module().DemoEquityObservation(archive, binding, policy)
    assert again.calculate(captured['manifest_ref']) == expected
    assert archive.records() == before
    expected['positions'][0]['isolatedWallet'] = '0'
    assert again.calculate(captured['manifest_ref'])['positions'][0]['isolatedWallet'] == '50'


@pytest.mark.parametrize('field,value', [('totalWalletBalance', True),
    ('totalWalletBalance', 999), ('totalWalletBalance', 1.5),
    ('totalWalletBalance', 'NaN'), ('totalWalletBalance', '1e3'),
    ('totalWalletBalance', '01'), ('totalWalletBalance', '1.'),
    ('totalWalletBalance', '1.0000000000000000000000001'),
    ('totalWalletBalance', '1234567890123456789'),
    ('totalMarginBalance', '996.123456789012345678901234')])
def test_invalid_or_contradictory_account_money_has_no_computed_equity(field, value):
    raw = account()
    raw[field] = value
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'not_estimable'
    assert result['response_consistent'] is False and result['amounts'] is None
    assert result['reason_codes']


@pytest.mark.parametrize('mutation,reason', [
    ('missing_asset', 'usdt_asset_missing'), ('duplicate_asset', 'asset_duplicate'),
    ('missing_asset_upnl', 'asset_amount_missing'), ('asset_equation', 'asset_equity_mismatch'),
    ('asset_total', 'account_asset_mismatch'), ('foreign_asset', 'non_usdt_asset_nonzero'),
    ('duplicate_position', 'position_duplicate'), ('missing_position_upnl', 'position_amount_missing'),
    ('position_total', 'positions_unrealized_mismatch'),
    ('missing_positions', 'observation_capture_incomplete'),
    ('foreign_quote', 'position_symbol_unsupported'), ('multimode', 'multi_assets_mode_unsupported'),
])
def test_missing_duplicate_foreign_and_mode_facts_never_become_zero_or_complete(mutation, reason):
    raw = account()
    if mutation == 'missing_asset':
        raw['assets'] = []
    if mutation == 'duplicate_asset':
        raw['assets'].append(deepcopy(raw['assets'][0]))
    if mutation == 'missing_asset_upnl':
        del raw['assets'][0]['unrealizedProfit']
    if mutation == 'asset_equation':
        raw['assets'][0]['marginBalance'] = '0'
    if mutation == 'asset_total':
        raw['assets'][0].update(walletBalance='1000', marginBalance='996.999999999999999999999999')
    if mutation == 'foreign_asset':
        raw['assets'].append({'asset': 'BNB', 'walletBalance': '1', 'unrealizedProfit': '0', 'marginBalance': '1'})
    if mutation == 'duplicate_position':
        raw['positions'].append(deepcopy(raw['positions'][0]))
    if mutation == 'missing_position_upnl':
        del raw['positions'][0]['unrealizedProfit']
    if mutation == 'position_total':
        raw['positions'][0]['unrealizedProfit'] = '0'
    if mutation == 'missing_positions':
        del raw['positions']
    if mutation == 'foreign_quote':
        raw['positions'][0]['symbol'] = 'BNBBTC'
    producer, _, captured, _, _ = observed(raw, mutation == 'multimode')
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'not_estimable'
    assert result['reason_codes'] == [reason]
    assert result['amounts'] is None and result['data_complete'] is False


def test_explicit_zero_and_outside_strategy_symbol_are_preserved_without_flat_claim():
    raw = account()
    raw['positions'][0]['symbol'] = 'XRPUSDT'
    raw['assets'].append({'asset': 'BNB', 'walletBalance': '0', 'unrealizedProfit': '0', 'marginBalance': '0'})
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'computed'
    assert result['positions'][0]['symbol'] == 'XRPUSDT'
    assert 'flat' not in result and result['boundary_verified'] is False


def test_frozen_policy_and_binding_reject_drift_and_do_not_keep_mutable_input():
    api = module()
    binding = FrozenCollectionBinding(SCOPE, IDENTITY)
    spec = policy_spec()
    policy = api.FrozenObservationPolicy(binding, spec)
    initial_hash = policy.sha256
    spec['money_scale'] = 1
    assert policy.sha256 == initial_hash and policy.spec['money_scale'] == 24
    policy.spec['money_scale'] = 1
    assert policy.spec['money_scale'] == 24
    with pytest.raises((FrozenInstanceError, AttributeError)):
        policy._spec_items = ()
    _, archive, _, _, _ = observed()
    other = FrozenCollectionBinding({**SCOPE, 'run_id': 'other'}, IDENTITY)
    with pytest.raises(ValueError):
        api.DemoEquityObservation(archive, other, policy)


@pytest.mark.parametrize('change', [{'verified': True}, {'asset': 'BTC'}, {'money_scale': True},
    {'money_scale': 25}, {'max_integer_digits': 19}, {'max_assets': 0},
    {'max_positions': 0}, {'account_api': 'fapiPrivate'}, {'equation': 'add_isolated_margin'}])
def test_policy_rejects_extra_truth_flags_or_unsupported_numeric_contract(change):
    with pytest.raises(ValueError):
        module().FrozenObservationPolicy(FrozenCollectionBinding(SCOPE, IDENTITY), policy_spec(**change))


def test_missing_manifest_tampered_chain_and_foreign_scope_fail_without_new_rows():
    producer, archive, captured, binding, policy = observed()
    before = deepcopy(archive.records())
    with pytest.raises(module().ObservationError, match='observation_archive_unverified'):
        producer.calculate('evidence:missing')
    foreign = FrozenCollectionBinding({**SCOPE, 'run_id': 'foreign'}, IDENTITY)
    other = module().DemoEquityObservation(archive, foreign,
        module().FrozenObservationPolicy(foreign, policy_spec()))
    with pytest.raises(module().ObservationError, match='observation_binding_mismatch'):
        other.calculate(captured['manifest_ref'])
    assert archive.records() == before
    archive.store.rows[2]['body']['payload']['data'] = 'tampered'
    with pytest.raises(module().ObservationError, match='observation_archive_unverified'):
        producer.calculate(captured['manifest_ref'])


def test_income_manifest_cannot_be_substituted_for_account_observation():
    run, archive, _, binding = setup()
    captured = run.collect_income(since_ms=1, until_ms=2)
    api = module()
    producer = api.DemoEquityObservation(archive, binding, api.FrozenObservationPolicy(binding, policy_spec()))
    with pytest.raises(api.ObservationError, match='observation_capture_manifest_required'):
        producer.calculate(captured['manifest_ref'])


def test_operational_log_contains_bounded_diagnostics_without_amounts_or_malicious_raw(caplog):
    raw = account()
    raw['totalWalletBalance'] = 'password=private-observation-secret'
    producer, _, captured, _, _ = observed(raw)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='demo_equity_observation'):
        result = producer.calculate(captured['manifest_ref'])
    assert result['reason_codes'] == ['observation_amount_invalid']
    assert '账户观察' in caplog.text and 'observation_amount_invalid' in caplog.text
    for secret in ('private-observation-secret', 'exclusive-demo', '999.123', 'strategy-v1'):
        assert secret not in caplog.text


def test_persisted_observation_restarts_idempotently_and_is_not_a_safety_projection():
    producer, archive, captured, binding, policy = observed()
    expected = producer.calculate(captured['manifest_ref'])
    before = len(archive.records())
    receipt = producer.persist(captured['manifest_ref'])
    row = archive.read(receipt['ref'])
    assert row['kind'] == 'equity_reconciliation'
    assert row['payload'] == expected
    assert row['payload']['schema'] == 'demo-equity-observation-v1'
    assert 'collection_id' not in row['payload']
    again = module().DemoEquityObservation(archive, binding, policy)
    assert again.calculate(captured['manifest_ref']) == expected
    assert again.persist(captured['manifest_ref']) == receipt
    assert len(archive.records()) == before + 1


def test_persist_commit_failure_returns_no_receipt_and_no_secret_exception(caplog):
    producer, archive, captured, _, _ = observed()
    def fail(*_args, **_kwargs):
        raise RuntimeError('password=private-observation-secret')
    archive.append = fail
    with pytest.raises(module().ObservationError, match='observation_commit_failed'):
        producer.persist(captured['manifest_ref'])
    assert 'private-observation-secret' not in caplog.text


@pytest.mark.parametrize('part,field,value,reason', [
    ('account', 'availableBalance', 'NaN', 'observation_amount_invalid'),
    ('asset', 'crossWalletBalance', '1e3', 'observation_amount_invalid'),
    ('position', 'isolatedWallet', 'password=hidden', 'observation_amount_invalid'),
    ('position', 'entryPrice', True, 'observation_amount_invalid'),
    ('position', 'positionSide', [], 'position_side_invalid'),
    ('position', 'positionSide', 'LONG', None),
    ('account', 'verified', True, 'account_field_unsupported'),
    ('asset', 'verified', True, 'asset_field_unsupported'),
    ('position', 'verified', True, 'position_field_unsupported'),
])
def test_optional_official_fields_are_checked_and_truth_extras_cannot_pass_through(part, field, value, reason):
    raw = account()
    target = raw if part == 'account' else raw['assets' if part == 'asset' else 'positions'][0]
    target[field] = value
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    if reason is None:
        assert result['calculation_status'] == 'computed'
    else:
        assert result['reason_codes'] == [reason]
        assert result['amounts'] is None and result['positions'] == []


def test_frozen_resource_limit_and_money_precision_change_policy_hash_and_actual_result():
    producer, _, captured, binding, policy = observed()
    strict = module().FrozenObservationPolicy(binding, policy_spec(money_scale=23))
    assert strict.sha256 != policy.sha256
    limited = module().DemoEquityObservation(producer.archive, binding, strict)
    assert limited.calculate(captured['manifest_ref'])['reason_codes'] == ['observation_amount_invalid']
    raw = account()
    raw['assets'].append({'asset': 'BNB', 'walletBalance': '0', 'unrealizedProfit': '0', 'marginBalance': '0'})
    limited, _, captured, _, _ = observed(raw, spec=policy_spec(max_assets=1))
    assert limited.calculate(captured['manifest_ref'])['reason_codes'] == ['observation_resource_limit']


def test_bad_commit_receipt_cannot_be_returned_even_when_payload_exists():
    producer, archive, captured, _, _ = observed()
    append = archive.append
    def uncommitted(*args, **kwargs):
        return {**append(*args, **kwargs), 'committed': False}
    archive.append = uncommitted
    with pytest.raises(module().ObservationError, match='observation_commit_failed'):
        producer.persist(captured['manifest_ref'])


@pytest.mark.parametrize('payload', [None, [], 'private-bad-payload', True])
def test_valid_archive_chain_with_nonmapping_payload_has_bounded_error(payload):
    producer, archive, _, _, _ = observed()
    ref = archive.append('equity_reconciliation', SCOPE, payload, {})['ref']
    with pytest.raises(module().ObservationError, match='observation_capture_manifest_required'):
        producer.calculate(ref)


@pytest.mark.parametrize('change', ['derived_value', 'normal_params', 'clock_result'])
def test_legitimate_chain_with_wrong_collection_assembly_is_recomputed_and_rejected(change):
    _, archive, _, binding, policy = observed()
    other = DemoEvidenceStore(MemorySQL(), 'exclusive-demo', 'stage-1000')
    def mutate(kind, body, metadata):
        if change == 'derived_value' and kind == 'positions':
            body['data']['positions'] = []
        if change == 'normal_params' and kind == 'normal_orders':
            metadata['request_params'] = {'symbol': 'BTCUSDT'}
        if change == 'clock_result' and body.get('event') == 'end':
            body['result']['clock_bracket']['clock_before'] = 1
        return kind, metadata
    rebuild_collection_rows(archive.records(), other, mutate)
    ref = next(row['ref'] for row in other.records() if row['payload'].get('event') == 'end')
    producer = module().DemoEquityObservation(other, binding, policy)
    with pytest.raises(module().ObservationError, match='observation_archive_unverified'):
        producer.calculate(ref)


@pytest.mark.parametrize('value', ['1970-01-02T00:00:01Z', '1970-01-02T00:00:00', 'bad-time'])
def test_bad_request_interval_is_not_promoted_to_observation_time(value):
    adapter = AccountAdapter()
    original = adapter.fetch_account_snapshot
    def changed(**kwargs):
        response = original(**kwargs)
        response['request_evidence']['started_at_utc'] = value
        return response
    adapter.fetch_account_snapshot = changed
    run, archive, _, binding = setup(adapter=adapter)
    captured = run.capture_account()
    api = module()
    producer = api.DemoEquityObservation(archive, binding, api.FrozenObservationPolicy(binding, policy_spec()))
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'not_estimable' and result['amounts'] is None
    assert result['as_of'] is None and result['boundary_verified'] is False


def test_empty_positions_only_computes_when_explicit_unrealized_is_zero():
    raw = account()
    raw['positions'] = []
    raw['totalUnrealizedProfit'] = '0'
    raw['totalMarginBalance'] = raw['totalWalletBalance']
    raw['assets'][0].update(unrealizedProfit='0', marginBalance=raw['totalWalletBalance'])
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'computed' and result['positions'] == []
    assert result['amounts']['unrealized_pnl_usdt'] == '0'
    assert 'flat' not in result


def test_mixed_oneway_and_hedge_rows_cannot_form_consistent_account_response():
    raw = account()
    raw['positions'].append({'symbol': 'BTCUSDT', 'positionSide': 'LONG',
        'positionAmt': '1', 'unrealizedProfit': '0'})
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    assert result['reason_codes'] == ['position_mode_mixed']
    assert result['response_consistent'] is False and result['amounts'] is None


def test_calculate_whole_log_path_has_no_account_or_amount_identifiers(caplog):
    producer, _, captured, _, _ = observed()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        producer.calculate(captured['manifest_ref'])
    assert '账户观察复算完成' in caplog.text
    for secret in ('exclusive-demo', 'stage-1000', '999.123', 'strategy-v1', 'run-1'):
        assert secret not in caplog.text


def test_failed_capture_replay_logs_no_account_or_raw_failure_text(caplog):
    adapter = AccountAdapter()
    def fail(**_kwargs):
        raise RuntimeError('password=hidden-observation-value')
    adapter.fetch_open_orders = fail
    run, archive, _, binding = setup(adapter=adapter)
    captured = run.capture_account()
    api = module()
    producer = api.DemoEquityObservation(archive, binding, api.FrozenObservationPolicy(binding, policy_spec()))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = producer.calculate(captured['manifest_ref'])
    assert result['reason_codes'] == ['observation_capture_incomplete']
    assert '账户观察复算完成' in caplog.text
    for secret in ('exclusive-demo', 'stage-1000', 'hidden-observation-value', 'strategy-v1'):
        assert secret not in caplog.text


@pytest.mark.parametrize('part,field,value', [('account', 'canTrade', 'true'),
    ('account', 'updateTime', True), ('asset', 'marginAvailable', 'private-marker'),
    ('asset', 'updateTime', -1), ('position', 'isolated', 'private-marker'),
    ('position', 'marginType', 'private-marker'), ('position', 'updateTime', True)])
def test_non_amount_fields_are_typed_before_raw_rows_can_be_returned(part, field, value):
    raw = account()
    target = raw if part == 'account' else raw['assets' if part == 'asset' else 'positions'][0]
    target[field] = value
    producer, _, captured, _, _ = observed(raw)
    result = producer.calculate(captured['manifest_ref'])
    assert result['reason_codes'] == ['observation_field_type_invalid']
    assert result['amounts'] is None and result['positions'] == [] and result['assets'] == []


def test_behavioral_binding_and_policy_subclasses_are_rejected_before_archive_reads():
    api = module()
    class Binding(FrozenCollectionBinding):
        @property
        def sha256(self):
            return 'f' * 64
    class Policy(api.FrozenObservationPolicy):
        @property
        def spec(self):
            return policy_spec(money_scale=999)
    binding = FrozenCollectionBinding(SCOPE, IDENTITY)
    with pytest.raises(ValueError):
        api.FrozenObservationPolicy(Binding(SCOPE, IDENTITY), policy_spec())
    _, archive, _, _, _ = observed()
    with pytest.raises(ValueError):
        api.DemoEquityObservation(archive, binding, Policy(binding, policy_spec()))


def test_producer_copies_canonical_policy_and_binding_instead_of_retaining_mutable_owner():
    producer, _, captured, binding, policy = observed()
    before = producer.calculate(captured['manifest_ref'])
    object.__setattr__(policy, '_spec_items', tuple(policy_spec(money_scale=1).items()))
    object.__setattr__(binding, '_scope_items', tuple({**SCOPE, 'run_id': 'changed'}.items()))
    assert producer.calculate(captured['manifest_ref']) == before


def test_behavioral_decimal_string_is_not_permitted_to_override_numeric_parsing():
    api = module()
    producer, _, _, _, _ = observed()
    class Forged(str):
        def lstrip(self, *_args):
            return '1'
    with pytest.raises(api.ObservationError, match='observation_amount_invalid'):
        producer._units(Forged('999'))


def test_real_guarded_adapter_v3_account_contract_feeds_the_observation_offline():
    from binance_demo_adapter import BinanceDemoAdapter
    from tests.test_binance_demo_adapter import FakeClient
    client = FakeClient()
    raw = account()
    raw['untrusted_extra'] = 'must-not-pass-through-adapter'
    client.responses.update({('account', 'GET'): raw, ('time', 'GET'): {'serverTime': 60_000_000},
        ('multiAssetsMargin', 'GET'): {'multiAssetsMargin': False}})
    adapter = BinanceDemoAdapter(client=client, api_key='offline-key', api_secret='offline-secret')
    run, archive, _, binding = setup(adapter=adapter)
    captured = run.capture_account()
    assert captured['capture_complete'] is True
    assert [call[:3] for call in client.calls] == [
        ('time', 'fapiPublic', 'GET'), ('account', 'fapiPrivateV3', 'GET'),
        ('multiAssetsMargin', 'fapiPrivate', 'GET'), ('openOrders', 'fapiPrivate', 'GET'),
        ('openAlgoOrders', 'fapiPrivate', 'GET'), ('time', 'fapiPublic', 'GET')]
    api = module()
    producer = api.DemoEquityObservation(archive, binding, api.FrozenObservationPolicy(binding, policy_spec()))
    result = producer.calculate(captured['manifest_ref'])
    assert result['calculation_status'] == 'computed'
    assert result['amounts']['net_equity_usdt'] == '996.123456789012345678901233'
    assert 'untrusted_extra' not in str(result)
