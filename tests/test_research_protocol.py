"""新研究协议的时间隔离、完整实验族和成本门槛行为。"""
import copy
import dataclasses
import importlib
from decimal import localcontext

import pytest


def api():
    # 缺少模块时也以明确断言显示 RED，而不是收集阶段异常。
    try:
        return importlib.import_module('research_protocol')
    except ModuleNotFoundError:
        pytest.fail('research_protocol implementation is missing')


DAY = 86_400_000
HOUR = 3_600_000
BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
TRX = 'TRX/USDT:USDT'


def hashes():
    return {key: char * 64 for key, char in zip(
        ('code_hash', 'data_hash', 'cost_hash', 'risk_hash', 'strategy_hash'), 'abcde')}


def family_spec():
    return {
        'family_id': 'research-20261116', 'registered_ms': 0,
        'dataset': {'start_ms': 0, 'end_ms': 365 * DAY, 'source_ref': 'dataset:1',
                    'sha256': 'b' * 64},
        'symbols': [BTC, ETH, TRX], 'timeframes': ['5m'],
        'factor_sets': [{'id': 'volume-2x', 'sha256': '1' * 64},
                        {'id': 'volume-4x', 'sha256': '2' * 64}],
        'exit_sets': [{'id': 'atr-1', 'sha256': '3' * 64}],
        'cost_scenarios': ['base', 'stress'], 'seeds': [7],
        'label_horizons': {'5m': 3},
        'label_span_ms': HOUR, 'purge_ms': HOUR, 'embargo_ms': HOUR,
        'execution_policy': {'min_fill_rate': '0.5', 'max_partial_fill_rate': '0.5',
                             'max_latency_p99_ms': 2000, 'max_adverse_selection': '0.01',
                             'max_unfilled_opportunity_cost': '10'},
        'comparison': {'method': 'time_block_bootstrap', 'block_ms': DAY,
                       'multiple_testing': 'holm', 'alpha': '0.05'},
        'identities': hashes(),
    }


def candidate(family):
    frozen = family.to_dict()
    config = frozen['configurations'][0]
    stress_config = next(row for row in frozen['configurations'] if
                         row['cost_scenario'] == 'stress' and all(row[key] == config[key]
                         for key in ('symbol', 'timeframe', 'factor_set', 'exit_set', 'seed')))
    return {
        'family_sha256': family.sha256, 'config_id': config['config_id'],
        'evaluation_stage': 'final_holdout',
        'evaluation_window': {'start_ms': 275 * DAY, 'end_ms': 365 * DAY},
        'identities': hashes(), 'evaluation_ref': 'artifact:final:1',
        'dataset_quality': 'complete', 'cost_completeness': 'reconciled',
        'all_attempts_registered': True, 'holdout_once_verified': True,
        'risk_compliant': True, 'native_protection_verified': True,
        'base': {'complete_roundtrips': 100, 'net_profit': '10',
                 'gross_wins': '60', 'gross_losses': '50'},
        'stress': {'net_profit': '0', 'config_id': stress_config['config_id']},
        'execution': {'fill_rate': '0.8', 'partial_fill_rate': '0.1',
                      'unfilled_opportunity_cost': '1.2', 'latency_p99_ms': 100,
                      'adverse_selection': '0.003', 'complete': True},
        'statistics': {'method': 'time_block_bootstrap', 'block_ms': DAY,
                       'multiple_testing': 'holm', 'family_size': 12,
                       'adjusted_p_value': '0.04', 'net_profit_lower_bound': '0.1'},
    }


def hourly_bars():
    return [{'timestamp_ms': n * HOUR, 'closed': True, 'available_ms': (n + 1) * HOUR,
             'open': '100', 'high': '102', 'low': '99', 'close': '101',
             'volume': '0'} for n in range(365 * 24)]


