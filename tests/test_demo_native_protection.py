"""原生保护协调器离线行为：真实重放器配内存持久边界，不访问交易所。"""
import copy
import importlib
import importlib.util
from decimal import localcontext
from threading import RLock
from types import SimpleNamespace

import pytest

from binance_demo_adapter import DemoSubmissionUnknown, stable_client_order_id
from demo_conditional_store import DemoConditionalStore

BTC = 'BTC/USDT:USDT'
PARENT = stable_client_order_id('coordinator-test', 'entry-1', BTC, 'buy')


def plan(**changes):
    return {
        'environment': 'binance_demo', 'account_id': 'account-1',
        'strategy_id': 'strategy-1', 'run_id': 'run-1', 'entry_decision_id': 'decision-1',
        'parent_entry_client_id': PARENT, 'pair': BTC, 'position_side': 'long',
        'entry_price': '100', 'initial_stop_price': '99', 'target_price': '102',
        'entry_atr': '1', 'break_even_stop_price': '100.1', 'break_even_costs_verified': True,
        'price_tick': '0.1', 'working_type': 'MARK_PRICE', 'price_protect': False,
        'max_snapshot_age_ms': 500, 'max_market_age_ms': 500, 'protection_target_ms': 2000,
        'exit_params_hash': 'a' * 64, **changes,
    }


def position(**changes):
    return {
        'environment': 'binance_demo', 'account_id': 'account-1',
        'strategy_id': 'strategy-1', 'run_id': 'run-1',
        'parent_entry_client_id': PARENT, 'pair': BTC, 'signed_quantity': '0.002',
        'observed_ms': 1100, 'exposure_started_ms': 1000,
        'source_ref': 'account/position/1', 'fill_watermark': 'fills/2',
        'facts_complete': True, 'owned_position': True, 'fills_reconciled': True,
        'normal_orders_complete': True, 'conditional_orders_complete': True,
        'normal_open_orders': [], 'ownership_conflicts': [], **changes,
    }


def market(**changes):
    return {'price': '100.5', 'observed_ms': 1100, 'source_ref': 'mark/1',
            'data_complete': True, **changes}


class MemoryLedger(DemoConditionalStore):
    """只替换慢数据库边界，意图验证与生产事件重放保持真实。"""
    def __init__(self, trace):
        super().__init__(SimpleNamespace(), 'namespace-1')
        self.trace, self.intents, self.journal = trace, {}, {}

    def _row(self, client):
        if client not in self.intents:
            return None
        return copy.deepcopy(self._replay(self.intents[client], self.journal[client]))

    def _event(self, client, kind, payload, stamp):
        from demo_store import _digest
        event = {'event_type': kind, 'payload': copy.deepcopy(payload), 'observed_ms': stamp}
        event['payload_hash'] = _digest({'client_algo_id': client, 'event_type': kind,
                                        'observed_ms': stamp, 'payload': payload})
        self.journal[client].append(event)
        return self._row(client)

    def reserve_conditional(self, value):
        from demo_store import _digest
        fixed = self._intent(value)
        client = fixed['client_algo_id']
        if client in self.intents:
            if fixed != self.intents[client]:
                raise ValueError('frozen intent conflict')
            return self._row(client), False
        self.trace.append(('reserve', client))
        self.intents[client], self.journal[client] = fixed, []
        return self._event(client, 'reserved', {'intent_hash': _digest(fixed)}, fixed['created_ms']), True

    def get_conditional(self, client):
        return self._row(client)

    def list_conditionals(self):
        return [self._row(client) for client in self.intents]

    def claim_conditional_submission(self, client, claimed_ms):
        if self._row(client)['submission_attempts']:
            return False
        self.trace.append(('claim', client))
        self._event(client, 'submission_claimed', {}, claimed_ms)
        return True

    def record_conditional_observation(self, client, value, observed_ms):
        self.trace.append(('record', client, None if value is None else value.get('algo_status')))
        if value is None:
            return self._event(client, 'lookup_absent', {}, observed_ms)
        payload = self._observation(self.intents[client], value)
        if not self.intents[client]['created_ms'] <= payload['created_ms'] <= payload['updated_ms'] <= observed_ms:
            raise ValueError('conditional exchange time is inconsistent with observation time')
        kind = 'cancel_ack' if value.get('cancel_acknowledged') is True else 'observed'
        return self._event(client, kind, payload, observed_ms)

    def mark_conditional_unknown(self, client, error_type, observed_ms):
        return self._event(client, 'submission_unknown', {'error_type': error_type}, observed_ms)


