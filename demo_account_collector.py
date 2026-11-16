"""Demo 只读采集编排；持久归档和边界真实性由内部调用者提供。

本模块没有调度线程、交易命令或数据库连接。网络发生于归档事务之外。
完整短页只证明所查窗口的结构覆盖，不能代替延迟入账和钱包差额对账。
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import logging
import re
import time

from demo_account_reconciliation import EXTERNAL_TYPES, INTERNAL_TYPES, reconcile_account_risk_evidence

LOGGER = logging.getLogger(__name__)
DAY_MS = 86_400_000
WINDOW_MS = 7 * DAY_MS
IDENTITY = re.compile(r'[A-Za-z0-9_./:@+-]{1,256}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
SCOPE_FIELDS = ('environment', 'account_id', 'stage_id')


class CollectionError(ValueError):
    """只携带稳定原因码，不记录上游异常文本。"""


def _scope(value):
    if (not isinstance(value, dict) or value.get('environment') != 'binance_demo'
            or any(not isinstance(value.get(key), str) or not IDENTITY.fullmatch(value[key])
                   or '://' in value[key] or '@' in value[key]
                   for key in SCOPE_FIELDS)):
        raise ValueError('invalid account collection scope')
    return {key: value[key] for key in SCOPE_FIELDS}


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('invalid timestamp')
    return value


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('invalid exact amount')
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError('invalid exact amount') from None
    if (not number.is_finite() or len(number.as_tuple().digits) > 96
            or abs(number.as_tuple().exponent) > 192):
        raise ValueError('invalid exact amount')
    return number


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _receipt(value, kind, scope):
    if (not isinstance(value, dict) or value.get('committed') is not True
            or value.get('kind') != kind or value.get('scope') != scope
            or not isinstance(value.get('ref'), str) or not IDENTITY.fullmatch(value['ref'])
            or any(not isinstance(value.get(key), str) or not SHA.fullmatch(value[key])
                   for key in ('sha256', 'record_sha256'))
            or type(value.get('sequence')) is not int or value['sequence'] <= 0):
        raise CollectionError('archive_commit_unverified')
    return deepcopy(value)


def _verified_projection(value, receipt, scope, resolver, proof_validator):
    """存储完整性之外，必须由内部语义验证器核验时点、覆盖和估值。

    逐个解析固定原始引用并核对record hash。archive.read仅能证明已存资料，
    不具有证明缺口不存在、午夜状态或估值规则正确的语义。
    """
    payload = value.get('payload')
    if not callable(proof_validator) or not isinstance(payload, dict):
        raise ValueError('internal semantic proof validator required')
    refs, hashes = payload.get('source_refs'), payload.get('source_record_hashes')
    if (not isinstance(refs, list) or not refs or len(refs) > 4096
            or len(set(refs)) != len(refs) or not isinstance(hashes, dict) or set(hashes) != set(refs)
            or any(not isinstance(ref, str) or not IDENTITY.fullmatch(ref) for ref in refs)):
        raise ValueError('frozen source references required')
    sources = {}
    for ref in refs:
        if ref == receipt['ref'] or not isinstance(hashes[ref], str) or not SHA.fullmatch(hashes[ref]):
            raise ValueError('invalid linked source hash')
        source = resolver(ref)
        if (not isinstance(source, dict) or source.get('scope') != scope
                or source.get('kind') not in {'account_snapshot', 'positions', 'normal_orders',
                                               'conditional_orders', 'fills', 'cashflows', 'clock'}):
            raise ValueError('linked source kind or scope mismatch')
        linked = _receipt(source.get('receipt', source), source['kind'], scope)
        if linked['ref'] != ref or linked['record_sha256'] != hashes[ref]:
            raise ValueError('linked source identity mismatch')
        sources[ref] = deepcopy(source)
    checked = proof_validator(deepcopy(value), sources)
    expected = {'verified': True, 'scope': scope, 'proof_record_sha256': receipt['record_sha256'],
                'source_record_hashes': hashes, 'method': payload.get('method'),
                'state_at_ms': payload.get('state_at_ms'),
                'valuation_policy_hash': payload.get('valuation_policy_hash')}
    if (not isinstance(checked, dict) or checked != expected
            or checked['verified'] is not True
            or not isinstance(checked['valuation_policy_hash'], str)
            or not SHA.fullmatch(checked['valuation_policy_hash'])):
        raise ValueError('semantic proof not verified for frozen facts')
    return deepcopy(payload)


def _envelope(value, path, api, params):
    if not isinstance(value, dict) or set(value) != {'data', 'request_evidence'}:
        raise CollectionError('request_evidence_unverified')
    evidence = value['request_evidence']
    if (not isinstance(evidence, dict) or evidence.get('method') != 'GET'
            or evidence.get('path') != path or evidence.get('api') != api
            or evidence.get('status') != 'ok' or evidence.get('request_params') != params
            or evidence.get('response_hash_basis') not in {'raw_http_response_bytes', 'canonical_json'}
            or not isinstance(evidence.get('response_sha256'), str)
            or not SHA.fullmatch(evidence['response_sha256'])):
        raise CollectionError('request_evidence_unverified')
    return deepcopy(value['data']), deepcopy(evidence)


class DemoAccountCollector:
    """只信内部注入的守卫适配器和已提交归档接口，不接受 HTTP 真实性旗标。"""

    def __init__(self, adapter, archive, scope, *, clock_ms=None, max_requests=4096):
        self.scope = _scope(scope)
        if type(max_requests) is not int or not 1 <= max_requests <= 100_000:
            raise ValueError('invalid request budget')
        if not callable(getattr(archive, 'append', None)):
            raise ValueError('committed archive required')
        self.adapter, self.archive = adapter, archive
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_requests = max_requests

    def _archive(self, kind, payload, evidence):
        try:
            receipt = self.archive.append(kind, deepcopy(self.scope), deepcopy(payload), deepcopy(evidence))
        except Exception as error:
            LOGGER.warning('Demo账户归档提交失败 账户=%s 阶段=%s 类型=%s 错误类型=%s；停止新增风险',
                           self.scope['account_id'], self.scope['stage_id'], kind, type(error).__name__)
            raise CollectionError('archive_commit_failed') from None
        return _receipt(receipt, kind, self.scope)

    def collect_income(self, *, since_ms, until_ms, limit=1000):
        """查询所有类型/币种流水，含端点分窗；单毫秒仍满页时拒绝完整结论。

        官方接口未提供已审查的 page 游标。每个完整叶窗口仅保存一张严格短页；
        满页父响应仍归档，但不参与叶流水求和。没有网络自动重试。
        """
        start, end = _stamp(since_ms), _stamp(until_ms)
        if end < start or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid income window')
        result = {**self.scope, 'start_ms': start, 'end_ms': end, 'complete': False,
                  'income_scans': [], 'rows': [], 'source_refs': [], 'reasons': [],
                  'acceptance_eligible': False, 'settlement_finality_verified': False}
        # 先用整数算术限制计划规模，避免恶意长区间在网络前创建海量分块。
        if (end - start) // WINDOW_MS + 1 > self.max_requests:
            result['reasons'] = ['income_request_budget_exhausted']
            return result
        stack, cursor = [], start
        while cursor <= end:
            finish = min(end, cursor + WINDOW_MS - 1)
            stack.append((cursor, finish))
            cursor = finish + 1
        stack.reverse()
        seen, observed, requests = {}, {}, 0

        def deny(reason):
            if reason not in result['reasons']:
                result['reasons'].append(reason)

        while stack:
            if requests >= self.max_requests:
                deny('income_request_budget_exhausted')
                break
            left, right = stack.pop()
            requests += 1
            try:
                response = self.adapter.fetch_income_window(since_ms=left, until_ms=right,
                                                            limit=limit, with_evidence=True)
                rows, evidence = _envelope(response, 'income', 'fapiPrivate',
                                           {'startTime': left, 'endTime': right, 'limit': limit})
                if not isinstance(rows, list) or len(rows) > limit:
                    raise CollectionError('income_rows_invalid')
                response_seen = {}
                for item in rows:
                    if (not isinstance(item, dict) or not isinstance(item.get('incomeType'), str)
                            or not item['incomeType'] or not isinstance(item.get('asset'), str)
                            or not item['asset'] or type(item.get('tranId')) not in (str, int)
                            or not str(item['tranId']).strip()
                            or not left <= _stamp(item.get('time')) <= right):
                        raise CollectionError('income_rows_invalid')
                    _decimal(item.get('income'))
                    key = item['incomeType'], str(item['tranId'])
                    fingerprint = _digest(item)
                    if key in response_seen:
                        deny('income_transaction_duplicate' if response_seen[key] == fingerprint
                             else 'income_transaction_conflict')
                    response_seen[key] = fingerprint
                    if key in observed and observed[key] != fingerprint:
                        deny('income_transaction_conflict')
                    observed[key] = fingerprint
                receipt = self._archive('cashflows', {'start_ms': left, 'end_ms': right,
                    'limit': limit, 'rows': rows, 'all_income_types': True,
                    'window_saturated': len(rows) == limit}, evidence)
            except CollectionError as error:
                deny(str(error))
                break
            except Exception as error:
                LOGGER.warning('Demo全类型资金流水采集失败 账户=%s 窗口=%s..%s 错误类型=%s；保留缺口',
                               self.scope['account_id'], left, right, type(error).__name__)
                deny('income_capture_failed')
                break
            result['source_refs'].append(receipt['ref'])
            if len(rows) == limit:
                if left == right:
                    deny('income_single_millisecond_saturated')
                    break
                middle = (left + right) // 2
                stack.extend(((middle + 1, right), (left, middle)))
                continue
            result['income_scans'].append({**self.scope, 'source': 'exchange_income_history',
                'all_income_types': True, 'start_ms': left, 'end_ms': right, 'limit': limit,
                'pages': [{'page': 1, 'start_ms': left, 'end_ms': right,
                           'source_ref': receipt['ref'], 'has_more': False, 'rows': rows}]})
            for item in rows:
                key = item['incomeType'], str(item['tranId'])
                fingerprint = _digest(item)
                if key in seen:
                    deny('income_transaction_duplicate' if seen[key] == fingerprint else 'income_transaction_conflict')
                    continue
                seen[key] = fingerprint
                result['rows'].append(deepcopy(item))
                if item['incomeType'] not in EXTERNAL_TYPES | INTERNAL_TYPES:
                    deny('income_type_unclassified')
                if item['asset'] != 'USDT':
                    deny('income_asset_unsupported')
        leaves_cover_window = sum(scan['end_ms'] - scan['start_ms'] + 1
                                  for scan in result['income_scans']) == end - start + 1
        if leaves_cover_window and any(seen.get(key) != fingerprint for key, fingerprint in observed.items()):
            deny('income_window_changed_during_capture')
        result['complete'] = not result['reasons'] and not stack and leaves_cover_window
        LOGGER.info('Demo资金流水窗口归档 账户=%s 阶段=%s 窗口=%s..%s 请求数=%s 叶窗口=%s '
                    '流水数=%s 结构完整=%s 原因=%s；仍须钱包和延迟入账对账',
                    self.scope['account_id'], self.scope['stage_id'], start, end, requests,
                    len(result['income_scans']), len(result['rows']), result['complete'], result['reasons'])
        return result

    def capture_account(self):
        """归档账户、模式和订单读数；跨多个请求，不声称原子快照或精确时点。"""
        result = {**self.scope, 'source_refs': {}, 'reasons': [], 'capture_complete': False,
                  'transport_evidence_complete': False, 'reconciled': False, 'as_of_ms': None,
                  'boundary_verified': False, 'acceptance_eligible': False}
        clocks = {}

        def capture(name, kind, function, path=None, api=None):
            try:
                response = function()
                if path is None:
                    data, evidence = response, {}
                else:
                    data, evidence = _envelope(response, path, api, {})
                if name.startswith('clock_'):
                    _stamp(data)
                    clocks[name] = data
                if name == 'mode' and type(data) is not bool:
                    raise ValueError('multi asset mode unknown')
                if name in {'normal_orders', 'conditional_orders'} and not isinstance(data, list):
                    raise ValueError('order observation must be list')
                if name in {'normal_orders', 'conditional_orders'}:
                    data = deepcopy(data)
                    for item in data:
                        if not isinstance(item, dict):
                            raise ValueError('malformed normalized order')
                        latency = item.get('request_latency_ms')
                        if type(latency) is float:
                            # 仅转换适配器本机计时metadata；金额float仍交归档严格拒绝。
                            value = _decimal(str(latency))
                            if value < 0:
                                raise ValueError('invalid request latency')
                            item['request_latency_ms'] = format(value, 'f')
                receipt = self._archive(kind, {'capture_component': name, 'data': data,
                    'local_observed_ms': _stamp(self.clock_ms()),
                    'point_time_verified': False, 'transport_evidence_present': bool(evidence)}, evidence)
                result['source_refs'][name] = receipt['ref']
                return data
            except CollectionError as error:
                result['reasons'].append(str(error))
            except Exception as error:
                LOGGER.warning('Demo账户读数采集失败 账户=%s 组件=%s 错误类型=%s；不能把缺失记为空',
                               self.scope['account_id'], name, type(error).__name__)
                result['reasons'].append(name + '_capture_failed')
            return None

        capture('clock_before', 'clock', lambda: self.adapter.fetch_server_time(with_evidence=True),
                'time', 'fapiPublic')
        account = capture('account_snapshot', 'account_snapshot',
                          lambda: self.adapter.fetch_account_snapshot(with_evidence=True), 'account', 'fapiPrivateV3')
        if isinstance(account, dict) and isinstance(account.get('positions'), list):
            # V3原文只列有仓位/挂单的symbol。派生记录引用同一次账户原文，不冒充独立REST。
            capture('positions', 'positions', lambda: {'positions': account['positions'],
                    'source_ref': result['source_refs']['account_snapshot'], 'derived_from_account': True})
        else:
            result['reasons'].append('positions_capture_failed')
        capture('mode', 'account_snapshot', lambda: self.adapter.fetch_multi_assets_mode(with_evidence=True),
                'multiAssetsMargin', 'fapiPrivate')
        # 现有订单接口没有原子请求证据封套。保存观察仍有价值，但明确保持传输证据不完整。
        capture('normal_orders', 'normal_orders', self.adapter.fetch_open_orders)
        capture('conditional_orders', 'conditional_orders', self.adapter.fetch_open_conditional_orders)
        capture('clock_after', 'clock', lambda: self.adapter.fetch_server_time(with_evidence=True),
                'time', 'fapiPublic')
        if len(clocks) == 2 and clocks['clock_after'] < clocks['clock_before']:
            result['reasons'].append('exchange_clock_regressed')
        result['capture_complete'] = not result['reasons']
        result['clock_bracket'] = deepcopy(clocks)
        LOGGER.info('Demo账户多请求归档 账户=%s 阶段=%s 已提交组件=%s 采集完成=%s 原因=%s；'
                    '未授予原子时点、午夜或新增风险资格', self.scope['account_id'], self.scope['stage_id'],
                    sorted(result['source_refs']), result['capture_complete'], result['reasons'])
        return result

    def build_risk_evidence(self, *, stage_start_ms, stage_opening_ref, day_opening_ref,
                            current_ref, income, resolver, now_ms, max_snapshot_age_ms, proof_validator=None):
        """内部已验证归档投影供旧reducer使用；普通REST采集结果不能直接进入。

        当前reducer仅接受exchange_account_snapshot。重放边界虽可绑定槽位，
        仍需另行评审/版本化来源合同，此处不会把重放结果冒称交易所原始快照。
        """
        incomplete = {'data_complete': False, 'funding_fee_complete': False,
                      'margin_balance': None, 'risk_day_opening': None,
                      'reasons': ['archived_risk_projection_unverified'],
                      'acceptance_eligible': False}
        try:
            start, now, max_age = _stamp(stage_start_ms), _stamp(now_ms), _stamp(max_snapshot_age_ms)
            if (not callable(resolver) or not isinstance(income, dict)
                    or any(income.get(key) != self.scope[key] for key in SCOPE_FIELDS)
                    or income.get('complete') is not True or income.get('start_ms') != start + 1
                    or not isinstance(income.get('income_scans'), list)):
                raise ValueError('trusted income projection required')

            def snapshot(ref, kind):
                if not isinstance(ref, str) or not IDENTITY.fullmatch(ref):
                    raise ValueError('archive reference required')
                value = resolver(ref)
                if not isinstance(value, dict) or value.get('kind') != kind or value.get('scope') != self.scope:
                    raise ValueError('archive scope mismatch')
                receipt = _receipt(value.get('receipt', value), kind, self.scope)
                payload = _verified_projection(value, receipt, self.scope, resolver, proof_validator)
                if (receipt['ref'] != ref or not isinstance(payload, dict)
                        or payload.get('source') != 'exchange_account_snapshot'
                        or payload.get('method') != 'direct_exchange_asof'
                        or payload.get('coverage_complete') is not True
                        or type(payload.get('timestamp_ms')) is not int
                        or payload.get('state_at_ms') != payload['timestamp_ms']):
                    raise ValueError('point-time proof unverified')
                # content-addressed正文不能包含其未知的最终自ref。归档读完并
                # 验证底层来源后，只向传给reducer的副本注入这条投影的真实ref。
                payload['source_ref'] = receipt['ref']
                return deepcopy(payload)

            stage = snapshot(stage_opening_ref, 'equity_reconciliation')
            opening = snapshot(day_opening_ref, 'boundary')
            current = snapshot(current_ref, 'equity_reconciliation')
            for scan in income['income_scans']:
                if len(scan.get('pages', [])) != 1:
                    raise ValueError('collector leaf scan required')
                page = scan['pages'][0]
                archived = resolver(page['source_ref'])
                receipt = _receipt(archived.get('receipt', archived), 'cashflows', self.scope)
                body = archived.get('payload')
                if (receipt['ref'] != page['source_ref'] or archived.get('scope') != self.scope
                        or not isinstance(body, dict) or body.get('all_income_types') is not True
                        or body.get('window_saturated') is not False
                        or body.get('start_ms') != scan['start_ms']
                        or body.get('end_ms') != scan['end_ms']
                        or body.get('limit') != scan['limit'] or body.get('rows') != page['rows']):
                    raise ValueError('income archive mismatch')
            if income.get('end_ms') != current['timestamp_ms']:
                raise ValueError('income current window mismatch')
            evidence = {**self.scope, 'stage_start_ms': start, 'stage_opening': stage,
                        'day_opening': opening, 'current': current,
                        'income_scans': deepcopy(income['income_scans'])}
            result = reconcile_account_risk_evidence(evidence, now_ms=now, max_snapshot_age_ms=max_age)
            result['acceptance_eligible'] = False
            return result
        except Exception as error:
            LOGGER.warning('Demo归档风险投影未验证 账户=%s 阶段=%s 错误类型=%s；禁止新增风险',
                           self.scope['account_id'], self.scope['stage_id'], type(error).__name__)
            return incomplete


class DailyBoundarySlot:
    """边界槽位纯状态机；resolver必须是内部可信归档/重放验证器。

    resolve成功不是来源真实性的密码学证明；持久化及覆盖证明在调用者。
    此对象不接受HTTP正文作为证明，也不由最近REST读数推断午夜。
    """

    def __init__(self, scope, midnight_ms):
        self.scope = _scope(scope)
        self.midnight_ms = _stamp(midnight_ms)
        if (midnight_ms + 28_800_000) % DAY_MS:
            raise ValueError('exact Beijing midnight required')
        self._proof = None
        self._conflict = False

    def status(self, *, now_ms):
        now = _stamp(now_ms)
        status = ('conflict' if self._conflict else 'verified' if self._proof else
                  'missing' if now >= self.midnight_ms else 'pending')
        return {**self.scope, 'boundary_ms': self.midnight_ms, 'status': status,
                'entries_allowed': status == 'verified', 'boundary_only': True,
                'acceptance_eligible': False, 'proof_ref': self._proof['ref'] if self._proof else None}

    def fulfill(self, proof_ref, *, resolver, proof_validator=None):
        if not isinstance(proof_ref, str) or not IDENTITY.fullmatch(proof_ref) or not callable(resolver):
            raise ValueError('trusted boundary reference required')
        try:
            resolved = resolver(proof_ref)
            if (not isinstance(resolved, dict) or resolved.get('kind') != 'boundary'
                    or resolved.get('scope') != self.scope):
                raise ValueError('boundary identity mismatch')
            # DemoEvidenceStore.read把回执字段平铺在原文顶层；也接受显式嵌套的
            # 内部resolver合同，两者仍必须经过相同的提交/身份检查。
            receipt = _receipt(resolved.get('receipt', resolved), 'boundary', self.scope)
            payload = _verified_projection(resolved, receipt, self.scope, resolver, proof_validator)
            if (receipt['ref'] != proof_ref or not isinstance(payload, dict)
                    or payload.get('boundary_ms') != self.midnight_ms
                    or payload.get('state_at_ms') != self.midnight_ms
                    or payload.get('method') not in {'direct_exchange_asof', 'replayed_boundary_projection'}
                    or payload.get('coverage_complete') is not True
                    or not isinstance(payload.get('valuation_policy_hash'), str)
                    or not SHA.fullmatch(payload['valuation_policy_hash'])
                    or not isinstance(payload.get('source_refs'), list) or not payload['source_refs']
                    or any(not isinstance(ref, str) or not IDENTITY.fullmatch(ref) for ref in payload['source_refs'])):
                raise ValueError('boundary proof incomplete')
            with localcontext() as ctx:
                ctx.prec = 1024
                wallet = _decimal(payload.get('wallet_balance_usdt'))
                unrealized = _decimal(payload.get('unrealized_pnl_usdt'))
                if _decimal(payload.get('net_equity_usdt')) != wallet + unrealized:
                    raise ValueError('boundary equity sum mismatch')
            proof = {'ref': proof_ref, 'sha256': _digest(resolved)}
        except Exception:
            raise ValueError('boundary proof unverified') from None
        if self._proof is not None and self._proof != proof:
            self._conflict = True
            raise ValueError('immutable boundary conflict')
        if self._conflict:
            raise ValueError('boundary conflict requires reconciliation')
        self._proof = proof
        LOGGER.info('Demo午夜边界内部证明已绑定 账户=%s 阶段=%s 边界毫秒=%s 归档=%s；仅解决边界门禁',
                    self.scope['account_id'], self.scope['stage_id'], self.midnight_ms, proof_ref)
        return self.status(now_ms=self.midnight_ms)
