"""账户只读归档入口：合成响应不赋予对账或精确时点证明。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from binance_demo_adapter import (
    BinanceDemoAdapter, DemoSafetyError, _build_client, _request_allowed,
)
from tests.test_binance_demo_adapter import FakeClient


def income(**changes):
    return {'symbol': 'BTCUSDT', 'incomeType': 'FUNDING_FEE',
            'income': '-0.123456789012345678901234567890', 'asset': 'USDT',
            'time': 1200, 'info': '', 'tranId': '12345678901234567890',
            'tradeId': '', **changes}


def account(**changes):
    return {'feeTier': 1, 'canTrade': True, 'canDeposit': True, 'canWithdraw': True,
            'totalWalletBalance': '1000.0000000000000000001',
            'totalUnrealizedProfit': '-0.0000000000000000001',
            'totalMarginBalance': '1000.0000000000000000000',
            'assets': [{'asset': 'USDT', 'walletBalance': '1000.0000000000000000001',
                        'unrealizedProfit': '-0.0000000000000000001',
                        'marginBalance': '1000.0000000000000000000', 'updateTime': 100}],
            'positions': [{'symbol': 'SOLUSDT', 'positionAmt': '1.2',
                           'positionSide': 'BOTH', 'updateTime': 101}], **changes}


@pytest.fixture
def boundary():
    client = FakeClient()
    client.responses[('income', 'GET')] = [income()]
    client.responses[('multiAssetsMargin', 'GET')] = {'multiAssetsMargin': False}
    client.responses[('time', 'GET')] = {'serverTime': 1234}
    client.responses[('account', 'GET')] = account()
    adapter = BinanceDemoAdapter(client=client, api_key='offline-key', api_secret='offline-secret')
    return adapter, client


def test_income_window_is_all_type_all_symbol_and_preserves_exact_original_values(boundary):
    adapter, client = boundary
    rows = [income(), income(symbol='SOLUSDT', asset='BNB', incomeType='FUTURE_UNKNOWN',
                             tranId=456, time=1500, income='0.0010000000000000001'),
            income(symbol='', incomeType='TRANSFER', tranId='567', time=1000, income='100')]
    client.responses[('income', 'GET')] = rows
    result = adapter.fetch_income_window(since_ms=1000, until_ms=2000, limit=100)
    assert result == rows
    assert client.calls == [('income', 'fapiPrivate', 'GET',
                             {'startTime': 1000, 'endTime': 2000, 'limit': 100})]
    assert client.configs == [{'cost': 30}]
    assert adapter._attempts == {}
    assert adapter._owned_client_ids == set()
    assert adapter.last_call['rate_limit_cost'] == 30


@pytest.mark.parametrize('changes', [
    {'since_ms': True}, {'since_ms': -1}, {'since_ms': 1000.0}, {'until_ms': '2000'},
    {'until_ms': 999}, {'until_ms': 1000 + 7 * 86400000},
    {'limit': 0}, {'limit': 1001}, {'limit': True}, {'limit': 1.0},
])
def test_bad_income_window_rejected_before_network(boundary, changes):
    adapter, client = boundary
    fields = {'since_ms': 1000, 'until_ms': 2000, 'limit': 1000, **changes}
    with pytest.raises(DemoSafetyError):
        adapter.fetch_income_window(**fields)
    assert client.calls == []


@pytest.mark.parametrize('raw', [
    {}, [None], [income(time=999)], [income(time=2001)], [income(time=True)],
    [income(tranId='')], [income(tranId=None)], [income(tranId=False)],
    [income(incomeType='')], [income(asset='')], [income(income='NaN')],
    [income(income=True)], [income(income='Infinity')], [income(symbol=None)],
])
def test_bad_income_response_is_not_a_valid_archive(boundary, raw):
    adapter, client = boundary
    client.responses[('income', 'GET')] = raw
    with pytest.raises(DemoSafetyError):
        adapter.fetch_income_window(since_ms=1000, until_ms=2000)


def test_zero_width_income_window_keeps_all_events_at_the_exact_millisecond(boundary):
    adapter, client = boundary
    client.responses[('income', 'GET')] = [income(time=1000)]
    assert adapter.fetch_income_window(since_ms=1000, until_ms=1000) == [income(time=1000)]


def test_empty_income_window_is_valid_but_not_inferred_completeness(boundary):
    adapter, client = boundary
    client.responses[('income', 'GET')] = []
    assert adapter.fetch_income_window(since_ms=1000, until_ms=2000) == []
    assert not any(key in adapter.last_call for key in ('complete', 'reconciled', 'as_of'))


@pytest.mark.parametrize('raw', [{}, {'multiAssetsMargin': 'false'},
                                  {'multiAssetsMargin': 0}, {'multiAssetsMargin': None}])
def test_multi_assets_mode_requires_explicit_boolean(boundary, raw):
    adapter, client = boundary
    client.responses[('multiAssetsMargin', 'GET')] = raw
    with pytest.raises(DemoSafetyError):
        adapter.fetch_multi_assets_mode()


@pytest.mark.parametrize('mode', [False, True])
def test_multi_assets_mode_preserves_true_and_false_without_configuration_write(boundary, mode):
    adapter, client = boundary
    client.responses[('multiAssetsMargin', 'GET')] = {'multiAssetsMargin': mode}
    assert adapter.fetch_multi_assets_mode() is mode
    assert client.calls == [('multiAssetsMargin', 'fapiPrivate', 'GET', {})]
    assert adapter.last_call['rate_limit_cost'] == 30


@pytest.mark.parametrize('server_time', [0, 1234567890123])
def test_server_time_is_public_nonnegative_integer(boundary, server_time):
    _, client = boundary
    client.responses[('time', 'GET')] = {'serverTime': server_time}
    adapter = BinanceDemoAdapter(client=client)
    assert adapter.fetch_server_time() == server_time
    assert client.calls == [('time', 'fapiPublic', 'GET', {})]
    assert adapter.last_call['rate_limit_cost'] == 1


@pytest.mark.parametrize('raw', [{}, {'serverTime': True}, {'serverTime': -1},
                                 {'serverTime': 1.0}, {'serverTime': '1234'}])
def test_server_time_cannot_be_inferred_or_coerced(boundary, raw):
    adapter, client = boundary
    client.responses[('time', 'GET')] = raw
    with pytest.raises(DemoSafetyError):
        adapter.fetch_server_time()


def test_account_snapshot_retains_raw_amount_precision_and_foreign_exposure(boundary):
    adapter, client = boundary
    raw = account()
    assert adapter.fetch_account_snapshot() == raw
    assert client.calls == [('account', 'fapiPrivateV3', 'GET', {})]
    assert adapter.last_call['rate_limit_cost'] == 5
    assert adapter.protection_capabilities()['acceptance_eligible'] is False


def test_account_read_discards_synthetic_reconciliation_and_time_claims(boundary):
    adapter, client = boundary
    client.responses[('account', 'GET')] = account(
        data_complete=True, funding_fee_complete=True, as_of_ms=1234,
        risk_day_opening={'complete': True}, cumulative_external_net_flow_usdt='0',
        reconciled=True, risk_observation_id='fake', apiKey='should-redact')
    result = adapter.fetch_account_snapshot()
    assert result == account()
    assert 'as_of' not in result


@pytest.mark.parametrize('raw', [
    {}, account(totalWalletBalance='NaN'), account(totalMarginBalance=True),
    account(totalUnrealizedProfit=None), account(assets={}), account(positions={}),
    account(assets=[None]), account(positions=[None]),
    account(assets=[{'asset': 'USDT', 'walletBalance': 'Infinity'}]),
    account(positions=[{'symbol': 'BTCUSDT', 'positionAmt': 'NaN'}]),
])
def test_malformed_account_snapshot_is_rejected(boundary, raw):
    adapter, client = boundary
    client.responses[('account', 'GET')] = raw
    with pytest.raises(DemoSafetyError):
        adapter.fetch_account_snapshot()


def test_capture_envelope_is_atomic_safe_request_evidence_not_a_reconciliation(boundary):
    adapter, client = boundary
    raw = [income(apiKey='sensitive-key')]
    client.responses[('income', 'GET')] = raw
    result = adapter.fetch_income_window(since_ms=1000, until_ms=2000, with_evidence=True)
    assert result['data'] == [income()]
    evidence = result['request_evidence']
    assert evidence['path'] == 'income'
    assert evidence['api'] == 'fapiPrivate'
    assert evidence['method'] == 'GET'
    assert evidence['request_params'] == {'startTime': 1000, 'endTime': 2000, 'limit': 1000}
    assert evidence['response_hash_basis'] == 'canonical_json'
    canonical = json.dumps([income()], ensure_ascii=False, sort_keys=True,
                           allow_nan=False, separators=(',', ':')).encode()
    assert evidence['response_sha256'] == hashlib.sha256(canonical).hexdigest()
    assert evidence['received_at_utc'].endswith('Z')
    assert 'sensitive-key' not in str(result)
    assert not any(key in evidence for key in ('complete', 'reconciled', 'as_of'))


def test_real_transport_digest_is_not_replaced_by_canonical_fallback(boundary):
    adapter, client = boundary
    original = client.request
    def request(*args, **kwargs):
        result = original(*args, **kwargs)
        client._quant_last_response_sha256 = 'a' * 64
        client._quant_last_response_hash_basis = 'raw_http_response_bytes'
        return result
    client.request = request
    result = adapter.fetch_account_snapshot(with_evidence=True)
    assert result['request_evidence']['response_sha256'] == 'a' * 64
    assert result['request_evidence']['response_hash_basis'] == 'raw_http_response_bytes'


def test_stale_digest_from_previous_request_is_not_attached_to_current_read(boundary):
    adapter, client = boundary
    client._quant_last_response_sha256 = 'b' * 64
    client._quant_last_response_hash_basis = 'raw_http_response_bytes'
    result = adapter.fetch_server_time(with_evidence=True)
    assert result['request_evidence']['response_hash_basis'] == 'canonical_json'
    assert result['request_evidence']['response_sha256'] != 'b' * 64


def test_concurrent_capture_results_do_not_share_mutable_last_call_evidence(boundary):
    adapter, _ = boundary
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adapter.fetch_server_time, with_evidence=True),
                   pool.submit(adapter.fetch_multi_assets_mode, with_evidence=True)]
        time_result, mode_result = [future.result() for future in futures]
    assert time_result['data'] == 1234
    assert time_result['request_evidence']['path'] == 'time'
    assert mode_result['data'] is False
    assert mode_result['request_evidence']['path'] == 'multiAssetsMargin'
    assert time_result['request_evidence'] is not mode_result['request_evidence']


def test_private_account_reads_need_explicit_demo_credentials(boundary):
    _, client = boundary
    adapter = BinanceDemoAdapter(client=client)
    for operation in (lambda: adapter.fetch_income_window(since_ms=0, until_ms=1),
                      adapter.fetch_multi_assets_mode, adapter.fetch_account_snapshot):
        with pytest.raises(DemoSafetyError, match='explicit Demo credentials'):
            operation()
    assert client.calls == []


def test_new_account_paths_are_get_only_with_reviewed_costs():
    for path in ('income', 'multiAssetsMargin'):
        _request_allowed(path, 'fapiPrivate', 'GET')
        for method in ('POST', 'PUT', 'DELETE'):
            with pytest.raises(DemoSafetyError):
                _request_allowed(path, 'fapiPrivate', method)
    client = _build_client('offline-key', 'offline-secret')
    costs = []
    client.throttle = lambda cost: costs.append(cost)
    client.fetch = lambda *args, **kwargs: {}
    for path in ('income', 'multiAssetsMargin'):
        client.request(path, 'fapiPrivate', 'GET', {}, config={'cost': 0})
    assert costs == [30, 30]


@pytest.mark.parametrize('changes', [
    {'totalInitialMargin': 'NaN'}, {'availableBalance': True},
    {'totalMaintMargin': 'Infinity'}, {'totalCrossWalletBalance': 1.5},
])
def test_optional_top_level_amounts_are_validated_if_retained(boundary, changes):
    adapter, client = boundary
    client.responses[('account', 'GET')] = account(**changes)
    with pytest.raises(DemoSafetyError):
        adapter.fetch_account_snapshot()


def test_nested_account_rows_do_not_carry_synthetic_reconciliation_claims(boundary):
    adapter, client = boundary
    raw = account()
    for row in raw['assets'] + raw['positions']:
        row.update(reconciled=True, as_of_ms=1234, data_complete=True,
                   funding_fee_complete=True, risk_day_opening={'complete': True})
    client.responses[('account', 'GET')] = raw
    assert adapter.fetch_account_snapshot() == account()