def test_freeze_expands_every_axis_and_returns_deeply_immutable_identity():
    spec = family_spec()
    frozen = api().freeze_experiment_family(spec)
    value = frozen.to_dict()
    assert value['configuration_count'] == 12
    assert len({row['config_id'] for row in value['configurations']}) == 12
    assert {row['symbol'] for row in value['configurations']} == {BTC, ETH, TRX}
    assert {row['cost_scenario'] for row in value['configurations']} == {'base', 'stress'}
    assert value['acceptance_eligible'] is False
    assert value['holdout_claim_committed'] is False
    original = frozen.canonical_json
    spec['factor_sets'][0]['id'] = 'changed'
    value['configurations'].clear()
    assert frozen.canonical_json == original
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.sha256 = '0' * 64


def test_rolling_splits_keep_final_90_days_untouched_and_purge_labels():
    value = api().freeze_experiment_family(family_spec()).to_dict()
    assert value['holdout'] == {'start_ms': 275 * DAY, 'end_ms': 365 * DAY,
                                'evaluation_start_ms': 275 * DAY + HOUR,
                                'last_label_start_before_ms': 365 * DAY - HOUR}
    folds = value['folds']
    assert len(folds) == 5
    assert folds[0] == {'train_start_ms': 0, 'train_end_ms': 120 * DAY - HOUR,
                        'validation_start_ms': 120 * DAY + HOUR,
                        'validation_end_ms': 150 * DAY - HOUR}
    assert folds[-1]['train_start_ms'] == 120 * DAY
    assert folds[-1]['validation_end_ms'] == 270 * DAY - HOUR
    assert all(fold['validation_end_ms'] <= value['holdout']['start_ms']
               for fold in folds)


@pytest.mark.parametrize(('field', 'value'), [
    ('symbols', [BTC, ETH, TRX, 'SOL/USDT:USDT']),
    ('timeframes', ['1d']), ('symbols', [BTC, BTC]),
    ('seeds', [True]), ('cost_scenarios', ['base']),
    ('cost_scenarios', ['base', 'stress', 'secret-extra']),
    ('purge_ms', HOUR - 1), ('embargo_ms', HOUR - 1),
    ('label_span_ms', True),
])
def test_invalid_or_unfrozen_family_dimensions_are_rejected(field, value):
    spec = family_spec()
    spec[field] = value
    with pytest.raises(ValueError):
        api().freeze_experiment_family(spec)


def test_120_configurations_include_seed_and_cost_axes_and_121_is_not_trimmed():
    spec = family_spec()
    spec['factor_sets'] = [{'id': f'f{n}', 'sha256': f'{n:064x}'} for n in range(10)]
    spec['seeds'] = [1, 2]
    assert api().freeze_experiment_family(spec).to_dict()['configuration_count'] == 120
    spec['factor_sets'].append({'id': 'f10', 'sha256': 'f' * 64})
    with pytest.raises(ValueError, match='120'):
        api().freeze_experiment_family(spec)


@pytest.mark.parametrize('change', ['short', 'unregistered-time', 'hash', 'comparison', 'long-label'])
def test_invalid_coverage_identity_or_statistics_prevents_freeze(change):
    spec = family_spec()
    if change == 'short':
        spec['dataset']['end_ms'] -= 1
    elif change == 'unregistered-time':
        spec['registered_ms'] = -1
    elif change == 'hash':
        spec['identities']['data_hash'] = 'missing'
    elif change == 'comparison':
        spec['comparison']['multiple_testing'] = 'none'
    else:
        spec['label_span_ms'] = spec['purge_ms'] = spec['embargo_ms'] = 16 * DAY
    with pytest.raises(ValueError):
        api().freeze_experiment_family(spec)


def test_candle_coverage_accepts_true_zero_volume_and_hashes_input_facts():
    bars = hourly_bars()
    result = api().assess_candle_coverage(
        bars, symbol=BTC, timeframe='1h', start_ms=0, end_ms=365 * DAY,
        as_of_ms=365 * DAY, source_ref='archive:1', trusted_source=True)
    value = result.to_dict()
    assert value['status'] == 'pass'
    assert value['expected_bars'] == 8760
    assert value['observed_bars'] == 8760
    assert value['zero_volume_bars'] == 8760
    bars[-1]['close'] = '100.5'
    changed = api().assess_candle_coverage(
        bars, symbol=BTC, timeframe='1h', start_ms=0, end_ms=365 * DAY,
        as_of_ms=365 * DAY, source_ref='archive:1', trusted_source=True)
    assert changed.to_dict()['data_sha256'] != value['data_sha256']


