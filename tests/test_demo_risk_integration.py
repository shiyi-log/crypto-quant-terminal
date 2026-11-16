from unittest.mock import Mock

from demo_execution_service import DemoExecutionService, DemoRunnerConfig


def test_account_snapshot_without_cashflow_and_funding_evidence_blocks_entries():
    store = Mock()
    store.risk_events.return_value = []
    adapter = Mock()
    service = DemoExecutionService(adapter, store, Mock(), DemoRunnerConfig("risk"))
    service._started = True
    service._entries_enabled = True
    service._status["entries_enabled"] = True

    state = service._observe_risk(
        {"margin_balance": "1000", "available_balance": "900"}, [], 1_000
    )

    assert state["entries_mode"] == "blocked_evidence_incomplete"
    assert service._entries_enabled is False
    assert service._status["risk_alert"] == "risk_evidence_incomplete"


def test_complete_account_snapshot_persists_new_risk_events_and_allows_entries():
    store = Mock()
    store.risk_events.return_value = []
    store.append_risk_events = Mock()
    adapter = Mock()
    service = DemoExecutionService(adapter, store, Mock(), DemoRunnerConfig("risk"))
    service._started = True
    service._entries_enabled = True
    service._status["entries_enabled"] = True

    state = service._observe_risk(
        {
            "margin_balance": "1000",
            "available_balance": "900",
            "cumulative_external_net_flow_usdt": "0",
            "funding_fee_complete": True,
            "data_complete": True,
        },
        [],
        57_600_000,
    )

    assert state["entries_mode"] == "enabled"
    assert service._entries_enabled is True
    store.append_risk_events.assert_called_once()
    assert store.append_risk_events.call_args.args[0][0]["event_type"] == "equity_observed"


def test_failed_risk_event_write_is_retried_before_entries_can_resume():
    store = Mock()
    store.risk_events.return_value = []
    store.append_risk_events.side_effect = [OSError("database unavailable"), None]
    service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig("risk"))
    service._started = True
    service._entries_enabled = True
    service._risk_events_loaded = True

    evidence = {"margin_balance": "1000", "cumulative_external_net_flow_usdt": "0",
                "funding_fee_complete": True, "data_complete": True}
    first = service._observe_risk(evidence, [], 57_600_000)
    second = service._observe_risk(evidence, [], 57_600_001)

    assert first["risk_event_persistence_error"] == "OSError"
    assert second["entries_mode"] == "enabled"
    assert store.append_risk_events.call_count == 2
    first_ids = {row["event_id"] for row in store.append_risk_events.call_args_list[0].args[0]}
    second_ids = {row["event_id"] for row in store.append_risk_events.call_args_list[1].args[0]}
    assert first_ids <= second_ids

def test_risk_event_read_error_blocks_entries_without_aborting_protection_cycle():
    store = Mock()
    store.risk_events.return_value = []
    store.risk_events.side_effect = OSError('unavailable')
    service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
    service._entries_enabled = True
    state = service._observe_risk(None, [], 1_000)
    assert state['entries_mode'] == 'blocked_evidence_incomplete'
    assert state['risk_event_persistence_error'] == 'OSError'
    assert service._entries_enabled is False
    assert service._risk_events_loaded is False


def test_malformed_risk_event_store_is_never_treated_as_empty_history():
    store = Mock()
    store.risk_events.return_value = []
    store.risk_events.return_value = {'unexpected': []}
    service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
    state = service._observe_risk(None, [], 1_000)
    assert state['entries_mode'] == 'blocked_evidence_incomplete'
    assert state['risk_event_persistence_error'] == 'TypeError'
    assert service._risk_events_loaded is False


def test_missing_snapshot_cannot_downgrade_a_confirmed_hard_failure():
    service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('risk'))
    service._risk_events_loaded = True
    service.risk_policy.observe_equity(timestamp_ms=0, net_equity_usdt='1000')
    service.risk_policy.observe_equity(timestamp_ms=1, net_equity_usdt='849')
    state = service._observe_risk(None, [], 2)
    assert state['entries_mode'] == 'terminated'
    assert state['hard_failure'] == 'drawdown_limit_reached'

def test_verified_day_opening_restores_daily_loss_before_current_equity():
    store = Mock()
    store.risk_events.return_value = []
    service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
    state = service._observe_risk({
        'margin_balance': '960', 'cumulative_external_net_flow_usdt': '0',
        'funding_fee_complete': True, 'data_complete': True,
        'risk_day_opening': {'timestamp_ms': 57_600_000, 'net_equity_usdt': '1000',
                             'cumulative_external_net_flow_usdt': '0', 'data_complete': True},
    }, [], 60_000_000)
    assert state['daily_loss_usdt'] == '40'
    assert state['entries_mode'] == 'lock_until_reconciled'


def test_future_or_non_midnight_opening_cannot_unlock_account():
    for opening_ts in (57_600_001, 144_000_000):
        store = Mock()
        store.risk_events.return_value = []
        service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
        state = service._observe_risk({
            'margin_balance': '1000', 'cumulative_external_net_flow_usdt': '0',
            'funding_fee_complete': True, 'data_complete': True,
            'risk_day_opening': {'timestamp_ms': opening_ts, 'net_equity_usdt': '1000',
                'cumulative_external_net_flow_usdt': '0', 'data_complete': True},
        }, [], 60_000_000)
        assert state['entries_mode'] == 'blocked_evidence_incomplete'


