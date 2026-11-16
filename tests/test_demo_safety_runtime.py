"""生产安全接线的离线行为合同；不访问交易所或数据库。"""
import copy
import importlib
import importlib.util
import threading
from decimal import Decimal
from unittest.mock import Mock

import pytest

from demo_execution_service import DemoExecutionService, DemoRunnerConfig
from demo_runner_controller import DemoRunnerController
from tests.test_demo_entry_decision import proposal, BTC


def context(account):
    return {'context_id': 'ctx-1', 'sequence': 1, 'environment': 'binance_demo',
        'account_id': account, 'stage_id': 'stage-1000', 'stage_initial_usdt': '1000',
        'net_equity_usdt': '1000', 'account_at_ms': 1100, 'max_account_age_ms': 500,
        'account_snapshot_ref': 'account/snapshot/1', 'account_snapshot_hash': 'e'*64,
        'exposure_watermark': 'external-ledger/1', 'external_risk_usdt': '0',
        'external_margin_usdt': '0', 'external_symbols': [],
        'exposure_scope': 'external_only_excludes_local_reservations',
        'account_reconciled': True, 'exposure_complete': True,
        'margin_evidence_complete': True, 'risk_entries_enabled': True,
        'funding_complete': True, 'costs_complete': True, 'native_protection_verified': True,
        'approvals': {'strategy-v1': {'run_id': 'run-1', 'approved': True,
            'strategy_hash': 'b'*64, 'risk_policy_hash': 'c'*64, 'cost_model_hash': 'd'*64}}}


def runtime_class():
    assert importlib.util.find_spec('demo_safety_runtime'), '生产安全运行时尚未实现'
    return importlib.import_module('demo_safety_runtime').SafetyRuntime


class MemoryReservation:
    account_scope = 'account-safe-id'
    virtual_account_key = 'safe-ns'

    def __init__(self, ledger, trace):
        self.ledger, self.trace = ledger, trace
        self.store, self.environment = ledger.store, ledger.environment
        self.claimed = set()

    def install_context(self, value):
        self.trace.append('context')
        self.context = copy.deepcopy(value)

    def reserve_entry(self, source, intent, *, context_id):
        from demo_entry_decision import propose_entry_decision
        self.trace.append('reserve')
        decision = propose_entry_decision(source).to_dict()
        accepted = decision['verdict'] == 'eligible_proposal'
        decision['reservation_committed'] = accepted
        self.decision = decision
        row = {**intent, 'status': 'intent', 'submission_attempts': 0,
               'risk_reservation_decision_id': source['decision_id'],
               'intent_payload': intent['raw']} if accepted else None
        if row:
            self.ledger.orders[row['client_id']] = row
        return {'accepted': accepted, 'decision': decision, 'order': row, 'reservation': {}}

    def claim_reserved_entry(self, client_id, *, now_ms, context_id):
        self.trace.append('claim_reserved')
        if client_id in self.claimed:
            return False
        self.claimed.add(client_id)
        self.ledger.orders[client_id]['status'] = 'submitting'
        return True

    def mark_reserved_unknown(self, *args, **kwargs):
        self.trace.append('unknown_reserved')

    def decisions(self):
        return [self.decision] if hasattr(self, 'decision') else []


class Ledger:
    virtual_account_key = 'safe-ns'
    environment = 'binance_demo'

    def __init__(self):
        self.store = object()
        self.orders = {}
        self.append_comparison = Mock()
        self.reserve_order = Mock(side_effect=AssertionError('不应使用旧预占'))
        self.claim_submission = Mock(side_effect=AssertionError('不应使用普通领取'))
        self.risk_events = Mock(return_value=[])
        self.append_risk_events = Mock()

    def get_order(self, client):
        return self.orders.get(client)

    def runner_orders(self):
        return list(self.orders.values())

    def net_fill_positions(self):
        return {}

    def record_order_observation(self, client, value):
        self.orders[client].update(value)
        return self.orders[client]