@pytest.mark.parametrize(('change', 'reason'), [
    ('missing', 'candle_gap'), ('duplicate', 'candle_duplicate'),
    ('unclosed', 'candle_not_closed'), ('future', 'candle_not_available'),
    ('bad-ohlc', 'candle_ohlc_invalid'), ('negative-volume', 'candle_volume_invalid'),
    ('untrusted', 'source_untrusted'), ('short', 'coverage_less_than_365_days'),
    ('unaligned', 'coverage_grid_invalid'),
])
def test_candle_coverage_never_fills_gaps_or_accepts_future_facts(change, reason):
    bars, end, trusted = hourly_bars(), 365 * DAY, True
    if change == 'missing':
        bars.pop(100)
    elif change == 'duplicate':
        bars.append(copy.deepcopy(bars[100]))
    elif change == 'unclosed':
        bars[100]['closed'] = False
    elif change == 'future':
        bars[100]['available_ms'] = end + 1
    elif change == 'bad-ohlc':
        bars[100]['high'] = '99'
    elif change == 'negative-volume':
        bars[100]['volume'] = '-1'
    elif change == 'untrusted':
        trusted = False
    elif change == 'short':
        end -= HOUR
        bars.pop()
    else:
        bars[100]['timestamp_ms'] += 1
    value = api().assess_candle_coverage(
        bars, symbol=BTC, timeframe='1h', start_ms=0, end_ms=end, as_of_ms=365 * DAY,
        source_ref='archive:1', trusted_source=trusted).to_dict()
    assert value['status'] == 'not_estimable'
    assert reason in value['reason_codes']


def test_mature_label_requires_every_closed_available_bar_and_never_reads_future():
    bars = [{'timestamp_ms': n * 300_000, 'closed': True, 'available_ms': (n + 1) * 300_000,
             'close': price} for n, price in enumerate(['100', '90', '110', '103', '999'])]
    args = dict(anchor_ms=0, timeframe='5m', horizon_bars=3, as_of_ms=1_200_000)
    result = api().mature_close_label(bars, **args).to_dict()
    assert result['status'] == 'matured'
    assert result['return_fraction'] == '0.03'
    bars[-1]['close'] = '1'
    assert api().mature_close_label(bars, **args).sha256 == api().mature_close_label(bars[:-1], **args).sha256
    bars.pop(2)
    assert api().mature_close_label(bars, **args).to_dict()['status'] == 'not_estimable'


def test_label_before_maturity_is_pending_and_duplicate_or_unclosed_is_not_estimable():
    bars = [{'timestamp_ms': n * 300_000, 'closed': True, 'available_ms': (n + 1) * 300_000,
             'close': '100'} for n in range(4)]
    assert api().mature_close_label(bars, anchor_ms=0, timeframe='5m', horizon_bars=3,
                                    as_of_ms=1_199_999).to_dict()['status'] == 'pending'
    bars[2]['closed'] = False
    assert api().mature_close_label(bars, anchor_ms=0, timeframe='5m', horizon_bars=3,
                                    as_of_ms=1_200_000).to_dict()['status'] == 'not_estimable'


def test_book_history_unknown_does_not_become_zero_or_pass():
    assert api().assess_book_history(None, required_start_ms=0, required_end_ms=DAY).to_dict()['status'] == 'not_estimable'
    evidence = {'source_ref': 'book:1', 'sha256': 'a' * 64, 'sequence_contiguous': True,
                'snapshots_complete': True, 'start_ms': 0, 'end_ms': DAY}
    assert api().assess_book_history(evidence, required_start_ms=0, required_end_ms=DAY).to_dict()['status'] == 'pass'
    evidence['sequence_contiguous'] = False
    assert api().assess_book_history(evidence, required_start_ms=0, required_end_ms=DAY).to_dict()['status'] == 'not_estimable'