class Exchange:
    def __init__(self, trace):
        self.trace, self.orders, self.authorized = trace, {}, set()
        self.post_error = None
        self.get_override = None
        self.cancel_pending = False
        self.cancel_trigger = False
        self.external = []
        self.clock = 1100

    def authorize_owned_client_id(self, client):
        self.authorized.add(client)

    def _submit(self, pair, side, quantity, trigger, client, kind, **kwargs):
        self.trace.append(('post', client, kind))
        if self.post_error:
            raise self.post_error
        fact = {
            'algo_id': str(100 + len(self.orders)), 'client_algo_id': client,
            'algo_type': 'CONDITIONAL', 'algo_status': 'NEW', 'pair': pair,
            'side': side, 'order_type': kind, 'position_side': 'BOTH',
            'quantity': quantity, 'trigger_price': trigger, 'reduce_only': True,
            'close_position': False, 'working_type': kwargs['working_type'],
            'price_protect': kwargs['price_protect'], 'created_ms': self.clock,
            'updated_ms': self.clock, 'trigger_ms': 0, 'actual_order_id': None,
            'execution_complete': False, 'raw': {},
        }
        self.orders[client] = fact
        return copy.deepcopy(fact)

    def submit_stop_market_order(self, pair, side, quantity, trigger, client, **kwargs):
        return self._submit(pair, side, quantity, trigger, client, 'STOP_MARKET', **kwargs)

    def submit_take_profit_order(self, pair, side, quantity, trigger, client, **kwargs):
        return self._submit(pair, side, quantity, trigger, client, 'TAKE_PROFIT_MARKET', **kwargs)

    def fetch_conditional_order(self, pair, client):
        self.trace.append(('get', client))
        if self.get_override:
            return self.get_override(client)
        return copy.deepcopy(self.orders.get(client))

    def cancel_conditional_order(self, pair, algo_id=None, *, client_algo_id):
        self.trace.append(('cancel', client_algo_id))
        fact = self.orders[client_algo_id]
        if self.cancel_trigger:
            fact.update(algo_status='TRIGGERED', actual_order_id='900', updated_ms=self.clock)
            return copy.deepcopy(fact)
        receipt = {**copy.deepcopy(fact), 'cancel_acknowledged': True,
                   'cancellation_confirmed': False, 'cancel_raw': {
                       'algoId': fact['algo_id'], 'clientAlgoId': client_algo_id, 'code': '200'}}
        if not self.cancel_pending:
            fact.update(algo_status='CANCELED', updated_ms=self.clock)
        return receipt

    def fetch_conditional_child_order(self, pair, client):
        self.trace.append(('child', client))
        return {'order_id': '900', 'status': 'PARTIALLY_FILLED', 'filled_qty': '0.001'}

    def fetch_open_conditional_orders(self, pair=None):
        return [copy.deepcopy(row) for row in self.orders.values() if row['algo_status'] == 'NEW'] + self.external


@pytest.fixture
def setup():
    trace = []
    return trace, MemoryLedger(trace), Exchange(trace)


def coordinator(setup):
    assert importlib.util.find_spec('demo_native_protection'), 'native protection coordinator missing'
    module = importlib.import_module('demo_native_protection')
    trace, ledger, exchange = setup
    return module.NativeProtectionCoordinator(
        ledger, exchange, account_id='account-1', account_lock=RLock(),
        pause_entries=lambda reason, evidence: trace.append(('pause', reason)),
        cancel_remaining_entry=lambda client, reason: trace.append(('cancel_entry', client, reason)),
        reduce_owned=lambda evidence, reason, key: trace.append(('reduce', reason, key)),
        monotonic_ms=lambda:0,
    )


