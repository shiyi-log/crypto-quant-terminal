"""EntryDecision 纯计算合同；不写数据库，不代表预算已事务预占。"""
import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
import importlib
import importlib.util
import json

import pytest

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
TRX = 'TRX/USDT:USDT'


@pytest.fixture
def module():
    return (importlib.import_module('demo_entry_decision')
            if importlib.util.find_spec('demo_entry_decision') else None)


def proposal(**changes):
    data = {
        'environment': 'binance_demo', 'account_id': 'account-safe-id',
        'strategy_version_id': 'strategy-v1', 'run_id': 'run-1', 'stage_id': 'stage-1000',
        'signal_id': 'signal-1', 'decision_id': 'decision-1',
        'input_fingerprint': 'a' * 64, 'strategy_hash': 'b' * 64,
        'risk_policy_hash': 'c' * 64, 'cost_model_hash': 'd' * 64,
        'account_snapshot_hash': 'a' * 64, 'market_snapshot_hash': 'b' * 64,
        'exchange_rules_hash': 'c' * 64, 'exit_params_hash': 'd' * 64,
        'account_snapshot_ref': 'account/snapshot/1', 'market_snapshot_ref': 'market/quote/1',
        'exchange_rules_ref': 'exchange/rules/1', 'exposure_watermark': 'account-ledger/55',
        'signal_at_ms': 1000, 'decision_at_ms': 1200, 'quote_at_ms': 1150,
        'account_at_ms': 1100, 'max_signal_age_ms': 1000, 'max_quote_age_ms': 500,
        'max_account_age_ms': 500,
        'strategy_approved': True, 'indicators_ready': True, 'data_complete': True,
        'inputs_closed': True, 'time_aligned': True, 'costs_complete': True,
        'funding_complete': True, 'account_reconciled': True, 'risk_entries_enabled': True,
        'native_protection_verified': True, 'exposure_complete': True,
        'margin_evidence_complete': True,
        'actual_leverage': 10, 'margin_type': 'ISOLATED', 'is_auto_add_margin': False,
        'hedged': False, 'pair': BTC, 'side': 'buy', 'stage_initial_usdt': '1000',
        'net_equity_usdt': '1000', 'used_risk_usdt': '0', 'used_margin_usdt': '0',
        'active_symbols': [], 'entry_price': '100', 'stop_price': '99',
        'target_price': '102', 'atr14': '1', 'price_tick': '0.01',
        'quantity_step': '0.01', 'min_quantity': '0.01', 'max_quantity': '10000',
        'min_notional_usdt': '5', 'frozen_max_loss_usdt': '100',
        'frozen_max_notional_usdt': '100000', 'initial_margin_per_unit_usdt': '10',
        'entry_maker_fee_rate': '0', 'stop_taker_fee_rate': '0',
        'target_exit_fee_rate': '0', 'stop_slippage_per_unit_usdt': '0',
        'target_slippage_per_unit_usdt': '0', 'funding_expense_per_unit_usdt': '0',
        'other_cost_per_unit_usdt': '0',
    }
    data.update(changes)
    return data


def evaluate(module, **changes):
    assert module is not None, 'immutable EntryDecision sizing module is missing'
    return module.propose_entry_decision(proposal(**changes))


def payload(module, **changes):
    return evaluate(module, **changes).to_dict()


def test_exact_budget_boundaries_accept_and_never_claim_reservation(module):
    result = payload(module, used_risk_usdt='10', used_margin_usdt='400')
    assert result['verdict'] == 'eligible_proposal'
    assert result['reason_codes'] == []
    assert result['quantity'] == '10'
    assert result['planned_loss_usdt'] == '10'
    assert result['net_target_gain_usdt'] == '20'
    assert result['net_reward_risk_ratio'] == '2'
    assert result['risk_base_usdt'] == '1000'
    assert result['account_risk_after_usdt'] == '20'
    assert result['account_margin_after_usdt'] == '500'
    assert result['reservation_committed'] is False
    assert result['acceptance_eligible'] is False


@pytest.mark.parametrize('equity,expected_base,expected_quantity', [
    ('1200', '1000', '10'), ('1000', '1000', '10'), ('800', '800', '8'),
])
def test_profit_does_not_compound_and_losses_shrink_new_risk(module, equity, expected_base, expected_quantity):
    result = payload(module, net_equity_usdt=equity)
    assert result['risk_base_usdt'] == expected_base
    assert result['quantity'] == expected_quantity


@pytest.mark.parametrize('capital,loss,total,margin', [
    ('100', '1', '2', '50'), ('300', '3', '6', '150'), ('1000', '10', '20', '500'),
])
def test_stage_limits_scale_without_changing_policy(module, capital, loss, total, margin):
    result = payload(module, stage_initial_usdt=capital, net_equity_usdt=capital)
    assert result['entry_risk_limit_usdt'] == loss
    assert result['account_risk_limit_usdt'] == total
    assert result['account_margin_limit_usdt'] == margin