def test_stress_doubles_expenses_and_preserves_funding_income_with_exact_decimal():
    cost = {'fees': '1.25', 'adverse_slippage': '2', 'funding_expense': '0.5',
            'funding_income': '0.7', 'other_expense': '0.3'}
    stressed = api().stress_costs(cost).to_dict()
    assert stressed['fees'] == '2.5'
    assert stressed['adverse_slippage'] == '4'
    assert stressed['funding_expense'] == '1'
    assert stressed['funding_income'] == '0.7'
    assert stressed['other_expense'] == '0.3'
    assert stressed['net_cost'] == '7.1'
    with localcontext() as context:
        context.prec = 5
        cost['fees'] = '0.12345678901234567890123456789012345'
        assert api().stress_costs(cost).to_dict()['fees'] == '0.2469135780246913578024691357802469'


@pytest.mark.parametrize('bad', [True, 0.1, 'NaN', 'Infinity', '-1', None])
def test_unknown_float_or_negative_cost_never_becomes_eligible(bad):
    cost = {'fees': bad, 'adverse_slippage': '0', 'funding_expense': '0',
            'funding_income': '0', 'other_expense': '0'}
    with pytest.raises(ValueError):
        api().stress_costs(cost)


def test_candidate_only_proposes_demo_precheck_and_exact_profit_factor_boundary():
    family = api().freeze_experiment_family(family_spec())
    result = api().evaluate_candidate(candidate(family), family).to_dict()
    assert result['status'] == 'research_pass'
    assert result['demo_precheck_candidate'] is True
    assert result['acceptance_eligible'] is False
    assert result['holdout_claim_committed'] is False
    assert result['production_authorized'] is False


@pytest.mark.parametrize(('path', 'value', 'reason'), [
    (('base', 'complete_roundtrips'), 99, 'insufficient_roundtrips'),
    (('base', 'net_profit'), '0', 'base_net_not_positive'),
    (('base', 'gross_wins'), '59.9999', 'profit_factor_below_1_2'),
    (('stress', 'net_profit'), '-0.0000001', 'stress_net_negative'),
    (('risk_compliant',), False, 'risk_noncompliant'),
    (('cost_completeness',), 'unknown', 'cost_incomplete'),
    (('dataset_quality',), 'unknown', 'data_incomplete'),
    (('holdout_once_verified',), False, 'holdout_once_unverified'),
    (('all_attempts_registered',), False, 'attempt_history_incomplete'),
    (('identities', 'risk_hash'), 'f' * 64, 'identity_mismatch'),
    (('statistics', 'family_size'), 6, 'family_comparison_incomplete'),
    (('statistics', 'adjusted_p_value'), '0.0500001', 'comparison_not_significant'),
    (('statistics', 'net_profit_lower_bound'), '0', 'net_lower_bound_not_positive'),
    (('execution', 'complete'), False, 'execution_incomplete'),
])
def test_missing_or_failed_final_research_evidence_is_not_promoted(path, value, reason):
    family = api().freeze_experiment_family(family_spec())
    facts = candidate(family)
    target = facts
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = api().evaluate_candidate(facts, family).to_dict()
    assert result['status'] != 'research_pass'
    assert reason in result['reason_codes']
    assert result['demo_precheck_candidate'] is False


def test_candidate_collects_failures_without_rounding_profit_factor_or_hiding_missing_fields():
    family = api().freeze_experiment_family(family_spec())
    facts = candidate(family)
    facts['base']['gross_wins'] = '59.999999999999999999999999999999'
    facts['risk_compliant'] = False
    del facts['execution']['adverse_selection']
    result = api().evaluate_candidate(facts, family).to_dict()
    assert {'profit_factor_below_1_2', 'risk_noncompliant', 'execution_incomplete'} <= set(result['reason_codes'])