def test_invalid_current_equity_blocks_entries_without_aborting_risk_observation():
    for invalid in ('NaN', 'not-money', True):
        store = Mock()
        store.risk_events.return_value = []
        service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
        service._entries_enabled = True
        state = service._observe_risk({
            'margin_balance': invalid, 'cumulative_external_net_flow_usdt': '0',
            'funding_fee_complete': True, 'data_complete': True,
        }, [], 57_600_000)
        assert state['entries_mode'] == 'blocked_evidence_incomplete'
        assert state['risk_snapshot_error'] == 'invalid_equity_evidence'
        assert service._entries_enabled is False


def test_risk_hard_exit_repeats_cancel_and_reduce_until_exchange_reconciliation():
    service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('risk'))
    service._reconcile = Mock()
    service._flatten_owned = Mock()
    state = {'entries_mode': 'lock_until_reconciled', 'hard_failure': None}
    service._apply_risk_actions(state, 60_000_000)
    service._apply_risk_actions(state, 60_001_000)
    assert [call.kwargs for call in service._reconcile.call_args_list] == [
        {'cancel_entries': True}, {'cancel_entries': True}]
    assert [call.args for call in service._flatten_owned.call_args_list] == [
        (60_000_000, 'account_risk_lock_until_reconciled'),
        (60_001_000, 'account_risk_lock_until_reconciled')]


def test_cooldown_cancels_entries_but_retains_existing_exit_protection():
    service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('risk'))
    service._reconcile = Mock()
    service._flatten_owned = Mock()
    service._apply_risk_actions({'entries_mode': 'cooldown', 'hard_failure': None}, 60_000_000)
    service._reconcile.assert_called_once_with(60_000_000, cancel_entries=True)
    service._flatten_owned.assert_not_called()


def test_refreshed_account_configuration_denies_excess_leverage_and_unknown_margin_policy():
    service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('risk'))
    service.account = {'execution': {'leverage': 10, 'execution_universe': ['BTC/USDT:USDT']}}
    mode = {'hedged': False}
    row = {'pair': 'BTC/USDT:USDT', 'leverage': 20, 'margin_type': 'ISOLATED',
           'is_auto_add_margin': False}
    assert service._validate_execution_config(mode, [row]) == ['demo_actual_leverage_invalid']
    row.update(leverage=10, is_auto_add_margin=None)
    assert service._validate_execution_config(mode, [row]) == ['demo_auto_add_margin_not_disabled']
    row['is_auto_add_margin'] = False
    assert service._validate_execution_config(mode, [row]) == []
    assert service._status['account_policy']['max_leverage'] == 10


def test_account_risk_store_is_shared_across_strategy_namespaces_and_refreshes_before_observing():
    persisted = []
    shared = Mock()
    shared.risk_events.side_effect = lambda: list(persisted)
    shared.append_risk_events.side_effect = lambda events: persisted.extend(events)
    first = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('first'), risk_store=shared)
    second = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig('second'), risk_store=shared)
    evidence = {'margin_balance': '1000', 'cumulative_external_net_flow_usdt': '0',
                'data_complete': True, 'funding_fee_complete': True}
    first._observe_risk(evidence, [], 57_600_000)
    second._observe_risk(evidence, [], 57_600_001)
    first._observe_risk({**evidence, 'margin_balance': '849'}, [], 57_600_002)
    result = second._observe_risk(evidence, [], 57_600_003)
    assert result['entries_mode'] == 'terminated'
    assert result['hard_failure'] == 'drawdown_limit_reached'


def test_incompatible_schema_blocks_new_risk_without_reading_or_discarding_history():
    store = Mock()
    store.check_schema.side_effect = RuntimeError('migrations pending')
    service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
    state = service._observe_risk(None, [], 57_600_000)
    assert state['entries_mode'] == 'blocked_evidence_incomplete'
    store.risk_events.assert_not_called()


def test_reconciled_snapshot_is_recorded_at_fact_time_and_stale_or_future_facts_do_not_unlock():
    for observed_ms, mode in ((57_600_001, 'enabled'),
                              (57_590_000, 'blocked_evidence_incomplete'),
                              (57_600_003, 'blocked_evidence_incomplete')):
        store = Mock()
        store.risk_events.return_value = []
        service = DemoExecutionService(Mock(), store, Mock(), DemoRunnerConfig('risk'))
        result = service._observe_risk({
            'margin_balance': '1000', 'cumulative_external_net_flow_usdt': '0',
            'data_complete': True, 'funding_fee_complete': True, 'as_of_ms': observed_ms,
            'risk_day_opening': {'timestamp_ms': 57_600_000, 'net_equity_usdt': '1000',
                                'cumulative_external_net_flow_usdt': '0', 'data_complete': True},
        }, [], 57_600_002)
        assert result['entries_mode'] == mode
        if mode == 'enabled':
            facts = [event for event in service.risk_policy.journal
                     if event['event_type'] == 'equity_observed']
            assert facts[-1]['timestamp_ms'] == observed_ms


def test_formal_acceptance_refuses_polling_fallback_even_when_account_configuration_is_valid():
    adapter = Mock()
    adapter.protection_capabilities.return_value = {'acceptance_eligible': False}
    service = DemoExecutionService(adapter, Mock(), Mock(), DemoRunnerConfig('risk'),
                                   native_protection_required=True)
    service.account = {'execution': {'leverage': 10, 'execution_universe': ['BTC/USDT:USDT']}}
    errors = service._validate_execution_config({'hedged': False}, [{
        'pair': 'BTC/USDT:USDT', 'leverage': 10, 'margin_type': 'ISOLATED',
        'is_auto_add_margin': False}])
    assert errors == ['demo_native_protection_unverified']
