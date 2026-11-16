"""原生条件单窄接口的离线合同；不连接交易所、不授予正式验收资格。"""
from concurrent.futures import ThreadPoolExecutor

import ccxt
import pytest

from binance_demo_adapter import (
    BinanceDemoAdapter, DemoExchangeRejected, DemoRequestError, DemoSafetyError,
    DemoSubmissionUnknown, _build_client, _request_allowed, stable_client_order_id,
)
from tests.test_binance_demo_adapter import BTC, FakeClient

ALGO_ID = stable_client_order_id('native-test', 'fill-1', BTC, 'sell', 'stop-1')


def algo(**changes):
    return {
        'algoId': 7001, 'clientAlgoId': ALGO_ID, 'algoType': 'CONDITIONAL',
        'symbol': 'BTCUSDT', 'side': 'SELL', 'positionSide': 'BOTH',
        'orderType': 'STOP_MARKET', 'algoStatus': 'NEW', 'quantity': '0.002',
        'triggerPrice': '59000.10', 'reduceOnly': True, 'closePosition': False,
        'workingType': 'MARK_PRICE', 'priceProtect': False, 'createTime': 1000,
        'updateTime': 1001, 'triggerTime': 0, 'actualOrderId': '', **changes,
    }


@pytest.fixture
def boundary():
    client = FakeClient()
    client.responses[('algoOrder', 'POST')] = algo()
    client.responses[('algoOrder', 'GET')] = algo()
    adapter = BinanceDemoAdapter(client=client, api_key='offline-key',
                                api_secret='offline-secret', allow_trading=True)
    return adapter, client


def submit(adapter, **changes):
    fields = dict(pair=BTC, side='sell', quantity='0.002', trigger_price='59000.10',
                  client_algo_id=ALGO_ID, working_type='MARK_PRICE', price_protect=False)
    fields.update(changes)
    return adapter.submit_stop_market_order(**fields)


def test_stop_contract_uses_algo_identity_and_explicit_protection_semantics(boundary):
    adapter, client = boundary
    result = submit(adapter)
    assert client.calls[-1] == ('algoOrder', 'fapiPrivate', 'POST', {
        'algoType': 'CONDITIONAL', 'symbol': 'BTCUSDT', 'side': 'SELL',
        'positionSide': 'BOTH', 'type': 'STOP_MARKET', 'quantity': '0.002',
        'triggerPrice': '59000.10', 'reduceOnly': 'true', 'clientAlgoId': ALGO_ID,
        'workingType': 'MARK_PRICE', 'priceProtect': 'false',
    })
    assert client.configs[-1] == {'cost': 1}
    assert result['algo_id'] == '7001'
    assert result['client_algo_id'] == ALGO_ID
    assert result['algo_status'] == 'NEW'
    assert result['quantity'] == '0.002'
    assert result['trigger_price'] == '59000.10'
    assert result['actual_order_id'] is None
    assert result['execution_complete'] is False
    assert adapter._attempts[ALGO_ID] == 'confirmed'
    assert adapter.protection_capabilities()['acceptance_eligible'] is False


def test_take_profit_uses_separate_role_and_does_not_become_an_entry(boundary):
    adapter, client = boundary
    target_id = stable_client_order_id('native-test', 'fill-1', BTC, 'sell', 'target-1')
    client.responses[('algoOrder', 'POST')] = algo(
        clientAlgoId=target_id, orderType='TAKE_PROFIT_MARKET', triggerPrice='62000.20')
    result = adapter.submit_take_profit_order(
        BTC, 'sell', '0.002', '62000.20', target_id,
        working_type='MARK_PRICE', price_protect=False)
    assert result['order_type'] == 'TAKE_PROFIT_MARKET'
    assert client.calls[-1][3]['reduceOnly'] == 'true'
    assert client.calls[-1][3]['type'] == 'TAKE_PROFIT_MARKET'