def test_attempt_sequence_is_contiguous_and_retries_are_visible_not_new_configurations():
    family = api().freeze_experiment_family(family_spec())
    cid = family.to_dict()['configurations'][0]['config_id']
    attempts = [
        {'sequence': 1, 'attempt_id': 'attempt:1', 'config_id': cid, 'status': 'failed',
         'started_ms': 365 * DAY + 1, 'finished_ms': 365 * DAY + 2, 'artifact_ref': 'artifact:failed',
         'phase': 'validation', 'family_sha256': family.sha256},
        {'sequence': 2, 'attempt_id': 'attempt:2', 'config_id': cid, 'status': 'completed',
         'started_ms': 365 * DAY + 3, 'finished_ms': 365 * DAY + 4, 'artifact_ref': 'artifact:passed',
         'phase': 'validation', 'family_sha256': family.sha256},
    ]
    value = api().audit_attempt_history(family, attempts).to_dict()
    assert value['status'] == 'pass'
    assert value['attempt_count'] == 2
    assert value['configuration_count'] == 12
    assert value['unattempted_configuration_count'] == 11
    attempts.pop(0)
    assert api().audit_attempt_history(family, attempts).to_dict()['status'] == 'not_estimable'


def test_attempt_audit_rejects_changed_configuration_and_duplicate_ids():
    family = api().freeze_experiment_family(family_spec())
    row = {'sequence': 1, 'attempt_id': 'attempt:1', 'config_id': 'f' * 64, 'status': 'failed',
           'started_ms': 1, 'finished_ms': 2, 'artifact_ref': 'artifact:failed'}
    assert 'attempt_config_unknown' in api().audit_attempt_history(family, [row]).to_dict()['reason_codes']


@pytest.mark.parametrize(('change', 'reason'), [
    ('stress-other-symbol', 'stress_configuration_mismatch'),
    ('stress-selected', 'base_configuration_required'),
    ('net-inconsistent', 'roundtrip_net_sum_mismatch'),
    ('window-moved', 'holdout_window_mismatch'),
    ('validation-promoted', 'holdout_window_mismatch'),
])
def test_candidate_cannot_mix_unrelated_scenarios_or_reselect_window(change, reason):
    family = api().freeze_experiment_family(family_spec())
    facts = candidate(family)
    if change == 'stress-other-symbol':
        facts['stress']['config_id'] = next(row['config_id'] for row in
            family.to_dict()['configurations'] if row['symbol'] == ETH and row['cost_scenario'] == 'stress')
    elif change == 'stress-selected':
        facts['config_id'] = facts['stress']['config_id']
    elif change == 'net-inconsistent':
        facts['base']['net_profit'] = '11'
    elif change == 'window-moved':
        facts['evaluation_window']['start_ms'] += DAY
    else:
        facts['evaluation_stage'] = 'validation'
    assert reason in api().evaluate_candidate(facts, family).to_dict()['reason_codes']