def event(kind='entry'):
    return {'event_id': 'signal-1', 'event_type': 'order_submitted',
            'rule_hash': 'b'*64, 'data_fingerprint': 'a'*64,
            'observed_at_ms': 1000, 'candle_ms': 1000,
            'payload': {'pair': BTC, 'kind': kind, 'buy_sell': 'buy',
                        'signal_candle_ms': 1000, 'evidence': {'entry_atr': '1'}}}


def make_service(*, runtime=None, required=True, trace=None):
    trace = trace if trace is not None else []
    ledger, adapter, paper = Ledger(), Mock(), Mock()
    adapter.protection_capabilities.return_value = {'acceptance_eligible': True}
    adapter.fetch_open_orders.return_value = []
    adapter.submit_limit_order.side_effect = lambda *a, **k: trace.append('post') or {'status': 'open'}
    service = DemoExecutionService(adapter, ledger, paper,
        DemoRunnerConfig('strategy-v1', virtual_account_key='safe-ns', trading_enabled=True),
        clock_ms=lambda: 1200, gate_registry=Mock(is_enabled=lambda key: True),
        safety_runtime=runtime, safety_runtime_required=required, account_writer_lock=threading.RLock())
    service.account = {'rule_hash': 'b'*64, 'execution_hash': '2'*64,
        'definition': {'params': {'risk_stop_atr_mult': '1'}},
        'execution': {'timeframe': '1m', 'execution_universe': [BTC], 'stop_policy': 'atr'}}
    service.binding = {'input_fingerprint': 'a'*64, 'forward_boundary_ms': 900}
    service._entries_enabled = True
    return service


def attach_runtime(service, trace, **providers):
    reserve = MemoryReservation(service.store, trace)
    source = proposal(account_snapshot_hash='e'*64, market_snapshot_hash='f'*64,
                      exchange_rules_hash='1'*64, exit_params_hash='2'*64,
                      exposure_watermark='external-ledger/1', frozen_max_notional_usdt='1000')
    defaults = {
        'context_provider': lambda **kw: {'committed': True, 'archive_ref': 'archive/context/1',
            'context': context('account-safe-id')},
        'entry_source_provider': lambda **kw: {'committed': True, 'archive_ref': 'archive/entry/1', 'source': source},
        'protection_facts_provider': lambda **kw: {'committed': True, 'archive_ref': 'archive/protection/1', 'items': []},
        'completion_provider': lambda **kw: {'committed': True, 'archive_ref': 'archive/costs/1', 'events': []},
    }
    defaults.update(providers)
    # 内部归档必须显式绑定账户范围，不能仅依赖 provider 被调用时的参数。
    for name, provider in list(defaults.items()):
        if callable(provider):
            def scoped(*, _provider=provider, **kwargs):
                value = _provider(**kwargs)
                return {'account_scope': 'account-safe-id', **value} if isinstance(value, dict) else value
            defaults[name] = scoped
    runtime = runtime_class()(reserve, account_scope='account-safe-id',
        account_lock=service._account_lock(), conditional_store=Mock(virtual_account_key='safe-ns',
            store=service.store.store, environment='binance_demo'), **defaults)
    service.safety_runtime = runtime
    runtime.bind(service)
    return runtime, reserve, source


def test_production_missing_runtime_denies_before_legacy_candle_sizing_or_reserve():
    service = make_service()
    service.paper.closed_candle.side_effect = AssertionError('不能读取旧定仓输入')
    service._paper_event(event(), 1200, [])
    service.store.reserve_order.assert_not_called()
    service.adapter.prepare_maker_limit_order.assert_not_called()
    service.adapter.submit_limit_order.assert_not_called()
    payload = service.store.append_comparison.call_args.args[0]
    assert payload['reason'] == 'safety_runtime_unavailable'
    assert '权威' in payload['reason_zh']