@pytest.mark.parametrize('changes', [
    {'reduce_only': False}, {'price_protect': 'false'}, {'working_type': 'LAST_PRICE'},
    {'client_algo_id': 'random'}, {'side': 'BUY'}, {'pair': 'SOL/USDT:USDT'},
    {'quantity': '0'}, {'quantity': True}, {'quantity': 'NaN'},
    {'trigger_price': 'Infinity'}, {'quantity': '0.0021'}, {'trigger_price': '59000.11'},
])
def test_invalid_native_intent_never_sends_a_mutation(boundary, changes):
    adapter, client = boundary
    with pytest.raises(DemoSafetyError):
        submit(adapter, **changes)
    assert not any(call[2] != 'GET' for call in client.calls)
    assert adapter._attempts == {}


def test_readonly_native_calls_and_foreign_cancel_do_not_send_any_request(boundary):
    _, client = boundary
    adapter = BinanceDemoAdapter(client=client, api_key='offline-key', api_secret='offline-secret')
    with pytest.raises(DemoSafetyError, match='read-only'):
        submit(adapter)
    with pytest.raises(DemoSafetyError, match='read-only'):
        adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)
    writable = BinanceDemoAdapter(client=client, api_key='offline-key',
                                  api_secret='offline-secret', allow_trading=True)
    with pytest.raises(DemoSafetyError, match='owned durable'):
        writable.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)
    assert client.calls == []


@pytest.mark.parametrize('response', [
    ccxt.RequestTimeout('signed secret must not escape'),
    ccxt.InvalidOrder('{"code":-4116,"msg":"duplicate"}'),
    {}, algo(clientAlgoId='foreign'), algo(quantity='0.001'),
    algo(side='BUY'), algo(orderType='TAKE_PROFIT_MARKET'),
    algo(reduceOnly=False), algo(closePosition=True), algo(workingType='CONTRACT_PRICE'),
])
def test_ambiguous_or_mismatched_native_ack_permanently_claims_id(boundary, response):
    adapter, client = boundary
    client.responses[('algoOrder', 'POST')] = response
    with pytest.raises(DemoSubmissionUnknown) as caught:
        submit(adapter)
    assert 'secret' not in str(caught.value)
    assert adapter._attempts[ALGO_ID] == 'unknown'
    client.responses[('algoOrder', 'GET')] = ccxt.OrderNotFound('not indexed yet')
    assert adapter.fetch_conditional_order(BTC, ALGO_ID) is None
    with pytest.raises(DemoSafetyError, match='already attempted'):
        submit(adapter)
    assert len([call for call in client.calls if call[2] == 'POST']) == 1
    assert adapter._attempts[ALGO_ID] == 'unknown'


def test_definite_native_rejection_is_distinct_and_never_retried(boundary):
    adapter, client = boundary
    client.responses[('algoOrder', 'POST')] = ccxt.InvalidOrder('{"code":-2021}')
    with pytest.raises(DemoExchangeRejected):
        submit(adapter)
    assert adapter._attempts[ALGO_ID] == 'rejected'
    with pytest.raises(DemoSafetyError):
        submit(adapter)
    assert len([call for call in client.calls if call[2] == 'POST']) == 1


def test_concurrent_same_native_id_has_one_post(boundary):
    adapter, client = boundary
    def attempt(_):
        try:
            return submit(adapter)
        except DemoSafetyError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert len([value for value in results if value]) == 1
    assert len([call for call in client.calls if call[2] == 'POST']) == 1


@pytest.mark.parametrize('status', ['TRIGGERING', 'TRIGGERED', 'FINISHED'])
def test_triggered_algo_never_implies_trade_fill_or_flat(boundary, status):
    adapter, client = boundary
    client.responses[('algoOrder', 'GET')] = algo(
        algoStatus=status, actualOrderId='808', triggerTime=1002)
    result = adapter.fetch_conditional_order(BTC, ALGO_ID)
    assert result['algo_status'] == status
    assert result['actual_order_id'] == '808'
    assert result['execution_complete'] is False
    assert client.calls[-1] == ('algoOrder', 'fapiPrivate', 'GET', {'clientAlgoId': ALGO_ID})


