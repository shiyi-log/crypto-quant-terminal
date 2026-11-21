"""唯一账户 writer 的内部安全接线；缺少权威归档事实时拒绝新增风险。

Provider 由服务端组合根注入，绝不能绑定 HTTP 请求字段。此模块不授予真实
验收资格，不推断保护能力，不以超时、暂停或查询 absent 释放风险预占。
"""
from __future__ import annotations

import copy
from decimal import Decimal, localcontext
import logging
import re
import time

from demo_entry_decision import propose_entry_decision
from demo_native_protection import NativeProtectionCoordinator

LOGGER = logging.getLogger(__name__)


class SafetyRuntime:
    """可信内部 provider 与原子风险账本间的窄接口。"""

    def __init__(self, reservation_store, *, account_scope, account_lock,
                 context_provider=None, entry_source_provider=None,
                 protection_facts_provider=None, completion_provider=None, conditional_store=None):
        if (not isinstance(account_scope, str) or not re.fullmatch(r'[A-Za-z0-9_./:+-]{1,256}', account_scope)
                or '://' in account_scope or '@' in account_scope):
            raise ValueError('必须配置可信账户范围')
        if not hasattr(account_lock, '__enter__') or not hasattr(account_lock, '__exit__'):
            raise ValueError('必须使用唯一账户 writer 锁')
        self.store, self.account_scope, self.lock = reservation_store, account_scope, account_lock
        self.context_provider = context_provider
        self.entry_source_provider = entry_source_provider
        self.protection_facts_provider = protection_facts_provider
        self.completion_provider = completion_provider
        self.conditional_store = conditional_store
        self.service = self.coordinator = None

    def bind(self, service):
        if self.service is not None and self.service is not service:
            raise ValueError('安全运行时不得跨策略复用')
        if (service._account_lock() is not self.lock
                or self.store.account_scope != self.account_scope
                or self.store.virtual_account_key != service.config.virtual_account_key
                or self.store.store is not service.store.store
                or self.store.environment != service.store.environment):
            raise ValueError('安全运行时账户、namespace 或 writer 锁不匹配')
        self.service = service
        if self.conditional_store is None:
            from demo_conditional_store import DemoConditionalStore
            self.conditional_store = DemoConditionalStore(service.store.store, service.config.virtual_account_key)
        if (self.conditional_store.virtual_account_key != service.config.virtual_account_key
                or self.conditional_store.store is not service.store.store
                or self.conditional_store.environment != service.store.environment):
            raise ValueError('条件单账本 namespace 不匹配')
        self.coordinator = NativeProtectionCoordinator(
            self.conditional_store, service.adapter, account_id=self.account_scope, account_lock=self.lock,
            pause_entries=lambda reason, facts: self._pause(service, reason, service.clock_ms()),
            cancel_remaining_entry=lambda parent, reason: service._reconcile(
                service.clock_ms(), cancel_entries=True),
            reduce_owned=lambda facts, reason, key: self._reduce_owned(service, facts, reason, key))

    def _bound(self, service):
        if self.service is not service or service._account_lock() is not self.lock:
            raise ValueError('安全运行时未绑定当前唯一 writer')

    @staticmethod
    def _archive(value):
        if (not isinstance(value, dict) or value.get('committed') is not True
                or not isinstance(value.get('archive_ref'), str) or not value['archive_ref']):
            raise ValueError('权威归档尚未提交')
        return copy.deepcopy(value)

    def _facts(self, provider, service, now, **extra):
        if not callable(provider):
            raise ValueError('缺少权威内部事实提供器')
        archive = self._archive(provider(account_scope=self.account_scope,
            strategy_id=service.config.strategy_id, now_ms=now, **extra))
        if archive.get('account_scope') != self.account_scope:
            raise ValueError('权威归档账户范围不匹配')
        return archive

    def _deny(self, service, event, reason, now, *, detail=None):
        service._status['safety_runtime_reason'] = reason
        try:
            service._record('entry_denied', event['event_id'], {
                'reason': reason, 'reason_zh': '权威安全事实不完整或不匹配，拒绝新增风险并继续自有仓位保护',
                'pair': event['payload']['pair'], 'detail': detail},
                fingerprint=event['data_fingerprint'], now=now)
        except Exception as error:
            service._status['safety_runtime_audit_error'] = type(error).__name__
        LOGGER.warning('Demo 安全运行时拒绝新增风险 账户=%s 策略=%s 信号=%s 原因=%s',
                       self.account_scope, service.config.strategy_id, event['event_id'], reason)

    def enter(self, service, event, *, now_ms):
        """最终报价、ATR、退出与成本来自内部已提交归档；事务内重算账户占用。"""
        with self.lock:
            try:
                self._bound(service)
                if not all(callable(provider) for provider in (self.context_provider,
                        self.entry_source_provider, self.protection_facts_provider, self.completion_provider)):
                    raise ValueError('缺少权威内部事实提供器')
                capabilities = service.adapter.protection_capabilities()
                if not isinstance(capabilities, dict) or capabilities.get('acceptance_eligible') is not True:
                    self._deny(service, event, 'demo_native_protection_unverified', now_ms)
                    return None
                protection = self.reconcile(service, now_ms=now_ms, positions=[])
                if (not protection['entries_allowed']
                        or not self.record_completions(service, now_ms=now_ms)
                        or not service._entries_enabled):
                    self._deny(service, event, 'safety_authoritative_inputs_unavailable', now_ms)
                    return None
                context_archive = self._facts(self.context_provider, service, now_ms)
                entry_archive = self._facts(self.entry_source_provider, service, now_ms,
                                            event=copy.deepcopy(event), context=copy.deepcopy(context_archive['context']))
                context, source = context_archive['context'], entry_archive['source']
                self._validate_source(service, event, source, context, now_ms)
                # 纯提案仅用于冻结最终数量；存储层在同一账户事务内重算并可进一步拒绝。
                proposal = propose_entry_decision(source).to_dict()
                if proposal['verdict'] != 'eligible_proposal':
                    self._deny(service, event, 'safety_entry_proposal_rejected', now_ms,
                               detail=proposal['reason_codes'])
                    return None
                intent = service._base_intent(event, source['pair'], source['side'], 'entry',
                    proposal['quantity'], proposal['entry_price'], source['decision_at_ms'], raw={
                        'entry_atr': source['atr14'],
                        'risk_stop_atr_mult': str(self._stop_multiple(service)),
                        'initial_stop_price': source['stop_price'], 'target_price': source['target_price'],
                        'entry_decision_id': source['decision_id'],
                        'entry_source_archive_ref': entry_archive['archive_ref'],
                        'account_context_archive_ref': context_archive['archive_ref'],
                        'expires_ms': source['decision_at_ms'] + service.config.entry_order_ttl_ms,
                        'safety_runtime': True})
                intent['signal_ms'] = source['signal_at_ms']
                self.store.install_context(context)
                result = self.store.reserve_entry(source, intent, context_id=context['context_id'])
                if result.get('accepted') is not True:
                    self._deny(service, event, 'safety_atomic_reservation_rejected', now_ms,
                               detail=result.get('decision', {}).get('reason_codes'))
                    return result
                return service._submit(intent, reservation=result, context_id=context['context_id'])
            except Exception as error:
                # 异常文本可能含上游敏感数据，只记录固定原因及异常类型。
                self._deny(service, event, 'safety_authoritative_inputs_unavailable', now_ms,
                           detail=type(error).__name__)
                return None

    @staticmethod
    def _stop_multiple(service):
        definition = service.account['definition']
        return Decimal(str((definition.get('params') or definition.get('params_frozen') or {})['risk_stop_atr_mult']))

    def _validate_source(self, service, event, source, context, now):
        payload = event['payload']
        expected = {'environment': 'binance_demo', 'account_id': self.account_scope,
            'strategy_version_id': service.config.strategy_id, 'signal_id': str(event['event_id']),
            'strategy_hash': service.account['rule_hash'], 'exit_params_hash': service.account['execution_hash'],
            'input_fingerprint': event['data_fingerprint'], 'pair': payload['pair'], 'side': payload['buy_sell']}
        if not isinstance(source, dict) or any(source.get(key) != value for key, value in expected.items()):
            raise ValueError('最终入场身份不匹配')
        if not isinstance(context, dict) or context.get('account_id') != self.account_scope:
            raise ValueError('账户上下文身份不匹配')
        if Decimal(str(context.get('stage_initial_usdt'))) != service.risk_policy.stage_initial_capital:
            raise ValueError('账户阶段本金与已恢复风险政策不匹配')
        for key in ('stage_id', 'stage_initial_usdt', 'net_equity_usdt', 'account_at_ms',
                    'max_account_age_ms', 'account_snapshot_ref', 'account_snapshot_hash', 'exposure_watermark'):
            if source.get(key) != context.get(key):
                raise ValueError('入场事实与账户上下文不匹配')
        for key in ('account_reconciled', 'exposure_complete', 'margin_evidence_complete',
                    'risk_entries_enabled', 'funding_complete', 'costs_complete', 'native_protection_verified'):
            if source.get(key) is not True or context.get(key) is not True:
                raise ValueError('账户安全事实不完整')
        if type(source.get('decision_at_ms')) is not int or source['decision_at_ms'] > now:
            raise ValueError('入场决策时间不合法')
        for stamp, maximum in (('signal_at_ms', 'max_signal_age_ms'),
                               ('quote_at_ms', 'max_quote_age_ms'), ('account_at_ms', 'max_account_age_ms')):
            if (type(source.get(stamp)) is not int or type(source.get(maximum)) is not int
                    or not 0 <= now - source[stamp] <= source[maximum]):
                raise ValueError('入场事实已过期')
        atr = (payload.get('evidence') or {}).get('entry_atr', (payload.get('evidence') or {}).get('atr'))
        with localcontext() as ctx:
            ctx.prec = 1024
            if Decimal(str(source['atr14'])) != Decimal(str(atr)):
                raise ValueError('入场 ATR 与冻结信号不匹配')
            distance = self._stop_multiple(service) * Decimal(str(source['atr14']))
            actual = (Decimal(str(source['entry_price'])) - Decimal(str(source['stop_price'])))
            if (distance <= 0 or actual != distance * (1 if source['side'] == 'buy' else -1)
                    or Decimal(str(source['frozen_max_notional_usdt'])) > Decimal(service.config.max_notional_usdt)):
                raise ValueError('最终报价或冻结退出参数不匹配')
            frozen_loss = service.account['execution'].get('max_loss_per_trade_usdt')
            if frozen_loss is not None and Decimal(str(source['frozen_max_loss_usdt'])) > Decimal(str(frozen_loss)):
                raise ValueError('入场事实放大冻结风险限制')

    def claim_entry(self, service, row, *, context_id, now_ms):
        self._bound(service)
        capabilities = service.adapter.protection_capabilities()
        if not isinstance(capabilities, dict) or capabilities.get('acceptance_eligible') is not True:
            return False
        return self.store.claim_reserved_entry(row['client_id'], now_ms=now_ms, context_id=context_id)

    def mark_unknown(self, client_id, error_type, *, observed_ms):
        self.store.mark_reserved_unknown(client_id, error_type, observed_ms=observed_ms)

    def _pause(self, service, reason, now):
        service._entries_enabled = False
        service._status.update(entries_enabled=False, safety_runtime_reason=reason)
        try:
            service._record('safety_runtime_paused', 'safety-runtime', {
                'reason': reason, 'reason_zh': '安全保护事实不足，暂停入口并继续对账和自有减仓'}, now=now)
        except Exception as error:
            service._status['safety_runtime_audit_error'] = type(error).__name__
            LOGGER.error('安全运行时审计未提交 账户=%s 错误类型=%s；入口保持暂停',
                         self.account_scope, type(error).__name__)

    def reconcile(self, service, *, now_ms, positions):
        """每轮对实际自有持仓运行原生协调；缺证据不会中断调用方旧保护。"""
        result = {'entries_allowed': False, 'acceptance_eligible': False, 'protection': []}
        with self.lock:
            try:
                self._bound(service)
                archive = self._facts(self.protection_facts_provider, service, now_ms, positions=copy.deepcopy(positions))
                items = archive.get('items')
                if not isinstance(items, list):
                    raise ValueError('保护事实列表缺失')
                actual = service._actual_net
                owned = service._net()
                covered = set()
                for item in items:
                    plan, fact = item['plan'], item['position']
                    pair = plan['pair']
                    quantity = Decimal(str(fact['signed_quantity']))
                    if (pair in covered or plan['strategy_id'] != service.config.strategy_id
                            or fact['strategy_id'] != service.config.strategy_id
                            or quantity != owned.get(pair, Decimal(0))
                            or quantity != actual.get(pair, Decimal(0))):
                        raise ValueError('原生保护与当前实际自有仓位不匹配')
                    self._validate_protection_plan(service, plan)
                    covered.add(pair)
                    result['protection'].append(self.coordinator.protect(plan, fact, now_ms=now_ms, market=item.get('market')))
                if set(pair for pair, qty in owned.items() if qty) - covered:
                    raise ValueError('自有仓位保护事实缺失')
                result['entries_allowed'] = all(row['status'] in ('protected', 'flat_reconciled')
                                                for row in result['protection'])
                if not result['entries_allowed']:
                    raise ValueError('原生保护未完成独立核对')
            except Exception as error:
                self._pause(service, 'safety_protection_facts_unavailable', now_ms)
                try:
                    service._reconcile(now_ms, cancel_entries=True)
                except Exception as cancel_error:
                    LOGGER.error('安全运行时入口撤销未完成 账户=%s 错误类型=%s', self.account_scope, type(cancel_error).__name__)
                result['error_type'] = type(error).__name__
        service._status['safety_runtime'] = copy.deepcopy(result)
        return result

    def _validate_protection_plan(self, service, plan):
        parent = service.store.get_order(plan['parent_entry_client_id'])
        if (parent is None or parent.get('reduce_only') is not False or parent.get('pair') != plan['pair']
                or parent.get('risk_reservation_decision_id') != plan['entry_decision_id']):
            raise ValueError('保护计划没有已接受的父入场身份')
        decision = next((row for row in self.store.decisions()
                         if row.get('decision_id') == plan['entry_decision_id']), None)
        if (decision is None or decision.get('reservation_committed') is not True
                or decision.get('run_id') != plan['run_id']
                or decision.get('exit_params_hash') != plan['exit_params_hash']
                or decision.get('strategy_version_id') != service.config.strategy_id
                or decision.get('pair') != plan['pair']):
            raise ValueError('保护计划与持久入场决策不匹配')
        intent = service._intent(parent)
        with localcontext() as ctx:
            ctx.prec = 1024
            actual = Decimal(str(parent['average_price']))
            atr = Decimal(str(intent['entry_atr']))
            distance = atr * Decimal(str(intent['risk_stop_atr_mult']))
            direction = 1 if decision['side'] == 'buy' else -1
            if (Decimal(str(plan['entry_price'])) != actual
                    or Decimal(str(plan['entry_atr'])) != atr
                    or Decimal(str(plan['initial_stop_price'])) != actual - direction * distance
                    or Decimal(str(plan['target_price'])) != actual + direction * 2 * distance):
                raise ValueError('保护计划必须基于实际成交价与冻结 ATR/2R')

    def _reduce_owned(self, service, facts, reason, key):
        # 协调器回调只提出请求；重新读取交易所及本地事实后才允许已有 reduce-only 通道。
        with self.lock:
            now = service.clock_ms()
            service._reconcile(now, cancel_entries=True)
            positions = service.adapter.fetch_positions()
            opened = service.adapter.fetch_open_orders()
            actual_by_pair = {}
            for row in positions:
                pair = row['pair']
                actual_by_pair[pair] = actual_by_pair.get(pair, Decimal(0)) + Decimal(str(row['signed_qty']))
            service._actual_net = actual_by_pair
            pair = facts['pair']
            owned, actual = service._net().get(pair, Decimal(0)), service._actual_net.get(pair, Decimal(0))
            if (service._ownership_conflicts(positions, opened) or not owned or owned != actual
                    or (owned > 0) != (Decimal(str(facts['signed_quantity'])) > 0)):
                self._pause(service, 'safety_fresh_owned_reduction_unverified', now)
                return
            service._exit(pair, owned, reason, key, now)

    def record_completions(self, service, *, now_ms):
        """只消费内部已提交且完整的成本结算事件；稳定事件 ID 由风险账本去重。"""
        started = time.perf_counter()
        input_count = 0
        new_count = 0
        replay_count = 0
        with self.lock:
            try:
                self._bound(service)
                archive = self._facts(self.completion_provider, service, now_ms)
                events = archive.get('events')
                if not isinstance(events, list):
                    raise ValueError('完整成本事件列表缺失')
                input_count = len(events)
                for event in events:
                    if (event.get('complete') is not True or event.get('funding_complete') is not True
                            or not isinstance(event.get('event_id'), str) or not event['event_id']
                            or not isinstance(event.get('trade_id'), str) or not event['trade_id']
                            or type(event.get('timestamp_ms')) is not int
                            or not 0 <= event['timestamp_ms'] <= now_ms):
                        raise ValueError('成本事件不完整或身份不稳定')
                # 候选策略隔离整批校验；后项无效或提交失败不能留下半批内存事件。
                refresh = service._shared_risk_store and not service.risk_policy.pending_events()
                if (refresh or not service._risk_events_loaded) and not service._load_risk_events():
                    raise ValueError('风险事件历史不可用')
                candidate = copy.deepcopy(service.risk_policy)
                ordered = self._ordered_completions(candidate.journal, events)
                new_count = len(ordered)
                replay_count = input_count - new_count
                events = ordered
                for event in events:
                    candidate.record_round_trip(timestamp_ms=event['timestamp_ms'],
                        net_pnl_usdt=event['net_pnl_usdt'], trade_id=event['trade_id'], event_id=event['event_id'])
                pending = candidate.pending_events()
                if pending:
                    service.risk_store.append_risk_events(pending)
                    candidate.acknowledge_events(event['event_id'] for event in pending)
                service.risk_policy = candidate
                state = candidate.status(now_ms)
                if state['hard_failure'] or state['entries_mode'] in {
                        'lock_until_reconciled', 'stop_until_next_day', 'cooldown', 'terminated'}:
                    self._pause(service, 'safety_committed_cost_risk_gate', now_ms)
                LOGGER.info('Demo 完整成本事件处理完成 阶段=风险账本提交 输入数=%d 新增数=%d 重放数=%d 拒绝原因码=none 耗时_ms=%.3f；不记录交易载荷',
                            input_count, new_count, replay_count, (time.perf_counter() - started) * 1000)
                return True
            except Exception as error:
                self._pause(service, 'safety_complete_costs_unavailable', now_ms)
                try:
                    service._reconcile(now_ms, cancel_entries=True)
                except Exception as cancel_error:
                    service._status['safety_runtime_cancel_error'] = type(cancel_error).__name__
                LOGGER.warning('Demo 完整成本事件处理失败 阶段=风险账本提交 输入数=%d 新增数=%d 重放数=%d 拒绝原因码=complete_costs_unavailable 异常类型=%s 耗时_ms=%.3f；保留暂停且不使用未含资金费的毛收益',
                               input_count, new_count, replay_count, type(error).__name__,
                               (time.perf_counter() - started) * 1000)
                return False

    @staticmethod
    def _ordered_completions(journal, events):
        # 使用已提交风险日志作为经济完成时间水位，重启后仍然生效。
        # 没有同毫秒经济顺序证明时拒绝不同交易的时间并列；稳定旧事件可精确重放。
        known, trades, watermark = {}, {}, -1
        for fact in journal:
            if fact['event_type'] != 'round_trip_closed':
                continue
            payload = fact['payload']
            event = {'event_id': fact['event_id'], 'trade_id': payload['trade_id'],
                     'timestamp_ms': fact['timestamp_ms'], 'net_pnl_usdt': payload['net_pnl_usdt']}
            if event['timestamp_ms'] <= watermark:
                raise ValueError('既有完整成本经济顺序不明，须人工核对')
            watermark = event['timestamp_ms']
            known[event['event_id']], trades[event['trade_id']] = event, event['event_id']
        pending, seen = [], set()
        for value in events:
            event = {key: value[key] for key in ('event_id', 'trade_id', 'timestamp_ms', 'net_pnl_usdt')}
            if event['event_id'] in seen:
                raise ValueError('成本批次重复事件身份')
            seen.add(event['event_id'])
            prior = known.get(event['event_id'])
            if prior is not None:
                if (prior['trade_id'] != event['trade_id'] or prior['timestamp_ms'] != event['timestamp_ms']
                        or Decimal(str(prior['net_pnl_usdt'])) != Decimal(str(event['net_pnl_usdt']))):
                    raise ValueError('稳定完整成本事件事实冲突')
                continue
            if event['trade_id'] in trades or event['timestamp_ms'] <= watermark:
                raise ValueError('未知晚到或同刻成本事件，不允许改写已消费经济顺序')
            pending.append(value)
        pending.sort(key=lambda event: event['timestamp_ms'])
        if len({event['timestamp_ms'] for event in pending}) != len(pending):
            raise ValueError('同刻完整交易缺少可验证经济顺序')
        return pending