def test_missing_runtime_keeps_strategy_exit_path_available():
    service = make_service()
    service._reconcile = Mock()
    service._exit = Mock()
    service._paper_event(event('exit'), 1200, [])
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)
    service._exit.assert_called_once()


def test_internal_evidence_commits_reservation_then_reserved_claim_then_single_post():
    trace = []
    service = make_service(trace=trace)
    attach_runtime(service, trace)
    service._paper_event(event(), 1200, [])
    service._paper_event(event(), 1200, [])
    assert trace.count('post') == 1
    assert trace.index('reserve') < trace.index('claim_reserved') < trace.index('post')
    service.store.reserve_order.assert_not_called()
    service.store.claim_submission.assert_not_called()
    submitted = service.adapter.submit_limit_order.call_args.args
    assert submitted[:4] == (BTC, 'buy', '10', '100')


@pytest.mark.parametrize('field,value', [('entry_source_provider', None), ('context_provider', None),
    ('protection_facts_provider', None), ('completion_provider', None)])
def test_any_missing_internal_provider_denies_before_atomic_reserve(field, value):
    service = make_service()
    trace = []
    attach_runtime(service, trace, **{field: value})
    service._paper_event(event(), 1200, [])
    assert trace == []
    service.adapter.submit_limit_order.assert_not_called()


@pytest.mark.parametrize('change', [{'quote_at_ms': 1}, {'account_id': 'other'},
    {'signal_id': 'other'}, {'exit_params_hash': '9'*64}, {'atr14': '2'},
    {'funding_complete': False}, {'stop_price': '98', 'target_price': '104'}])
def test_stale_or_mismatched_final_evidence_cannot_post(change):
    service = make_service()
    trace = []
    _, _, source = attach_runtime(service, trace)
    source.update(change)
    service._paper_event(event(), 1200, [])
    assert 'reserve' not in trace
    service.adapter.submit_limit_order.assert_not_called()


def test_real_adapter_capability_gate_remains_required_even_with_internal_true_flags():
    service = make_service()
    trace = []
    attach_runtime(service, trace)
    service.adapter.protection_capabilities.return_value = {'acceptance_eligible': False}
    service._paper_event(event(), 1200, [])
    assert trace == []
    service.adapter.submit_limit_order.assert_not_called()


def test_ambiguous_post_uses_reserved_unknown_without_releasing_risk():
    service = make_service()
    trace = []
    attach_runtime(service, trace)
    service.adapter.submit_limit_order.side_effect = TimeoutError()
    service._paper_event(event(), 1200, [])
    assert 'unknown_reserved' in trace
    assert 'release' not in trace


def test_controller_scoped_production_always_requires_runtime_and_same_account_lock():
    data, ledger = Mock(), Ledger()
    controller = DemoRunnerController(data, ledger, Mock(), Mock(),
        risk_gate_required=False, risk_account_scope='account-safe-id',
        ledger_factory=lambda namespace: Mock(), safety_runtime_factory=None)
    service = controller._service('strategy-v1', ledger, Mock())
    assert service.safety_runtime_required is True
    assert service._account_lock() is controller._account_cycle_lock
    assert service.status()['acceptance_eligible'] is False


def test_missing_protection_facts_pauses_and_cancels_without_blocking_legacy_protect():
    service = make_service()
    runtime, _, _ = attach_runtime(service, [], protection_facts_provider=lambda **kw: None)
    service._reconcile = Mock()
    result = runtime.reconcile(service, now_ms=1200, positions=[])
    assert result['entries_allowed'] is False
    assert service._entries_enabled is False
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)


