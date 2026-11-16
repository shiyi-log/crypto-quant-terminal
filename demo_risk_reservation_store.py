"""账户风险预占事务；输入由可信服务归档，模块本身不访问交易所或授权交易。"""
from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib
import json
import logging
import re

from data_store import _serializable, _timestamp
from demo_entry_decision import propose_entry_decision, _decimal, _string, _text
from demo_store import DemoStore

LOGGER = logging.getLogger(__name__)
CLIENT = re.compile(r'qd-[a-f0-9]{32}\Z')
CONTEXT_FLAGS = ('account_reconciled', 'exposure_complete', 'margin_evidence_complete',
                 'risk_entries_enabled', 'funding_complete', 'costs_complete', 'native_protection_verified')
CONTEXT_FIELDS = ('stage_id', 'stage_initial_usdt', 'net_equity_usdt', 'account_at_ms',
                  'max_account_age_ms', 'account_snapshot_ref', 'account_snapshot_hash', 'exposure_watermark')


def _json_default(value):
    if isinstance(value, Decimal):
        return _amount(value)
    raise TypeError('unsupported immutable risk input type')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
                      default=_json_default)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _id(value):
    if _text(value) is None:
        raise ValueError('invalid safe risk identity')
    return value


def _amount(value, *, positive=False):
    result = _decimal(value, positive=positive)
    if result is None:
        raise ValueError('invalid exact risk amount')
    with localcontext() as ctx:
        ctx.prec = 1024
        return _string(result)


