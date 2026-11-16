"""只读归档到安全运行时的内部桥接；存储存在不等于事实完整或允许交易。"""
from __future__ import annotations

from copy import deepcopy
import re

from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_round_trip_costs import reconcile_round_trip_costs

SHA = re.compile(r'[a-f0-9]{64}\Z')
ID = re.compile(r'[A-Za-z0-9_./:+-]{1,256}\Z')
SCOPE = ('environment', 'account_id', 'stage_id', 'strategy_version_id', 'run_id')
ROLES = {
    'context': {'account': 'account_snapshot', 'positions': 'positions', 'normal': 'normal_orders',
        'conditional': 'conditional_orders', 'fills': 'fills', 'income': 'cashflows', 'clock': 'clock',
        'stage_opening': 'boundary', 'day_opening': 'boundary'},
    'entry': {'market': 'equity_reconciliation', 'exchange_rules': 'equity_reconciliation', 'clock': 'clock'},
    'protection': {'positions': 'positions', 'normal': 'normal_orders', 'conditional': 'conditional_orders',
        'fills': 'fills', 'clock': 'clock'},
    'completion': {'account': 'account_snapshot', 'normal': 'normal_orders', 'conditional': 'conditional_orders',
        'fills': 'fills', 'income': 'cashflows'},
}


class ProviderUnavailable(ValueError):
    """仅包含固定错误码，不回显选择器、数据库或上游异常文本。"""


def _safe(value):
    return isinstance(value, str) and ID.fullmatch(value) and '://' not in value and '@' not in value


def _approval_text(value):
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value) <= 512 and '://' not in value
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))


def _stamp(value):
    return type(value) is int and 0 <= value < 2**63