def test_remaining_account_risk_and_margin_and_frozen_caps_each_limit_quantity(module):
    assert payload(module, used_risk_usdt='19.55')['quantity'] == '0.45'
    assert payload(module, used_margin_usdt='499')['quantity'] == '0.1'
    assert payload(module, frozen_max_loss_usdt='3.456')['quantity'] == '3.45'
    assert payload(module, frozen_max_notional_usdt='123.45')['quantity'] == '1.23'
    assert payload(module, max_quantity='2.345')['quantity'] == '2.34'


def test_quantity_never_rounds_up_for_minimum_order(module):
    result = payload(module, used_risk_usdt='19.999', quantity_step='0.01')
    assert result['verdict'] == 'rejected'
    assert result['quantity'] == '0'
    assert 'quantity_below_minimum' in result['reason_codes']
    assert 'notional_below_minimum' in result['reason_codes']
    result = payload(module, min_notional_usdt='1001')
    assert result['verdict'] == 'rejected'
    assert result['quantity'] == '10'
    assert 'notional_below_minimum' in result['reason_codes']


def test_exact_costs_recompute_loss_gain_rr_and_quantity(module):
    result = payload(module, entry_maker_fee_rate='0.001', stop_taker_fee_rate='0.002',
                     target_exit_fee_rate='0.002', stop_slippage_per_unit_usdt='0.01',
                     target_slippage_per_unit_usdt='0.02',
                     funding_expense_per_unit_usdt='0.01', other_cost_per_unit_usdt='0.005')
    # Loss per unit = 1 + .1 + .198 + .01 + .01 + .005 = 1.323.
    assert result['quantity'] == '7.55'
    assert result['planned_loss_usdt'] == '9.98865'
    assert result['expected_stop_cost_usdt'] == '2.43865'
    # Gain per unit = 2 - .1 - .204 - .02 - .01 - .005 = 1.661.
    assert result['net_target_gain_usdt'] == '12.54055'
    assert result['verdict'] == 'rejected'
    assert 'net_reward_risk_below_1_5' in result['reason_codes']


def test_short_costs_use_actual_stop_and_target_prices(module):
    result = payload(module, side='sell', stop_price='101', target_price='98',
                     stop_taker_fee_rate='0.001', target_exit_fee_rate='0.001')
    assert result['quantity'] == '9.08'
    assert result['planned_loss_usdt'] == '9.99708'
    assert result['net_target_gain_usdt'] == '17.27016'
    assert result['verdict'] == 'eligible_proposal'


def test_rr_threshold_is_checked_by_exact_cross_product(module):
    # 2 - c >= 1.5 * (1+c) iff c <= .2.
    exact = payload(module, other_cost_per_unit_usdt='0.2')
    assert exact['verdict'] == 'eligible_proposal'
    below = payload(module, other_cost_per_unit_usdt='0.2000000000000000000000000000000000000001')
    assert below['verdict'] == 'rejected'
    assert 'net_reward_risk_below_1_5' in below['reason_codes']


@pytest.mark.parametrize('changes,reason', [
    ({'side': 'buy', 'stop_price': '101', 'target_price': '98'}, 'stop_direction_invalid'),
    ({'side': 'sell', 'stop_price': '99', 'target_price': '102'}, 'stop_direction_invalid'),
    ({'target_price': '103'}, 'target_not_frozen_2r'),
    ({'entry_price': '100.001'}, 'entry_price_not_on_tick'),
    ({'stop_price': '99.001'}, 'stop_price_not_on_tick'),
    ({'target_price': '102.001'}, 'target_price_not_on_tick'),
])
def test_prices_are_validated_and_never_silently_adjusted(module, changes, reason):
    result = payload(module, **changes)
    assert result['verdict'] == 'rejected'
    assert reason in result['reason_codes']
    for key, value in changes.items():
        if key.endswith('price'):
            assert Decimal(result[key]) == Decimal(value)


def test_active_positions_and_pending_entries_share_two_symbol_slots(module):
    result = payload(module, pair=TRX, active_symbols=['BTCUSDT', ETH])
    assert result['verdict'] == 'rejected'
    assert 'active_symbol_limit_exceeded' in result['reason_codes']
    same = payload(module, active_symbols=['BTCUSDT', BTC, 'ETHUSDT'])
    assert same['verdict'] == 'eligible_proposal'
    assert same['active_symbols_after'] == [BTC, ETH]


def test_lower_equity_preserves_existing_overbudget_amounts_and_rejects(module):
    result = payload(module, net_equity_usdt='500', used_risk_usdt='18', used_margin_usdt='300')
    assert result['verdict'] == 'rejected'
    assert result['account_risk_before_usdt'] == '18'
    assert result['account_margin_before_usdt'] == '300'
    assert 'account_risk_budget_exhausted' in result['reason_codes']
    assert 'account_margin_budget_exhausted' in result['reason_codes']


