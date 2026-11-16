"""订单快照的原子读取证据；全部为离线传输，不授予交易或验收资格。"""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import threading
from unittest.mock import patch

import ccxt
import pytest

from binance_demo_adapter import BinanceDemoAdapter, DemoRequestError, DemoSafetyError
from tests.test_binance_demo_adapter import BTC, FakeClient, order
from tests.test_binance_demo_native_orders import algo

READS = [
    ('fetch_open_orders', 'openOrders', order),
    ('fetch_open_conditional_orders', 'openAlgoOrders', algo),
]


@pytest.fixture
def boundary():
    client = FakeClient()
    adapter = BinanceDemoAdapter(client=client, api_key='offline-key', api_secret='offline-secret')
    return adapter, client


@pytest.mark.parametrize('method,path,row', READS)
def test_order_envelope_preserves_external_rows_exact_raw_and_readonly_boundary(boundary, method, path, row):
    adapter, client = boundary
    known = row()
    foreign = row(symbol='SOLUSDT', **({'orderId': 456, 'clientOrderId': 'external'}
                                       if path == 'openOrders' else
                                       {'algoId': 7002, 'clientAlgoId': 'external',
                                        'closePosition': 'unknown'}))
    raw = [known, foreign]
    client.responses[(path, 'GET')] = raw
    result = getattr(adapter, method)(with_evidence=True)
    assert set(result) == {'data', 'request_evidence'}
    assert len(result['data']) == 2
    assert result['data'][0]['pair'] == BTC
    assert result['data'][1]['pair'] == 'SOLUSDT'
    assert result['data'][1]['allowed_pair'] is False
    assert [item['raw'] for item in result['data']] == raw
    if path == 'openAlgoOrders':
        assert result['data'][1]['close_position'] is None
        assert result['data'][1]['execution_complete'] is False
    evidence = result['request_evidence']
    assert evidence['path'] == path
    assert evidence['api'] == 'fapiPrivate'
    assert evidence['method'] == 'GET'
    assert evidence['status'] == 'ok'
    assert evidence['request_params'] == {}
    assert evidence['rate_limit_cost'] == 40
    assert evidence['response_hash_basis'] == 'canonical_json'
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                           allow_nan=False, separators=(',', ':')).encode()
    assert evidence['response_sha256'] == hashlib.sha256(canonical).hexdigest()
    assert evidence['started_at_utc'].endswith('Z')
    assert evidence['received_at_utc'].endswith('Z')
    assert not {'complete', 'reconciled', 'as_of', 'acceptance_eligible'} & evidence.keys()
    assert client.calls == [(path, 'fapiPrivate', 'GET', {})]
    assert adapter._attempts == {}
    assert adapter._owned_client_ids == set()
    assert adapter.protection_capabilities()['acceptance_eligible'] is False


@pytest.mark.parametrize('method,path,row', READS)
def test_default_and_explicit_false_keep_normalized_list_contract(boundary, method, path, row):
    adapter, client = boundary
    client.responses[(path, 'GET')] = [row()]
    before = getattr(adapter, method)()
    explicit = getattr(adapter, method)(with_evidence=False)
    enveloped = getattr(adapter, method)(with_evidence=True)['data']
    for result in (before, explicit, enveloped):
        assert isinstance(result, list)
        assert result[0]['raw'] == row()
        assert result[0]['pair'] == BTC


@pytest.mark.parametrize('method,path,row', READS)
@pytest.mark.parametrize('raw', [{}, {'orders': []}, None, '', [None], [1], [[]]])
def test_malformed_snapshot_never_becomes_an_empty_order_fact(boundary, method, path, row, raw):
    adapter, client = boundary
    client.request = lambda *args, **kwargs: raw
    for with_evidence in (False, True):
        with pytest.raises(DemoSafetyError):
            getattr(adapter, method)(with_evidence=with_evidence)


@pytest.mark.parametrize('method,path,row', READS)
def test_empty_list_is_a_valid_observation_without_completeness_claim(boundary, method, path, row):
    adapter, client = boundary
    client.responses[(path, 'GET')] = []
    assert getattr(adapter, method)() == []
    result = getattr(adapter, method)(with_evidence=True)
    assert result['data'] == []
    assert 'complete' not in result['request_evidence']