def test_cancel_ack_is_only_ack_and_terminal_requires_query(boundary):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'DELETE')] = {
        'algoId': 7001, 'clientAlgoId': ALGO_ID, 'code': 200, 'msg': 'success'}
    result = adapter.cancel_conditional_order(BTC, '7001', client_algo_id=ALGO_ID)
    assert result['cancel_acknowledged'] is True
    assert result['cancellation_confirmed'] is False
    assert result['execution_complete'] is False
    assert result['algo_status'] == 'NEW'
    assert client.calls[-1] == ('algoOrder', 'fapiPrivate', 'DELETE', {'clientAlgoId': ALGO_ID})
    client.responses[('algoOrder', 'GET')] = algo(algoStatus='CANCELED')
    assert adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)['algo_status'] == 'CANCELED'
    assert len([call for call in client.calls if call[2] == 'DELETE']) == 1


def test_cancel_mismatched_id_and_timeout_never_reports_success(boundary):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    with pytest.raises(DemoSafetyError, match='does not match'):
        adapter.cancel_conditional_order(BTC, '88', client_algo_id=ALGO_ID)
    assert not any(call[2] == 'DELETE' for call in client.calls)
    client.responses[('algoOrder', 'DELETE')] = ccxt.RequestTimeout('sensitive')
    with pytest.raises(DemoRequestError):
        adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)


def test_open_algo_snapshot_preserves_foreign_exposure_and_bool_unknown(boundary):
    adapter, client = boundary
    client.responses[('openAlgoOrders', 'GET')] = [
        algo(), algo(algoId=7002, clientAlgoId='external', symbol='SOLUSDT',
                     reduceOnly='false', closePosition='unknown'),
    ]
    known, foreign = adapter.fetch_open_conditional_orders()
    assert known['pair'] == BTC
    assert foreign['pair'] == 'SOLUSDT'
    assert foreign['allowed_pair'] is False
    assert foreign['reduce_only'] is False
    assert foreign['close_position'] is None
    assert adapter.last_call['rate_limit_cost'] == 40
    assert adapter._owned_client_ids == set()


def test_history_is_symbol_scoped_and_has_bounded_pagination(boundary):
    adapter, client = boundary
    client.responses[('allAlgoOrders', 'GET')] = [algo(algoStatus='FINISHED')]
    result = adapter.fetch_conditional_orders(BTC, since_ms=1000, limit=100)
    assert result[0]['algo_status'] == 'FINISHED'
    assert client.calls[-1] == ('allAlgoOrders', 'fapiPrivate', 'GET', {
        'symbol': 'BTCUSDT', 'startTime': 1000, 'limit': 100})
    assert adapter.last_call['rate_limit_cost'] == 5


def test_algo_paths_have_guarded_costs_and_bulk_cancel_stays_prohibited():
    for method, path in [('POST', 'algoOrder'), ('GET', 'algoOrder'),
                         ('DELETE', 'algoOrder'), ('GET', 'openAlgoOrders'),
                         ('GET', 'allAlgoOrders')]:
        _request_allowed(path, 'fapiPrivate', method)
    with pytest.raises(DemoSafetyError):
        _request_allowed('algoOpenOrders', 'fapiPrivate', 'DELETE')
    client = _build_client('offline-key', 'offline-secret')
    captured = []
    client.throttle = lambda cost: captured.append(cost)
    client.fetch = lambda *args, **kwargs: {}
    client.request('openAlgoOrders', 'fapiPrivate', 'GET', {}, config={'cost': 0})
    assert captured == [40]