def test_only_committed_complete_cost_events_enter_risk_policy_once():
    service = make_service()
    completion = {'committed': True, 'archive_ref': 'cost/archive/1', 'events': [
        {'event_id': 'closed-1', 'trade_id': 'trade-1', 'timestamp_ms': 1100,
         'net_pnl_usdt': '-1', 'complete': True, 'funding_complete': True}]}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: completion)
    runtime.record_completions(service, now_ms=1200)
    runtime.record_completions(service, now_ms=1200)
    assert len(service.risk_policy.journal) == 1
    assert service.risk_policy.pending_events() == []
    service.store.append_risk_events.assert_called_once()
    completion['events'][0]['funding_complete'] = False
    completion['events'][0]['event_id'] = 'closed-2'
    runtime.record_completions(service, now_ms=1200)
    assert len(service.risk_policy.journal) == 1


@pytest.mark.parametrize('provider', ['protection_facts_provider', 'completion_provider'])
def test_callable_but_uncommitted_internal_facts_cannot_reserve(provider):
    service = make_service()
    trace = []
    attach_runtime(service, trace, **{provider: lambda **kw: None})
    service._reconcile = Mock()
    service._paper_event(event(), 1200, [])
    assert 'reserve' not in trace
    service.adapter.submit_limit_order.assert_not_called()


def test_invalid_cost_batch_does_not_partially_mutate_risk_or_persist():
    service = make_service()
    completion = {'committed': True, 'archive_ref': 'cost/archive/1', 'events': [
        {'event_id': 'closed-1', 'trade_id': 'trade-1', 'timestamp_ms': 1100,
         'net_pnl_usdt': '-1', 'complete': True, 'funding_complete': True},
        {'event_id': 'closed-2', 'trade_id': 'trade-2', 'timestamp_ms': 1101,
         'net_pnl_usdt': 'NaN', 'complete': True, 'funding_complete': True}]}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: completion)
    assert runtime.record_completions(service, now_ms=1200) is False
    assert service.risk_policy.journal == []
    service.store.append_risk_events.assert_not_called()


def test_cost_write_failure_pauses_entries_and_replays_same_durable_event_after_recovery():
    service = make_service()
    completion = {'committed': True, 'archive_ref': 'cost/archive/1', 'events': [
        {'event_id': 'closed-1', 'trade_id': 'trade-1', 'timestamp_ms': 1100,
         'net_pnl_usdt': '-1', 'complete': True, 'funding_complete': True}]}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: completion)
    service.store.append_risk_events.side_effect = [OSError(), None]
    assert runtime.record_completions(service, now_ms=1200) is False
    assert service.risk_policy.journal == []
    assert service._entries_enabled is False
    assert runtime.record_completions(service, now_ms=1201) is True
    assert len(service.risk_policy.journal) == 1
    assert service._entries_enabled is False


def test_owned_position_facts_run_native_coordinator_and_ack_cannot_grant_completion():
    service = make_service()
    service.store.net_fill_positions = lambda: {BTC: Decimal('2')}
    service._actual_net = {BTC: Decimal('2')}
    facts = {'committed': True, 'archive_ref': 'protection/1', 'items': []}
    runtime, reserve, source = attach_runtime(service, [], protection_facts_provider=lambda **kw: facts)
    reserve.decision = {**source, 'reservation_committed': True}
    service.store.orders['parent'] = {'client_id': 'parent', 'pair': BTC, 'reduce_only': False,
        'average_price': '100', 'risk_reservation_decision_id': 'decision-1',
        'raw': {'entry_decision_id': 'decision-1', 'entry_atr': '1', 'risk_stop_atr_mult': '1'}}
    facts['items'] = [{'plan': {'pair': BTC, 'strategy_id': 'strategy-v1',
        'parent_entry_client_id': 'parent', 'entry_decision_id': 'decision-1', 'run_id': 'run-1',
        'exit_params_hash': '2'*64, 'entry_atr': '1', 'entry_price': '100',
        'initial_stop_price': '99', 'target_price': '102'},
        'position': {'pair': BTC, 'strategy_id': 'strategy-v1', 'signed_quantity': '2'}}]
    runtime.coordinator = Mock()
    runtime.coordinator.protect.return_value = {'status': 'protected', 'execution_complete': False}
    result = runtime.reconcile(service, now_ms=1200, positions=[{'pair': BTC, 'signed_qty': '2'}])
    runtime.coordinator.protect.assert_called_once()
    assert result['acceptance_eligible'] is False
    assert result['protection'][0]['execution_complete'] is False