@pytest.mark.parametrize('method,path,row', READS)
def test_scoped_snapshot_rejects_a_different_allowed_or_external_symbol(boundary, method, path, row):
    adapter, client = boundary
    for symbol in ('ETHUSDT', 'SOLUSDT'):
        client.responses[(path, 'GET')] = [row(symbol=symbol)]
        for with_evidence in (False, True):
            with pytest.raises(DemoSafetyError, match='scope mismatch'):
                getattr(adapter, method)(BTC, with_evidence=with_evidence)


@pytest.mark.parametrize('method,path,row', READS)
@pytest.mark.parametrize('invalid', [0, 1, None, 'true', {}, []])
def test_non_boolean_evidence_mode_is_rejected_before_transport(boundary, method, path, row, invalid):
    adapter, client = boundary
    with pytest.raises(DemoSafetyError):
        getattr(adapter, method)(with_evidence=invalid)
    assert client.calls == []


BAD_ORDINARY = [
    {'orderId': True}, {'orderId': -1}, {'orderId': '1.1'},
    {'clientOrderId': ''}, {'side': ''}, {'side': 'sell'}, {'side': []}, {'side': {}},
    {'type': None}, {'status': ''},
    {'origQty': None}, {'origQty': 0.01}, {'origQty': True}, {'origQty': 'NaN'},
    {'origQty': '-1'}, {'executedQty': 'Infinity'}, {'executedQty': '-1'},
    {'executedQty': '0.011'}, {'price': True}, {'price': '-1'}, {'avgPrice': 1.1},
    {'time': True}, {'updateTime': -1}, {'symbol': 'bad' + chr(10) + 'symbol'},
]
BAD_CONDITIONAL = [
    {'algoId': True}, {'algoId': 0}, {'algoId': -1}, {'clientAlgoId': ''},
    {'side': 'sell'}, {'side': []}, {'side': {}}, {'algoStatus': ''}, {'orderType': ''},
    {'quantity': None}, {'quantity': 0.002}, {'quantity': True}, {'quantity': 'NaN'},
    {'quantity': '-1'}, {'triggerPrice': 'Infinity'}, {'triggerPrice': -1},
    {'triggerPrice': True}, {'createTime': True}, {'updateTime': -1},
    {'symbol': 'bad' + chr(10) + 'symbol'},
]


@pytest.mark.parametrize('method,path,row,changes', [
    *[('fetch_open_orders', 'openOrders', order, changes) for changes in BAD_ORDINARY],
    *[('fetch_open_conditional_orders', 'openAlgoOrders', algo, changes) for changes in BAD_CONDITIONAL],
])
def test_evidence_mode_rejects_imprecise_or_malformed_required_facts(boundary, method, path, row, changes):
    adapter, client = boundary
    client.responses[(path, 'GET')] = [row(**changes)]
    with pytest.raises(DemoSafetyError):
        getattr(adapter, method)(with_evidence=True)


@pytest.mark.parametrize('method,path,row,field,normalized', [
    ('fetch_open_orders', 'openOrders', order, 'origQty', 'qty'),
    ('fetch_open_conditional_orders', 'openAlgoOrders', algo, 'quantity', 'quantity'),
])
def test_evidence_keeps_decimal_tail_without_float_roundtrip(boundary, method, path, row, field, normalized):
    adapter, client = boundary
    value = '0.010000000000000000000000000000000001'
    client.responses[(path, 'GET')] = [row(**{field: value})]
    result = getattr(adapter, method)(with_evidence=True)
    assert result['data'][0][normalized] == value
    assert result['data'][0]['raw'][field] == value