def test_native_audit_logs_chinese_results_without_transport_secret(boundary, caplog):
    adapter, client = boundary
    with caplog.at_level('INFO', logger='binance_demo_adapter'):
        submit(adapter)
    assert '原生条件保护提交已确认' in caplog.text
    assert ALGO_ID in caplog.text
    assert 'offline-secret' not in caplog.text
    assert 'offline-key' not in caplog.text
    caplog.clear()
    next_id = stable_client_order_id('native-test', 'fill-2', BTC, 'sell', 'stop-1')
    client.responses[('algoOrder', 'POST')] = ccxt.RequestTimeout('signed_secret')
    with caplog.at_level('WARNING', logger='binance_demo_adapter'):
        with pytest.raises(DemoSubmissionUnknown):
            submit(adapter, client_algo_id=next_id)
    assert '原生条件保护提交结果未知' in caplog.text
    assert 'signed_secret' not in caplog.text


def test_short_protection_requires_explicit_contract_price_and_preserves_decimal(boundary):
    adapter, client = boundary
    short_id = stable_client_order_id('native-test', 'short-fill', BTC, 'buy', 'stop-1')
    client.responses[('algoOrder', 'POST')] = algo(
        side='BUY', clientAlgoId=short_id, triggerPrice='61000.20',
        workingType='CONTRACT_PRICE', priceProtect=True)
    result = submit(adapter, side='buy', client_algo_id=short_id,
                    trigger_price='61000.20', working_type='CONTRACT_PRICE', price_protect=True)
    assert result['side'] == 'buy'
    assert result['working_type'] == 'CONTRACT_PRICE'
    assert client.calls[-1][3]['priceProtect'] == 'true'


@pytest.mark.parametrize('updates', [
    {'symbol': 'ETHUSDT'}, {'positionSide': 'LONG'}, {'quantity': None},
    {'priceProtect': None}, {'reduceOnly': 1}, {'algoId': True},
    {'triggerPrice': '59000.20'}, {'algoStatus': 'CANCELED'},
])
def test_malformed_or_unusable_ack_is_unknown(boundary, updates):
    adapter, client = boundary
    client.responses[('algoOrder', 'POST')] = algo(**updates)
    with pytest.raises(DemoSubmissionUnknown):
        submit(adapter)
    assert adapter._attempts[ALGO_ID] == 'unknown'


def test_scoped_snapshot_and_cancel_ack_cannot_borrow_foreign_identity(boundary):
    adapter, client = boundary
    client.responses[('openAlgoOrders', 'GET')] = [algo(symbol='ETHUSDT')]
    with pytest.raises(DemoSafetyError):
        adapter.fetch_open_conditional_orders(BTC)
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'DELETE')] = {
        'algoId': 7002, 'clientAlgoId': ALGO_ID, 'code': 200}
    with pytest.raises(DemoRequestError):
        adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)


@pytest.mark.parametrize('code', [200, '200'])
def test_documented_cancel_ack_code_only_accepts_exact_success(boundary, code):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'DELETE')] = {
        'algoId': 7001, 'clientAlgoId': ALGO_ID, 'code': code}
    result = adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)
    assert result['cancel_acknowledged'] is True
    assert result['cancellation_confirmed'] is False


@pytest.mark.parametrize('code', [True, 200.0, '0200', '200 ', None])
def test_bad_cancel_ack_after_delete_has_unknown_request_semantics(boundary, code):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'DELETE')] = {
        'algoId': 7001, 'clientAlgoId': ALGO_ID, 'code': code}
    with pytest.raises(DemoRequestError) as caught:
        adapter.cancel_conditional_order(BTC, client_algo_id=ALGO_ID)
    assert caught.value.error_kind == 'conditional_cancel_unknown'
    assert len([call for call in client.calls if call[2] == 'DELETE']) == 1


@pytest.mark.parametrize('updates', [{'algoId': 0}, {'algoId': '0'}, {'algoType': 'WRONG'}])
def test_invalid_exchange_identity_does_not_confirm_protection(boundary, updates):
    adapter, client = boundary
    client.responses[('algoOrder', 'POST')] = algo(**updates)
    with pytest.raises(DemoSubmissionUnknown):
        submit(adapter)
    assert adapter._attempts[ALGO_ID] == 'unknown'