class DemoRiskReservationStore(DemoStore):
    """跨namespace使用相同账户锁，Decision/预占/订单一次事务提交。"""

    def __init__(self, data_store, account_scope, virtual_account_key, **kwargs):
        super().__init__(data_store, virtual_account_key, **kwargs)
        self.account_scope = _id(account_scope)

    def _account_identity(self):
        return self.environment, self.account_scope

    def _lock_account(self, conn):
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                     ('demo-risk-reserve:' + ':'.join(self._account_identity()),))

    def _latest_context(self, conn):
        row = conn.execute('SELECT * FROM demo_risk_contexts WHERE environment=%s AND account_scope=%s '
                           'ORDER BY sequence_id DESC LIMIT 1', self._account_identity()).fetchone()
        if row and _hash(row['payload']) != row['context_hash']:
            raise RuntimeError('risk context integrity mismatch')
        return row

    def install_context(self, context):
        """可信归档层调用；不接受隐式外部风险归零或阶段本金上调。"""
        if not isinstance(context, dict):
            raise ValueError('invalid risk context')
        value = json.loads(_canonical(context))
        if value.get('environment') != self.environment or value.get('account_id') != self.account_scope:
            raise ValueError('risk context identity mismatch')
        for key in ('context_id', 'stage_id', 'account_snapshot_ref', 'exposure_watermark'):
            _id(value.get(key))
        if _text(value.get('account_snapshot_hash'), hashed=True) is None:
            raise ValueError('risk context snapshot hash missing')
        for key in ('sequence', 'account_at_ms', 'max_account_age_ms'):
            if type(value.get(key)) is not int or value[key] < (1 if key == 'sequence' else 0):
                raise ValueError('invalid context sequence/time')
        for key in ('stage_initial_usdt', 'net_equity_usdt', 'external_risk_usdt', 'external_margin_usdt'):
            value[key] = _amount(value.get(key), positive=key in ('stage_initial_usdt', 'net_equity_usdt'))
        if (value.get('exposure_scope') != 'external_only_excludes_local_reservations'
                or not isinstance(value.get('external_symbols'), list)
                or any(pair not in ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'TRX/USDT:USDT')
                       for pair in value['external_symbols'])
                or any(type(value.get(key)) is not bool for key in CONTEXT_FLAGS)
                or not isinstance(value.get('approvals'), dict)):
            raise ValueError('incomplete explicit context evidence')
        value['external_symbols'] = sorted(set(value['external_symbols']))
        for strategy, approval in value['approvals'].items():
            _id(strategy)
            if not isinstance(approval, dict) or type(approval.get('approved')) is not bool:
                raise ValueError('invalid context approval')
            _id(approval.get('run_id'))
            for key in ('strategy_hash', 'risk_policy_hash', 'cost_model_hash'):
                if _text(approval.get(key), hashed=True) is None:
                    raise ValueError('invalid context approval hash')
        with self.store._connection() as conn:
            self._lock_account(conn)
            existing = conn.execute('SELECT * FROM demo_risk_contexts WHERE environment=%s AND account_scope=%s '
                                    'AND context_id=%s', (*self._account_identity(), value['context_id'])).fetchone()
            if existing:
                if existing['context_hash'] != _hash(value):
                    raise ValueError('immutable risk context conflict')
                return existing['payload']
            latest = self._latest_context(conn)
            if latest:
                old = latest['payload']
                if (value['sequence'] != latest['sequence_id'] + 1
                        or value['account_at_ms'] < old['account_at_ms']
                        or value['stage_id'] != old['stage_id']
                        or value['stage_initial_usdt'] != old['stage_initial_usdt']
                        or Decimal(value['external_risk_usdt']) < Decimal(old['external_risk_usdt'])
                        or Decimal(value['external_margin_usdt']) < Decimal(old['external_margin_usdt'])
                        or not set(old['external_symbols']) <= set(value['external_symbols'])):
                    raise ValueError('context cannot silently drop external exposure or change capital stage')
            elif value['sequence'] != 1:
                raise ValueError('first context sequence must be one')
            conn.execute('INSERT INTO demo_risk_contexts(environment,account_scope,context_id,sequence_id,context_hash,payload) '
                         'VALUES(%s,%s,%s,%s,%s,%s)', (*self._account_identity(), value['context_id'],
                         value['sequence'], _hash(value), self.store._json(value)))
        LOGGER.info('账户风险上下文已冻结 账户=%s 上下文=%s 序号=%s 净权益=%s 外部风险=%s 外部保证金=%s',
                    self.account_scope, value['context_id'], value['sequence'], value['net_equity_usdt'],
                    value['external_risk_usdt'], value['external_margin_usdt'])
        return value

    def _event(self, conn, decision_id, kind, timestamp, payload):
        event = {'type': kind, 'observed_ms': timestamp, 'payload': payload}
        conn.execute('INSERT INTO demo_risk_reservation_events(environment,account_scope,decision_id,event_id,event_type,'
                     'observed_ms,payload_hash,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (*self._account_identity(), decision_id, _hash([decision_id, event]), kind, timestamp,
                      _hash(event), self.store._json(event)))

    def _reservations(self, conn):
        decisions = conn.execute('SELECT * FROM demo_entry_decisions WHERE environment=%s AND account_scope=%s AND accepted '
                                 'ORDER BY decision_id', self._account_identity()).fetchall()
        result = []
        for record in decisions:
            decision = record['payload']
            if _hash(decision) != record['decision_hash']:
                raise RuntimeError('risk decision integrity mismatch')
            events = conn.execute('SELECT * FROM demo_risk_reservation_events WHERE environment=%s AND account_scope=%s '
                                  'AND decision_id=%s ORDER BY sequence_id',
                                  (*self._account_identity(), record['decision_id'])).fetchall()
            state = None
            for row in events:
                event = row['payload']
                if (_hash(event) != row['payload_hash'] or event['type'] != row['event_type']
                        or event['observed_ms'] != row['observed_ms']):
                    raise RuntimeError('risk reservation event integrity mismatch')
                kind = row['event_type']
                if kind == 'reserved' and state is None:
                    state = 'reserved'
                elif kind == 'claimed' and state == 'reserved':
                    state = 'claimed'
                elif kind == 'unknown' and state in ('claimed', 'unknown'):
                    state = 'unknown'
                elif kind == 'released' and state in ('reserved', 'claimed', 'unknown'):
                    state = 'released'
                else:
                    raise RuntimeError('invalid risk reservation event sequence')
            if state is None:
                raise RuntimeError('risk reservation missing immutable event')
            if state == 'released' and events[-1]['payload']['payload'].get('released_client_hash') != self._client_fact_hash(
                    conn, record['strategy_namespace'], record['client_id']):
                raise RuntimeError('released reservation local facts changed; reconcile before new risk')
            result.append({'decision_id': record['decision_id'], 'client_id': record['client_id'],
                           'strategy_namespace': record['strategy_namespace'], 'status': state,
                           'pair': decision['pair'], 'loss_usdt': decision['planned_loss_usdt'],
                           'margin_usdt': decision['planned_margin_usdt'], 'decision': decision,
                           'last_event_ms': events[-1]['observed_ms']})
        return result

    def _unresolved_submissions(self, conn, reservations):
        for current in reservations:
            if current['status'] == 'unknown':
                return True
            row = conn.execute('SELECT status FROM demo_orders WHERE environment=%s '
                               'AND virtual_account_key=%s AND client_id=%s',
                               (self.environment, current['strategy_namespace'], current['client_id'])).fetchone()
            if row is None or row['status'] in ('submitting', 'unknown'):
                return True
        return False

    def _exposure(self, conn, context):
        with localcontext() as ctx:
            ctx.prec = 1024
            active = [row for row in self._reservations(conn) if row['status'] != 'released']
            risk = Decimal(context['external_risk_usdt']) + sum((Decimal(row['loss_usdt']) for row in active), Decimal(0))
            margin = Decimal(context['external_margin_usdt']) + sum((Decimal(row['margin_usdt']) for row in active), Decimal(0))
            symbols = sorted(set(context['external_symbols']) | {row['pair'] for row in active})
            return {'risk_usdt': _string(risk), 'margin_usdt': _string(margin), 'symbols': symbols,
                    'reservations': active}

    def exposure(self):
        with self.store._connection() as conn:
            self._lock_account(conn)
            latest = self._latest_context(conn)
            if latest is None:
                raise ValueError('risk context is required')
            return self._exposure(conn, latest['payload'])

    def decisions(self):
        with self.store._connection() as conn:
            return [row['payload'] for row in conn.execute('SELECT payload FROM demo_entry_decisions '
                    'WHERE environment=%s AND account_scope=%s ORDER BY decision_id', self._account_identity()).fetchall()]

    def _stored_result(self, conn, record):
        decision = record['payload']
        reservation = next((row for row in self._reservations(conn) if row['decision_id'] == record['decision_id']), None)
        order = conn.execute('SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s AND client_id=%s',
                             (self.environment, record['strategy_namespace'], record['client_id'])).fetchone() if record['accepted'] else None
        return {'accepted': record['accepted'], 'decision': decision, 'reservation': reservation, 'order': _serializable(order)}

    def reserve_entry(self, source, intent, *, context_id):
        """同一事务内重算当前占用，拒绝也保存不可变裁定，不把调用方余额当权威。"""
        source, intent = json.loads(_canonical(source)), json.loads(_canonical(intent))
        decision_id, client = _id(source.get('decision_id')), intent.get('client_id')
        if not isinstance(client, str) or not CLIENT.fullmatch(client):
            raise ValueError('stable entry client identity required')
        request = {'source': source, 'intent': intent, 'context_id': _id(context_id), 'namespace': self.virtual_account_key}
        digest = _hash(request)
        with self.store._connection() as conn:
            self._lock_account(conn)
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                         ('demo-risk-namespace:' + self.environment + ':' + self.virtual_account_key,))
            namespace = conn.execute('SELECT account_scope FROM demo_entry_decisions WHERE environment=%s '
                                     'AND strategy_namespace=%s LIMIT 1', self._identity()).fetchone()
            if namespace and namespace['account_scope'] != self.account_scope:
                raise ValueError('strategy namespace belongs to another account')
            old = conn.execute('SELECT * FROM demo_entry_decisions WHERE environment=%s AND account_scope=%s AND decision_id=%s',
                               (*self._account_identity(), decision_id)).fetchone()
            if old:
                if old['request_hash'] != digest:
                    raise ValueError('immutable entry decision conflict')
                return self._stored_result(conn, old)
            collision = conn.execute('SELECT 1 FROM demo_entry_decisions WHERE environment=%s AND account_scope=%s '
                                     'AND client_id=%s', (*self._account_identity(), client)).fetchone()
            if collision:
                raise ValueError('immutable account client identity conflict')
            latest = self._latest_context(conn)
            if latest is None or latest['context_id'] != context_id:
                raise ValueError('latest account risk context is required')
            context = latest['payload']
            used = self._exposure(conn, context)
            effective = dict(source)
            for key in CONTEXT_FIELDS + CONTEXT_FLAGS:
                effective[key] = context[key]
            effective.update(account_id=self.account_scope, environment=self.environment,
                             used_risk_usdt=used['risk_usdt'], used_margin_usdt=used['margin_usdt'], active_symbols=used['symbols'])
            approval = context['approvals'].get(source.get('strategy_version_id'), {})
            effective['strategy_approved'] = (approval.get('approved') is True and approval.get('run_id') == source.get('run_id')
                and all(approval.get(key) == source.get(key) for key in ('strategy_hash', 'risk_policy_hash', 'cost_model_hash')))
            decision = propose_entry_decision(effective).to_dict()
            mismatch = (intent.get('strategy_id') != source.get('strategy_version_id') or intent.get('signal_id') != source.get('signal_id')
                or intent.get('pair') != decision['pair'] or intent.get('side') != decision['side']
                or intent.get('rule_hash') != source.get('strategy_hash') or intent.get('execution_hash') != source.get('exit_params_hash')
                or intent.get('input_fingerprint') != source.get('input_fingerprint') or intent.get('reduce_only') is not False
                or intent.get('type', 'limit') != 'limit' or intent.get('order_type', 'limit') != 'limit'
                or intent.get('time_in_force') != 'GTX'
                or intent.get('signal_ms') != source.get('signal_at_ms'))
            try:
                mismatch = mismatch or _amount(intent.get('quantity'), positive=True) != decision['quantity'] or _amount(intent.get('price'), positive=True) != decision['entry_price']
            except ValueError:
                mismatch = True
            if mismatch:
                decision['reason_codes'].append('order_intent_mismatch')
                decision['verdict'] = 'rejected'
            if self._unresolved_submissions(conn, used['reservations']):
                decision['reason_codes'].append('unresolved_account_submission')
                decision['verdict'] = 'rejected'
            if any(row['pair'] == decision['pair'] for row in used['reservations']):
                decision['reason_codes'].append('account_pair_ownership_conflict')
                decision['verdict'] = 'rejected'
            accepted = decision['verdict'] == 'eligible_proposal'
            decision.update(reservation_committed=accepted, acceptance_eligible=False)
            conn.execute('INSERT INTO demo_entry_decisions(environment,account_scope,decision_id,strategy_namespace,client_id,context_id,'
                         'request_hash,decision_hash,accepted,payload,request_payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (*self._account_identity(), decision_id, self.virtual_account_key, client, context_id, digest, _hash(decision), accepted,
                          self.store._json(decision), self.store._json(request)))
            if accepted:
                reservation = {'status': 'reserved', 'decision_id': decision_id, 'client_id': client,
                               'loss_usdt': decision['planned_loss_usdt'], 'margin_usdt': decision['planned_margin_usdt']}
                conn.execute('INSERT INTO demo_risk_reservations(environment,account_scope,decision_id,strategy_namespace,client_id,status,loss_usdt,'
                             'margin_usdt,pair,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                             (*self._account_identity(), decision_id, self.virtual_account_key, client, 'reserved', reservation['loss_usdt'],
                              reservation['margin_usdt'], decision['pair'], self.store._json(reservation)))
                self._event(conn, decision_id, 'reserved', decision['decision_at_ms'], reservation)
                row, created = self._reserve_order_on_connection(conn, intent)
                if (not created or Decimal(row['quantity']) != Decimal(decision['quantity'])
                        or Decimal(row['price']) != Decimal(decision['entry_price'])):
                    raise ValueError('order must be newly reserved and match exact decision amount')
                conn.execute('UPDATE demo_orders SET risk_reservation_account_scope=%s,risk_reservation_decision_id=%s '
                             'WHERE environment=%s AND virtual_account_key=%s AND client_id=%s',
                             (self.account_scope, decision_id, *self._identity(), client))
            record = conn.execute('SELECT * FROM demo_entry_decisions WHERE environment=%s AND account_scope=%s AND decision_id=%s',
                                  (*self._account_identity(), decision_id)).fetchone()
            result = self._stored_result(conn, record)
        LOGGER.info('账户入场裁定已持久化 账户=%s 策略=%s 决策=%s 客户端=%s 接受=%s 计划风险=%s 保证金=%s 原因=%s',
                    self.account_scope, self.virtual_account_key, decision_id, client, accepted,
                    decision['planned_loss_usdt'], decision['planned_margin_usdt'], ','.join(decision['reason_codes']))
        return result

    def claim_reserved_entry(self, client_id, *, now_ms, context_id):
        now = _timestamp(now_ms)
        with self.store._connection() as conn:
            self._lock_account(conn)
            latest = self._latest_context(conn)
            if latest is None or latest['context_id'] != context_id:
                return False
            context = latest['payload']
            current = next((row for row in self._reservations(conn) if row['client_id'] == client_id
                            and row['strategy_namespace'] == self.virtual_account_key), None)
            if current is None or current['status'] != 'reserved':
                return False
            decision = current['decision']
            approval = context['approvals'].get(decision['strategy_version_id'], {})
            if (any(context[key] is not True for key in CONTEXT_FLAGS) or approval.get('approved') is not True
                    or approval.get('run_id') != decision['run_id']
                    or any(approval.get(key) != decision[key] for key in ('strategy_hash','risk_policy_hash','cost_model_hash'))
                    or now < decision['decision_at_ms']
                    or any(now < stamp or now - stamp > maximum for stamp, maximum in (
                        (context['account_at_ms'], context['max_account_age_ms']),
                        (decision['signal_at_ms'], decision['max_signal_age_ms']),
                        (decision['quote_at_ms'], decision['max_quote_age_ms'])))):
                return False
            used = self._exposure(conn, context)
            if self._unresolved_submissions(conn, used['reservations']):
                return False
            with localcontext() as ctx:
                ctx.prec = 1024
                base = min(Decimal(context['stage_initial_usdt']), Decimal(context['net_equity_usdt']))
                if (Decimal(current['loss_usdt']) > base * Decimal('.01') or len(used['symbols']) > 2
                        or Decimal(used['risk_usdt']) > base * Decimal('.02')
                        or Decimal(used['margin_usdt']) > base * Decimal('.5')):
                    return False
            order = self._order(conn, client_id, lock=True)
            if (order is None or order['status'] != 'intent' or order['submission_attempts'] != 0
                    or order['risk_reservation_decision_id'] != current['decision_id']
                    or now >= int(order['intent_payload'].get('expires_ms', decision['decision_at_ms']))):
                return False
            self._event(conn, current['decision_id'], 'claimed', now, {'client_id': client_id, 'context_id': context_id})
            conn.execute("UPDATE demo_risk_reservations SET status='claimed' WHERE environment=%s AND account_scope=%s AND decision_id=%s",
                         (*self._account_identity(), current['decision_id']))
            conn.execute("UPDATE demo_orders SET status='submitting',submission_attempts=1,submission_claimed_ms=%s WHERE environment=%s "
                         'AND virtual_account_key=%s AND client_id=%s', (now, *self._identity(), client_id))
        LOGGER.info('账户预算提交身份已单次领取 账户=%s 决策=%s 客户端=%s；崩溃后仅查询',self.account_scope,current['decision_id'],client_id)
        return True

    def mark_reserved_unknown(self, client_id, error_type, *, observed_ms):
        now = _timestamp(observed_ms)
        if not isinstance(error_type, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}', error_type):
            raise ValueError('safe error type required')
        with self.store._connection() as conn:
            self._lock_account(conn)
            current = next((row for row in self._reservations(conn) if row['client_id'] == client_id
                            and row['strategy_namespace'] == self.virtual_account_key), None)
            if current is None or current['status'] not in ('claimed', 'unknown') or now < current['last_event_ms']:
                raise ValueError('unknown requires claimed risk reservation')
            self._event(conn,current['decision_id'],'unknown',now,{'client_id':client_id,'error_type':error_type})
            conn.execute("UPDATE demo_risk_reservations SET status='unknown' WHERE environment=%s AND account_scope=%s AND decision_id=%s",
                         (*self._account_identity(),current['decision_id']))
        LOGGER.warning('提交结果未知保留全部风险预占 账户=%s 客户端=%s 错误类型=%s',self.account_scope,client_id,error_type)

    def _client_fact_hash(self, conn, namespace, client_id):
        order = conn.execute('SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s '
                             'AND client_id=%s', (self.environment, namespace, client_id)).fetchone()
        fills = conn.execute('SELECT * FROM demo_fills WHERE environment=%s AND virtual_account_key=%s '
                             'AND client_id=%s ORDER BY pair,exchange_order_id,exchange_trade_id',
                             (self.environment, namespace, client_id)).fetchall()
        conditionals = conn.execute('SELECT e.* FROM demo_conditional_events e JOIN demo_conditional_intents i '
                                   'USING(environment,virtual_account_key,client_algo_id) WHERE e.environment=%s '
                                   'AND e.virtual_account_key=%s AND i.parent_entry_client_id=%s ORDER BY e.sequence_id',
                                   (self.environment, namespace, client_id)).fetchall()
        return _hash(_serializable({'order': order, 'fills': fills, 'conditionals': conditionals}))

    def _local_fact_hashes(self, conn):
        """当前账户全部已绑定namespace的普通/条件事实身份；不把哈希当交易所证明。"""
        namespaces = [row['strategy_namespace'] for row in conn.execute(
            'SELECT DISTINCT strategy_namespace FROM demo_entry_decisions WHERE environment=%s AND account_scope=%s',
            self._account_identity()).fetchall()]
        orders = conn.execute('SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=ANY(%s) '
                              'ORDER BY virtual_account_key,client_id', (self.environment, namespaces)).fetchall()
        fills = conn.execute('SELECT * FROM demo_fills WHERE environment=%s AND virtual_account_key=ANY(%s) '
                             'ORDER BY virtual_account_key,pair,exchange_order_id,exchange_trade_id',
                             (self.environment, namespaces)).fetchall()
        conditionals = conn.execute('SELECT * FROM demo_conditional_events WHERE environment=%s '
                                   'AND virtual_account_key=ANY(%s) ORDER BY sequence_id',
                                   (self.environment, namespaces)).fetchall()
        return {kind: _hash(_serializable(rows)) for kind, rows in
                (('orders', orders), ('fills', fills), ('conditional_orders', conditionals))}

    def local_fact_hashes(self):
        """归档采集器冻结内部事实水位；随后变化使该事实包失效。"""
        with self.store._connection() as conn:
            self._lock_account(conn)
            return self._local_fact_hashes(conn)

    def record_flat_reconciliation(self, fact):
        """可信归档服务提交完整事实包；原文复制到不可变表，普通documents不是权威。"""
        if not isinstance(fact, dict) or 'archived_sources' in fact:
            raise ValueError('structured archived reconciliation required')
        value = json.loads(_canonical(fact))
        for key in ('fact_id', 'stage_id', 'exposure_watermark', 'client_id'):
            _id(value.get(key))
        if (value.get('environment') != self.environment or value.get('account_id') != self.account_scope
                or not isinstance(value.get('source_refs'), dict)
                or set(value['source_refs']) != {'positions', 'orders', 'conditional_orders', 'fills', 'cashflows'}):
            raise ValueError('complete archived fact references required')
        now = _timestamp(value.get('observed_ms'))
        with self.store._connection() as conn:
            self._lock_account(conn)
            old = conn.execute('SELECT payload_hash,payload FROM demo_risk_flat_reconciliations '
                               'WHERE environment=%s AND account_scope=%s AND fact_id=%s',
                               (*self._account_identity(), value['fact_id'])).fetchone()
            if old:
                if (_hash(old['payload']) != old['payload_hash']
                        or {key: val for key, val in old['payload'].items() if key != 'archived_sources'} != value):
                    raise ValueError('immutable reconciliation conflict')
                return value['fact_id']
            latest = self._latest_context(conn)
            if latest is None or value['stage_id'] != latest['payload']['stage_id']:
                raise ValueError('flat fact stage mismatch')
            hashes = self._local_fact_hashes(conn)
            if value.get('local_ledger_hashes') != hashes:
                raise ValueError('flat fact does not cover current local facts')
            archived = {}
            for kind, ref in value['source_refs'].items():
                if not isinstance(ref, dict) or _text(ref.get('sha256'), hashed=True) is None:
                    raise ValueError('archived fact hash required')
                source = conn.execute('SELECT payload FROM documents WHERE key=%s', (_id(ref.get('key')),)).fetchone()
                payload = source['payload'] if source else None
                if (not isinstance(payload, dict) or _hash(payload) != ref['sha256'] or payload.get('kind') != kind
                        or payload.get('account_id') != self.account_scope or payload.get('stage_id') != value['stage_id']
                        or payload.get('environment') != self.environment or payload.get('observed_ms') != now
                        or payload.get('watermark') != value['exposure_watermark'] or payload.get('complete') is not True
                        or not isinstance(payload.get('items'), list)
                        or (kind in hashes and payload.get('local_ledger_hash') != hashes[kind])):
                    raise ValueError('archived reconciliation identity or hash mismatch')
                if kind in ('positions', 'orders', 'conditional_orders') and payload['items']:
                    raise ValueError('residual exposure cannot release reservation')
                archived[kind] = payload
            value['archived_sources'] = archived
            conn.execute('INSERT INTO demo_risk_flat_reconciliations(environment,account_scope,fact_id,payload_hash,payload) '
                         'VALUES(%s,%s,%s,%s,%s)', (*self._account_identity(), value['fact_id'],
                         _hash(value), self.store._json(value)))
        return value['fact_id']

    def release_with_reconciliation(self, client_id, fact_id):
        with self.store._connection() as conn:
            self._lock_account(conn)
            # 旧普通/条件写者尚未统一账户锁。稀少空仓清理暂冻结事实DML；
            # NOWAIT避免events→intents外键与反向锁序互等，冲突整笔回滚保留额度。
            conn.execute('LOCK TABLE demo_orders,demo_fills,demo_conditional_intents,'
                         'demo_conditional_events IN EXCLUSIVE MODE NOWAIT')
            current = next((row for row in self._reservations(conn) if row['client_id'] == client_id
                            and row['strategy_namespace'] == self.virtual_account_key), None)
            fact = conn.execute('SELECT * FROM demo_risk_flat_reconciliations WHERE environment=%s '
                                'AND account_scope=%s AND fact_id=%s',
                                (*self._account_identity(), _id(fact_id))).fetchone()
            if current is None or fact is None:
                raise ValueError('archived flat reconciliation required')
            value = fact['payload']
            if (_hash(value) != fact['payload_hash'] or value['client_id'] != client_id
                    or value['stage_id'] != current['decision']['stage_id']
                    or value['observed_ms'] < current['last_event_ms']):
                raise ValueError('flat reconciliation does not cover reservation')
            if current['status'] == 'released':
                return False
            if value['local_ledger_hashes'] != self._local_fact_hashes(conn):
                raise ValueError('local facts changed after flat reconciliation')
            order = self._order(conn, client_id, lock=True)
            if (order is None or order['status'] not in ('intent', 'filled', 'canceled', 'expired', 'rejected')
                    or order['exchange_updated_ms'] > value['observed_ms']):
                raise ValueError('unknown or pending ordinary order cannot release')
            fill_time = conn.execute('SELECT MAX(GREATEST(timestamp_ms,exchange_updated_ms)) AS last_ms '
                                     'FROM demo_fills WHERE environment=%s AND virtual_account_key=%s AND client_id=%s',
                                     (*self._identity(), client_id)).fetchone()['last_ms']
            if fill_time is not None and fill_time > value['observed_ms']:
                raise ValueError('flat reconciliation does not cover fills')
            if order['status'] == 'intent':
                conn.execute("UPDATE demo_orders SET status='expired' WHERE environment=%s "
                             'AND virtual_account_key=%s AND client_id=%s', (*self._identity(), client_id))
            self._event(conn, current['decision_id'], 'released', value['observed_ms'],
                        {'fact_id': fact_id, 'client_id': client_id,
                         'released_client_hash': self._client_fact_hash(conn, self.virtual_account_key, client_id)})
            conn.execute("UPDATE demo_risk_reservations SET status='released' WHERE environment=%s "
                         'AND account_scope=%s AND decision_id=%s', (*self._account_identity(), current['decision_id']))
        LOGGER.info('完整归档空仓事实确认后释放风险预占 账户=%s 客户端=%s 事实=%s',
                    self.account_scope, client_id, fact_id)
        return True
