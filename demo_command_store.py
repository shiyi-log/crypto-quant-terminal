"""被动 Demo 命令账本；保存请求和观察，不执行命令或认证交易所事实。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
import re

from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_store import DemoStore

LOGGER = logging.getLogger(__name__)
SCOPE = ('environment', 'account_id', 'stage_id', 'strategy_version_id', 'run_id')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
SHA = re.compile(r'[a-f0-9]{64}')
OWNER = re.compile(r'writer-[a-f0-9]{32}')
REF = re.compile(r'evidence:[a-f0-9]{64}')
ZERO = '0' * 64
REASONS = {
    'pause_entries': frozenset({'operator_pause', 'safety_pause'}),
    'reconcile_owned': frozenset({'operator_reconcile', 'restart_reconciliation'}),
    'unknown': frozenset({'outcome_unknown', 'owner_restarted'}),
    'applied': frozenset({'control_applied'}),
    'rejected': frozenset({'binding_unavailable', 'command_refused', 'approval_unavailable'}),
    'observed': frozenset({'recovery_observed', 'control_observed'}),
}
PUBLIC_EVENTS = frozenset({'unknown', 'applied', 'rejected', 'observed'})
MAX_EVENTS = 100_000


def _id(value):
    if (not isinstance(value, str) or not ID.fullmatch(value) or '..' in value
            or value.lower() in {'secret', 'password', 'apikey', 'token', 'authorization'}):
        raise ValueError('invalid bounded command identity')
    return value


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('invalid command timestamp')
    return value


def _owner(value):
    if not isinstance(value, str) or not OWNER.fullmatch(value):
        raise ValueError('writer incarnation required; PID and lease are not ownership')
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _refs(values, *, required=False):
    if not isinstance(values, list) or len(values) > 16 or (required and not values):
        raise ValueError('bounded committed evidence references required')
    checked, seen = [], set()
    for value in values:
        if (not isinstance(value, dict) or set(value) != {'ref', 'record_sha256'}
                or not isinstance(value['ref'], str) or not REF.fullmatch(value['ref'])
                or not isinstance(value['record_sha256'], str) or not SHA.fullmatch(value['record_sha256'])
                or value['ref'] in seen):
            raise ValueError('invalid committed evidence reference')
        seen.add(value['ref'])
        checked.append(dict(value))
    return checked


@dataclass(frozen=True, init=False)
class FrozenCommandBinding:
    """内部身份容器；类型与摘要不认证来源，公开值仅为副本。"""
    binding_id: str
    namespace: str
    _scope: tuple
    _hashes: tuple

    def __init__(self, binding_id, scope, identity_hashes, namespace):
        if (not isinstance(scope, dict) or set(scope) != set(SCOPE)
                or scope.get('environment') != 'binance_demo'
                or not isinstance(identity_hashes, dict) or set(identity_hashes) != set(HASHES)):
            raise ValueError('complete internal command binding required')
        scopes = tuple((key, _id(scope[key])) for key in SCOPE)
        if any(not isinstance(value, str) or not SHA.fullmatch(value) for value in identity_hashes.values()):
            raise ValueError('five frozen command hashes required')
        object.__setattr__(self, 'binding_id', _id(binding_id))
        object.__setattr__(self, 'namespace', _id(namespace))
        object.__setattr__(self, '_scope', scopes)
        object.__setattr__(self, '_hashes', tuple((key, identity_hashes[key]) for key in HASHES))

    def to_dict(self):
        return {'schema': 'demo-command-binding-v1', 'binding_id': self.binding_id,
                'scope': dict(self._scope), 'identity_hashes': dict(self._hashes), 'namespace': self.namespace}

    @property
    def sha256(self):
        return _hash(self.to_dict())


class DemoCommandStore(DemoStore):
    """只有数据库事务；claim票据不证明进程锁、批准或实际执行。"""

    def __init__(self, data_store, account_id):
        self.account_id = _id(account_id)
        super().__init__(data_store, 'command_control')

    def _account(self):
        return self.environment, self.account_id

    def _lock(self, conn):
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                     ('demo-command:' + ':'.join(self._account()),))

    def _binding(self, conn, binding_id):
        row = conn.execute('SELECT * FROM demo_command_bindings WHERE environment=%s AND account_id=%s '
                           'AND binding_id=%s', (*self._account(), binding_id)).fetchone()
        if row is None:
            raise ValueError('command binding unavailable')
        body = row['payload']
        try:
            checked = FrozenCommandBinding(body['binding_id'], body['scope'], body['identity_hashes'], body['namespace'])
            valid = (body == checked.to_dict() and checked.sha256 == row['payload_sha256']
                     and row['binding_id'] == binding_id and body['scope']['account_id'] == self.account_id
                     and body['scope']['environment'] == self.environment)
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ValueError('command binding integrity failed')
        return deepcopy(body), row['payload_sha256']

    def register_binding(self, binding):
        if type(binding) is not FrozenCommandBinding:
            raise ValueError('internal FrozenCommandBinding required')
        # 重新构造规范类型；即使内部对象被强行改字段也不能提交坏的不可变事实。
        try:
            checked = FrozenCommandBinding(binding.binding_id, dict(binding._scope),
                                           dict(binding._hashes), binding.namespace)
            body, digest = checked.to_dict(), checked.sha256
            if body != binding.to_dict() or digest != binding.sha256:
                raise ValueError('invalid frozen command binding')
        except (KeyError, TypeError, AttributeError):
            raise ValueError('invalid frozen command binding') from None
        if body['scope']['account_id'] != self.account_id:
            raise ValueError('command binding account mismatch')
        with self.store._connection() as conn:
            self._lock(conn)
            row = conn.execute('SELECT payload_sha256 FROM demo_command_bindings WHERE environment=%s '
                'AND account_id=%s AND binding_id=%s', (*self._account(), binding.binding_id)).fetchone()
            if row:
                old, old_hash = self._binding(conn, binding.binding_id)
                if old != body or old_hash != digest:
                    raise ValueError('immutable command binding conflict')
            else:
                conn.execute('INSERT INTO demo_command_bindings(environment,account_id,binding_id,payload_sha256,payload) '
                    'VALUES(%s,%s,%s,%s,%s)', (*self._account(), binding.binding_id, digest, self.store._json(body)))
        return {'committed': True, 'binding': deepcopy(body), 'binding_sha256': digest,
                'source_authenticated': False, 'acceptance_eligible': False}

    def get_binding(self, binding_id):
        binding_id = _id(binding_id)
        with self.store._connection() as conn:
            body, _ = self._binding(conn, binding_id)
        return body

    def _intent(self, conn, command_id):
        row = conn.execute('SELECT * FROM demo_command_intents WHERE environment=%s AND account_id=%s '
                           'AND command_id=%s', (*self._account(), command_id)).fetchone()
        if row is None:
            return None
        body = row['payload']
        try:
            binding, binding_hash = self._binding(conn, row['binding_id'])
            request = {key: value for key, value in body.items() if key != 'submitted_ms'}
            expected = {'schema', 'environment', 'account_id', 'command_id', 'binding_id', 'binding_sha256',
                        'kind', 'actor', 'reason_code', 'submitted_ms'}
            self._request(body['binding_id'], body['command_id'], body['kind'], body['actor'],
                          body['reason_code'], body['submitted_ms'])
            valid = (set(body) == expected and body['schema'] == 'demo-command-intent-v1'
                     and body['environment'] == self.environment and body['account_id'] == self.account_id
                     and body['command_id'] == command_id and body['binding_id'] == row['binding_id']
                     and body['binding_sha256'] == binding_hash and _hash(body) == row['payload_sha256']
                     and _hash(request) == row['request_sha256'])
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ValueError('command intent integrity failed')
        return body, row['payload_sha256'], binding

    @staticmethod
    def _request(binding_id, command_id, kind, actor, reason_code, submitted_ms):
        _id(binding_id), _id(command_id), _id(actor), _stamp(submitted_ms)
        if not isinstance(kind, str) or kind not in {'pause_entries', 'reconcile_owned'}:
            raise ValueError('unsupported passive command kind')
        if not isinstance(reason_code, str) or reason_code not in REASONS[kind]:
            raise ValueError('invalid bounded command reason')

    def _chain(self, conn):
        rows = conn.execute('SELECT * FROM demo_command_events WHERE environment=%s AND account_id=%s '
            'ORDER BY account_sequence LIMIT %s', (*self._account(), MAX_EVENTS + 1)).fetchall()
        if len(rows) > MAX_EVENTS:
            raise ValueError('command event replay limit exceeded')
        previous = ZERO
        for sequence, row in enumerate(rows, 1):
            body = row['payload']
            if (not isinstance(body, dict) or row['account_sequence'] != sequence
                    or row['previous_hash'] != previous or row['payload_sha256'] != _hash(body)
                    or row['record_sha256'] != _hash({'sequence': sequence, 'previous_hash': previous,
                        'payload_sha256': row['payload_sha256']})
                    or any(body.get(key) != row[key] for key in
                        ('environment', 'account_id', 'command_id', 'event_id', 'event_type', 'observed_ms'))):
                raise ValueError('command event chain integrity failed')
            previous = row['record_sha256']
        return rows

    def _evidence(self, conn, binding, refs):
        if not refs:
            return
        archive = DemoEvidenceStore(self.store, self.account_id, binding['scope']['stage_id'])
        # 复用当前连接读取并重验真实归档链，不在池内嵌套获取第二连接。
        records = {row['ref']: row for row in archive._chain(conn)}
        for ref in refs:
            row = records.get(ref['ref'])
            if (row is None or row['record_sha256'] != ref['record_sha256']
                    or row['body']['scope'] != binding['scope']
                    or not isinstance(row['body']['payload'], dict)
                    or row['body']['payload'].get('identity_hashes') != binding['identity_hashes']):
                raise ValueError('command evidence unavailable or binding mismatch')

    def _state(self, conn, command_id, rows=None):
        fixed = self._intent(conn, command_id)
        if fixed is None:
            return None
        intent, digest, binding = fixed
        rows = self._chain(conn) if rows is None else rows
        state = {'command_id': command_id, 'binding': deepcopy(binding), 'intent': deepcopy(intent),
                 'intent_sha256': digest, 'status': None, 'owner_id': None, 'submission_attempts': 0,
                 'last_observed_ms': intent['submitted_ms'], 'execution_complete': False,
                 'acceptance_eligible': False, 'production_authorized': False, 'source_authenticated': False}
        matching = [row for row in rows if row['command_id'] == command_id]
        for index, row in enumerate(matching):
            body = row['payload']
            required = {'schema', 'environment', 'account_id', 'command_id', 'intent_sha256', 'binding_sha256',
                        'event_id', 'event_type', 'owner_id', 'observed_ms', 'reason_code', 'evidence_refs'}
            try:
                _id(body['event_id']), _stamp(body['observed_ms'])
                kind = body['event_type']
                if kind not in {'accepted', 'claimed'} | PUBLIC_EVENTS:
                    raise ValueError('unsupported command event')
                refs = _refs(body['evidence_refs'], required=kind == 'applied')
                valid = (set(body) == required and body['schema'] == 'demo-command-event-v1'
                         and body['intent_sha256'] == digest and body['binding_sha256'] == intent['binding_sha256']
                         and body['observed_ms'] >= state['last_observed_ms'])
                if kind == 'accepted':
                    valid = valid and index == 0 and body['owner_id'] is None and not refs
                    valid = valid and body['reason_code'] == intent['reason_code'] and body['observed_ms'] == intent['submitted_ms']
                    state['status'] = 'accepted'
                elif kind == 'claimed':
                    _owner(body['owner_id'])
                    valid = valid and state['status'] == 'accepted' and state['submission_attempts'] == 0
                    valid = valid and not refs and body['reason_code'] == 'claim_once'
                    state.update(status='claimed', owner_id=body['owner_id'], submission_attempts=1)
                else:
                    _owner(body['owner_id'])
                    valid = valid and isinstance(body['reason_code'], str) and body['reason_code'] in REASONS[kind]
                    valid = valid and state['status'] in {'claimed', 'unknown'}
                    if kind in {'applied', 'rejected'}:
                        valid = valid and body['owner_id'] == state['owner_id']
                        state['status'] = kind
                    elif kind == 'unknown':
                        state['status'] = 'unknown'
                    self._evidence(conn, binding, refs)
                if not valid:
                    raise ValueError('invalid event transition')
                state['last_observed_ms'] = body['observed_ms']
            except (ValueError, TypeError, KeyError):
                raise ValueError('command state replay integrity failed') from None
        if not matching or state['status'] is None:
            raise ValueError('command accepted event integrity failed')
        return state

    def _append(self, conn, intent, event_id, kind, owner, now, reason, refs, rows):
        body = {'schema': 'demo-command-event-v1', **dict(zip(('environment', 'account_id'), self._account(), strict=True)),
                'command_id': intent['command_id'], 'intent_sha256': _hash(intent),
                'binding_sha256': intent['binding_sha256'], 'event_id': event_id, 'event_type': kind,
                'owner_id': owner, 'observed_ms': now, 'reason_code': reason, 'evidence_refs': refs}
        old = next((row for row in rows if row['event_id'] == event_id), None)
        if old:
            if old['payload'] != body:
                raise ValueError('immutable command event conflict')
            return rows
        sequence, previous = len(rows) + 1, rows[-1]['record_sha256'] if rows else ZERO
        if sequence > MAX_EVENTS:
            raise ValueError('command event replay limit exceeded')
        digest = _hash(body)
        record_hash = _hash({'sequence': sequence, 'previous_hash': previous, 'payload_sha256': digest})
        inserted = conn.execute('INSERT INTO demo_command_events(environment,account_id,account_sequence,command_id,event_id,'
            'event_type,observed_ms,previous_hash,payload_sha256,record_sha256,payload) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
            (*self._account(), sequence, intent['command_id'], event_id, kind, now, previous,
             digest, record_hash, self.store._json(body))).fetchone()
        return [*rows, inserted]

    @staticmethod
    def _receipt(state, *, ticket=False):
        return {'committed': True, 'action_ticket': ticket, 'command': deepcopy(state),
                'execution_complete': False, 'acceptance_eligible': False, 'production_authorized': False}

    def enqueue(self, *, binding_id, command_id, kind, actor, reason_code, submitted_ms):
        self._request(binding_id, command_id, kind, actor, reason_code, submitted_ms)
        with self.store._connection() as conn:
            self._lock(conn)
            _, binding_hash = self._binding(conn, binding_id)
            request = {'schema': 'demo-command-intent-v1', 'environment': self.environment,
                       'account_id': self.account_id, 'binding_id': binding_id, 'binding_sha256': binding_hash,
                       'command_id': command_id, 'kind': kind, 'actor': actor, 'reason_code': reason_code}
            old = self._intent(conn, command_id)
            if old:
                original = {key: value for key, value in old[0].items() if key != 'submitted_ms'}
                if original != request:
                    raise ValueError('immutable command intent conflict')
            else:
                intent = {**request, 'submitted_ms': submitted_ms}
                conn.execute('INSERT INTO demo_command_intents(environment,account_id,command_id,binding_id,request_sha256,'
                    'payload_sha256,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                    (*self._account(), command_id, binding_id, _hash(request), _hash(intent), self.store._json(intent)))
                self._append(conn, intent, 'accepted-' + _hash(request), 'accepted', None,
                             submitted_ms, reason_code, [], self._chain(conn))
            state = self._state(conn, command_id)
        LOGGER.info('Demo 被动命令已提交 类型=%s 状态=%s；尚未执行交易', kind, state['status'])
        return self._receipt(state)

    def get(self, command_id):
        command_id = _id(command_id)
        with self.store._connection() as conn:
            self._lock(conn)
            state = self._state(conn, command_id)
        return deepcopy(state)

    def events(self, command_id):
        command_id = _id(command_id)
        with self.store._connection() as conn:
            self._lock(conn)
            rows = self._chain(conn)
            if self._state(conn, command_id, rows) is None:
                return []
            result = [deepcopy(row['payload']) for row in rows if row['command_id'] == command_id]
        return result

    def unresolved_commands(self):
        """在账户锁内重放全部意图，只列出仍需保守处理的状态。"""
        with self.store._connection() as conn:
            self._lock(conn)
            states = self._replayed_command_states(conn)
            unresolved = [state for state in states if state['status'] in {'claimed', 'unknown'}]
        return deepcopy(unresolved)

    def _replayed_command_states(self, conn):
        """先验证账户总链，再按命令分组重放，避免逐命令重复扫描全历史。"""
        rows = self._chain(conn)
        intents = conn.execute(
            'SELECT command_id FROM demo_command_intents WHERE environment=%s AND account_id=%s '
            'ORDER BY command_id LIMIT %s',
            (*self._account(), MAX_EVENTS + 1),
        ).fetchall()
        if len(intents) > MAX_EVENTS:
            raise ValueError('command intent replay limit exceeded')
        command_ids = []
        for row in intents:
            try:
                command_ids.append(_id(row['command_id']))
            except (TypeError, KeyError, ValueError):
                raise ValueError('command intent integrity failed') from None
        events_by_command = {}
        for row in rows:
            events_by_command.setdefault(row['command_id'], []).append(row)
        if set(events_by_command) != set(command_ids):
            raise ValueError('command event intent linkage integrity failed')
        states = []
        for command_id in command_ids:
            state = self._state(conn, command_id, events_by_command[command_id])
            if state is None:
                raise ValueError('command intent integrity failed')
            states.append(state)
        return states

    def pending_commands(self):
        """完整重放账户事实，返回已提交但尚未结束的命令。"""
        with self.store._connection() as conn:
            self._lock(conn)
            states = self._replayed_command_states(conn)
            pending = [state for state in states if state['status'] in {'accepted', 'claimed', 'unknown'}]
        return deepcopy(pending)

    def accepted_commands(self, binding_id, *, include_unresolved=False):
        """列出指定冻结绑定下的待处理命令，并完整校验账户事件链。

        默认保持旧接口语义，只返回可首次领取的 ``accepted`` 命令；前台
        writer 通过 ``include_unresolved`` 在同一次账本重放中观察重启遗留的
        ``claimed``/``unknown`` 命令，避免为了恢复检查再次扫描整条账本。
        """
        binding_id = _id(binding_id)
        with self.store._connection() as conn:
            self._lock(conn)
            states = self._replayed_command_states(conn)
            statuses = {'accepted', 'claimed', 'unknown'} if include_unresolved else {'accepted'}
            accepted = [state for state in states if state['status'] in statuses
                        and state['intent']['binding_id'] == binding_id]
        return deepcopy(accepted)

    def claim(self, command_id, *, owner_id, claimed_ms):
        command_id, owner_id, now = _id(command_id), _owner(owner_id), _stamp(claimed_ms)
        ticket = False
        with self.store._connection() as conn:
            self._lock(conn)
            conn.execute('SELECT command_id FROM demo_command_intents WHERE environment=%s AND account_id=%s '
                         'AND command_id=%s FOR UPDATE', (*self._account(), command_id)).fetchone()
            rows = self._chain(conn)
            state = self._state(conn, command_id, rows)
            if state is None:
                raise ValueError('command unavailable')
            if now < state['last_observed_ms']:
                raise ValueError('command timestamp regressed')
            if state['status'] == 'accepted':
                rows = self._append(conn, state['intent'], 'claimed-' + state['intent_sha256'],
                                    'claimed', owner_id, now, 'claim_once', [], rows)
                state = self._state(conn, command_id, rows)
                ticket = True
        # 必须先离开连接上下文完成 COMMIT，才将一次性处理票据交给调用方。
        LOGGER.info('Demo 被动命令处理权记录 状态=%s 首次票据=%s；不代表已执行', state['status'], ticket)
        return self._receipt(state, ticket=ticket)

    def append_event(self, command_id, *, event_id, event_type, owner_id, observed_ms, reason_code, evidence_refs):
        command_id, event_id, owner_id, now = _id(command_id), _id(event_id), _owner(owner_id), _stamp(observed_ms)
        if (not isinstance(event_type, str) or event_type not in PUBLIC_EVENTS
                or not isinstance(reason_code, str) or reason_code not in REASONS[event_type]):
            raise ValueError('invalid bounded command event')
        refs = _refs(evidence_refs, required=event_type == 'applied')
        with self.store._connection() as conn:
            self._lock(conn)
            rows = self._chain(conn)
            state = self._state(conn, command_id, rows)
            if state is None:
                raise ValueError('command unavailable')
            updated = self._append(conn, state['intent'], event_id, event_type, owner_id, now, reason_code, refs, rows)
            # 重放在提交前检验状态与归档；不合法事件整笔回滚。
            state = self._state(conn, command_id, updated)
        LOGGER.info('Demo 被动命令事件已提交 类型=%s 状态=%s；交易完成资格保持关闭', event_type, state['status'])
        return deepcopy(state)