class ArchiveProviderBridge:
    """服务端组合根固定身份，选择器和语义校验器不能来自 HTTP 参数。"""

    def __init__(self, archive, *, scope, identity_hashes, selectors=None,
                 semantic_validator=None, max_age_ms=None):
        if (not isinstance(archive, DemoEvidenceStore) or not isinstance(scope, dict)
                or set(scope) != set(SCOPE) or scope.get('environment') != 'binance_demo'
                or any(not _safe(scope.get(key)) for key in SCOPE)
                or archive.account_id != scope['account_id'] or archive.stage_id != scope['stage_id']
                or archive.environment != scope['environment']):
            raise ValueError('invalid internal archive binding')
        if (not isinstance(identity_hashes, dict) or set(identity_hashes) != set(HASHES)
                or any(not isinstance(value, str) or not SHA.fullmatch(value) for value in identity_hashes.values())):
            raise ValueError('invalid immutable identity hashes')
        if selectors is not None and (not isinstance(selectors, dict) or set(selectors) - set(ROLES)):
            raise ValueError('invalid internal selectors')
        if max_age_ms is not None and (not isinstance(max_age_ms, dict) or set(max_age_ms) != set(ROLES)
                or any(not _stamp(value) for value in max_age_ms.values())):
            raise ValueError('invalid frozen provider freshness')
        self.archive = archive
        self.scope, self.hashes = deepcopy(scope), deepcopy(identity_hashes)
        self.selectors = dict(selectors or {})
        self.semantic_validator = semantic_validator
        self.max_age = dict(max_age_ms or {})

    def _unavailable(self, reason):
        return {'committed': False, 'account_scope': self.scope['account_id'],
                'reason_codes': [reason], 'acceptance_eligible': False}

    def _call(self, account_scope, strategy_id, now_ms, extra, permitted):
        if (account_scope != self.scope['account_id'] or strategy_id != self.scope['strategy_version_id']
                or not _stamp(now_ms) or set(extra) - set(permitted)):
            raise ProviderUnavailable('provider_call_contract_invalid')

    def _read(self, ref, kind):
        if not _safe(ref):
            raise ProviderUnavailable('archive_chain_or_reference_invalid')
        try:
            row = self.archive.read(ref)
        except Exception:
            raise ProviderUnavailable('archive_chain_or_reference_invalid') from None
        if (row.get('committed') is not True or row.get('ref') != ref or row.get('kind') != kind
                or row.get('scope') != self.scope or not isinstance(row.get('payload'), dict)
                or (kind != 'completion' and row['payload'].get('identity_hashes') != self.hashes)
                or not _stamp(row.get('sequence')) or row['sequence'] <= 0
                or any(not isinstance(row.get(key), str) or not SHA.fullmatch(row[key])
                       for key in ('sha256', 'record_sha256'))):
            raise ProviderUnavailable('archive_scope_kind_or_identity_mismatch')
        return row

    def _links(self, row, purpose):
        payload = row['payload']
        refs, frozen = payload.get('source_refs'), payload.get('source_record_hashes')
        if (not isinstance(refs, dict) or set(refs) != set(ROLES[purpose])
                or not isinstance(frozen, dict) or set(frozen) != set(refs.values())):
            raise ProviderUnavailable(purpose + '_dependencies_incomplete')
        sources = {}
        for role, kind in ROLES[purpose].items():
            ref = refs[role]
            if ref == row['ref']:
                raise ProviderUnavailable('archive_dependency_cycle')
            linked = self._read(ref, kind)
            if frozen[ref] != linked['record_sha256'] or linked['sequence'] >= row['sequence']:
                raise ProviderUnavailable('archive_dependency_hash_or_order_mismatch')
            sources[ref] = linked
        if not callable(self.semantic_validator):
            raise ProviderUnavailable('semantic_validator_unavailable')
        try:
            verdict = self.semantic_validator(deepcopy(row), deepcopy(sources), purpose=purpose,
                binding={'scope': deepcopy(self.scope), 'identity_hashes': deepcopy(self.hashes)})
        except Exception:
            raise ProviderUnavailable('semantic_proof_unverified') from None
        expected = {'verified': True, 'purpose': purpose, 'scope': self.scope, 'identity_hashes': self.hashes,
                    'projection_record_sha256': row['record_sha256'], 'source_record_hashes': frozen}
        if type(verdict) is not dict or verdict != expected or verdict.get('verified') is not True:
            raise ProviderUnavailable('semantic_proof_unverified')
        return sources

    def _projection(self, purpose, *, account_scope, strategy_id, now_ms, extra, permitted):
        self._call(account_scope, strategy_id, now_ms, extra, permitted)
        selector = self.selectors.get(purpose)
        if not callable(selector):
            raise ProviderUnavailable(purpose + '_selector_unavailable')
        if purpose not in self.max_age:
            raise ProviderUnavailable(purpose + '_freshness_unconfigured')
        try:
            ref = selector(scope=deepcopy(self.scope), identity_hashes=deepcopy(self.hashes),
                           now_ms=now_ms, **deepcopy(extra))
        except Exception:
            raise ProviderUnavailable(purpose + '_selector_failed') from None
        row = self._read(ref, 'equity_reconciliation')
        payload = row['payload']
        if (payload.get('schema') != 'demo-safety-provider-v1' or payload.get('provider') != purpose
                or not _stamp(payload.get('as_of_ms'))
                or not 0 <= now_ms - payload['as_of_ms'] <= self.max_age[purpose]):
            raise ProviderUnavailable(purpose + '_projection_invalid_or_stale')
        self._links(row, purpose)
        value = payload.get('value')
        if (purpose != 'protection' and not isinstance(value, dict)) or (purpose == 'protection' and not isinstance(value, list)):
            raise ProviderUnavailable(purpose + '_projection_value_invalid')
        self._value(purpose, value)
        return row, deepcopy(value)

    def _value(self, purpose, value):
        if purpose == 'context':
            if (value.get('account_id') != self.scope['account_id'] or value.get('stage_id') != self.scope['stage_id']
                    or value.get('environment') != self.scope['environment']):
                raise ProviderUnavailable('context_value_identity_mismatch')
            approval = value.get('approvals', {}).get(self.scope['strategy_version_id'], {})
            if (approval.get('run_id') != self.scope['run_id'] or approval.get('approved') is not True
                    or any(approval.get(key) != self.hashes[key] for key in ('strategy_hash', 'risk_policy_hash', 'cost_model_hash'))):
                raise ProviderUnavailable('context_approval_identity_mismatch')
        elif purpose == 'entry':
            expected = {**self.scope, 'strategy_hash': self.hashes['strategy_hash'],
                'exit_params_hash': self.hashes['execution_hash'], 'risk_policy_hash': self.hashes['risk_policy_hash'],
                'cost_model_hash': self.hashes['cost_model_hash']}
            if any(value.get(key) != item for key, item in expected.items()):
                raise ProviderUnavailable('entry_value_identity_mismatch')
        elif purpose == 'completion':
            if (not isinstance(value.get('completion_refs'), list)
                    or any(not _safe(ref) for ref in value['completion_refs'])
                    or len(set(value['completion_refs'])) != len(value['completion_refs'])
                    or not _stamp(value.get('scan_through_ms'))):
                raise ProviderUnavailable('completion_coverage_value_invalid')
        else:
            for item in value:
                if not isinstance(item, dict):
                    raise ProviderUnavailable('protection_value_identity_mismatch')
                for name in ('plan', 'position'):
                    fact = item.get(name)
                    expected = {'environment': 'binance_demo', 'account_id': self.scope['account_id'],
                        'strategy_id': self.scope['strategy_version_id'], 'run_id': self.scope['run_id']}
                    if not isinstance(fact, dict) or any(fact.get(key) != val for key, val in expected.items()):
                        raise ProviderUnavailable('protection_value_identity_mismatch')
                if item['plan'].get('exit_params_hash') != self.hashes['execution_hash']:
                    raise ProviderUnavailable('protection_value_identity_mismatch')

    def _provide(self, purpose, key, *, account_scope, strategy_id, now_ms, extra, permitted):
        try:
            row, value = self._projection(purpose, account_scope=account_scope, strategy_id=strategy_id,
                now_ms=now_ms, extra=extra, permitted=permitted)
            return {'committed': True, 'account_scope': self.scope['account_id'], 'archive_ref': row['ref'],
                    key: value, 'acceptance_eligible': False}
        except ProviderUnavailable as error:
            return self._unavailable(str(error))
        except Exception:
            return self._unavailable(purpose + '_projection_unavailable')

    def context_provider(self, *, account_scope, strategy_id, now_ms, **extra):
        return self._provide('context', 'context', account_scope=account_scope, strategy_id=strategy_id,
                             now_ms=now_ms, extra=extra, permitted=())

    def entry_source_provider(self, *, account_scope, strategy_id, now_ms, **extra):
        return self._provide('entry', 'source', account_scope=account_scope, strategy_id=strategy_id,
                             now_ms=now_ms, extra=extra, permitted=('event', 'context'))

    def protection_provider(self, *, account_scope, strategy_id, now_ms, **extra):
        return self._provide('protection', 'items', account_scope=account_scope, strategy_id=strategy_id,
                             now_ms=now_ms, extra=extra, permitted=('positions',))

    def completion_provider(self, *, account_scope, strategy_id, now_ms, **extra):
        try:
            coverage, coverage_value = self._projection('completion', account_scope=account_scope,
                strategy_id=strategy_id, now_ms=now_ms, extra=extra, permitted=())
            try:
                records = self.archive.completion_records()
            except Exception:
                raise ProviderUnavailable('completion_index_invalid') from None
            if (set(coverage_value['completion_refs']) != {row['ref'] for row in records}
                    or coverage_value['scan_through_ms'] != coverage['payload']['as_of_ms']):
                raise ProviderUnavailable('completion_coverage_index_mismatch')
            events, refs, sequences = [], [], []
            for indexed in records:
                row = self._read(indexed['ref'], 'completion')
                if row['sequence'] >= coverage['sequence']:
                    raise ProviderUnavailable('completion_coverage_precedes_index')
                payload = row['payload']
                if payload.get('correction_required') is not False:
                    raise ProviderUnavailable('completion_correction_requires_review')
                source_refs = payload.get('source_refs')
                if not isinstance(source_refs, list) or not source_refs or len(set(source_refs)) != len(source_refs):
                    raise ProviderUnavailable('completion_sources_missing')
                trip = payload.get('completion')
                for ref in source_refs:
                    source = self._read(ref, 'equity_reconciliation')
                    self._links(source, 'completion')
                    proof = source['payload'].get('cost_evidence')
                    if (not isinstance(proof, dict) or any(proof.get(key) != value for key, value in self.scope.items())
                            or any(proof.get(key) != value for key, value in self.hashes.items())):
                        raise ProviderUnavailable('completion_cost_identity_mismatch')
                    result = reconcile_round_trip_costs(proof).to_dict()
                    actual = [item for item in result['trips'] if item['trade_id'] == payload.get('trade_id')]
                    if len(actual) != 1 or result['complete'] is not True or actual[0] != trip:
                        raise ProviderUnavailable('completion_cost_proof_mismatch')
                if (not isinstance(trip, dict) or trip.get('complete') is not True
                        or trip.get('funding_complete') is not True or trip.get('costs_included') is not True
                        or trip.get('risk_event') != payload.get('risk_event') or not isinstance(trip.get('risk_event'), dict)
                        or not _stamp(trip['risk_event'].get('timestamp_ms'))
                        or trip['risk_event']['timestamp_ms'] > coverage_value['scan_through_ms']):
                    raise ProviderUnavailable('completion_costs_incomplete')
                events.append({**trip['risk_event'], 'complete': True, 'funding_complete': True})
                refs.append(row['ref'])
                sequences.append(row['sequence'])
            events.sort(key=lambda event: (event['timestamp_ms'], event['trade_id'], event['event_id']))
            if len({event['event_id'] for event in events}) != len(events):
                raise ProviderUnavailable('completion_event_identity_conflict')
            return {'committed': True, 'account_scope': self.scope['account_id'],
                'archive_ref': coverage['ref'],
                'completion_refs': refs, 'completion_sequence': max(sequences, default=0),
                'events': events, 'acceptance_eligible': False}
        except ProviderUnavailable as error:
            return self._unavailable(str(error))
        except Exception:
            return self._unavailable('completion_projection_unavailable')