def test_missing_owned_position_item_pauses_entry_and_cancels_remaining_entry():
    service = make_service()
    service.store.net_fill_positions = lambda: {BTC: Decimal('2')}
    service._actual_net = {BTC: Decimal('2')}
    service._reconcile = Mock()
    runtime, _, _ = attach_runtime(service, [])
    result = runtime.reconcile(service, now_ms=1200, positions=[{'pair': BTC, 'signed_qty': '2'}])
    assert result['entries_allowed'] is False
    assert service._entries_enabled is False
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)


def test_emergency_reduction_refreshes_owned_facts_and_passes_stable_key_to_exit():
    service = make_service()
    service._reconcile = Mock()
    service._exit = Mock()
    service.store.net_fill_positions = lambda: {BTC: Decimal('2')}
    service.adapter.fetch_positions.return_value = [{'pair': BTC, 'signed_qty': '2'}]
    runtime, _, _ = attach_runtime(service, [])
    runtime._reduce_owned(service, {'pair': BTC, 'signed_quantity': '2'}, 'timeout', 'stable-fallback')
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)
    service._exit.assert_called_once_with(BTC, Decimal('2'), 'timeout', 'stable-fallback', 1200)
    service.adapter.fetch_positions.return_value = [{'pair': BTC, 'signed_qty': '3'}]
    runtime._reduce_owned(service, {'pair': BTC, 'signed_quantity': '2'}, 'timeout', 'stable-fallback')
    assert service._exit.call_count == 1


def test_protection_audit_failure_keeps_entry_paused_without_escaping_reconcile():
    service = make_service()
    service._reconcile = Mock()
    service.store.append_comparison.side_effect = OSError()
    runtime, _, _ = attach_runtime(service, [], protection_facts_provider=lambda **kw: None)
    result = runtime.reconcile(service, now_ms=1200, positions=[])
    assert result['entries_allowed'] is False
    assert service._entries_enabled is False
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)
    assert service._status['safety_runtime_audit_error'] == 'OSError'


def test_controller_runtime_factory_error_cannot_fall_back_to_legacy_sizing():
    controller = DemoRunnerController(Mock(), Ledger(), Mock(), Mock(), risk_gate_required=False,
        ledger_factory=lambda namespace: Mock(), safety_runtime_factory=lambda **kw: None)
    service = controller._service('strategy-v1', controller.demo_store, Mock())
    assert service.safety_runtime_required is True
    assert service.safety_runtime is None


def test_context_stage_different_from_restored_risk_policy_fails_closed():
    service = make_service()
    trace = []
    runtime, _, source = attach_runtime(service, trace, context_provider=lambda **kw: {
        'committed': True, 'archive_ref': 'context/100',
        'context': {**context('account-safe-id'), 'stage_initial_usdt': '100', 'net_equity_usdt': '100'}})
    source.update(stage_initial_usdt='100', net_equity_usdt='100')
    service._paper_event(event(), 1200, [])
    assert 'reserve' not in trace
    assert runtime.store.claimed == set()


def test_emergency_reduction_does_not_use_fresh_quantity_with_unowned_open_order():
    service = make_service()
    service._reconcile = Mock()
    service._exit = Mock()
    service.store.net_fill_positions = lambda: {BTC: Decimal('2')}
    service.adapter.fetch_positions.return_value = [{'pair': BTC, 'signed_qty': '2'}]
    service.adapter.fetch_open_orders.return_value = [{'pair': BTC, 'client_id': 'external'}]
    runtime, _, _ = attach_runtime(service, [])
    runtime._reduce_owned(service, {'pair': BTC, 'signed_quantity': '2'}, 'timeout', 'stable-fallback')
    service._exit.assert_not_called()


