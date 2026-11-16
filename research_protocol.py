"""P2 研究冻结与离线证据门槛；纯计算不授予账户或交易资格。

区间均为 [start_ms,end_ms)。来源真实性、尝试完整性与留出一次性消费
仍须调用方的不可变归档和数据库事务证明。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

DAY_MS = 86_400_000
TIMEFRAMES = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000}
SYMBOLS = frozenset({'BTC/USDT:USDT', 'ETH/USDT:USDT', 'TRX/USDT:USDT'})
HASH = re.compile(r'[a-f0-9]{64}\Z')
SAFE_ID = re.compile(r'[A-Za-z0-9_.:/@-]{1,200}\Z')
IDENTITIES = ('code_hash', 'data_hash', 'cost_hash', 'risk_hash', 'strategy_hash')
LOGGER = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False, default=_json_decimal)


def _json_decimal(value):
    if isinstance(value, Decimal):
        return _text(_amount(value))
    raise TypeError('unsupported research evidence JSON type')


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenResearchEvidence:
    """JSON 字符串保存冻结事实，返回字典始终是独立副本。"""
    canonical_json: str
    sha256: str

    def to_dict(self):
        return json.loads(self.canonical_json)


def _freeze(value):
    text = _json(value)
    return FrozenResearchEvidence(text, hashlib.sha256(text.encode()).hexdigest())


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('timestamp must be nonnegative integer milliseconds')
    return value


def _id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError('invalid immutable reference')
    return value


def _sha(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError('invalid SHA-256 identity')
    return value


def _amount(value, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('finite exact decimal required')
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError('finite exact decimal required') from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 96
            or abs(result.as_tuple().exponent) > 96 or result.adjusted() > 96
            or (nonnegative and result < 0)):
        raise ValueError('invalid decimal magnitude or sign')
    return result


def _text(value):
    if value == 0:
        return '0'
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _identities(value):
    if not isinstance(value, dict) or set(value) != set(IDENTITIES):
        raise ValueError('all frozen code/data/cost/risk/strategy identities required')
    return {key: _sha(value[key]) for key in IDENTITIES}


def _axis(values, validator):
    if not isinstance(values, list) or not values:
        raise ValueError('experiment axes must be explicit nonempty lists')
    result = [validator(value) for value in values]
    if len({_json(value) for value in result}) != len(result):
        raise ValueError('duplicate experiment axis identity')
    return sorted(result, key=_json)


def _version(value):
    if not isinstance(value, dict) or set(value) != {'id', 'sha256'}:
        raise ValueError('factor/exit versions require exact id and sha256')
    return {'id': _id(value['id']), 'sha256': _sha(value['sha256'])}


def _choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError('unsupported research symbol/timeframe/scenario')
    return value


def _seed(value):
    if type(value) is not int or not 0 <= value < 2**32:
        raise ValueError('seed must be explicit uint32')
    return value


def _execution_policy(value):
    keys = {'min_fill_rate', 'max_partial_fill_rate', 'max_latency_p99_ms',
            'max_adverse_selection', 'max_unfilled_opportunity_cost'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('explicit frozen execution thresholds required')
    result = {key: _amount(value[key], nonnegative=True) for key in keys - {'max_latency_p99_ms'}}
    if (not 0 < result['min_fill_rate'] <= 1 or not 0 <= result['max_partial_fill_rate'] <= 1
            or _stamp(value['max_latency_p99_ms']) <= 0):
        raise ValueError('execution thresholds invalid')
    return {**{key: _text(amount) for key, amount in result.items()},
            'max_latency_p99_ms': value['max_latency_p99_ms']}


def freeze_experiment_family(spec):
    """冻结完整笛卡尔积；超过 120 时拒绝，不截断或省略压力场景。"""
    required = {'family_id', 'registered_ms', 'dataset', 'symbols', 'timeframes',
                'factor_sets', 'exit_sets', 'cost_scenarios', 'seeds', 'label_span_ms',
                'purge_ms', 'embargo_ms', 'comparison', 'identities', 'execution_policy',
                'label_horizons'}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError('explicit complete family protocol required')
    dataset = spec['dataset']
    if not isinstance(dataset, dict) or set(dataset) != {'start_ms', 'end_ms', 'source_ref', 'sha256'}:
        raise ValueError('dataset version missing')
    start, end = _stamp(dataset['start_ms']), _stamp(dataset['end_ms'])
    if end - start < 365 * DAY_MS:
        raise ValueError('at least 365 days required')
    dataset = dict(start_ms=start, end_ms=end, source_ref=_id(dataset['source_ref']),
                   sha256=_sha(dataset['sha256']))
    identities = _identities(spec['identities'])
    if dataset['sha256'] != identities['data_hash']:
        raise ValueError('dataset fingerprint differs from data identity')
    symbols = _axis(spec['symbols'], lambda value: _choice(value, SYMBOLS))
    timeframes = _axis(spec['timeframes'], lambda value: _choice(value, TIMEFRAMES))
    if any(start % TIMEFRAMES[tf] or end % TIMEFRAMES[tf] for tf in timeframes):
        raise ValueError('dataset boundaries must align with every timeframe')
    factors, exits = _axis(spec['factor_sets'], _version), _axis(spec['exit_sets'], _version)
    if any(len({version['id'] for version in axis}) != len(axis) for axis in (factors, exits)):
        raise ValueError('conflicting immutable factor/exit version id')
    scenarios = _axis(spec['cost_scenarios'], lambda value: _choice(value, {'base', 'stress'}))
    if set(scenarios) != {'base', 'stress'}:
        raise ValueError('both base and expense-doubled stress required')
    seeds = _axis(spec['seeds'], _seed)
    count = len(symbols) * len(timeframes) * len(factors) * len(exits) * len(scenarios) * len(seeds)
    if count > 120:
        raise ValueError('complete experiment family exceeds 120 configurations')
    label, purge, embargo = (_stamp(spec[key]) for key in ('label_span_ms', 'purge_ms', 'embargo_ms'))
    if label <= 0 or purge < label or embargo < label or purge + embargo >= 30 * DAY_MS:
        raise ValueError('purge/embargo must cover label span and leave validation observations')
    horizons = spec['label_horizons']
    if (not isinstance(horizons, dict) or set(horizons) != set(timeframes)
            or any(type(n) is not int or not 1 <= n <= 10_000 for n in horizons.values())
            or label < max((horizons[tf] + 1) * TIMEFRAMES[tf] for tf in timeframes)):
        raise ValueError('label span must cover declared horizons through final close maturity')
    policy = _execution_policy(spec['execution_policy'])
    comparison = spec['comparison']
    if (not isinstance(comparison, dict)
            or set(comparison) != {'method', 'block_ms', 'multiple_testing', 'alpha'}
            or comparison['method'] not in {'time_block_bootstrap', 'hac'}
            or comparison['multiple_testing'] not in {'holm', 'bonferroni'}):
        raise ValueError('time-dependent complete-family comparison required')
    block = _stamp(comparison['block_ms'])
    alpha = _amount(comparison['alpha'], nonnegative=True)
    if block < label or not 0 < alpha <= Decimal('0.05'):
        raise ValueError('comparison block/alpha invalid')
    comparison = dict(method=comparison['method'], block_ms=block,
                      multiple_testing=comparison['multiple_testing'], alpha=_text(alpha))
    configs = []
    for symbol, tf, factor, exit_set, scenario, seed in itertools.product(
            symbols, timeframes, factors, exits, scenarios, seeds):
        row = dict(symbol=symbol, timeframe=tf, factor_set=factor, exit_set=exit_set,
                   cost_scenario=scenario, seed=seed)
        configs.append({'config_id': _hash(row), **row})
    holdout_start = end - 90 * DAY_MS
    folds, cursor = [], start
    while cursor + 150 * DAY_MS <= holdout_start:
        boundary = cursor + 120 * DAY_MS
        folds.append(dict(train_start_ms=cursor, train_end_ms=boundary - purge,
                          validation_start_ms=boundary + embargo,
                          validation_end_ms=cursor + 150 * DAY_MS - purge))
        cursor += 30 * DAY_MS
    result = _freeze(dict(protocol_version='research-protocol-v1', family_id=_id(spec['family_id']),
                        registered_ms=_stamp(spec['registered_ms']), dataset=dataset, identities=identities,
                        axes=dict(symbols=symbols, timeframes=timeframes, factor_sets=factors,
                                  exit_sets=exits, cost_scenarios=scenarios, seeds=seeds),
                        label_span_ms=label, purge_ms=purge, embargo_ms=embargo,
                        label_horizons=horizons, execution_policy=policy,
                        comparison=comparison, configuration_count=count, configurations=configs,
                        folds=folds, holdout=dict(start_ms=holdout_start, end_ms=end,
                        evaluation_start_ms=holdout_start + embargo,
                        last_label_start_before_ms=end - label),
                        acceptance_eligible=False, holdout_claim_committed=False))
    LOGGER.info('研究协议冻结 family=%s sha256=%s configs=%s folds=%s holdout_start_ms=%s holdout_end_ms=%s；仅研究协议',
                spec['family_id'], result.sha256, count, len(folds), holdout_start, end)
    return result


def assess_candle_coverage(bars, *, symbol, timeframe, start_ms, end_ms, as_of_ms,
                           source_ref, trusted_source):
    """逐根验证真实闭盘事实；零量是事实，缺根永远不补零。"""
    _choice(symbol, SYMBOLS)
    interval = TIMEFRAMES[_choice(timeframe, TIMEFRAMES)]
    start, end, now = _stamp(start_ms), _stamp(end_ms), _stamp(as_of_ms)
    reference = _id(source_ref)
    if not isinstance(bars, list):
        raise ValueError('candle rows must be a list')
    reasons, by_time, facts, zero = set(), set(), [], 0
    if trusted_source is not True:
        reasons.add('source_untrusted')
    if end - start < 365 * DAY_MS:
        reasons.add('coverage_less_than_365_days')
    if start >= end or start % interval or end % interval:
        reasons.add('coverage_grid_invalid')
    expected = max(0, (end - start) // interval)
    for bar in bars:
        if not isinstance(bar, dict):
            reasons.add('candle_row_invalid')
            continue
        try:
            timestamp = _stamp(bar.get('timestamp_ms'))
        except ValueError:
            reasons.add('coverage_grid_invalid')
            continue
        if timestamp in by_time:
            reasons.add('candle_duplicate')
        by_time.add(timestamp)
        if not start <= timestamp < end:
            reasons.add('candle_outside_window')
        if timestamp % interval:
            reasons.add('coverage_grid_invalid')
        if bar.get('closed') is not True:
            reasons.add('candle_not_closed')
        available = bar.get('available_ms')
        if type(available) is not int or not timestamp + interval <= available <= now:
            reasons.add('candle_not_available')
        normalized = dict(timestamp_ms=timestamp, available_ms=available if type(available) is int else None,
                          closed=bar.get('closed') is True)
        try:
            amounts = {key: _amount(bar.get(key)) for key in ('open', 'high', 'low', 'close')}
            if (any(value <= 0 for value in amounts.values())
                    or amounts['high'] < max(amounts.values()) or amounts['low'] > min(amounts.values())):
                raise ValueError('invalid OHLC')
            normalized.update({key: _text(value) for key, value in amounts.items()})
        except ValueError:
            reasons.add('candle_ohlc_invalid')
        try:
            volume = _amount(bar.get('volume'), nonnegative=True)
            normalized['volume'] = _text(volume)
            zero += volume == 0
        except ValueError:
            reasons.add('candle_volume_invalid')
        facts.append(normalized)
    # 只计数真实网格位置，禁止构造补齐的价格序列。
    valid_positions = sum(start <= timestamp < end and timestamp % interval == 0 for timestamp in by_time)
    if valid_positions != expected:
        reasons.add('candle_gap')
    result = _freeze(dict(status='not_estimable' if reasons else 'pass', reason_codes=sorted(reasons),
                        symbol=symbol, timeframe=timeframe, start_ms=start, end_ms=end, as_of_ms=now,
                        source_ref=reference, expected_bars=expected, observed_bars=len(bars),
                        zero_volume_bars=zero, data_sha256=_hash(sorted(facts, key=lambda row: row['timestamp_ms'])),
                        acceptance_eligible=False))
    LOGGER.info('研究数据覆盖 symbol=%s timeframe=%s start_ms=%s end_ms=%s rows=%s expected=%s status=%s reasons=%s sha256=%s',
                symbol, timeframe, start, end, len(bars), expected,
                'not_estimable' if reasons else 'pass', ','.join(sorted(reasons)), result.sha256)
    return result


def mature_close_label(bars, *, anchor_ms, timeframe, horizon_bars, as_of_ms):
    """只读锚点到标签终点的闭盘事实，未来根不参与 hash 或返回值。"""
    anchor, now = _stamp(anchor_ms), _stamp(as_of_ms)
    interval = TIMEFRAMES[_choice(timeframe, TIMEFRAMES)]
    if type(horizon_bars) is not int or not 1 <= horizon_bars <= 10_000 or anchor % interval:
        raise ValueError('label horizon/grid invalid')
    end = anchor + horizon_bars * interval
    base = dict(anchor_ms=anchor, label_end_ms=end, available_after_ms=end + interval,
                timeframe=timeframe, horizon_bars=horizon_bars, acceptance_eligible=False)
    if now < end + interval:
        return _freeze(dict(**base, status='pending', reason_codes=['label_not_matured']))
    if not isinstance(bars, list):
        raise ValueError('candle rows must be a list')
    selected = {}
    reasons = set()
    for bar in bars:
        if not isinstance(bar, dict) or type(bar.get('timestamp_ms')) is not int:
            reasons.add('label_row_invalid')
            continue
        timestamp = bar['timestamp_ms']
        if not anchor <= timestamp <= end:
            continue
        if timestamp in selected:
            reasons.add('label_duplicate')
        if (timestamp % interval or bar.get('closed') is not True
                or type(bar.get('available_ms')) is not int
                or not timestamp + interval <= bar['available_ms'] <= now):
            reasons.add('label_fact_unavailable')
        try:
            close = _amount(bar.get('close'))
            if close <= 0:
                raise ValueError('label price must be positive')
            selected[timestamp] = dict(timestamp_ms=timestamp, close=_text(close),
                                       available_ms=bar.get('available_ms'))
        except ValueError:
            reasons.add('label_price_invalid')
    expected = [anchor + n * interval for n in range(horizon_bars + 1)]
    if set(selected) != set(expected):
        reasons.add('label_gap')
    if reasons:
        return _freeze(dict(**base, status='not_estimable', reason_codes=sorted(reasons)))
    with localcontext() as context:
        context.prec = 256
        change = _amount(selected[end]['close']) / _amount(selected[anchor]['close']) - 1
    return _freeze(dict(**base, status='matured', reason_codes=[], return_fraction=_text(change),
                        facts_sha256=_hash([selected[timestamp] for timestamp in expected])))


def assess_book_history(evidence, *, required_start_ms, required_end_ms):
    """只接受明确时间覆盖和连续序列证明；不推断全市场队列。"""
    start, end = _stamp(required_start_ms), _stamp(required_end_ms)
    if start >= end:
        raise ValueError('book history window invalid')
    try:
        if (not isinstance(evidence, dict) or evidence.get('sequence_contiguous') is not True
                or evidence.get('snapshots_complete') is not True
                or _stamp(evidence.get('start_ms')) > start or _stamp(evidence.get('end_ms')) < end):
            raise ValueError('book history unavailable')
        ref, sha = _id(evidence.get('source_ref')), _sha(evidence.get('sha256'))
    except ValueError:
        return _freeze(dict(status='not_estimable', reason_codes=['book_history_unverified'],
                            acceptance_eligible=False))
    return _freeze(dict(status='pass', reason_codes=[], source_ref=ref, data_sha256=sha,
                        start_ms=start, end_ms=end, scope='visible_depth_only', acceptance_eligible=False))


def stress_costs(cost):
    """费用、不利滑点和资金费支出翻倍；资金费收入保持原值。"""
    fields = {'fees', 'adverse_slippage', 'funding_expense', 'funding_income', 'other_expense'}
    if not isinstance(cost, dict) or set(cost) != fields:
        raise ValueError('all exact cost components required')
    values = {key: _amount(cost[key], nonnegative=True) for key in fields}
    with localcontext() as context:
        context.prec = 512
        for key in ('fees', 'adverse_slippage', 'funding_expense'):
            values[key] *= 2
        net = sum(values[key] for key in fields - {'funding_income'}) - values['funding_income']
    return _freeze({**{key: _text(value) for key, value in values.items()}, 'net_cost': _text(net)})


def _family(value):
    if not isinstance(value, FrozenResearchEvidence):
        raise ValueError('frozen family required')
    if hashlib.sha256(value.canonical_json.encode()).hexdigest() != value.sha256:
        raise ValueError('family checksum mismatch')
    result = value.to_dict()
    if result.get('protocol_version') != 'research-protocol-v1':
        raise ValueError('unsupported family protocol')
    try:
        source = {key: result[key] for key in (
            'family_id', 'registered_ms', 'dataset', 'label_span_ms', 'purge_ms',
            'embargo_ms', 'comparison', 'identities', 'execution_policy', 'label_horizons')}
        source.update(result['axes'])
        expected = freeze_experiment_family(source)
    except (KeyError, TypeError, ValueError):
        raise ValueError('family is not canonical') from None
    if expected.canonical_json != value.canonical_json:
        raise ValueError('family is not canonical')
    return result


def evaluate_candidate(facts, family):
    """评估已归档结果，始终保留批准、留出事务与生产资格边界。"""
    frozen = _family(family)
    if not isinstance(facts, dict):
        raise ValueError('candidate evidence required')
    reasons = set()
    configurations = {row['config_id']: row for row in frozen['configurations']}
    selected = configurations.get(facts.get('config_id'))
    if (facts.get('family_sha256') != family.sha256
            or selected is None
            or facts.get('identities') != frozen['identities']):
        reasons.add('identity_mismatch')
    if selected is None or selected['cost_scenario'] != 'base':
        reasons.add('base_configuration_required')
    window = facts.get('evaluation_window')
    if (facts.get('evaluation_stage') != 'final_holdout'
            or not isinstance(window, dict) or set(window) != {'start_ms', 'end_ms'}
            or any(type(window[key]) is not int or window[key] != frozen['holdout'][key]
                   for key in ('start_ms', 'end_ms'))):
        reasons.add('holdout_window_mismatch')
    try:
        _id(facts.get('evaluation_ref'))
    except ValueError:
        reasons.add('evaluation_reference_missing')
    for key, expected, reason in (
        ('dataset_quality', 'complete', 'data_incomplete'),
        ('cost_completeness', 'reconciled', 'cost_incomplete'),
        ('all_attempts_registered', True, 'attempt_history_incomplete'),
        ('holdout_once_verified', True, 'holdout_once_unverified'),
        ('risk_compliant', True, 'risk_noncompliant'),
        ('native_protection_verified', True, 'protection_unverified'),
    ):
        if facts.get(key) != expected or (expected is True and facts.get(key) is not True):
            reasons.add(reason)
    base = facts.get('base') if isinstance(facts.get('base'), dict) else {}
    count = base.get('complete_roundtrips')
    if type(count) is not int or count < 100:
        reasons.add('insufficient_roundtrips')
    try:
        if _amount(base.get('net_profit')) <= 0:
            reasons.add('base_net_not_positive')
    except ValueError:
        reasons.add('base_net_unknown')
    try:
        wins = _amount(base.get('gross_wins'), nonnegative=True)
        losses = _amount(base.get('gross_losses'), nonnegative=True)
        with localcontext() as context:
            context.prec = 512
            if wins < Decimal('1.2') * losses or (wins == losses == 0):
                reasons.add('profit_factor_below_1_2')
            if wins - losses != _amount(base.get('net_profit')):
                reasons.add('roundtrip_net_sum_mismatch')
    except ValueError:
        reasons.add('profit_factor_unknown')
    stress = facts.get('stress') if isinstance(facts.get('stress'), dict) else {}
    stressed = configurations.get(stress.get('config_id'))
    if (selected is None or stressed is None or stressed['cost_scenario'] != 'stress'
            or any(stressed[key] != selected[key] for key in
                   ('symbol', 'timeframe', 'factor_set', 'exit_set', 'seed'))):
        reasons.add('stress_configuration_mismatch')
    try:
        if _amount(stress.get('net_profit')) < 0:
            reasons.add('stress_net_negative')
    except ValueError:
        reasons.add('stress_net_unknown')
    execution = facts.get('execution')
    try:
        if not isinstance(execution, dict) or execution.get('complete') is not True:
            raise ValueError('execution evidence incomplete')
        for key in ('fill_rate', 'partial_fill_rate'):
            if not 0 <= _amount(execution.get(key), nonnegative=True) <= 1:
                raise ValueError('invalid execution rate')
        _amount(execution.get('unfilled_opportunity_cost'), nonnegative=True)
        _amount(execution.get('adverse_selection'))
        _stamp(execution.get('latency_p99_ms'))
        policy = frozen['execution_policy']
        for key, limit, above, reason in (
            ('fill_rate', 'min_fill_rate', False, 'execution_fill_rate_below_minimum'),
            ('partial_fill_rate', 'max_partial_fill_rate', True, 'execution_partial_fill_rate_above_maximum'),
            ('adverse_selection', 'max_adverse_selection', True, 'execution_adverse_selection_above_maximum'),
            ('unfilled_opportunity_cost', 'max_unfilled_opportunity_cost', True, 'execution_opportunity_cost_above_maximum'),
        ):
            observed, bound = _amount(execution[key]), _amount(policy[limit])
            if (above and observed > bound) or (not above and observed < bound):
                reasons.add(reason)
        if execution['latency_p99_ms'] > policy['max_latency_p99_ms']:
            reasons.add('execution_latency_above_maximum')
    except ValueError:
        reasons.add('execution_incomplete')
    statistics = facts.get('statistics')
    comparison = frozen['comparison']
    try:
        if (not isinstance(statistics, dict)
                or statistics.get('method') != comparison['method']
                or statistics.get('block_ms') != comparison['block_ms']
                or statistics.get('multiple_testing') != comparison['multiple_testing']
                or type(statistics.get('family_size')) is not int
                or statistics['family_size'] != frozen['configuration_count']):
            raise ValueError('complete family comparison missing')
        adjusted = _amount(statistics.get('adjusted_p_value'), nonnegative=True)
        if adjusted > 1:
            raise ValueError('invalid adjusted probability')
        if adjusted > _amount(comparison['alpha']):
            reasons.add('comparison_not_significant')
        if _amount(statistics.get('net_profit_lower_bound')) <= 0:
            reasons.add('net_lower_bound_not_positive')
    except ValueError:
        reasons.add('family_comparison_incomplete')
    result = _freeze(dict(status='research_pass' if not reasons else 'not_estimable',
                        reason_codes=sorted(reasons), family_sha256=family.sha256,
                        config_id=facts.get('config_id'), evaluation_sha256=_hash(facts),
                        demo_precheck_candidate=not reasons, acceptance_eligible=False,
                        production_authorized=False, holdout_claim_committed=False))
    LOGGER.info('研究候选门槛 family_sha256=%s evaluation_sha256=%s status=%s reasons=%s；生产授权=False',
                family.sha256, result.to_dict()['evaluation_sha256'],
                'research_pass' if not reasons else 'not_estimable', ','.join(sorted(reasons)))
    return result


def audit_attempt_history(family, attempts):
    """检查提供的全量历史，不声称能发现调用者隐瞒的外部试验。"""
    frozen = _family(family)
    if not isinstance(attempts, list):
        raise ValueError('full attempt history required')
    configs = {row['config_id'] for row in frozen['configurations']}
    base_configs = {row['config_id'] for row in frozen['configurations'] if row['cost_scenario'] == 'base'}
    reasons, seen, attempted, holdout_count = set(), set(), set(), 0
    for sequence, row in enumerate(attempts, 1):
        if not isinstance(row, dict):
            reasons.add('attempt_row_invalid')
            continue
        if type(row.get('sequence')) is not int or row['sequence'] != sequence:
            reasons.add('attempt_sequence_gap')
        if row.get('family_sha256') != family.sha256:
            reasons.add('attempt_family_mismatch')
        if row.get('phase') not in {'validation', 'final_holdout'}:
            reasons.add('attempt_phase_unknown')
        if row.get('phase') == 'final_holdout':
            holdout_count += 1
            if holdout_count > 1:
                reasons.add('holdout_reused')
            if row.get('config_id') not in base_configs:
                reasons.add('holdout_base_configuration_required')
        try:
            attempt_id = _id(row.get('attempt_id'))
            _id(row.get('artifact_ref'))
            if attempt_id in seen:
                reasons.add('attempt_duplicate')
            seen.add(attempt_id)
            started, finished = _stamp(row.get('started_ms')), _stamp(row.get('finished_ms'))
            if started < frozen['registered_ms'] or finished < started:
                reasons.add('attempt_time_invalid')
            if started < frozen['dataset']['end_ms']:
                reasons.add('attempt_dataset_not_available')
        except ValueError:
            reasons.add('attempt_reference_or_time_invalid')
        if row.get('status') not in {'failed', 'completed', 'canceled', 'not_estimable'}:
            reasons.add('attempt_status_unknown')
        if row.get('config_id') not in configs:
            reasons.add('attempt_config_unknown')
        else:
            attempted.add(row['config_id'])
    result = _freeze(dict(status='not_estimable' if reasons else 'pass', reason_codes=sorted(reasons),
                        family_sha256=family.sha256, history_sha256=_hash(attempts),
                        attempt_count=len(attempts), configuration_count=frozen['configuration_count'],
                        unattempted_configuration_count=len(configs - attempted),
                        hidden_attempts_excluded=False, acceptance_eligible=False))
    LOGGER.info('研究尝试审计 family_sha256=%s attempts=%s configs=%s holdout_attempts=%s status=%s reasons=%s',
                family.sha256, len(attempts), len(configs), holdout_count,
                'not_estimable' if reasons else 'pass', ','.join(sorted(reasons)))
    return result