def test_changed_frozen_family_is_revalidated_even_with_recomputed_checksum():
    import hashlib
    import json
    family = api().freeze_experiment_family(family_spec())
    payload = family.to_dict()
    payload['configuration_count'] = 1
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    forged = api().FrozenResearchEvidence(text, hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(ValueError, match='canonical'):
        api().audit_attempt_history(forged, [])


def test_final_holdout_is_consumed_by_failed_attempt_and_may_not_be_retried_in_history():
    family = api().freeze_experiment_family(family_spec())
    cid = family.to_dict()['configurations'][0]['config_id']
    attempts = [{'sequence': n, 'attempt_id': f'attempt:{n}', 'config_id': cid,
                 'status': 'failed', 'started_ms': n, 'finished_ms': n + 1,
                 'artifact_ref': f'artifact:{n}', 'family_sha256': family.sha256,
                 'phase': 'final_holdout'} for n in (1, 2)]
    assert 'holdout_reused' in api().audit_attempt_history(family, attempts).to_dict()['reason_codes']


def test_attempt_cannot_precede_registration_or_borrow_another_family():
    spec = family_spec()
    spec['registered_ms'] = 10
    family = api().freeze_experiment_family(spec)
    row = {'sequence': 1, 'attempt_id': 'attempt:1',
           'config_id': family.to_dict()['configurations'][0]['config_id'],
           'status': 'failed', 'started_ms': 9, 'finished_ms': 11,
           'artifact_ref': 'artifact:1', 'phase': 'validation', 'family_sha256': 'f' * 64}
    value = api().audit_attempt_history(family, [row]).to_dict()
    assert {'attempt_time_invalid', 'attempt_family_mismatch'} <= set(value['reason_codes'])


def test_reordering_semantically_equal_axes_does_not_create_new_evidence_identity():
    spec = family_spec()
    first = api().freeze_experiment_family(spec)
    for key in ('symbols', 'factor_sets', 'cost_scenarios'):
        spec[key].reverse()
    assert api().freeze_experiment_family(spec).sha256 == first.sha256


def test_conflicting_version_id_cannot_hide_as_distinct_factor_version():
    spec = family_spec()
    spec['factor_sets'][1]['id'] = spec['factor_sets'][0]['id']
    with pytest.raises(ValueError, match='version'):
        api().freeze_experiment_family(spec)


def test_label_purge_span_covers_final_future_bar_close_not_only_its_open():
    spec = family_spec()
    spec['label_horizons'] = {'5m': 12}
    with pytest.raises(ValueError, match='maturity'):
        api().freeze_experiment_family(spec)


@pytest.mark.parametrize(('key', 'value', 'reason'), [
    ('fill_rate', '0', 'execution_fill_rate_below_minimum'),
    ('partial_fill_rate', '0.50001', 'execution_partial_fill_rate_above_maximum'),
    ('latency_p99_ms', 2001, 'execution_latency_above_maximum'),
    ('adverse_selection', '0.01001', 'execution_adverse_selection_above_maximum'),
    ('unfilled_opportunity_cost', '10.01', 'execution_opportunity_cost_above_maximum'),
])
def test_frozen_execution_thresholds_block_inadequate_fills_latency_and_selection(key, value, reason):
    family = api().freeze_experiment_family(family_spec())
    facts = candidate(family)
    facts['execution'][key] = value
    assert reason in api().evaluate_candidate(facts, family).to_dict()['reason_codes']


def test_execution_thresholds_must_be_explicit_and_cannot_allow_zero_fill_rate():
    spec = family_spec()
    spec['execution_policy']['min_fill_rate'] = '0'
    with pytest.raises(ValueError, match='execution'):
        api().freeze_experiment_family(spec)


def test_signed_zero_cost_has_same_canonical_identity_as_zero():
    cost = {'fees': '-0.000', 'adverse_slippage': '0', 'funding_expense': '0',
            'funding_income': '0', 'other_expense': '0'}
    first = api().stress_costs(cost)
    cost['fees'] = '0'
    assert api().stress_costs(cost).sha256 == first.sha256


def test_final_holdout_attempt_must_be_base_configuration_with_stress_in_same_bundle():
    family = api().freeze_experiment_family(family_spec())
    row = {'sequence': 1, 'attempt_id': 'attempt:1',
           'config_id': next(item['config_id'] for item in family.to_dict()['configurations']
                             if item['cost_scenario'] == 'stress'),
           'status': 'completed', 'started_ms': 1, 'finished_ms': 2,
           'artifact_ref': 'artifact:1', 'phase': 'final_holdout', 'family_sha256': family.sha256}
    assert 'holdout_base_configuration_required' in api().audit_attempt_history(family, [row]).to_dict()['reason_codes']


def test_attempt_cannot_run_before_last_dataset_bar_is_closed():
    family = api().freeze_experiment_family(family_spec())
    row = {'sequence': 1, 'attempt_id': 'attempt:premature',
           'config_id': family.to_dict()['configurations'][0]['config_id'],
           'status': 'failed', 'started_ms': 365 * DAY - 1, 'finished_ms': 365 * DAY + 1,
           'artifact_ref': 'artifact:1', 'phase': 'validation', 'family_sha256': family.sha256}
    assert 'attempt_dataset_not_available' in api().audit_attempt_history(family, [row]).to_dict()['reason_codes']


def test_candidate_decimal_evidence_can_be_hashed_without_losing_precision():
    from decimal import Decimal
    family = api().freeze_experiment_family(family_spec())
    facts = candidate(family)
    facts['base']['net_profit'] = Decimal('10')
    facts['base']['gross_wins'] = Decimal('60')
    facts['base']['gross_losses'] = Decimal('50')
    assert api().evaluate_candidate(facts, family).to_dict()['status'] == 'research_pass'