def test_completion_archive_for_other_account_cannot_enter_risk_journal():
    service = make_service()
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: {
        'committed': True, 'archive_ref': 'cost/other', 'account_scope': 'other-account', 'events': [
            {'complete': True, 'funding_complete': True, 'event_id': 'other-1',
             'trade_id': 'other-trade', 'timestamp_ms': 1100, 'net_pnl_usdt': '-1'}]})
    assert runtime.record_completions(service, now_ms=1200) is False
    assert service.risk_policy.journal == []


def test_protection_provider_failure_in_real_cycle_keeps_owned_protect_and_signal_exit():
    service = make_service()
    attach_runtime(service, [], protection_facts_provider=lambda **kw: None)
    service._started = True
    service._metadata_checked_ms = 1200
    service.paper.account.return_value = service.account
    service.paper.events.return_value = [{**event('exit'), 'sequence_id': 1}]
    service.adapter.fetch_positions.return_value = []
    service.adapter.fetch_balance.return_value = {}
    service.store.append_position_snapshot = Mock()
    service.store.append_account_snapshot = Mock()
    service.store.runner_fills = Mock(return_value=[])
    service._reconcile = Mock()
    service._observe_risk = Mock(return_value={'entries_mode': 'enabled'})
    service._apply_risk_actions = Mock()
    service._finalize_absent_submissions = Mock(return_value=False)
    service._ownership_conflicts = Mock(return_value=[])
    service._protect = Mock()
    service._exit = Mock()
    state = service.cycle(1200)
    service._protect.assert_called_once_with(1200)
    service._exit.assert_called_once()
    assert state['entries_enabled'] is False
    assert state['status'] != 'reconciliation_error'


def test_runtime_binding_rejects_other_database_despite_matching_account_namespace():
    service = make_service()
    runtime, reserve, _ = attach_runtime(service, [])
    reserve.store = object()
    with pytest.raises(ValueError):
        runtime.bind(service)


def costs(*rows):
    return [{'event_id': name, 'trade_id': name, 'timestamp_ms': stamp, 'net_pnl_usdt': pnl,
             'complete': True, 'funding_complete': True} for name, stamp, pnl in rows]


def test_unsorted_complete_batch_does_not_persist_arrival_order_that_bypasses_three_losses():
    service = make_service()
    events = costs(('loss1', 1010, '-1'), ('loss2', 1020, '-1'), ('win', 1000, '1'), ('loss3', 1030, '-1'))
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: {
        'committed': True, 'archive_ref': 'cost/index', 'events': events})
    assert runtime.record_completions(service, now_ms=1200) is True
    assert service.risk_policy.status(1200)['cooldown_until_ms'] == 3601030
    assert service._entries_enabled is False
    assert [row['timestamp_ms'] for row in service.risk_policy.journal] == [1000, 1010, 1020, 1030]


@pytest.mark.parametrize('restart', [False, True])
def test_earlier_unseen_completion_across_batches_or_restart_pauses_without_consuming(restart):
    service = make_service()
    snapshot = {'committed': True, 'archive_ref': 'cost/index',
                'events': costs(('loss1', 1010, '-1'), ('loss2', 1020, '-1'))}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: snapshot)
    assert runtime.record_completions(service, now_ms=1200) is True
    original = service.risk_policy.journal
    if restart:
        service = make_service()
        service.store.risk_events.return_value = original
        runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: snapshot)
    snapshot['events'] = costs(('win', 1000, '1'), ('loss3', 1030, '-1'))
    service._reconcile = Mock()
    assert runtime.record_completions(service, now_ms=1200) is False
    assert service._entries_enabled is False
    assert service.risk_policy.journal == original
    service._reconcile.assert_called_once_with(1200, cancel_entries=True)