def archive_runtime_factory(bridge):
    """返回可传 controller 的内部工厂；不安装表、不采集、不检查 HTTP 声明。"""
    if not isinstance(bridge, ArchiveProviderBridge):
        raise ValueError('internal archive bridge required')

    def create(*, account_scope, ledger, adapter, account_lock, strategy_id, acceptance_round):
        if (account_scope != bridge.scope['account_id'] or strategy_id != bridge.scope['strategy_version_id']
                or ledger.store is not bridge.archive.store or ledger.environment != bridge.scope['environment']):
            raise ValueError('runtime factory binding mismatch')
        if acceptance_round is not None:
            required = ('round_id', 'environment', 'account_scope', 'strategy_id', 'rule_hash',
                        'execution_hash', 'status', 'approval_id', 'approved_by')
            if (not isinstance(acceptance_round, dict) or any(key not in acceptance_round for key in required)
                    or any(not _approval_text(acceptance_round[key]) for key in required)
                    or acceptance_round['environment'] != bridge.scope['environment']
                    or acceptance_round['account_scope'] != account_scope
                    or acceptance_round['strategy_id'] != strategy_id
                    or acceptance_round['rule_hash'] != bridge.hashes['strategy_hash']
                    or acceptance_round['execution_hash'] != bridge.hashes['execution_hash']
                    or acceptance_round['status'] != 'active'):
                raise ValueError('acceptance binding mismatch')
        from demo_risk_reservation_store import DemoRiskReservationStore
        from demo_safety_runtime import SafetyRuntime
        reservations = DemoRiskReservationStore(ledger.store, account_scope, ledger.virtual_account_key)
        return SafetyRuntime(reservations, account_scope=account_scope, account_lock=account_lock,
            context_provider=bridge.context_provider, entry_source_provider=bridge.entry_source_provider,
            protection_facts_provider=bridge.protection_provider, completion_provider=bridge.completion_provider)

    return create