def test_malformed_side_is_a_boundary_error_not_attribute_error(boundary):
    adapter, client = boundary
    client.responses[('algoOrder', 'GET')] = algo(side=None)
    with pytest.raises(DemoSafetyError):
        adapter.fetch_conditional_order(BTC, ALGO_ID)


def test_trigger_child_lookup_uses_real_order_id_and_keeps_its_own_client_id(boundary):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'GET')] = algo(algoStatus='TRIGGERED', actualOrderId='808')
    client.responses[('order', 'GET')] = {
        'symbol': 'BTCUSDT', 'orderId': 808, 'clientOrderId': 'exchange-child-id',
        'side': 'SELL', 'type': 'MARKET', 'status': 'PARTIALLY_FILLED',
        'origQty': '0.002', 'executedQty': '0.001', 'price': '0',
        'avgPrice': '58999.9', 'time': 1002, 'updateTime': 1003, 'reduceOnly': True,
    }
    result = adapter.fetch_conditional_child_order(BTC, ALGO_ID)
    assert result['order_id'] == '808'
    assert result['client_id'] == 'exchange-child-id'
    assert result['parent_client_algo_id'] == ALGO_ID
    assert result['parent_algo_id'] == '7001'
    assert client.calls[-1] == ('order', 'fapiPrivate', 'GET', {'symbol': 'BTCUSDT', 'orderId': '808'})


def test_child_lookup_never_infers_fill_for_missing_or_foreign_parent(boundary):
    adapter, client = boundary
    with pytest.raises(DemoSafetyError, match='owned durable'):
        adapter.fetch_conditional_child_order(BTC, ALGO_ID)
    assert client.calls == []
    adapter.authorize_owned_client_id(ALGO_ID)
    assert adapter.fetch_conditional_child_order(BTC, ALGO_ID) is None
    assert not any(call[0] == 'order' for call in client.calls)


def test_child_lookup_mismatch_fails_closed(boundary):
    adapter, client = boundary
    adapter.authorize_owned_client_id(ALGO_ID)
    client.responses[('algoOrder', 'GET')] = algo(algoStatus='TRIGGERED', actualOrderId='808')
    from tests.test_binance_demo_adapter import order
    client.responses[('order', 'GET')] = order(orderId=809)
    with pytest.raises(DemoSafetyError):
        adapter.fetch_conditional_child_order(BTC, ALGO_ID)


def test_history_supports_bounded_windows_and_inclusive_algo_cursor(boundary):
    adapter, client = boundary
    client.responses[('allAlgoOrders', 'GET')] = [algo()]
    adapter.fetch_conditional_orders(BTC, since_ms=1000, until_ms=2000, from_algo_id='7000', limit=10)
    assert client.calls[-1][3] == {
        'symbol': 'BTCUSDT', 'startTime': 1000, 'endTime': 2000, 'algoId': '7000', 'limit': 10}
    for changes in ({'since_ms': 1000, 'until_ms': 1000},
                    {'since_ms': 1000, 'until_ms': 1000 + 7 * 86400000},
                    {'until_ms': True}, {'from_algo_id': 'not-an-id'}):
        before = len(client.calls)
        with pytest.raises(DemoSafetyError):
            adapter.fetch_conditional_orders(BTC, **changes)
        assert len(client.calls) == before


@pytest.mark.parametrize('changes', [
    {'side': []}, {'working_type': {}}, {'pair': []},
    {'quantity': 0.002}, {'trigger_price': 59000.1},
])
def test_native_bad_types_fail_locally_with_consistent_boundary_error(boundary, changes):
    adapter, client = boundary
    with pytest.raises(DemoSafetyError):
        submit(adapter, **changes)
    assert not any(call[2] != 'GET' for call in client.calls)