def test_ambiguous_same_time_new_trade_rejects_but_exact_old_duplicate_is_idempotent():
    service = make_service()
    snapshot = {'committed': True, 'archive_ref': 'cost/index', 'events': costs(('loss1', 1010, '-1'))}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: snapshot)
    assert runtime.record_completions(service, now_ms=1200) is True
    assert runtime.record_completions(service, now_ms=1201) is True
    snapshot['events'] = costs(('other', 1010, '1'))
    service._reconcile = Mock()
    assert runtime.record_completions(service, now_ms=1201) is False
    assert len(service.risk_policy.journal) == 1


def test_full_stable_replay_never_extends_three_loss_cooldown():
    service = make_service()
    snapshot = {'committed': True, 'archive_ref': 'cost/index',
                'events': costs(('loss1', 1010, '-1'), ('loss2', 1020, '-1'), ('loss3', 1030, '-1'))}
    runtime, _, _ = attach_runtime(service, [], completion_provider=lambda **kw: snapshot)
    assert runtime.record_completions(service, now_ms=1200) is True
    original = service.risk_policy.status(1200)['cooldown_until_ms']
    assert runtime.record_completions(service, now_ms=5000) is True
    assert service.risk_policy.status(5000)['cooldown_until_ms'] == original
    assert len(service.risk_policy.journal) == 3


def test_preexisting_out_of_order_durable_cost_journal_blocks_until_manual_reconciliation():
    service = make_service()
    service.store.risk_events.return_value = [
        {'event_id': 'loss1', 'event_type': 'round_trip_closed', 'timestamp_ms': 1010,
         'payload': {'trade_id': 'loss1', 'net_pnl_usdt': '-1'}},
        {'event_id': 'win', 'event_type': 'round_trip_closed', 'timestamp_ms': 1000,
         'payload': {'trade_id': 'win', 'net_pnl_usdt': '1'}}]
    runtime, _, _ = attach_runtime(service, [])
    service._reconcile = Mock()
    assert runtime.record_completions(service, now_ms=1200) is False
    assert service._entries_enabled is False
    service.store.append_risk_events.assert_not_called()


@pytest.mark.parametrize('scope', ['postgresql://operator:synthetic-key@db/quant', 'account' + chr(10) + 'secret',
                                  'https://user:synthetic-key@host/path'])
def test_credential_or_control_account_scope_is_rejected_before_any_logs(scope, caplog):
    service = make_service()
    reserve = MemoryReservation(service.store, [])
    reserve.account_scope = scope
    with pytest.raises(ValueError):
        runtime_class()(reserve, account_scope=scope, account_lock=service._account_lock())
    assert 'synthetic-key' not in caplog.text
    assert 'secret' not in caplog.text


def test_missing_runtime_extra_cancel_failure_keeps_protect_and_strategy_exit_in_real_cycle():
    service = make_service()
    service._started = True
    service._metadata_checked_ms = 1200
    service.paper.account.return_value = service.account
    service.paper.events.return_value = [{**event('exit'), 'sequence_id': 1}]
    service.adapter.fetch_positions.return_value = []
    service.adapter.fetch_balance.return_value = {}
    service.store.append_position_snapshot = Mock()
    service.store.append_account_snapshot = Mock()
    service.store.runner_fills = Mock(return_value=[])
    service._reconcile = Mock(side_effect=[None, OSError(), None])
    service._observe_risk = Mock(return_value={'entries_mode': 'enabled'})
    service._apply_risk_actions = Mock()
    service._finalize_absent_submissions = Mock(return_value=False)
    service._ownership_conflicts = Mock(return_value=[])
    service._protect = Mock()
    service._exit = Mock()
    state = service.cycle(1200)
    service._protect.assert_called_once_with(1200)
    service._exit.assert_called_once()
    assert state['entries_enabled'] is False
    assert state['status'] != 'reconciliation_error'
    assert state['safety_runtime_cancel_error'] == 'OSError'