def test_new_position_persists_claims_then_independently_confirms_both_protections(setup):
    result = coordinator(setup).protect(plan(), position(), now_ms=1100)
    trace, ledger, _ = setup
    assert result['status'] == 'protected'
    assert result['stop_covered_quantity'] == '0.002'
    assert result['execution_complete'] is False
    assert result['acceptance_eligible'] is False
    assert len(ledger.list_conditionals()) == 2
    for row in ledger.list_conditionals():
        client = row['client_algo_id']
        assert trace.index(('reserve', client)) < trace.index(('claim', client))
        assert trace.index(('claim', client)) < trace.index(('post', client, row['order_type']))
        assert trace.index(('post', client, row['order_type'])) < trace.index(('get', client))
    assert not any(event[0] in {'cancel', 'reduce'} for event in trace)


def test_restart_reconciles_claimed_unknown_without_second_post_or_new_revision(setup):
    trace, ledger, exchange = setup
    exchange.post_error = DemoSubmissionUnknown('RequestTimeout')
    first = coordinator(setup).protect(plan(), position(), now_ms=1100)
    assert first['status'] == 'protection_incomplete'
    assert len([row for row in trace if row[0] == 'post']) == 1
    exchange.post_error = None
    second = coordinator(setup).protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert second['status'] == 'protection_incomplete'
    assert len([row for row in trace if row[0] == 'post']) == 1
    assert len(ledger.list_conditionals()) == 1
    assert any(row[0] == 'cancel_entry' for row in trace)


def test_partial_fill_increase_gets_full_quantity_new_stop_before_old_cancel(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(signed_quantity='0.001'), now_ms=1100)
    trace, ledger, exchange = setup
    old = [r for r in ledger.list_conditionals() if r['order_type'] == 'STOP_MARKET'][0]
    exchange.clock = 1200
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protected'
    assert result['stop_covered_quantity'] == '0.002'
    new = max((r for r in ledger.list_conditionals() if r['order_type'] == 'STOP_MARKET'), key=lambda r:r['revision'])
    assert trace.index(('get', new['client_algo_id'])) < trace.index(('cancel', old['client_algo_id']))
    assert new['supersedes_client_algo_id'] == old['client_algo_id']


def test_replacement_unknown_does_not_cancel_sole_old_stop(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(signed_quantity='0.001'), now_ms=1100)
    trace, ledger, exchange = setup
    old = [r for r in ledger.list_conditionals() if r['order_type'] == 'STOP_MARKET'][0]
    exchange.post_error = DemoSubmissionUnknown('RequestTimeout')
    exchange.clock = 1200
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protection_incomplete'
    assert ('cancel', old['client_algo_id']) not in trace
    assert result['stop_covered_quantity'] == '0.001'


def test_confirmation_timeout_cancels_entry_pauses_and_requests_idempotent_reduce(setup):
    trace, _, exchange = setup
    exchange.post_error = DemoSubmissionUnknown('RequestTimeout')
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    result = coordinator(setup).protect(
        plan(), position(observed_ms=3100), now_ms=3100)
    assert result['fallback_requested'] is True
    assert len([item for item in trace if item[0] == 'post']) == 1
    key = [item[2] for item in trace if item[0] == 'reduce'][-1]
    assert len(key) == 64
    coordinator(setup).protect(plan(), position(observed_ms=3200), now_ms=3200)
    assert [item[2] for item in trace if item[0] == 'reduce'][-1] == key


@pytest.mark.parametrize('change', [
    {'account_id': 'foreign'}, {'run_id': 'other'}, {'signed_quantity': '-0.002'},
    {'observed_ms': 2000}, {'facts_complete': None}, {'owned_position': False},
    {'fills_reconciled': False}, {'ownership_conflicts': ['external-position']},
])
def test_invalid_position_never_installs_or_reduces_unverified_exposure(setup, change):
    result = coordinator(setup).protect(plan(), position(**change), now_ms=1100)
    trace, ledger, _ = setup
    assert result['status'] == 'blocked_evidence'
    assert ledger.list_conditionals() == []
    assert not any(row[0] in {'post', 'cancel', 'reduce'} for row in trace)