@pytest.mark.parametrize('method,path,row', READS)
def test_snapshots_are_detached_from_client_last_call_and_other_calls(boundary, method, path, row):
    adapter, client = boundary
    raw = [row()]
    client.request = lambda *args, **kwargs: raw
    result = getattr(adapter, method)(BTC, with_evidence=True)
    original = copy.deepcopy(result)
    raw[0]['symbol'] = 'CHANGED'
    adapter.last_call['request_params']['symbol'] = 'CHANGED'
    assert result == original
    result['request_evidence']['request_params']['symbol'] = 'CALLER'
    result['data'][0]['raw']['symbol'] = 'CALLER'
    assert adapter.last_call['request_params']['symbol'] == 'CHANGED'
    assert raw[0]['symbol'] == 'CHANGED'


def test_concurrent_order_reads_keep_their_own_path_params_and_transport_digest(boundary):
    adapter, client = boundary
    validator_started = threading.Event()
    second_started = threading.Event()
    second_transport = threading.Event()
    release = threading.Event()
    request = client.request
    client.responses[('openOrders', 'GET')] = [order()]
    client.responses[('openAlgoOrders', 'GET')] = [algo()]
    def transport(path, *args, **kwargs):
        if path == 'openAlgoOrders':
            second_transport.set()
        value = request(path, *args, **kwargs)
        client._quant_last_response_sha256 = ('a' if path == 'openOrders' else 'b') * 64
        client._quant_last_response_hash_basis = 'raw_http_response_bytes'
        return value
    client.request = transport
    from binance_demo_adapter import normalize_order
    def blocked_validator(*args, **kwargs):
        # 第一条 HTTP 已写入摘要，但证据尚未冻结；此时另一个请求不能进入传输。
        validator_started.set()
        assert release.wait(3)
        return normalize_order(*args, **kwargs)
    def second_read():
        second_started.set()
        return adapter.fetch_open_conditional_orders(with_evidence=True)
    with patch('binance_demo_adapter.normalize_order', side_effect=blocked_validator):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(adapter.fetch_open_orders, BTC, with_evidence=True)
            try:
                assert validator_started.wait(3)
                second = pool.submit(second_read)
                assert second_started.wait(3)
                assert not second_transport.wait(0.2), '第二条请求越过了尚未冻结的归档读取锁'
            finally:
                release.set()
            ordinary, conditional = first.result(), second.result()
    for result, path, digest, params in (
        (ordinary, 'openOrders', 'a' * 64, {'symbol': 'BTCUSDT'}),
        (conditional, 'openAlgoOrders', 'b' * 64, {}),
    ):
        assert result['request_evidence']['path'] == path
        assert result['request_evidence']['response_sha256'] == digest
        assert result['request_evidence']['response_hash_basis'] == 'raw_http_response_bytes'
        assert result['request_evidence']['request_params'] == params


@pytest.mark.parametrize('method,path,row', READS)
def test_stale_transport_digest_is_cleared_and_secrets_are_not_retained(boundary, method, path, row):
    adapter, client = boundary
    raw = row(apiKey='secret-key', headers={'Authorization': 'private'})
    client.responses[(path, 'GET')] = [raw]
    client._quant_last_response_sha256 = 'c' * 64
    client._quant_last_response_hash_basis = 'raw_http_response_bytes'
    result = getattr(adapter, method)(with_evidence=True)
    assert result['request_evidence']['response_hash_basis'] == 'canonical_json'
    assert result['request_evidence']['response_sha256'] != 'c' * 64
    assert 'secret-key' not in str(result)
    assert 'Authorization' not in str(result)


@pytest.mark.parametrize('method,path,row', READS)
def test_transport_failure_never_becomes_empty_and_is_not_retried(boundary, method, path, row):
    adapter, client = boundary
    client.responses[(path, 'GET')] = ccxt.RequestTimeout('sensitive signed-url')
    with pytest.raises(DemoRequestError) as error:
        getattr(adapter, method)(with_evidence=True)
    assert len(client.calls) == 1
    assert 'sensitive' not in str(error.value)
    assert adapter.last_call['status'] == 'error'


@pytest.mark.parametrize('method,path,row', READS)
def test_private_snapshot_requires_demo_credentials(boundary, method, path, row):
    _, client = boundary
    adapter = BinanceDemoAdapter(client=client)
    with pytest.raises(DemoSafetyError, match='explicit Demo credentials'):
        getattr(adapter, method)(with_evidence=True)
    assert client.calls == []
