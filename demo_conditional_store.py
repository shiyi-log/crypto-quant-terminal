"""原生条件单持久化与顺序重放；本模块不访问交易所，不授予开仓资格。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import logging
import re

from data_store import _timestamp
from demo_store import DemoStore, _digest, _payload, _text

LOGGER = logging.getLogger(__name__)
CLIENT_ID = re.compile(r"qd-[a-f0-9]{32}\Z")
PAIRS = {'BTC/USDT:USDT', 'ETH/USDT:USDT', 'TRX/USDT:USDT'}
ALGO_STATUSES = {'NEW', 'TRIGGERING', 'TRIGGERED', 'FINISHED', 'REJECTED', 'EXPIRED', 'CANCELED'}
TERMINAL = {'FINISHED', 'REJECTED', 'EXPIRED', 'CANCELED'}
RAW_FIELDS = {
    'algoId', 'clientAlgoId', 'algoType', 'algoStatus', 'orderType', 'symbol',
    'side', 'positionSide', 'quantity', 'triggerPrice', 'reduceOnly', 'closePosition',
    'workingType', 'priceProtect', 'createTime', 'updateTime', 'triggerTime',
    'actualOrderId', 'actualPrice', 'actualQty', 'code',
}


def _client(value):
    if not isinstance(value, str) or not CLIENT_ID.fullmatch(value):
        raise ValueError('invalid stable conditional client identity')
    return value


def _amount(value):
    if isinstance(value, bool):
        raise ValueError('invalid conditional Decimal')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid conditional Decimal') from None
    if (not amount.is_finite() or amount <= 0 or len(str(value)) > 256
            or abs(amount.as_tuple().exponent) > 128 or amount.adjusted() > 128):
        raise ValueError('invalid conditional Decimal')
    # normalize会受Decimal上下文精度影响而舍入；只去小数尾零保持全部事实位。
    text = format(amount, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r'[0-9]+', str(value)) or int(value) <= 0):
        raise ValueError('invalid exchange algo/order identity')
    return str(value)


def _raw(value):
    if not isinstance(value, dict):
        return {}
    return _payload({key: value[key] for key in RAW_FIELDS if key in value})


class DemoConditionalStore(DemoStore):
    """意图、claim及观察同事务追加；重启只能查询，不能重发已claim身份。"""

    def _lock(self, conn):
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                     ('demo-conditional:' + ':'.join(self._identity()),))

    def _intent(self, value):
        if not isinstance(value, dict):
            raise ValueError('invalid conditional intent')
        if (value.get('environment', self.environment) != self.environment
                or value.get('endpoint_host', self.endpoint_host) != self.endpoint_host
                or value.get('virtual_account_key', self.virtual_account_key) != self.virtual_account_key):
            raise ValueError('conditional environment/account mismatch')
        fixed = {name: _text(value.get(name), name) for name in
                 ('strategy_id', 'run_id', 'entry_decision_id')}
        fixed.update(environment=self.environment, endpoint_host=self.endpoint_host,
                     virtual_account_key=self.virtual_account_key)
        fixed['client_algo_id'] = _client(value.get('client_algo_id'))
        fixed['parent_entry_client_id'] = _client(value.get('parent_entry_client_id'))
        fixed['pair'], fixed['side'] = value.get('pair'), value.get('side')
        fixed['order_type'] = value.get('order_type')
        if (fixed['pair'] not in PAIRS or fixed['side'] not in {'buy', 'sell'}
                or fixed['order_type'] not in {'STOP_MARKET', 'TAKE_PROFIT_MARKET'}
                or value.get('position_side', 'BOTH') != 'BOTH'
                or value.get('close_position', False) is not False
                or value.get('reduce_only') is not True
                or value.get('working_type') not in {'MARK_PRICE', 'CONTRACT_PRICE'}
                or not isinstance(value.get('price_protect'), bool)):
            raise ValueError('invalid conditional protection semantics')
        revision = value.get('revision')
        if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision < 2**63:
            raise ValueError('invalid conditional revision')
        fixed.update(position_side='BOTH', close_position=False, reduce_only=True,
                     algo_type='CONDITIONAL',
                     working_type=value['working_type'], price_protect=value['price_protect'],
                     quantity=_amount(value.get('quantity')),
                     trigger_price=_amount(value.get('trigger_price')), revision=revision,
                     created_ms=_timestamp(value.get('created_ms')))
        previous = value.get('supersedes_client_algo_id')
        fixed['supersedes_client_algo_id'] = None if previous is None else _client(previous)
        if previous == fixed['client_algo_id']:
            raise ValueError('conditional cannot supersede itself')
        return fixed

    def _read_intent(self, conn, client):
        return conn.execute('SELECT * FROM demo_conditional_intents WHERE environment=%s '
                            'AND virtual_account_key=%s AND client_algo_id=%s',
                            (*self._identity(), client)).fetchone()

    def _events(self, conn, client):
        return conn.execute('SELECT * FROM demo_conditional_events WHERE environment=%s '
                            'AND virtual_account_key=%s AND client_algo_id=%s ORDER BY sequence_id',
                            (*self._identity(), client)).fetchall()

    def _replay(self, fixed, events):
        row = {**fixed, 'intent_hash': _digest(fixed), 'status': 'intent',
               'submission_attempts': 0, 'submission_claimed_ms': None,
               'algo_id': None, 'algo_status': None, 'actual_order_id': None,
               'exchange_updated_ms': None, 'last_observed_ms': None,
               'last_fact': None, 'cancellation_confirmed': False,
               'cancel_pending': False, 'last_cancel_ack_ms': None,
               'last_exchange_observed_ms': None,
               'execution_complete': False}
        if not events or events[0]['event_type'] != 'reserved':
            raise RuntimeError('conditional event stream missing reservation')
        for event in events:
            payload, kind = event['payload'], event['event_type']
            expected = _digest({'client_algo_id': fixed['client_algo_id'], 'event_type': kind,
                                'observed_ms': event['observed_ms'], 'payload': payload})
            if event['payload_hash'] != expected:
                raise RuntimeError('conditional event checksum mismatch')
            if kind == 'reserved':
                if payload != {'intent_hash': row['intent_hash']}:
                    raise RuntimeError('conditional reservation checksum mismatch')
            elif kind == 'submission_claimed':
                if row['submission_attempts'] != 0:
                    raise RuntimeError('duplicate conditional claim in event stream')
                row.update(status='submitting', submission_attempts=1,
                           submission_claimed_ms=event['observed_ms'])
            elif kind in {'submission_unknown', 'lookup_absent'}:
                if row['submission_attempts'] != 1:
                    raise RuntimeError('conditional unknown without claim')
                if row['algo_id'] is None:
                    row['status'] = 'unknown'
            elif kind == 'observed':
                if self._apply_observation(row, payload):
                    row['last_exchange_observed_ms'] = event['observed_ms']
            elif kind == 'cancel_ack':
                row.update(cancel_pending=True, last_cancel_ack_ms=event['observed_ms'])
                if row['status'] != 'reconciliation_conflict':
                    row['status'] = 'cancel_pending'
            else:
                raise RuntimeError('unknown conditional event type')
            row['last_observed_ms'] = event['observed_ms']
        return row

    @staticmethod
    def _apply_observation(row, fact):
        if row['submission_attempts'] != 1:
            raise RuntimeError('conditional observation without claim')
        if row['algo_id'] is not None and row['algo_id'] != fact['algo_id']:
            row['status'] = 'reconciliation_conflict'
            return
        previous_time = row['exchange_updated_ms']
        if previous_time is not None and fact['updated_ms'] < previous_time:
            return
        previous = row['last_fact']
        if previous_time == fact['updated_ms'] and previous != fact:
            row['status'] = 'reconciliation_conflict'
            return
        if row['actual_order_id'] and fact['actual_order_id'] not in (None, row['actual_order_id']):
            row['status'] = 'reconciliation_conflict'
            return
        if ((row['algo_status'] in TERMINAL and fact['algo_status'] not in TERMINAL)
                or (row['algo_status'] in {'TRIGGERING', 'TRIGGERED'} and fact['algo_status'] == 'NEW')
                or (row['algo_status'] == 'TRIGGERED' and fact['algo_status'] == 'TRIGGERING')):
            row['status'] = 'reconciliation_conflict'
            return
        # 未知子订单不能覆盖已确认身份；FINISHED仍必须继续对账真实成交。
        row.update(algo_id=fact['algo_id'], algo_status=fact['algo_status'],
                   actual_order_id=fact['actual_order_id'] or row['actual_order_id'],
                   exchange_updated_ms=fact['updated_ms'], last_fact=fact,
                   cancellation_confirmed=fact['algo_status'] == 'CANCELED')
        if fact['algo_status'] in TERMINAL:
            row['cancel_pending'] = False
        if row['status'] != 'reconciliation_conflict':
            row['status'] = 'cancel_pending' if row['cancel_pending'] else 'confirmed'
        return True

    def _write_projection(self, conn, row):
        conn.execute('INSERT INTO demo_conditional_current '
                     '(environment,virtual_account_key,client_algo_id,status,submission_attempts,algo_id,payload) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(environment,virtual_account_key,client_algo_id) '
                     'DO UPDATE SET status=EXCLUDED.status,submission_attempts=EXCLUDED.submission_attempts,'
                     'algo_id=EXCLUDED.algo_id,payload=EXCLUDED.payload',
                     (*self._identity(), row['client_algo_id'], row['status'], row['submission_attempts'],
                      row['algo_id'], self.store._json(row)))

    def _append(self, conn, fixed, event_type, payload, observed_ms, event_id=None):
        client = fixed['client_algo_id']
        digest = _digest({'client_algo_id': client, 'event_type': event_type,
                          'observed_ms': observed_ms, 'payload': payload})
        event_id = _text(event_id or digest, 'conditional event id')
        inserted = conn.execute('INSERT INTO demo_conditional_events '
            '(environment,virtual_account_key,client_algo_id,event_id,event_type,observed_ms,payload_hash,payload) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(environment,virtual_account_key,event_id) '
            'DO NOTHING RETURNING sequence_id',
            (*self._identity(), client, event_id, event_type, observed_ms, digest,
             self.store._json(payload))).fetchone()
        if inserted is None:
            previous = conn.execute('SELECT payload_hash FROM demo_conditional_events WHERE '
                'environment=%s AND virtual_account_key=%s AND event_id=%s',
                (*self._identity(), event_id)).fetchone()
            if previous['payload_hash'] != digest:
                raise ValueError('different event facts for existing conditional event identity')
        row = self._replay(fixed, self._events(conn, client))
        self._write_projection(conn, row)
        return row

    def reserve_conditional(self, intent):
        fixed = self._intent(intent)
        client = fixed['client_algo_id']
        with self.store._connection() as conn:
            self._lock(conn)
            existing = self._read_intent(conn, client)
            if existing:
                if existing['intent_hash'] != _digest(fixed) or existing['payload'] != fixed:
                    raise ValueError('different frozen intent for conditional identity')
                return self._replay(fixed, self._events(conn, client)), False
            previous = fixed['supersedes_client_algo_id']
            if previous:
                parent = self._read_intent(conn, previous)
                keys = ('strategy_id', 'run_id', 'entry_decision_id', 'parent_entry_client_id',
                        'pair', 'side', 'order_type')
                if (not parent or any(parent['payload'][key] != fixed[key] for key in keys)
                        or parent['revision'] >= fixed['revision']):
                    raise ValueError('superseded conditional identity/round/revision mismatch')
            collision = conn.execute('SELECT client_algo_id FROM demo_conditional_intents WHERE '
                'environment=%s AND virtual_account_key=%s AND strategy_id=%s AND run_id=%s '
                'AND parent_entry_client_id=%s AND order_type=%s AND revision=%s',
                (*self._identity(), fixed['strategy_id'], fixed['run_id'], fixed['parent_entry_client_id'],
                 fixed['order_type'], fixed['revision'])).fetchone()
            if collision:
                raise ValueError('different frozen intent for conditional revision')
            conn.execute('INSERT INTO demo_conditional_intents '
                '(environment,virtual_account_key,client_algo_id,strategy_id,run_id,parent_entry_client_id,'
                'order_type,revision,quantity,trigger_price,intent_hash,payload) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (*self._identity(), client, fixed['strategy_id'], fixed['run_id'],
                 fixed['parent_entry_client_id'], fixed['order_type'], fixed['revision'],
                 Decimal(fixed['quantity']), Decimal(fixed['trigger_price']), _digest(fixed),
                 self.store._json(fixed)))
            row = self._append(conn, fixed, 'reserved', {'intent_hash': _digest(fixed)}, fixed['created_ms'])
        LOGGER.info('原生条件单意图已持久化 account=%s client=%s run=%s revision=%s quantity=%s trigger=%s',
                    self.virtual_account_key, client, fixed['run_id'], fixed['revision'],
                    fixed['quantity'], fixed['trigger_price'])
        return row, True

    def _load(self, conn, client):
        persisted = self._read_intent(conn, client)
        if persisted is None:
            return None, None
        fixed = persisted['payload']
        if _digest(fixed) != persisted['intent_hash']:
            raise RuntimeError('conditional intent checksum mismatch')
        return fixed, self._replay(fixed, self._events(conn, client))

    def get_conditional(self, client_algo_id):
        with self.store._connection() as conn:
            _, row = self._load(conn, _client(client_algo_id))
            return row

    def list_conditionals(self):
        with self.store._connection() as conn:
            clients = conn.execute('SELECT client_algo_id FROM demo_conditional_intents '
                'WHERE environment=%s AND virtual_account_key=%s ORDER BY client_algo_id',
                self._identity()).fetchall()
            return [self._load(conn, item['client_algo_id'])[1] for item in clients]

    def claim_conditional_submission(self, client_algo_id, claimed_ms):
        client, stamp = _client(client_algo_id), _timestamp(claimed_ms)
        with self.store._connection() as conn:
            self._lock(conn)
            fixed, row = self._load(conn, client)
            if row is None or row['submission_attempts'] or row['status'] != 'intent':
                return False
            if stamp < fixed['created_ms']:
                raise ValueError('claim predates conditional intent')
            self._append(conn, fixed, 'submission_claimed', {}, stamp)
        LOGGER.info('原生条件单单次提交已领取 account=%s client=%s claimed_ms=%s；重启后仅可查询',
                    self.virtual_account_key, client, stamp)
        return True

    def mark_conditional_unknown(self, client_algo_id, error_type, observed_ms):
        client, stamp = _client(client_algo_id), _timestamp(observed_ms)
        # 仅接受错误类型名，拒绝可能含签名URL或敏感文本的原始异常。
        if not isinstance(error_type, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}', error_type):
            raise ValueError('conditional error must be a safe type name')
        with self.store._connection() as conn:
            self._lock(conn)
            fixed, row = self._load(conn, client)
            if row is None:
                raise ValueError('unknown conditional identity')
            if row['submission_attempts'] != 1:
                raise ValueError('conditional unknown requires prior claim')
            row = self._append(conn, fixed, 'submission_unknown', {'error_type': error_type}, stamp)
        LOGGER.warning('原生条件单提交结果未知 account=%s client=%s error_type=%s；禁止再次发送',
                       self.virtual_account_key, client, error_type)
        return row

    def _observation(self, fixed, value):
        if not isinstance(value, dict):
            raise ValueError('invalid conditional observation')
        fields = ('client_algo_id', 'pair', 'side', 'order_type', 'position_side', 'algo_type',
                  'working_type', 'price_protect', 'reduce_only', 'close_position')
        for key in fields:
            if (key not in value or type(value[key]) is not type(fixed[key])
                    or value[key] != fixed[key]):
                raise ValueError('conditional observation intent mismatch: ' + key)
        result = {key: value[key] for key in fields}
        for key in ('quantity', 'trigger_price'):
            result[key] = _amount(value.get(key))
            if result[key] != fixed[key]:
                raise ValueError('conditional observation amount mismatch')
        status = value.get('algo_status')
        if status not in ALGO_STATUSES:
            raise ValueError('invalid raw conditional status')
        actual = value.get('actual_order_id')
        result.update(algo_id=_identifier(value.get('algo_id')), algo_status=status,
                      actual_order_id=None if actual is None else _identifier(actual),
                      created_ms=_timestamp(value.get('created_ms')),
                      updated_ms=_timestamp(value.get('updated_ms')),
                      raw=_raw(value.get('raw')))
        return result

    def record_conditional_observation(self, client_algo_id, observation, observed_ms):
        client, stamp = _client(client_algo_id), _timestamp(observed_ms)
        with self.store._connection() as conn:
            self._lock(conn)
            fixed, row = self._load(conn, client)
            if row is None:
                raise ValueError('unknown conditional identity')
            if row['submission_attempts'] != 1:
                raise ValueError('conditional observation requires prior claim')
            if stamp < row['submission_claimed_ms']:
                raise ValueError('conditional observation time predates claim')
            if observation is None:
                kind, payload, event_id = 'lookup_absent', {}, None
            else:
                payload = self._observation(fixed, observation)
                if not fixed['created_ms'] <= payload['created_ms'] <= payload['updated_ms'] <= stamp:
                    raise ValueError('conditional exchange time is inconsistent with observation time')
                event_id = observation.get('event_id')
                kind = 'observed'
                if observation.get('cancel_acknowledged') is True:
                    if observation.get('cancellation_confirmed') is not False:
                        raise ValueError('cancel ACK cannot certify cancellation')
                    receipt = observation.get('cancel_raw')
                    if (not isinstance(receipt, dict)
                            or receipt.get('clientAlgoId') != client
                            or _identifier(receipt.get('algoId')) != payload['algo_id']
                            or not ((type(receipt.get('code')) is int and receipt['code'] == 200)
                                    or (type(receipt.get('code')) is str and receipt['code'] == '200'))):
                        raise ValueError('invalid cancel receipt identity or success code')
                    kind = 'cancel_ack'
                    payload['cancel_raw'] = _raw(receipt)
            row = self._append(conn, fixed, kind, payload, stamp, event_id=event_id)
        LOGGER.info('原生条件单事实已追加 account=%s client=%s kind=%s algo_status=%s '
                    'actual_order=%s observed_ms=%s；不代表成交或空仓',
                    self.virtual_account_key, client, kind, row['algo_status'],
                    row['actual_order_id'], stamp)
        return row

    def conditional_events(self, client_algo_id):
        with self.store._connection() as conn:
            return self._events(conn, _client(client_algo_id))

    def rebuild_conditional_projection(self, client_algo_id):
        with self.store._connection() as conn:
            self._lock(conn)
            _, row = self._load(conn, _client(client_algo_id))
            if row is None:
                raise ValueError('unknown conditional identity')
            self._write_projection(conn, row)
            return row