def test_stale_position_keeps_protection_reconciliation_without_new_risk(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    trace, _, _ = setup
    count = len([row for row in trace if row[0] == 'post'])
    result = runner.protect(plan(), position(), now_ms=1700)
    assert result['status'] == 'blocked_evidence'
    assert len([row for row in trace if row[0] == 'post']) == count


def test_independent_get_mismatch_cannot_confirm_or_cancel_old(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(signed_quantity='0.001'), now_ms=1100)
    trace, ledger, exchange = setup
    old_ids = {row['client_algo_id'] for row in ledger.list_conditionals()}
    exchange.clock = 1200
    exchange.get_override = lambda client: ({**copy.deepcopy(exchange.orders[client]), 'quantity':'0.009'}
                                          if client not in old_ids else copy.deepcopy(exchange.orders[client]))
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protection_incomplete'
    assert not any(row[0] == 'cancel' for row in trace)


def test_cancel_ack_never_counts_old_stop_as_fresh_and_pending_is_visible(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(signed_quantity='0.001'), now_ms=1100)
    trace, _, exchange = setup
    exchange.cancel_pending = True
    exchange.clock = 1200
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protected_cleanup_pending'
    assert result['stop_covered_quantity'] == '0.002'
    assert result['entries_allowed'] is False
    assert any(item[0] == 'pause' for item in trace)


def test_triggered_stop_queries_child_does_not_mark_position_exit_complete(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    trace, ledger, exchange = setup
    stop = next(row for row in ledger.list_conditionals() if row['order_type'] == 'STOP_MARKET')
    exchange.orders[stop['client_algo_id']].update(algo_status='TRIGGERED', actual_order_id='900', updated_ms=1200)
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert ('child', stop['client_algo_id']) in trace
    assert result['execution_complete'] is False
    assert result['status'] == 'protection_incomplete'
    assert result['stop_covered_quantity'] == '0'
    assert len([row for row in trace if row[0] == 'post']) == 2


def test_flat_cleanup_requires_no_normal_orders_and_confirmed_conditionals_absent(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    _, _, exchange = setup
    exchange.clock = 1200
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1200), now_ms=1200)
    assert result['status'] == 'flat_reconciled'
    assert result['cleanup_complete'] is True
    assert result['execution_complete'] is False
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1250,
                          normal_open_orders=[{'client_id': PARENT}]), now_ms=1250)
    assert result['cleanup_complete'] is False


def test_flat_external_conditional_order_is_not_cancelled_or_hidden(setup):
    runner = coordinator(setup)
    _, _, exchange = setup
    exchange.external = [{'client_algo_id': 'external', 'pair': BTC, 'algo_status':'NEW'}]
    result = runner.protect(plan(), position(signed_quantity='0'), now_ms=1100)
    assert result['status'] == 'blocked_external_orders'
    assert result['cleanup_complete'] is False
    assert not any(item[0] == 'cancel' for item in setup[0])


def test_only_favorable_stop_movement_and_1atr_trailing_after_one_r(setup):
    runner = coordinator(setup)
    first = runner.protect(plan(), position(), now_ms=1100)
    assert first['desired_stop_price'] == '99'
    setup[2].clock = 1200
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200,
                            market=market(price='101.5', observed_ms=1200))
    assert result['desired_stop_price'] == '100.5'
    setup[2].clock = 1300
    result = runner.protect(plan(), position(observed_ms=1300), now_ms=1300,
                            market=market(price='101.1', observed_ms=1300))
    assert result['desired_stop_price'] == '100.5'