@pytest.mark.parametrize('key,value,reason', [
    ('strategy_approved', None, 'strategy_approved_required'),
    ('native_protection_verified', 'true', 'native_protection_verified_required'),
    ('costs_complete', 1, 'costs_complete_required'),
    ('funding_complete', False, 'funding_complete_required'),
    ('risk_entries_enabled', False, 'risk_entries_enabled_required'),
    ('account_reconciled', None, 'account_reconciled_required'),
    ('exposure_complete', False, 'exposure_complete_required'),
    ('margin_evidence_complete', False, 'margin_evidence_complete_required'),
    ('is_auto_add_margin', 'false', 'auto_add_margin_not_disabled'),
    ('hedged', None, 'one_way_mode_required'),
    ('margin_type', 'CROSSED', 'isolated_margin_required'),
    ('actual_leverage', 11, 'actual_leverage_invalid'),
    ('actual_leverage', True, 'actual_leverage_invalid'),
    ('initial_margin_per_unit_usdt', '9.99', 'margin_estimate_below_leverage_floor'),
])
def test_unknown_evidence_or_unsafe_configuration_never_opens_gate(module, key, value, reason):
    result = payload(module, **{key: value})
    assert result['verdict'] == 'rejected'
    assert reason in result['reason_codes']


@pytest.mark.parametrize('key,value', [
    ('net_equity_usdt', None), ('net_equity_usdt', 0), ('stage_initial_usdt', -1),
    ('stage_initial_usdt', True), ('used_risk_usdt', '-.1'), ('used_margin_usdt', 'NaN'),
    ('entry_maker_fee_rate', None), ('funding_expense_per_unit_usdt', None),
    ('stop_slippage_per_unit_usdt', 'Infinity'), ('other_cost_per_unit_usdt', '-.01'),
    ('atr14', '0'), ('quantity_step', '0'), ('price_tick', '0'),
    ('frozen_max_loss_usdt', 'NaN'), ('min_notional_usdt', None),
])
def test_invalid_decimal_fields_reject_without_inferred_zero(module, key, value):
    result = payload(module, **{key: value})
    assert result['verdict'] == 'rejected'
    assert f'{key}_invalid' in result['reason_codes']
    assert result[key] is None


@pytest.mark.parametrize('changes,reason', [
    ({'environment': 'live'}, 'environment_invalid'),
    ({'account_id': ''}, 'account_id_invalid'),
    ({'strategy_version_id': None}, 'strategy_version_id_invalid'),
    ({'stage_id': []}, 'stage_id_invalid'),
    ({'cost_model_hash': 'not-a-hash'}, 'cost_model_hash_invalid'),
    ({'input_fingerprint': None}, 'input_fingerprint_invalid'),
    ({'account_snapshot_ref': ''}, 'account_snapshot_ref_invalid'),
    ({'exposure_watermark': ''}, 'exposure_watermark_invalid'),
    ({'active_symbols': None}, 'active_symbols_invalid'),
    ({'active_symbols': ['SOLUSDT']}, 'active_symbols_invalid'),
    ({'pair': 'SOLUSDT'}, 'pair_invalid'),
    ({'side': 'BUY'}, 'side_invalid'),
])
def test_all_identity_and_source_references_are_explicit(module, changes, reason):
    result = payload(module, **changes)
    assert result['verdict'] == 'rejected'
    assert reason in result['reason_codes']


@pytest.mark.parametrize('changes,reason', [
    ({'signal_at_ms': 1201}, 'signal_from_future'),
    ({'quote_at_ms': 1201}, 'quote_from_future'),
    ({'account_at_ms': 1201}, 'account_from_future'),
    ({'signal_at_ms': 0}, 'signal_stale'),
    ({'quote_at_ms': 699}, 'quote_stale'),
    ({'account_at_ms': 699}, 'account_stale'),
    ({'signal_at_ms': True}, 'signal_at_ms_invalid'),
    ({'max_signal_age_ms': None}, 'max_signal_age_ms_invalid'),
])
def test_signal_quote_and_account_freshness_is_frozen(module, changes, reason):
    result = payload(module, **changes)
    assert result['verdict'] == 'rejected'
    assert reason in result['reason_codes']


def test_multiple_independent_denials_are_retained(module):
    result = payload(module, costs_complete=False, funding_complete=False,
                     target_price='103', is_auto_add_margin=True, net_equity_usdt=None)
    assert set(result['reason_codes']) >= {
        'costs_complete_required', 'funding_complete_required', 'target_not_frozen_2r',
        'auto_add_margin_not_disabled', 'net_equity_usdt_invalid',
    }


