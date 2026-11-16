"""原生条件保护的同步协调器；调用者提供独占账户写锁和可信完整事实。

本模块尚未接入生产执行服务，不能授予验收或新开仓资格。所有网络调用均在
持久意图/单次claim事务结束后发生；未知结果只查询原clientAlgoId。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext

from binance_demo_adapter import stable_client_order_id

LOGGER = logging.getLogger(__name__)
SAFE_ID = re.compile(r'[A-Za-z0-9_.:/@-]{1,160}\Z')
CLIENT_ID = re.compile(r'qd-[a-f0-9]{32}\Z')
PAIRS = {'BTC/USDT:USDT', 'ETH/USDT:USDT', 'TRX/USDT:USDT'}
KINDS = ('STOP_MARKET', 'TAKE_PROFIT_MARKET')
TERMINAL = {'CANCELED', 'EXPIRED', 'REJECTED', 'FINISHED'}


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('invalid decimal evidence')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid decimal evidence') from None
    if (not result.is_finite() or len(str(value)) > 128
            or abs(result.as_tuple().exponent) > 64 or result.adjusted() > 64
            or (positive and result <= 0)):
        raise ValueError('invalid decimal evidence')
    return result


def _number(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('invalid timestamp evidence')
    return value


def _safe_id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError('invalid safe identity')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


class NativeProtectionCoordinator:
    """调用者必须共享唯一账户锁；回调本身须持久化幂等并复核所有权。"""

    def __init__(self, ledger, adapter, *, account_id, account_lock, pause_entries,
                 cancel_remaining_entry, reduce_owned, monotonic_ms=None):
        self.account_id = _safe_id(account_id)
        if not hasattr(account_lock, '__enter__') or not hasattr(account_lock, '__exit__'):
            raise ValueError('account writer lock required')
        if not all(callable(callback) for callback in
                   (pause_entries, cancel_remaining_entry, reduce_owned)):
            raise ValueError('protection callbacks required')
        self.ledger, self.adapter, self.lock = ledger, adapter, account_lock
        self.pause_entries, self.cancel_remaining_entry = pause_entries, cancel_remaining_entry
        self.reduce_owned = reduce_owned
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1_000_000)
        if not callable(self.monotonic_ms):
            raise ValueError('monotonic clock required')

    def _now(self):
        elapsed = _stamp(self.monotonic_ms()) - self._clock_anchor
        if elapsed < 0:
            raise ValueError('monotonic clock regressed')
        self._last_now = max(self._last_now, self._wall_anchor + elapsed)
        return self._last_now

    def _plan(self, value):
        if not isinstance(value, dict):
            raise ValueError('frozen plan missing')
        fixed = {key: _safe_id(value.get(key)) for key in
                 ('account_id', 'strategy_id', 'run_id', 'entry_decision_id')}
        if value.get('environment') != 'binance_demo' or fixed['account_id'] != self.account_id:
            raise ValueError('plan account environment mismatch')
        parent, pair = value.get('parent_entry_client_id'), value.get('pair')
        if not isinstance(parent, str) or not CLIENT_ID.fullmatch(parent) or pair not in PAIRS:
            raise ValueError('plan parent pair invalid')
        side = value.get('position_side')
        if (side not in {'long', 'short'} or value.get('break_even_costs_verified') is not True
                or value.get('working_type') not in {'MARK_PRICE', 'CONTRACT_PRICE'}
                or type(value.get('price_protect')) is not bool
                or not isinstance(value.get('exit_params_hash'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', value['exit_params_hash'])):
            raise ValueError('frozen exit semantics missing')
        fixed.update(environment='binance_demo', parent_entry_client_id=parent, pair=pair,
                     position_side=side, working_type=value['working_type'],
                     price_protect=value['price_protect'], exit_params_hash=value['exit_params_hash'])
        amounts = {key: _decimal(value.get(key), positive=True) for key in
                   ('entry_price', 'initial_stop_price', 'target_price', 'entry_atr',
                    'break_even_stop_price', 'price_tick')}
        with localcontext() as ctx:
            ctx.prec = 256
            direction = Decimal(1 if side == 'long' else -1)
            risk = (amounts['entry_price'] - amounts['initial_stop_price']) * direction
            if (risk <= 0 or amounts['target_price'] != amounts['entry_price'] + direction * 2 * risk
                    or (amounts['break_even_stop_price'] - amounts['entry_price']) * direction <= 0
                    or (amounts['break_even_stop_price'] - amounts['entry_price']) * direction > risk
                    or any(amounts[key] % amounts['price_tick'] for key in
                           ('entry_price', 'initial_stop_price', 'target_price', 'break_even_stop_price'))):
                raise ValueError('frozen risk target or price grid invalid')
        fixed.update({key: _number(amount) for key, amount in amounts.items()})
        for key in ('max_snapshot_age_ms', 'max_market_age_ms', 'protection_target_ms'):
            stamp = _stamp(value.get(key))
            if stamp <= 0 or (key == 'protection_target_ms' and stamp > 2000):
                raise ValueError('invalid protection evidence deadline')
            fixed[key] = stamp
        return fixed

    def _position(self, value, plan, now):
        if not isinstance(value, dict):
            raise ValueError('position evidence missing')
        for key in ('environment', 'account_id', 'strategy_id', 'run_id',
                    'parent_entry_client_id', 'pair'):
            if value.get(key) != plan[key]:
                raise ValueError('position identity mismatch')
        if (any(value.get(key) is not True for key in
                ('facts_complete', 'owned_position', 'fills_reconciled',
                 'normal_orders_complete', 'conditional_orders_complete'))
                or value.get('ownership_conflicts') != []
                or not isinstance(value.get('normal_open_orders'), list)):
            raise ValueError('position facts incomplete')
        _safe_id(value.get('source_ref'))
        _safe_id(value.get('fill_watermark'))
        observed, started = _stamp(value.get('observed_ms')), _stamp(value.get('exposure_started_ms'))
        if not started <= observed <= now or now - observed > plan['max_snapshot_age_ms']:
            raise ValueError('position evidence not fresh')
        quantity = _decimal(value.get('signed_quantity'))
        if quantity and ((quantity > 0) != (plan['position_side'] == 'long')):
            raise ValueError('position direction mismatch')
        return quantity.copy_abs()

    def _client(self, plan, kind, revision):
        # 完整冻结参数绑定进每代client ID；重启时也能发现初始R/ATR/目标漂移。
        return stable_client_order_id(
            'native-protection:' + self.account_id, _digest(plan), plan['pair'],
            'sell' if plan['position_side'] == 'long' else 'buy',
            kind + ':' + str(revision))

    def _rows(self, plan):
        rows = [row for row in self.ledger.list_conditionals()
                if row.get('parent_entry_client_id') == plan['parent_entry_client_id']]
        for row in rows:
            if (any(row.get(key) != plan[key] for key in
                    ('environment', 'pair', 'strategy_id', 'run_id', 'entry_decision_id'))
                    or row['client_algo_id'] != self._client(plan, row['order_type'], row['revision'])):
                raise ValueError('durable frozen plan identity mismatch')
        return rows

    def _error(self, action, error):
        # 绝不记录异常消息、raw响应、签名URL或调用者完整事实。
        LOGGER.warning('原生保护协调 action=%s account=%s error_type=%s；保留未知并暂停新风险',
                       action, self.account_id, type(error).__name__)

    def _query(self, row, now, fresh):
        client = row['client_algo_id']
        self.adapter.authorize_owned_client_id(client)
        try:
            fact = self.adapter.fetch_conditional_order(row['pair'], client)
            now = self._now()
            row = self.ledger.record_conditional_observation(client, fact, now)
            if (fact is not None and row.get('last_exchange_observed_ms') == now
                    and fact.get('updated_ms') == row.get('exchange_updated_ms')
                    and fact.get('algo_status') == row.get('algo_status')):
                fresh.add(client)
            else:
                fresh.discard(client)
            if row.get('algo_status') in {'TRIGGERING', 'TRIGGERED', 'FINISHED'}:
                self._children[client] = self.adapter.fetch_conditional_child_order(row['pair'], client)
                LOGGER.info('原生保护协调 子订单已查询 account=%s client=%s algo_status=%s；仍待持仓成交对账',
                            self.account_id, client, row['algo_status'])
        except Exception as error:
            fresh.discard(client)
            self._error('query', error)
        return self.ledger.get_conditional(client)

    def _new(self, row, now, age, fresh):
        now = self._now()
        observed = row.get('last_exchange_observed_ms')
        return (row['client_algo_id'] in fresh and row.get('status') == 'confirmed'
                and row.get('algo_status') == 'NEW' and row.get('cancel_pending') is False
                and type(observed) is int and 0 <= now - observed <= age)

    def _install(self, plan, kind, quantity, trigger, previous, now, fresh):
        revision = previous['revision'] + 1 if previous else 1
        client = self._client(plan, kind, revision)
        intent = {key: plan[key] for key in
                  ('environment', 'strategy_id', 'run_id', 'entry_decision_id',
                   'parent_entry_client_id', 'pair', 'working_type', 'price_protect')}
        intent.update(client_algo_id=client, revision=revision, order_type=kind,
                      side='sell' if plan['position_side'] == 'long' else 'buy',
                      quantity=_number(quantity), trigger_price=_number(trigger),
                      reduce_only=True, created_ms=now,
                      supersedes_client_algo_id=previous['client_algo_id'] if previous else None)
        row, _ = self.ledger.reserve_conditional(intent)
        return self._submit_row(row, now, fresh)

    def _submit_row(self, row, now, fresh):
        client, kind = row['client_algo_id'], row['order_type']
        now = self._now()
        if not self.ledger.claim_conditional_submission(client, now):
            return self._query(row, now, fresh)
        self.adapter.authorize_owned_client_id(client)
        submit = (self.adapter.submit_stop_market_order if kind == 'STOP_MARKET'
                  else self.adapter.submit_take_profit_order)
        try:
            submit(row['pair'], row['side'], row['quantity'], row['trigger_price'],
                   client, working_type=row['working_type'],
                   price_protect=row['price_protect'], reduce_only=True)
        except Exception as error:
            # 已claim即使明确拒绝也不可复用身份；查询真实事实后再作决定。
            self.ledger.mark_conditional_unknown(client, type(error).__name__, self._now())
            self._error('submit', error)
        return self._query(self.ledger.get_conditional(client), now, fresh)

    def _cancel(self, row, now, fresh):
        row = self._query(row, now, fresh)
        now = self._now()
        if (now - self._position_evidence['observed_ms'] > self._current_plan['max_snapshot_age_ms']
                or not self._new(row, now, self._current_plan['max_snapshot_age_ms'], fresh)):
            return row
        try:
            receipt = self.adapter.cancel_conditional_order(
                row['pair'], row['algo_id'], client_algo_id=row['client_algo_id'])
            self.ledger.record_conditional_observation(row['client_algo_id'], receipt, self._now())
        except Exception as error:
            self._error('cancel', error)
            fresh.discard(row['client_algo_id'])
            return self.ledger.get_conditional(row['client_algo_id'])
        return self._query(self.ledger.get_conditional(row['client_algo_id']), now, fresh)

    def _stop(self, plan, rows, market, now):
        now = self._now()
        long = plan['position_side'] == 'long'
        prices = [_decimal(plan['initial_stop_price'])] + [
            _decimal(row['trigger_price']) for row in rows if row['order_type'] == 'STOP_MARKET']
        stop = max(prices) if long else min(prices)
        if market is None:
            return stop
        if (not isinstance(market, dict) or market.get('data_complete') is not True
                or not 0 <= now - _stamp(market.get('observed_ms')) <= plan['max_market_age_ms']):
            raise ValueError('market evidence not fresh')
        _safe_id(market.get('source_ref'))
        price = _decimal(market.get('price'), positive=True)
        with localcontext() as ctx:
            ctx.prec = 256
            entry, initial = _decimal(plan['entry_price']), _decimal(plan['initial_stop_price'])
            direction = Decimal(1 if long else -1)
            if (price - entry) * direction >= abs(entry - initial):
                trail = price - direction * _decimal(plan['entry_atr'])
                candidate = (max if long else min)(_decimal(plan['break_even_stop_price']), trail)
                tick = _decimal(plan['price_tick'])
                candidate = ((candidate / tick).to_integral_value(
                    rounding=ROUND_FLOOR if long else ROUND_CEILING) * tick)
                stop = (max if long else min)(stop, candidate)
        return stop

    def _pause(self, plan, position, reason, fallback, now):
        now = self._now()
        for callback, args in ((self.pause_entries, (reason, position)),
                               (self.cancel_remaining_entry, (plan['parent_entry_client_id'], reason))):
            try:
                callback(*args)
            except Exception as error:
                self._error('entry_pause_callback', error)
        if fallback and now - self._deadline_origin >= plan['protection_target_ms']:
            key = _digest({'account_id': self.account_id, 'run_id': plan['run_id'],
                           'entry_decision_id': plan['entry_decision_id'],
                           'parent': plan['parent_entry_client_id'],
                           'owned_quantity': _number(_decimal(position['signed_quantity']).copy_abs()),
                           'fill_watermark': position['fill_watermark'],
                           'role': 'owned-protection-fallback'})
            try:
                request = {**position, 'protection_request': {
                    'now_ms': now, 'requires_fresh_owned_reconciliation': True,
                    'snapshot_stale': now - position['observed_ms'] > plan['max_snapshot_age_ms'],
                    'reduce_only': True, 'account_id': self.account_id}}
                self.reduce_owned(request, reason, key)
                LOGGER.warning('原生保护协调 超过工程保护目标 account=%s run=%s key=%s；请求自有reduce-only兜底',
                               self.account_id, plan['run_id'], key)
                return True
            except Exception as error:
                self._error('reduce_callback', error)
        return False

    def _flat(self, plan, position, rows, now, fresh, result):
        # 先看完整开放快照，外部条件单由人工/其所有者处理，绝不自动撤销。
        try:
            opened = self.adapter.fetch_open_conditional_orders(plan['pair'])
            if not isinstance(opened, list):
                raise ValueError('conditional snapshot incomplete')
            owned = {row['client_algo_id'] for row in rows}
            if any(row.get('client_algo_id') not in owned for row in opened):
                result['status'] = 'blocked_external_orders'
                self._pause(plan, position, result['status'], False, now)
                return result
            for row in rows:
                if row.get('algo_status') == 'NEW':
                    self._cancel(row, now, fresh)
            opened = self.adapter.fetch_open_conditional_orders(plan['pair'])
            if not isinstance(opened, list):
                raise ValueError('conditional snapshot incomplete')
            rows = self._rows(plan)
            terminal = all(row['client_algo_id'] in fresh
                           and row.get('status') == 'confirmed'
                           and row.get('algo_status') in TERMINAL
                           and type(row.get('exchange_updated_ms')) is int
                           and row['exchange_updated_ms'] <= position['observed_ms']
                           for row in rows)
            for row in rows:
                if row.get('algo_status') == 'FINISHED' or row.get('actual_order_id'):
                    child = self._children.get(row['client_algo_id'])
                    terminal = bool(terminal and isinstance(child, dict)
                                    and child.get('order_id') == row.get('actual_order_id')
                                    and child.get('status') in {'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'}
                                    and type(child.get('updated_ms')) is int
                                    and child['updated_ms'] <= position['observed_ms']
                                    and isinstance(position.get('reconciled_child_order_ids'), list)
                                    and child['order_id'] in position['reconciled_child_order_ids'])
            result['cleanup_complete'] = bool(terminal and not opened
                                               and not position['normal_open_orders']
                                               and self._now() - position['observed_ms']
                                               <= plan['max_snapshot_age_ms'])
            result['status'] = 'flat_reconciled' if result['cleanup_complete'] else 'protection_incomplete'
        except Exception as error:
            self._error('flat_cleanup', error)
            result['status'] = 'protection_incomplete'
        self._pause(plan, position, result['status'], False, now)
        return result

    def protect(self, frozen_plan, position, *, now_ms, market=None):
        """对一次可信新鲜账户事实协调保护；结果永不表示成交/放行/真实验收。"""
        result = dict(status='blocked_evidence', entries_allowed=False, cleanup_complete=False,
                      fallback_requested=False, stop_covered_quantity='0', desired_stop_price=None,
                      execution_complete=False, acceptance_eligible=False)
        with self.lock:
            self._children = {}
            try:
                now = _stamp(now_ms)
                self._wall_anchor = self._last_now = now
                self._clock_anchor = _stamp(self.monotonic_ms())
                plan = self._plan(frozen_plan)
                quantity = self._position(position, plan, now)
                self._current_plan, self._position_evidence = plan, position
                rows = self._rows(plan)
                stop = self._stop(plan, rows, None, now)
            except (ValueError, TypeError, KeyError) as error:
                self._error('evidence', error)
                try:
                    self.pause_entries('blocked_evidence', {})
                except Exception as callback_error:
                    self._error('entry_pause_callback', callback_error)
                return result
            result['desired_stop_price'] = _number(stop)
            # 持久意图创建时间是保守上界，后续持仓快照不能把未知保护计时往后移。
            self._deadline_origin = min([position['exposure_started_ms']]
                                        + [row['created_ms'] for row in rows])
            fresh = set()
            for row in rows:
                if row.get('submission_attempts'):
                    self._query(row, now, fresh)
            rows = self._rows(plan)
            if quantity == 0:
                return self._flat(plan, position, rows, now, fresh, result)
            try:
                stop = self._stop(plan, rows, market, now)
                result['desired_stop_price'] = _number(stop)
            except (ValueError, TypeError, KeyError) as error:
                self._error('market_evidence', error)
                covered = max((_decimal(row['quantity']) for row in rows
                               if row['order_type'] == 'STOP_MARKET'
                               and self._new(row, now, plan['max_snapshot_age_ms'], fresh)),
                              default=Decimal(0))
                result['stop_covered_quantity'] = _number(min(quantity, covered))
                result['fallback_requested'] = self._pause(
                    plan, position, 'blocked_evidence', covered < quantity, now)
                return result
            if market is not None:
                with localcontext() as ctx:
                    ctx.prec = 256
                    crossed = ((_decimal(market['price']) - stop)
                               * Decimal(1 if plan['position_side'] == 'long' else -1) <= 0)
                if crossed:
                    result['status'] = 'protection_incomplete'
                    result['fallback_requested'] = self._pause(
                        plan, position, 'stop_crossed_pending_reconciliation', True, now)
                    return result
            cleanup_pending = False
            market_expired = False
            for kind, trigger in ((KINDS[0], stop), (KINDS[1], _decimal(plan['target_price']))):
                now = self._now()
                if now - position['observed_ms'] > plan['max_snapshot_age_ms']:
                    break
                if market is not None and now - market['observed_ms'] > plan['max_market_age_ms']:
                    market_expired = True
                    break
                selected = sorted((row for row in self._rows(plan) if row['order_type'] == kind),
                                  key=lambda row: row['revision'])
                latest = selected[-1] if selected else None
                if (latest is not None and latest.get('status') == 'intent'
                        and latest.get('submission_attempts') == 0
                        and _decimal(latest['quantity']) == quantity
                        and _decimal(latest['trigger_price']) == trigger):
                    latest = self._submit_row(latest, now, fresh)
                # 未知/已触发/冲突这一代必须先对账，不创建新身份绕过。
                if latest is not None and not self._new(latest, now, plan['max_snapshot_age_ms'], fresh):
                    break
                exact = (latest and _decimal(latest['quantity']) == quantity
                         and _decimal(latest['trigger_price']) == trigger)
                if not exact:
                    latest = self._install(plan, kind, quantity, trigger, latest, now, fresh)
                    if not self._new(latest, now, plan['max_snapshot_age_ms'], fresh):
                        break
                for old in selected:
                    if old['client_algo_id'] == latest['client_algo_id'] or old.get('algo_status') in TERMINAL:
                        continue
                    canceled = self._cancel(old, now, fresh)
                    if canceled.get('status') != 'confirmed' or canceled.get('algo_status') != 'CANCELED':
                        cleanup_pending = True
            rows = self._rows(plan)
            now = self._now()
            good = [row for row in rows if self._new(row, now, plan['max_snapshot_age_ms'], fresh)]
            covered = max((_decimal(row['quantity']) for row in good
                           if row['order_type'] == 'STOP_MARKET'), default=Decimal(0))
            result['stop_covered_quantity'] = _number(min(quantity, covered))
            full = all(any(row['order_type'] == kind and _decimal(row['quantity']) == quantity
                           and _decimal(row['trigger_price']) == trigger for row in good)
                       for kind, trigger in ((KINDS[0], stop), (KINDS[1], _decimal(plan['target_price']))))
            full = full and now - position['observed_ms'] <= plan['max_snapshot_age_ms']
            result['status'] = ('protected_cleanup_pending' if cleanup_pending else 'protected') if full else 'protection_incomplete'
            if market_expired:
                result['status'] = 'blocked_evidence'
            if result['status'] != 'protected':
                result['fallback_requested'] = self._pause(
                    plan, position, result['status'], not full, now)
            LOGGER.info('原生保护协调 account=%s strategy=%s run=%s parent=%s quantity=%s covered=%s '
                        'stop=%s status=%s source=%s fills=%s；不授予新开仓或验收资格',
                        self.account_id, plan['strategy_id'], plan['run_id'], plan['parent_entry_client_id'],
                        _number(quantity), result['stop_covered_quantity'], result['desired_stop_price'],
                        result['status'], position['source_ref'], position['fill_watermark'])
            return result