def test_stale_market_cannot_move_existing_stop(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    result = runner.protect(plan(), position(observed_ms=1800), now_ms=1800, market=market(price='101.5'))
    assert result['status'] == 'blocked_evidence'
    assert result['entries_allowed'] is False
    assert len([row for row in setup[0] if row[0] == 'post']) == 2


def test_short_breakeven_and_trailing_move_down_without_worsening(setup):
    runner = coordinator(setup)
    short = plan(position_side='short', initial_stop_price='101', target_price='98',
                 break_even_stop_price='99.9')
    result = runner.protect(short, position(signed_quantity='-0.002'), now_ms=1100,
                            market=market(price='98.5'))
    assert result['desired_stop_price'] == '99.5'
    assert result['status'] == 'protected'


def test_logs_include_safe_identity_quantities_and_never_raw_error(setup, caplog):
    with caplog.at_level('INFO', logger='demo_native_protection'):
        result = coordinator(setup).protect(plan(), position(), now_ms=1100)
    assert result['status'] == 'protected'
    assert '原生保护协调' in caplog.text
    assert 'account-1' in caplog.text
    assert '0.002' in caplog.text
    assert not any(word in caplog.text for word in ['offline-secret','signed_secret'])


def test_plan_is_frozen_by_decision_and_target_does_not_drift(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    result = runner.protect(plan(target_price='103'), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'blocked_evidence'
    assert len([row for row in setup[0] if row[0] == 'post']) == 2


def test_restart_binds_frozen_atr_and_decision_to_durable_client_identity(setup):
    coordinator(setup).protect(plan(), position(), now_ms=1100)
    result = coordinator(setup).protect(plan(entry_atr='2'), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'blocked_evidence'
    result = coordinator(setup).protect(plan(entry_decision_id='changed'),
                                        position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'blocked_evidence'
    assert len([row for row in setup[0] if row[0] == 'post']) == 2


def test_lookup_absent_does_not_reuse_prior_new_as_current_coverage(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    setup[2].get_override = lambda client: None
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protection_incomplete'
    assert result['stop_covered_quantity'] == '0'
    assert len([row for row in setup[0] if row[0] == 'post']) == 2


def test_flat_finished_algo_with_active_child_cannot_complete_cleanup(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    _, ledger, exchange = setup
    stop = next(row for row in ledger.list_conditionals() if row['order_type'] == 'STOP_MARKET')
    exchange.clock = 1200
    exchange.orders[stop['client_algo_id']].update(
        algo_status='FINISHED', actual_order_id='900', updated_ms=1200)
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1200), now_ms=1200)
    assert result['cleanup_complete'] is False
    assert result['status'] == 'protection_incomplete'
    assert ('child', stop['client_algo_id']) in setup[0]


def test_flat_unknown_absence_never_proves_cleanup(setup):
    setup[2].post_error = DemoSubmissionUnknown('RequestTimeout')
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1200), now_ms=1200)
    assert result['cleanup_complete'] is False
    assert result['status'] == 'protection_incomplete'
    assert len([row for row in setup[0] if row[0] == 'post']) == 1


def test_stale_zero_before_exchange_child_time_never_proves_cleanup(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    setup[2].clock = 1200
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1100), now_ms=1200)
    assert result['cleanup_complete'] is False


def test_cancel_trigger_race_blocks_even_when_new_stop_is_confirmed(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(signed_quantity='0.001'), now_ms=1100)
    setup[2].clock = 1200
    setup[2].cancel_trigger = True
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protected_cleanup_pending'
    assert result['entries_allowed'] is False
    assert any(row[0] == 'child' for row in setup[0])


def test_restart_can_claim_reserved_but_never_sent_intent_once(setup):
    coordinator(setup).protect(plan(), position(), now_ms=1100)
    trace, ledger, exchange = setup
    stop = next(row for row in ledger.list_conditionals() if row['order_type'] == 'STOP_MARKET')
    client = stop['client_algo_id']
    ledger.journal[client] = ledger.journal[client][:1]
    del exchange.orders[client]
    trace.clear()
    exchange.clock = 1200
    result = coordinator(setup).protect(plan(), position(observed_ms=1200), now_ms=1200)
    assert result['status'] == 'protected'
    assert ('claim', client) in trace
    assert len([row for row in trace if row[0] == 'post']) == 1
    assert ledger.get_conditional(client)['created_ms'] == 1100


def test_new_snapshot_cannot_reset_existing_unknown_protection_deadline(setup):
    setup[2].post_error = DemoSubmissionUnknown('RequestTimeout')
    coordinator(setup).protect(plan(), position(), now_ms=1100)
    result = coordinator(setup).protect(
        plan(), position(observed_ms=3200, exposure_started_ms=3199), now_ms=3200)
    assert result['fallback_requested'] is True


def test_low_decimal_context_never_rounds_owned_quantity_or_coverage(setup):
    with localcontext() as ctx:
        ctx.prec = 2
        result = coordinator(setup).protect(plan(), position(signed_quantity='0.00204'), now_ms=1100)
    assert result['status'] == 'protected'
    assert result['stop_covered_quantity'] == '0.00204'
    assert all(row['quantity'] == '0.00204' for row in setup[1].list_conditionals())


def test_crossed_stop_keeps_reconciling_and_requests_owned_fallback(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    trace = setup[0]
    trace.clear()
    result = runner.protect(plan(), position(observed_ms=3100), now_ms=3100,
                            market=market(price='98.5', observed_ms=3100))
    assert result['status'] == 'protection_incomplete'
    assert result['fallback_requested'] is True
    assert any(row[0] == 'get' for row in trace)
    assert any(row[0] == 'cancel_entry' for row in trace)
    assert not any(row[0] == 'post' for row in trace)


def test_stale_optional_market_still_reconciles_owned_stops(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    setup[0].clear()
    result = runner.protect(plan(), position(observed_ms=1800), now_ms=1800,
                            market=market(price='101.5'))
    assert result['status'] == 'blocked_evidence'
    assert any(row[0] == 'get' for row in setup[0])
    assert any(row[0] == 'cancel_entry' for row in setup[0])


def test_regressing_get_is_not_fresh_coverage(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    exchange = setup[2]
    for value in exchange.orders.values():
        value['updated_ms'] = 1200
    runner.protect(plan(), position(observed_ms=1200), now_ms=1200)
    exchange.get_override = lambda client: {**copy.deepcopy(exchange.orders[client]), 'updated_ms':1100}
    result = runner.protect(plan(), position(observed_ms=1300), now_ms=1300)
    assert result['status'] == 'protection_incomplete'
    assert result['stop_covered_quantity'] == '0'


def test_io_completion_time_advances_and_does_not_use_stale_entry_snapshot(setup):
    from demo_native_protection import NativeProtectionCoordinator
    trace, ledger, exchange = setup
    clock = [0]
    original = exchange.fetch_conditional_order
    def later_get(pair, client):
        clock[0] += 5
        exchange.clock = 1100 + clock[0]
        exchange.orders[client]['updated_ms'] = 1100 + clock[0]
        return original(pair, client)
    exchange.fetch_conditional_order = later_get
    runner = NativeProtectionCoordinator(
        ledger, exchange, account_id='account-1', account_lock=RLock(),
        pause_entries=lambda *args: trace.append(('pause',)),
        cancel_remaining_entry=lambda *args: trace.append(('cancel_entry',)),
        reduce_owned=lambda *args: trace.append(('reduce',)), monotonic_ms=lambda:clock[0])
    result = runner.protect(plan(), position(), now_ms=1100)
    assert result['status'] == 'protected'
    assert all(row['last_exchange_observed_ms'] > 1100 for row in ledger.list_conditionals())


def test_slow_unknown_submit_requests_fallback_with_mandatory_position_refresh(setup):
    from demo_native_protection import NativeProtectionCoordinator
    trace, ledger, exchange = setup
    clock = [0]
    captured = []
    def slow_post(*args, **kwargs):
        clock[0] += 2100
        raise DemoSubmissionUnknown('RequestTimeout')
    exchange.submit_stop_market_order = slow_post
    runner = NativeProtectionCoordinator(
        ledger, exchange, account_id='account-1', account_lock=RLock(),
        pause_entries=lambda *args: trace.append(('pause',)),
        cancel_remaining_entry=lambda *args: trace.append(('cancel_entry',)),
        reduce_owned=lambda evidence, *args: captured.append(evidence), monotonic_ms=lambda:clock[0])
    result = runner.protect(plan(), position(), now_ms=1100)
    assert result['fallback_requested'] is True
    assert captured[0]['protection_request']['requires_fresh_owned_reconciliation'] is True
    assert captured[0]['protection_request']['now_ms'] == 3200


def test_same_parent_cannot_evade_unknown_by_changing_pair(setup):
    setup[2].post_error = DemoSubmissionUnknown('RequestTimeout')
    coordinator(setup).protect(plan(), position(), now_ms=1100)
    result = coordinator(setup).protect(
        plan(pair='ETH/USDT:USDT'), position(pair='ETH/USDT:USDT', observed_ms=1200), now_ms=1200)
    assert result['status'] == 'blocked_evidence'
    assert len([row for row in setup[0] if row[0] == 'post']) == 1


def test_terminal_child_must_be_in_fill_reconciliation_proof(setup):
    runner = coordinator(setup)
    runner.protect(plan(), position(), now_ms=1100)
    _, ledger, exchange = setup
    stop = next(row for row in ledger.list_conditionals() if row['order_type'] == 'STOP_MARKET')
    exchange.clock = 1200
    exchange.orders[stop['client_algo_id']].update(
        algo_status='FINISHED', actual_order_id='900', updated_ms=1200)
    exchange.fetch_conditional_child_order = lambda *args: {
        'order_id':'900', 'status':'FILLED', 'filled_qty':'0.002', 'updated_ms':1200}
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1200), now_ms=1200)
    assert result['cleanup_complete'] is False
    result = runner.protect(plan(), position(signed_quantity='0', observed_ms=1200,
                            reconciled_child_order_ids=['900']), now_ms=1200)
    assert result['cleanup_complete'] is True


def test_later_partial_fill_has_distinct_fallback_key_but_restart_reuses_it(setup):
    trace, _, exchange = setup
    exchange.post_error = DemoSubmissionUnknown('RequestTimeout')
    coordinator(setup).protect(plan(), position(signed_quantity='0.001', observed_ms=3100), now_ms=3100)
    key1 = next(row[2] for row in trace if row[0] == 'reduce')
    coordinator(setup).protect(plan(), position(signed_quantity='0.002', observed_ms=3200), now_ms=3200)
    key2 = [row[2] for row in trace if row[0] == 'reduce'][-1]
    assert key1 != key2
    coordinator(setup).protect(plan(), position(signed_quantity='0.002', observed_ms=3300), now_ms=3300)
    assert [row[2] for row in trace if row[0] == 'reduce'][-1] == key2


def test_market_which_expires_during_gets_cannot_move_stop(setup):
    from demo_native_protection import NativeProtectionCoordinator
    coordinator(setup).protect(plan(), position(), now_ms=1100)
    trace, ledger, exchange = setup
    trace.clear()
    clock = [0]
    original = exchange.fetch_conditional_order
    def slow_get(pair, client):
        clock[0] += 200
        exchange.clock = 1200 + clock[0]
        return original(pair, client)
    exchange.fetch_conditional_order = slow_get
    runner = NativeProtectionCoordinator(
        ledger, exchange, account_id='account-1', account_lock=RLock(),
        pause_entries=lambda *args: trace.append(('pause',)),
        cancel_remaining_entry=lambda *args: trace.append(('cancel_entry',)),
        reduce_owned=lambda *args: trace.append(('reduce',)), monotonic_ms=lambda:clock[0])
    result = runner.protect(plan(), position(observed_ms=1200), now_ms=1200,
                            market=market(price='101.5', observed_ms=1000))
    assert result['status'] == 'blocked_evidence'
    assert not any(row[0] == 'post' for row in trace)