def test_decision_is_deeply_immutable_and_hashes_all_frozen_inputs(module):
    assert module is not None, 'immutable EntryDecision sizing module is missing'
    source = proposal(active_symbols=['ETHUSDT'])
    decision = module.propose_entry_decision(source)
    baseline = decision.to_dict()
    source['active_symbols'].append('TRXUSDT')
    changed = decision.to_dict()
    changed['active_symbols_after'].append(TRX)
    changed['reason_codes'].append('invented')
    assert decision.to_dict() == baseline
    with pytest.raises(FrozenInstanceError):
        decision.verdict = 'eligible_proposal'
    assert len(decision.sha256) == 64
    assert decision.sha256 == module.propose_entry_decision(proposal(active_symbols=[ETH])).sha256
    assert decision.sha256 != module.propose_entry_decision(proposal(signal_id='new')).sha256
    assert decision.sha256 != module.propose_entry_decision(proposal(other_cost_per_unit_usdt='.01')).sha256
    assert json.loads(decision.canonical_json) == baseline


def test_caller_decimal_context_and_numeric_spelling_cannot_change_decision(module):
    reference = evaluate(module)
    with localcontext() as context:
        context.prec = 6
        constrained = evaluate(module, entry_price='100.0000', stop_price='99.000',
                               target_price='102.0', net_equity_usdt=Decimal('1000.00'))
    assert constrained.to_dict() == reference.to_dict()
    assert constrained.sha256 == reference.sha256


def test_rejection_does_not_echo_arbitrary_or_secret_fields(module):
    assert module is not None, 'immutable EntryDecision sizing module is missing'
    source = proposal(account_id={'apiKey': 'SYNTHETIC_SECRET'},
                      unknown_request={'token': 'SYNTHETIC_SECRET'})
    result = module.propose_entry_decision(source)
    assert 'SYNTHETIC_SECRET' not in result.canonical_json
    assert result.to_dict()['account_id'] is None


def test_input_is_not_mutated(module):
    assert module is not None, 'immutable EntryDecision sizing module is missing'
    source = proposal()
    before = copy.deepcopy(source)
    module.propose_entry_decision(source)
    assert source == before


def test_short_adverse_execution_prices_are_used_for_taker_fees(module):
    result = payload(module, side='sell', stop_price='101', target_price='98',
                     stop_slippage_per_unit_usdt='.01', target_slippage_per_unit_usdt='.02',
                     stop_taker_fee_rate='.002', target_exit_fee_rate='.002')
    # Short stops BUY at101.01 and targets BUY at98.02 in the adverse scenario.
    assert result['quantity'] == '8.25'
    assert result['planned_loss_usdt'] == '9.999165'
    assert result['net_target_gain_usdt'] == '14.71767'
    assert result['stop_execution_price'] == '101.01'
    assert result['target_execution_price'] == '98.02'


def test_nonpositive_adverse_exit_price_is_refused(module):
    result = payload(module, stop_slippage_per_unit_usdt='100')
    assert result['verdict'] == 'rejected'
    assert 'stop_execution_price_invalid' in result['reason_codes']


@pytest.mark.parametrize('value', ['1e999999999', '0e-999999999', '1' * 97])
def test_extreme_numeric_encoding_is_a_fixed_rejection_not_exception(module, value):
    result = payload(module, other_cost_per_unit_usdt=value)
    assert result['verdict'] == 'rejected'
    assert 'other_cost_per_unit_usdt_invalid' in result['reason_codes']


def test_inconsistent_quantity_rules_are_refused(module):
    result = payload(module, min_quantity='3', max_quantity='2')
    assert result['verdict'] == 'rejected'
    assert 'quantity_rules_inconsistent' in result['reason_codes']


@pytest.mark.parametrize('key', [
    'account_snapshot_hash', 'market_snapshot_hash', 'exchange_rules_hash', 'exit_params_hash',
])
def test_archived_source_and_exit_contract_hashes_are_required_and_bound(module, key):
    result = payload(module, **{key: None})
    assert result['verdict'] == 'rejected'
    assert key + '_invalid' in result['reason_codes']
    assert result[key] is None
    original = evaluate(module)
    changed = evaluate(module, **{key: 'e' * 64})
    assert original.sha256 != changed.sha256


def test_quantity_rechecks_smallest_decimal_account_budget_change(module):
    accepted = payload(module, used_risk_usdt='10', used_margin_usdt='400')
    assert accepted['quantity'] == '10'
    smaller_risk = payload(module, used_risk_usdt='10.0000000000000000000000000000000000000001')
    assert smaller_risk['quantity'] == '9.99'
    smaller_margin = payload(module, used_margin_usdt='400.0000000000000000000000000000000000000001')
    assert smaller_margin['quantity'] == '9.99'
