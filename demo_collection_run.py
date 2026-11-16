"""内部冻结运行的只读采集清单；只证明提交与引用，不授予语义或交易资格。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
import re
import time

from demo_account_collector import (ARCHIVE_REASONS, CollectionError, DemoAccountCollector,
                                    _digest, _receipt, _stamp)
from demo_evidence_store import DemoEvidenceStore, HASHES, _id, _request

LOGGER = logging.getLogger(__name__)
SCHEMA = 'demo-collection-manifest-v1'
SCOPE_FIELDS = ('environment', 'account_id', 'stage_id', 'strategy_version_id', 'run_id')
COMPONENTS = ('clock_before', 'account_snapshot', 'positions', 'mode',
              'normal_orders', 'conditional_orders', 'clock_after')
REASONS = frozenset({
    'archive_commit_failed', 'archive_commit_unverified', 'request_evidence_unverified',
    'income_request_budget_exhausted', 'income_rows_invalid', 'income_transaction_duplicate',
    'income_transaction_conflict', 'income_capture_failed', 'income_single_millisecond_saturated',
    'income_type_unclassified', 'income_asset_unsupported', 'income_window_changed_during_capture',
    'exchange_clock_regressed', 'collection_capture_failed',
} | {name + '_capture_failed' for name in COMPONENTS})


def _committed_receipt(value, kind, scope):
    checked = _receipt(value, kind, scope)
    return {key: checked[key] for key in
            ('ref', 'sha256', 'record_sha256', 'sequence', 'committed', 'kind', 'scope')}


@dataclass(frozen=True, init=False)
class FrozenCollectionBinding:
    """仅由内部组合根构造；公开字典均为副本，底层不保留可变输入。"""
    _scope_items: tuple
    _hash_items: tuple

    def __init__(self, scope, identity_hashes):
        if (not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS)
                or scope.get('environment') != 'binance_demo'
                or not isinstance(identity_hashes, dict) or set(identity_hashes) != set(HASHES)):
            raise ValueError('complete frozen collection binding required')
        checked = tuple((key, _id(scope[key], 'collection scope')) for key in SCOPE_FIELDS)
        hashes = []
        for key in HASHES:
            value = identity_hashes[key]
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('invalid frozen collection hash')
            hashes.append((key, value))
        object.__setattr__(self, '_scope_items', checked)
        object.__setattr__(self, '_hash_items', tuple(hashes))

    @property
    def scope(self):
        return dict(self._scope_items)

    @property
    def identity_hashes(self):
        return dict(self._hash_items)

    @property
    def sha256(self):
        return _digest({'scope': self.scope, 'identity_hashes': self.identity_hashes})


class DemoCollectionRun:
    """一个 collection_id 只执行一个冻结计划；未封存的旧 ID 从不自动重采。"""

    def __setattr__(self, name, value):
        if name in {'adapter', 'archive', '_binding', 'collection_id', 'clock_ms', 'max_requests'} and hasattr(self, name):
            raise AttributeError('frozen collection configuration')
        object.__setattr__(self, name, value)

    def __init__(self, adapter, archive, binding, *, collection_id, clock_ms=None, max_requests=4096):
        if not isinstance(binding, FrozenCollectionBinding) or not isinstance(archive, DemoEvidenceStore):
            raise ValueError('internal frozen binding and real evidence archive required')
        scope = binding.scope
        if (archive.environment != scope['environment'] or archive.account_id != scope['account_id']
                or archive.stage_id != scope['stage_id']):
            raise ValueError('collection archive identity mismatch')
        _id(collection_id, 'collection id')
        if len(collection_id) > 96:
            raise ValueError('collection id too long')
        if type(max_requests) is not int or not 1 <= max_requests <= 100_000:
            raise ValueError('invalid collection request budget')
        self.adapter, self.archive = adapter, archive
        self._binding, self.collection_id = binding, collection_id
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_requests = max_requests
        self._components, self._component_events, self._failure_events = [], [], []

    @property
    def binding(self):
        return self._binding

    def _base(self):
        return {'schema': SCHEMA, 'collection_id': self.collection_id,
                'scope': self.binding.scope, 'identity_hashes': self.binding.identity_hashes,
                'binding_sha256': self.binding.sha256, 'as_of': None, 'as_of_ms': None,
                'reconciled': False, 'boundary_verified': False, 'acceptance_eligible': False}

    def _base_matches(self, value, *, result=False):
        expected = {key: item for key, item in self._base().items() if not result or key != 'schema'}
        return (isinstance(value, dict) and set(expected) <= set(value)
                and _digest({key: value[key] for key in expected}) == _digest(expected))

    def _manifest(self, event, suffix, **fields):
        try:
            value = self.archive.append('equity_reconciliation', self.binding.scope,
                {**self._base(), 'event': event, **deepcopy(fields)}, {},
                stable_key=f'collection:{self.collection_id}:{suffix}')
            receipt = _committed_receipt(value, 'equity_reconciliation', self.binding.scope)
            row = self.archive.read(receipt['ref'])
            if row['payload'] != {**self._base(), 'event': event, **fields}:
                raise ValueError('manifest sanitized unexpectedly')
            return receipt
        except Exception as error:
            LOGGER.warning('Demo采集清单提交失败 账户=%s 阶段=%s 运行=%s 事件=%s 错误类型=%s；保留已提交事实',
                           self.binding.scope['account_id'], self.binding.scope['stage_id'],
                           self.binding.scope['run_id'], event, type(error).__name__)
            raise CollectionError('collection_manifest_commit_failed') from None

    def _read_collection(self):
        try:
            return [row for row in self.archive.records()
                    if isinstance(row.get('payload'), dict)
                    and row['payload'].get('collection_id') == self.collection_id]
        except Exception:
            raise CollectionError('collection_archive_unverified') from None

    def _prepare(self, operation, parameters):
        plan = {'operation': operation, 'parameters': parameters, 'max_requests': self.max_requests}
        if operation == 'capture_account':
            plan['expected_components'] = list(COMPONENTS)
        rows = self._read_collection()
        if rows:
            for row in rows:
                payload = row['payload']
                if (row['scope'] != self.binding.scope
                        or payload.get('identity_hashes') != self.binding.identity_hashes
                        or payload.get('binding_sha256') != self.binding.sha256):
                    raise CollectionError('collection_identity_conflict')
            starts = [r for r in rows if r['payload'].get('event') == 'start'
                      and r['payload'].get('schema') == SCHEMA]
            if len(starts) != 1:
                raise CollectionError('collection_archive_unverified')
            if starts[0]['payload'].get('plan') != plan:
                raise CollectionError('collection_identity_conflict')
            ends = [r for r in rows if r['payload'].get('event') == 'end'
                    and r['payload'].get('schema') == SCHEMA]
            if not ends:
                raise CollectionError('collection_incomplete_requires_new_id')
            try:
                return self._replay(rows, starts[0], ends, operation)
            except Exception:
                raise CollectionError('collection_archive_unverified') from None
        self._start = self._manifest('start', 'start', plan=plan,
                                     local_started_ms=_stamp(self.clock_ms()))
        return None

    def _replay(self, rows, start, ends, operation):
        if len(ends) != 1:
            raise ValueError('duplicate collection end')
        end = ends[0]
        payload = end['payload']
        facts, events, failures = [], [], []
        for row in rows:
            body = row['payload']
            if body.get('schema') == SCHEMA:
                if not self._base_matches(body) or row['kind'] != 'equity_reconciliation':
                    raise ValueError('invalid collection event')
                if body.get('event') == 'component':
                    events.append(row)
                elif body.get('event') == 'failure':
                    if body.get('reason_code') not in REASONS:
                        raise ValueError('unbounded collection failure')
                    failures.append(row)
                elif body.get('event') not in {'start', 'end'}:
                    raise ValueError('unknown collection event')
            else:
                facts.append(row)
        receipts = [_committed_receipt(row, row['kind'], self.binding.scope) for row in facts]
        if (not start['sequence'] < end['sequence']
                or any(not start['sequence'] < row['sequence'] < end['sequence'] for row in rows if row not in (start, end))
                or payload.get('start_ref') != start['ref']
                or payload.get('start_record_sha256') != start['record_sha256']
                or payload.get('components') != receipts
                or payload.get('component_events') != [_committed_receipt(r, r['kind'], self.binding.scope) for r in events]
                or payload.get('failure_events') != [_committed_receipt(r, r['kind'], self.binding.scope) for r in failures]
                or len(events) != len(facts)):
            raise ValueError('collection links mismatch')
        for index, (row, event, receipt) in enumerate(zip(facts, events, receipts), 1):
            if (row['payload'].get('collection_component_index') != index
                    or event['payload'].get('component_index') != index
                    or event['payload'].get('receipt') != receipt
                    or row['sequence'] >= event['sequence']
                    or (index < len(facts) and event['sequence'] >= facts[index]['sequence'])):
                raise ValueError('component order mismatch')
            metadata = _request(row['request_evidence'])
            if row['kind'] != 'positions' and not metadata:
                raise ValueError('missing component transport')
        result = deepcopy(payload.get('result'))
        if (not self._base_matches(result, result=True)
                or result.get('reasons') != [r['payload']['reason_code'] for r in failures]):
            raise ValueError('collection result mismatch')
        successful = not result['reasons']
        if operation == 'capture_account':
            sources = {r['payload']['capture_component']: r['ref'] for r in facts}
            if result.get('source_refs') != sources or (successful and list(sources) != list(COMPONENTS)):
                raise ValueError('account collection references mismatch')
            if result.get('capture_complete') is not successful or result.get('transport_evidence_complete') is not successful:
                raise ValueError('invalid capture completion')
            if 'positions' in sources:
                account = next(r for r in facts if r['ref'] == sources['account_snapshot'])
                position = next(r for r in facts if r['ref'] == sources['positions'])
                data = position['payload']['data']
                if (position['request_evidence'] or data.get('derived_from_account') is not True
                        or data.get('source_ref') != account['ref']
                        or data.get('source_record_sha256') != account['record_sha256']
                        or data.get('source_sequence') != account['sequence']
                        or data.get('positions') != account['payload']['data']['positions']):
                    raise ValueError('derived positions mismatch')
        elif (result.get('source_refs') != [r['ref'] for r in facts]
              or result.get('complete') is not successful
              or result.get('settlement_finality_verified') is not False):
            raise ValueError('income collection references mismatch')
        status = 'completed' if successful else 'failed'
        if payload.get('status') != status or result.get('collection_status') != status:
            raise ValueError('collection completion mismatch')
        self._verify_projection(facts, start['payload']['plan'], result)
        return {**result, 'manifest_ref': end['ref']}

    def _verify_projection(self, facts, plan, result):
        """从固定原文重跑结构归约；适配器只回放资料，绝不访问远端。"""
        owner = self
        cursor = 0
        missing_reasons = {}
        if plan['operation'] == 'capture_account':
            reasons = iter(result['reasons'])
            present = {row['payload'].get('capture_component') for row in facts}
            missing_reasons = {name: next(reasons, None) for name in COMPONENTS if name not in present}

        def response(row):
            return {'data': deepcopy(row['payload']['data']),
                    'request_evidence': deepcopy(row['request_evidence'])}

        def missing(component):
            reason = missing_reasons.get(component)
            if reason in ARCHIVE_REASONS:
                raise CollectionError(reason)
            if reason == component + '_capture_failed':
                raise RuntimeError('归档中该组件采集失败')
            raise ValueError('missing component reason mismatch')

        class ReplayAdapter:
            clock_calls = 0

            def account_component(self, name):
                matches = [row for row in facts if row['payload'].get('capture_component') == name]
                if not matches:
                    return missing(name)
                if len(matches) != 1:
                    raise ValueError('duplicate capture component')
                return response(matches[0])

            def fetch_server_time(self, *, with_evidence):
                self.clock_calls += 1
                return self.account_component('clock_before' if self.clock_calls == 1 else 'clock_after')

            def fetch_account_snapshot(self, *, with_evidence):
                return self.account_component('account_snapshot')

            def fetch_multi_assets_mode(self, *, with_evidence):
                return self.account_component('mode')

            def fetch_open_orders(self, *, with_evidence):
                return self.account_component('normal_orders')

            def fetch_open_conditional_orders(self, *, with_evidence):
                return self.account_component('conditional_orders')

            def fetch_income_window(self, *, since_ms, until_ms, limit, with_evidence):
                if cursor >= len(facts):
                    reason = next((reason for reason in result['reasons'] if reason in ARCHIVE_REASONS
                                   or reason in {'income_capture_failed', 'income_rows_invalid'}), None)
                    if reason is None:
                        raise ValueError('income source window missing')
                    raise CollectionError(reason)
                row = facts[cursor]
                body = row['payload']
                if (row['kind'] != 'cashflows' or body.get('start_ms') != since_ms
                        or body.get('end_ms') != until_ms or body.get('limit') != limit):
                    raise ValueError('income source window mismatch')
                return {'data': deepcopy(body['rows']), 'request_evidence': deepcopy(row['request_evidence'])}

        class ReplayArchive:
            def append(self, kind, scope, payload, metadata):
                nonlocal cursor
                name = payload.get('capture_component')
                if name in missing_reasons:
                    return missing(name)
                if cursor >= len(facts):
                    raise ValueError('unlinked replay component')
                row = facts[cursor]
                expected = {key: value for key, value in row['payload'].items()
                            if key not in {'collection_id', 'binding_sha256', 'collection_component_index'}}
                if (kind != row['kind'] or scope != row['scope']
                        or _digest(payload) != _digest(expected)
                        or _digest(metadata) != _digest(row['request_evidence'])):
                    raise ValueError('replay raw component mismatch')
                cursor += 1
                return _committed_receipt(row, kind, scope)

        collector = DemoAccountCollector(ReplayAdapter(), ReplayArchive(), self.binding.scope,
            # 当前原文归档后才推进 cursor；失败派生组件不能消耗下一条事实。
            clock_ms=lambda: facts[cursor]['payload']['local_observed_ms'] if cursor < len(facts) else 0,
            max_requests=plan['max_requests'], run_binding=owner.binding, readonly_replay=True)
        if plan['operation'] == 'capture_account':
            rebuilt = collector.capture_account()
        else:
            rebuilt = collector.collect_income(**plan['parameters'])
        rebuilt = {**rebuilt, **{key: value for key, value in self._base().items() if key != 'schema'},
                   'collection_status': result['collection_status']}
        if cursor != len(facts) or _digest(rebuilt) != _digest(result):
            raise ValueError('collection projection differs from frozen sources')

    def _append_component(self, kind, scope, payload, evidence):
        if scope != self.binding.scope or payload.get('identity_hashes') != self.binding.identity_hashes:
            raise CollectionError('collection_identity_conflict')
        index = len(self._components) + 1
        payload = {**deepcopy(payload), 'collection_id': self.collection_id,
                   'binding_sha256': self.binding.sha256, 'collection_component_index': index}
        # 原文先提交，之后记录回执；后一步失败绝不撤销或丢弃已提交原文。
        receipt = self.archive.append(kind, scope, payload, evidence,
                                      stable_key=f'collection:{self.collection_id}:raw:{index}')
        receipt = _committed_receipt(receipt, kind, self.binding.scope)
        self._components.append(receipt)
        event = self._manifest('component', f'component:{index}', component_index=index, receipt=receipt)
        self._component_events.append(event)
        return receipt

    def _execute(self, operation, parameters):
        replay = self._prepare(operation, parameters)
        if replay is not None:
            return replay
        self._components, self._component_events, self._failure_events = [], [], []
        owner = self
        class ComponentArchive:
            def append(self, kind, scope, payload, evidence):
                return owner._append_component(kind, scope, payload, evidence)
        collector = DemoAccountCollector(self.adapter, ComponentArchive(), self.binding.scope,
            clock_ms=self.clock_ms, max_requests=self.max_requests, run_binding=self.binding)
        if operation == 'capture_account':
            result = collector.capture_account()
        else:
            result = collector.collect_income(**parameters)
        reasons = result.get('reasons', [])
        if not isinstance(reasons, list) or any(reason not in REASONS for reason in reasons):
            raise CollectionError('collection_archive_unverified')
        for index, reason in enumerate(reasons, 1):
            receipt = self._manifest('failure', f'failure:{index}', failure_index=index, reason_code=reason)
            self._failure_events.append(receipt)
        status = 'completed' if not reasons else 'failed'
        result = {**result, **{key: value for key, value in self._base().items() if key != 'schema'},
                  'collection_status': status}
        end = self._manifest('end', 'end', status=status, start_ref=self._start['ref'],
            start_record_sha256=self._start['record_sha256'], components=self._components,
            component_events=self._component_events, failure_events=self._failure_events, result=result)
        # 最终返回也经过持久原文、清单和派生来源核验，不信任仅有内存状态。
        rows = self._read_collection()
        try:
            checked = self._replay(rows, self.archive.read(self._start['ref']),
                                   [self.archive.read(end['ref'])], operation)
        except Exception:
            raise CollectionError('collection_archive_unverified') from None
        LOGGER.info('Demo运行采集清单已封存 账户=%s 阶段=%s 运行=%s 采集=%s 操作=%s 组件数=%s 状态=%s 原因=%s；未授予语义或交易资格',
                    self.binding.scope['account_id'], self.binding.scope['stage_id'],
                    self.binding.scope['run_id'], self.collection_id, operation, len(self._components), status, reasons)
        return checked

    def capture_account(self):
        return self._execute('capture_account', {})

    def replay_completed(self):
        """仅重放已封存运行；缺少清单时不创建 start、不读适配器或当前时钟。

        已封存失败也会原样返回失败事实，调用者仍须检查 collection_status。
        本入口不调用可能启动采集的 _prepare/_execute。
        """
        rows = self._read_collection()
        if not rows:
            raise CollectionError('collection_missing_read_only')
        for row in rows:
            body = row['payload']
            if (row['scope'] != self.binding.scope
                    or body.get('identity_hashes') != self.binding.identity_hashes
                    or body.get('binding_sha256') != self.binding.sha256):
                raise CollectionError('collection_identity_conflict')
        starts = [row for row in rows if row['payload'].get('schema') == SCHEMA
                  and row['payload'].get('event') == 'start']
        ends = [row for row in rows if row['payload'].get('schema') == SCHEMA
                and row['payload'].get('event') == 'end']
        if not ends:
            raise CollectionError('collection_incomplete_requires_new_id')
        try:
            if len(starts) != 1 or len(ends) != 1:
                raise ValueError('ambiguous sealed collection')
            plan = starts[0]['payload']['plan']
            operation, parameters = plan['operation'], plan['parameters']
            expected = {'operation': operation, 'parameters': parameters,
                        'max_requests': self.max_requests}
            if operation == 'capture_account':
                expected['expected_components'] = list(COMPONENTS)
                if parameters != {}:
                    raise ValueError('invalid capture plan')
            elif operation == 'collect_income':
                if type(parameters) is not dict or set(parameters) != {'since_ms', 'until_ms', 'limit'}:
                    raise ValueError('invalid income plan')
                start, end = _stamp(parameters['since_ms']), _stamp(parameters['until_ms'])
                limit = parameters['limit']
                if end < start or type(limit) is not int or not 1 <= limit <= 1000:
                    raise ValueError('invalid income plan')
            else:
                raise ValueError('unknown collection operation')
            if plan != expected:
                raise ValueError('frozen collection plan mismatch')
            result = self._replay(rows, starts[0], ends, operation)
        except Exception:
            raise CollectionError('collection_archive_unverified') from None
        LOGGER.info('Demo采集只读重放完成 操作=%s 组件数=%s 状态=%s；未触发采集或写入',
                    operation, len(ends[0]['payload']['components']), result['collection_status'])
        return result

    def collect_income(self, *, since_ms, until_ms, limit=1000):
        start, end = _stamp(since_ms), _stamp(until_ms)
        if end < start or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid income collection window')
        return self._execute('collect_income', {'since_ms': start, 'until_ms': end, 'limit': limit})
