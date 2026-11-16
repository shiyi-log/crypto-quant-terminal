"""冻结入场提案与精确风险定仓；无 I/O、无预算预占、无交易授权。

调用方必须先取得同一账户、阶段和水位的可信事实。此模块只校验显式输入并
计算提案，不能证明这些事实的来源真实性或解决并发。最终 Decision、预算预占
和订单意图仍须由唯一 writer 在同一数据库事务内裁定并提交。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Mapping

VERSION = 'demo-entry-decision-proposal-v1'
PAIRS = ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'TRX/USDT:USDT')
ALIASES = {pair.split('/')[0] + 'USDT': pair for pair in PAIRS}
DECIMAL_PATTERN = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
TEXT_PATTERN = re.compile(r'[A-Za-z0-9_./:@+-]{1,160}\Z')
HASH_PATTERN = re.compile(r'[0-9a-f]{64}\Z')
IDENTITIES = ('account_id', 'strategy_version_id', 'run_id', 'stage_id', 'signal_id', 'decision_id')
HASHES = ('input_fingerprint', 'strategy_hash', 'risk_policy_hash', 'cost_model_hash',
          'account_snapshot_hash', 'market_snapshot_hash', 'exchange_rules_hash', 'exit_params_hash')
REFERENCES = ('account_snapshot_ref', 'market_snapshot_ref', 'exchange_rules_ref', 'exposure_watermark')
TRUE_FIELDS = (
    'strategy_approved', 'indicators_ready', 'data_complete', 'inputs_closed',
    'time_aligned', 'costs_complete', 'funding_complete', 'account_reconciled',
    'risk_entries_enabled', 'native_protection_verified', 'exposure_complete',
    'margin_evidence_complete',
)
POSITIVE_FIELDS = (
    'stage_initial_usdt', 'net_equity_usdt', 'entry_price', 'stop_price', 'target_price',
    'atr14', 'price_tick', 'quantity_step', 'min_quantity', 'max_quantity',
    'frozen_max_loss_usdt', 'frozen_max_notional_usdt', 'initial_margin_per_unit_usdt',
)
NONNEGATIVE_FIELDS = (
    'used_risk_usdt', 'used_margin_usdt', 'min_notional_usdt', 'entry_maker_fee_rate',
    'stop_taker_fee_rate', 'target_exit_fee_rate', 'stop_slippage_per_unit_usdt',
    'target_slippage_per_unit_usdt', 'funding_expense_per_unit_usdt', 'other_cost_per_unit_usdt',
)
INTEGER_FIELDS = ('signal_at_ms', 'decision_at_ms', 'quote_at_ms', 'account_at_ms',
                  'max_signal_age_ms', 'max_quote_age_ms', 'max_account_age_ms')


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """结果仅持有不可变值；返回的字典是独立拷贝，不会改写冻结提案。"""

    verdict: str
    reason_codes: tuple[str, ...]
    canonical_json: str
    sha256: str

    def to_dict(self) -> dict:
        return json.loads(self.canonical_json)


def _decimal(value, *, positive):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if isinstance(value, str) and (len(value) > 200 or not DECIMAL_PATTERN.fullmatch(value)):
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if (not number.is_finite() or len(number.as_tuple().digits) > 96
            or abs(number.as_tuple().exponent) > 192
            or (number and abs(number.adjusted()) > 96)
            or (number <= 0 if positive else number < 0)):
        return None
    return number


def _text(value, *, hashed=False):
    pattern = HASH_PATTERN if hashed else TEXT_PATTERN
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _string(number):
    if number is None:
        return None
    if number == 0:
        return '0'
    return format(number.normalize(), 'f')


def propose_entry_decision(source: Mapping) -> EntryDecision:
    """冻结最终量价/成本和所有拒绝原因；eligible_proposal 仍需事务复核。

    所有数值接受 Decimal、十进制字符串或整数。拒绝 bool/float、非有限值及
    超过 96 位的输入，使用独立高精度上下文避免调用方的精度改变边界裁定。
    成本字段均为明确的非负支出，资金费收入不抵销保守预期开仓风险。
    """
    with localcontext(Context(prec=1024)):
        return _propose(source)


def _propose(source):
    data = source if isinstance(source, Mapping) else {}
    reasons, result, numbers = [], {'version': VERSION}, {}

    def deny(code):
        if code not in reasons:
            reasons.append(code)

    environment = data.get('environment')
    result['environment'] = environment if environment == 'binance_demo' else None
    if result['environment'] is None:
        deny('environment_invalid')
    for name in IDENTITIES + HASHES + REFERENCES:
        result[name] = _text(data.get(name), hashed=name in HASHES)
        if result[name] is None:
            deny(name + '_invalid')
    for name in TRUE_FIELDS:
        value = data.get(name)
        result[name] = value if type(value) is bool else None
        if value is not True:
            deny(name + '_required')
    for name in INTEGER_FIELDS:
        value = data.get(name)
        result[name] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        if result[name] is None:
            deny(name + '_invalid')
    for prefix in ('signal', 'quote', 'account'):
        stamp, now, max_age = (result[prefix + '_at_ms'], result['decision_at_ms'],
                               result['max_' + prefix + '_age_ms'])
        if stamp is not None and now is not None and max_age is not None:
            if stamp > now:
                deny(prefix + '_from_future')
            elif now - stamp > max_age:
                deny(prefix + '_stale')
    for name in POSITIVE_FIELDS + NONNEGATIVE_FIELDS:
        number = _decimal(data.get(name), positive=name in POSITIVE_FIELDS)
        numbers[name] = number
        result[name] = _string(number)
        if number is None:
            deny(name + '_invalid')
    leverage = data.get('actual_leverage')
    if type(leverage) is not int or not 1 <= leverage <= 10:
        leverage = None
        deny('actual_leverage_invalid')
    result['actual_leverage'] = leverage
    result['margin_type'] = data.get('margin_type') if data.get('margin_type') == 'ISOLATED' else None
    if result['margin_type'] is None:
        deny('isolated_margin_required')
    for field, code in (('is_auto_add_margin', 'auto_add_margin_not_disabled'),
                        ('hedged', 'one_way_mode_required')):
        value = data.get(field)
        result[field] = value if type(value) is bool else None
        if value is not False:
            deny(code)
    pair = data.get('pair')
    result['pair'] = pair if isinstance(pair, str) and pair in PAIRS else None
    if result['pair'] is None:
        deny('pair_invalid')
    side = data.get('side')
    result['side'] = side if isinstance(side, str) and side in ('buy', 'sell') else None
    if result['side'] is None:
        deny('side_invalid')
    active = data.get('active_symbols')
    if (not isinstance(active, (list, tuple))
            or any(not isinstance(item, str) or item not in PAIRS + tuple(ALIASES) for item in active)):
        deny('active_symbols_invalid')
        result['active_symbols_before'] = None
        result['active_symbols_after'] = None
    else:
        symbols = set(ALIASES.get(item, item) for item in active)
        result['active_symbols_before'] = sorted(symbols)
        if result['pair'] is not None:
            symbols.add(pair)
        result['active_symbols_after'] = sorted(symbols)
        if len(symbols) > 2:
            deny('active_symbol_limit_exceeded')
    entry, stop, target, tick = (numbers[key] for key in
                                ('entry_price', 'stop_price', 'target_price', 'price_tick'))
    for name in ('entry_price', 'stop_price', 'target_price'):
        if numbers[name] is not None and tick is not None and numbers[name] % tick:
            deny(name + '_not_on_tick')
    if entry is not None and stop is not None and result['side'] is not None:
        if not (stop < entry if side == 'buy' else stop > entry):
            deny('stop_direction_invalid')
    if entry is not None and stop is not None and target is not None and result['side'] is not None:
        expected_target = entry + (entry - stop) * 2
        if target != expected_target:
            deny('target_not_frozen_2r')
        if not (target > entry if side == 'buy' else target < entry):
            deny('target_direction_invalid')
    if (numbers['min_quantity'] is not None and numbers['max_quantity'] is not None
            and numbers['min_quantity'] > numbers['max_quantity']):
        deny('quantity_rules_inconsistent')
    if entry is not None and leverage is not None and numbers['initial_margin_per_unit_usdt'] is not None:
        # 真实档位/费用保证金由上游提供；连 notional/leverage 下界都不足则拒绝。
        if numbers['initial_margin_per_unit_usdt'] * leverage < entry:
            deny('margin_estimate_below_leverage_floor')

    derived = ('risk_base_usdt', 'entry_risk_limit_usdt', 'account_risk_limit_usdt',
               'account_margin_limit_usdt', 'account_risk_before_usdt', 'account_risk_after_usdt',
               'account_margin_before_usdt', 'account_margin_after_usdt',
               'quantity', 'notional_usdt', 'planned_loss_usdt', 'planned_margin_usdt',
               'expected_stop_cost_usdt', 'expected_target_cost_usdt', 'net_target_gain_usdt',
               'net_reward_risk_ratio', 'risk_distance_per_unit_usdt',
               'stop_execution_price', 'target_execution_price')
    result.update({name: None for name in derived})
    result['account_risk_before_usdt'] = result['used_risk_usdt']
    result['account_margin_before_usdt'] = result['used_margin_usdt']
    stage, equity = numbers['stage_initial_usdt'], numbers['net_equity_usdt']
    if stage is not None and equity is not None:
        base = min(stage, equity)
        result.update(risk_base_usdt=_string(base), entry_risk_limit_usdt=_string(base * Decimal('.01')),
                      account_risk_limit_usdt=_string(base * Decimal('.02')),
                      account_margin_limit_usdt=_string(base * Decimal('.5')))
    if (all(numbers[key] is not None for key in POSITIVE_FIELDS + NONNEGATIVE_FIELDS)
            and result['side'] is not None):
        base = min(stage, equity)
        used_risk, used_margin = numbers['used_risk_usdt'], numbers['used_margin_usdt']
        risk_available, margin_available = base * Decimal('.02') - used_risk, base * Decimal('.5') - used_margin
        if risk_available <= 0:
            deny('account_risk_budget_exhausted')
        if margin_available <= 0:
            deny('account_margin_budget_exhausted')
        distance = abs(entry - stop)
        # 滑点是每单位价格支出；空头 BUY 退出的价格上升也会增加手续费。
        # 费用基数采用触发价/不利执行价的较大者，长短方向均不低估费用。
        exit_direction = -1 if side == 'buy' else 1
        stop_execution = stop + exit_direction * numbers['stop_slippage_per_unit_usdt']
        target_execution = target + exit_direction * numbers['target_slippage_per_unit_usdt']
        result['stop_execution_price'] = _string(stop_execution) if stop_execution > 0 else None
        result['target_execution_price'] = _string(target_execution) if target_execution > 0 else None
        if stop_execution <= 0:
            deny('stop_execution_price_invalid')
        if target_execution <= 0:
            deny('target_execution_price_invalid')
        stop_cost = (entry * numbers['entry_maker_fee_rate'] + max(stop, stop_execution) * numbers['stop_taker_fee_rate']
                     + numbers['stop_slippage_per_unit_usdt'] + numbers['funding_expense_per_unit_usdt']
                     + numbers['other_cost_per_unit_usdt'])
        target_cost = (entry * numbers['entry_maker_fee_rate'] + max(target, target_execution) * numbers['target_exit_fee_rate']
                       + numbers['target_slippage_per_unit_usdt'] + numbers['funding_expense_per_unit_usdt']
                       + numbers['other_cost_per_unit_usdt'])
        unit_loss, unit_gain = distance + stop_cost, abs(target - entry) - target_cost
        if unit_loss <= 0:
            deny('planned_loss_not_positive')
        elif unit_gain * 2 < unit_loss * 3:
            # 不用四舍五入后的 RR 判断阈值，精确 .2 与最小超额必须区分。
            deny('net_reward_risk_below_1_5')
        step = numbers['quantity_step']
        if unit_loss > 0:
            caps = (
                max(Decimal(0), min(base * Decimal('.01'), numbers['frozen_max_loss_usdt'], risk_available)) / (unit_loss * step),
                max(Decimal(0), margin_available) / (numbers['initial_margin_per_unit_usdt'] * step),
                numbers['frozen_max_notional_usdt'] / (entry * step),
                numbers['max_quantity'] / step,
            )
            qty = min(cap.to_integral_value(rounding='ROUND_FLOOR') for cap in caps) * step
            notional, loss, margin = qty * entry, qty * unit_loss, qty * numbers['initial_margin_per_unit_usdt']
            if qty < numbers['min_quantity'] or qty <= 0:
                deny('quantity_below_minimum')
            if notional < numbers['min_notional_usdt']:
                deny('notional_below_minimum')
            if loss > base * Decimal('.01') or loss > numbers['frozen_max_loss_usdt']:
                deny('entry_risk_limit_exceeded')
            if used_risk + loss > base * Decimal('.02'):
                deny('account_risk_limit_exceeded')
            if used_margin + margin > base * Decimal('.5'):
                deny('account_margin_limit_exceeded')
            result.update(quantity=_string(qty), notional_usdt=_string(notional),
                          planned_loss_usdt=_string(loss), planned_margin_usdt=_string(margin),
                          expected_stop_cost_usdt=_string(qty * stop_cost),
                          expected_target_cost_usdt=_string(qty * target_cost),
                          net_target_gain_usdt=_string(qty * unit_gain),
                          net_reward_risk_ratio=_string(unit_gain / unit_loss),
                          risk_distance_per_unit_usdt=_string(distance),
                          account_risk_after_usdt=_string(used_risk + loss),
                          account_margin_after_usdt=_string(used_margin + margin))
    result['reason_codes'] = reasons
    result['verdict'] = 'rejected' if reasons else 'eligible_proposal'
    result['reservation_committed'] = False
    result['acceptance_eligible'] = False
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return EntryDecision(result['verdict'], tuple(reasons), encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest())
