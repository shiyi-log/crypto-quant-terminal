"""从已封存原文精确复算账户观察值；不证明历史时点、完整覆盖或交易资格。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re

from demo_account_collector import _digest, _receipt
from demo_collection_run import DemoCollectionRun, FrozenCollectionBinding, SCHEMA as COLLECTION_SCHEMA
from demo_evidence_store import DemoEvidenceStore

LOGGER = logging.getLogger(__name__)
SCHEMA = 'demo-equity-observation-v1'
DECIMAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:[.][0-9]+)?\Z')
SYMBOL = re.compile(r'[A-Z0-9]{1,40}USDT\Z')
FIXED_POLICY = {'schema': 'demo-equity-observation-policy-v1', 'account_api': 'fapiPrivateV3',
    'account_path': 'account', 'asset': 'USDT', 'multi_assets_margin': False,
    'equation': 'wallet_plus_unrealized_v1'}
LIMITS = {'money_scale': (0, 24), 'max_integer_digits': (1, 18),
          'max_assets': (1, 256), 'max_positions': (1, 5000)}
# 对应守卫适配器保留的 V3 account 字段；未知扩展需另行冻结政策版本。
ACCOUNT_AMOUNTS = frozenset({'totalInitialMargin', 'totalMaintMargin', 'totalWalletBalance',
    'totalUnrealizedProfit', 'totalMarginBalance', 'totalPositionInitialMargin',
    'totalOpenOrderInitialMargin', 'totalCrossWalletBalance', 'totalCrossUnPnl',
    'availableBalance', 'maxWithdrawAmount'})
ACCOUNT_FIELDS = ACCOUNT_AMOUNTS | {'feeTier', 'canTrade', 'canDeposit', 'canWithdraw',
    'updateTime', 'assets', 'positions'}
ASSET_AMOUNTS = frozenset({'walletBalance', 'unrealizedProfit', 'marginBalance', 'maintMargin',
    'initialMargin', 'positionInitialMargin', 'openOrderInitialMargin', 'crossWalletBalance',
    'crossUnPnl', 'availableBalance', 'maxWithdrawAmount'})
ASSET_FIELDS = ASSET_AMOUNTS | {'asset', 'marginAvailable', 'updateTime'}
POSITION_AMOUNTS = frozenset({'initialMargin', 'maintMargin', 'unrealizedProfit',
    'positionInitialMargin', 'openOrderInitialMargin', 'leverage', 'entryPrice', 'breakEvenPrice',
    'maxNotional', 'positionAmt', 'notional', 'isolatedWallet', 'bidNotional', 'askNotional', 'isolatedMargin'})
POSITION_FIELDS = POSITION_AMOUNTS | {'symbol', 'positionSide', 'isolated', 'marginType', 'updateTime'}


class ObservationError(ValueError):
    """仅含固定原因码，不回显归档、连接或上游异常文本。"""


def _require(condition, reason):
    if not condition:
        raise ObservationError(reason)


def _canonical_binding(binding):
    if type(binding) is not FrozenCollectionBinding:
        raise ValueError('观察冻结身份类型无效')
    scope_items, hash_items = binding._scope_items, binding._hash_items
    if (type(scope_items) is not tuple or type(hash_items) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2
                   or any(type(item) is not str for item in pair) for pair in scope_items + hash_items)):
        raise ValueError('观察冻结身份原语无效')
    return FrozenCollectionBinding(dict(scope_items), dict(hash_items))


@dataclass(frozen=True, init=False)
class FrozenObservationPolicy:
    """显式冻结数值及字段合同，绑定真实运行摘要，公开字典均为副本。"""
    _binding_sha256: str
    _spec_items: tuple

    def __init__(self, binding, spec):
        binding = _canonical_binding(binding)
        if (type(spec) is not dict or any(type(key) is not str for key in spec)
                or set(spec) != set(FIXED_POLICY) | set(LIMITS)
                or any(type(spec[key]) is not type(value) or spec[key] != value
                       for key, value in FIXED_POLICY.items())
                or any(type(spec[key]) is not int or not lower <= spec[key] <= upper
                       for key, (lower, upper) in LIMITS.items())):
            raise ValueError('观察冻结政策无效')
        object.__setattr__(self, '_binding_sha256', binding.sha256)
        object.__setattr__(self, '_spec_items', tuple(sorted(spec.items())))

    @property
    def spec(self):
        return dict(self._spec_items)

    @property
    def binding_sha256(self):
        return self._binding_sha256

    @property
    def sha256(self):
        return _digest({'binding_sha256': self.binding_sha256, 'spec': self.spec})


class DemoEquityObservation:
    """内部归档观察生产器；构造和计算均不创建适配器、线程或采集任务。"""

    def __setattr__(self, name, value):
        if name in {'archive', '_scope_items', '_hash_items', '_policy_items'} and hasattr(self, name):
            raise AttributeError('观察配置不可变')
        object.__setattr__(self, name, value)

    def __init__(self, archive, binding, policy):
        binding = _canonical_binding(binding)
        if (not isinstance(archive, DemoEvidenceStore) or type(policy) is not FrozenObservationPolicy
                or type(policy._binding_sha256) is not str or policy._binding_sha256 != binding.sha256
                or (archive.environment, archive.account_id, archive.stage_id)
                != tuple(binding.scope[key] for key in ('environment', 'account_id', 'stage_id'))):
            raise ValueError('观察内部归档与冻结身份不匹配')
        if (type(policy._spec_items) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2 for pair in policy._spec_items)):
            raise ValueError('观察冻结政策原语无效')
        checked = FrozenObservationPolicy(binding, dict(policy._spec_items))
        self.archive = archive
        self._scope_items, self._hash_items = binding._scope_items, binding._hash_items
        self._policy_items = checked._spec_items

    @property
    def binding(self):
        return FrozenCollectionBinding(dict(self._scope_items), dict(self._hash_items))

    @property
    def policy(self):
        return FrozenObservationPolicy(self.binding, dict(self._policy_items))

    def _load(self, manifest_ref):
        try:
            end = self.archive.read(manifest_ref)
        except Exception:
            raise ObservationError('observation_archive_unverified') from None
        payload = end['payload']
        _require(type(payload) is dict, 'observation_capture_manifest_required')
        _require(end['scope'] == self.binding.scope
            and payload.get('identity_hashes') == self.binding.identity_hashes
            and payload.get('binding_sha256') == self.binding.sha256, 'observation_binding_mismatch')
        _require(end['kind'] == 'equity_reconciliation' and payload.get('schema') == COLLECTION_SCHEMA
            and payload.get('event') == 'end', 'observation_capture_manifest_required')
        try:
            start = self.archive.read(payload['start_ref'])
            plan = start['payload']['plan']
            _require(plan.get('operation') == 'capture_account', 'observation_capture_manifest_required')
            run = DemoCollectionRun(object(), self.archive, self.binding,
                collection_id=payload['collection_id'], max_requests=plan['max_requests'])
            result = run.replay_completed()
            _require(result['manifest_ref'] == manifest_ref, 'observation_archive_unverified')
            rows = {name: self.archive.read(ref) for name, ref in result['source_refs'].items()}
        except ObservationError:
            raise
        except Exception:
            raise ObservationError('observation_archive_unverified') from None
        return result, start, end, rows

    def _units(self, value):
        spec = self.policy.spec
        _require(type(value) is str and len(value) <= 44 and DECIMAL.fullmatch(value),
                 'observation_amount_invalid')
        negative = value.startswith('-')
        whole, _, fraction = value.lstrip('-').partition('.')
        _require(len(whole) <= spec['max_integer_digits'] and len(fraction) <= spec['money_scale'],
                 'observation_amount_invalid')
        units = int(whole + fraction.ljust(spec['money_scale'], '0'))
        return -units if negative else units

    def _money(self, units):
        scale = self.policy.spec['money_scale']
        digits = str(abs(units)).rjust(scale + 1, '0')
        text = (digits[:-scale] + '.' + digits[-scale:]).rstrip('0').rstrip('.') if scale else digits
        return ('-' if units < 0 else '') + text

    def _triple(self, row, fields, missing):
        _require(type(row) is dict and all(key in row for key in fields), missing)
        return tuple(self._units(row[key]) for key in fields)

    @staticmethod
    def _typed_fields(row):
        for field in ('canTrade', 'canDeposit', 'canWithdraw', 'marginAvailable', 'isolated'):
            if field in row:
                _require(type(row[field]) is bool, 'observation_field_type_invalid')
        for field in ('updateTime', 'feeTier'):
            if field in row:
                _require(type(row[field]) is int and 0 <= row[field] < 2**63,
                         'observation_field_type_invalid')
        if 'marginType' in row:
            _require(row['marginType'] in ('isolated', 'cross', 'ISOLATED', 'CROSSED'),
                     'observation_field_type_invalid')

    def _evaluate(self, rows):
        raw, mode = rows['account_snapshot']['payload']['data'], rows['mode']['payload']['data']
        _require(mode is False, 'multi_assets_mode_unsupported')
        _require(set(raw) <= ACCOUNT_FIELDS, 'account_field_unsupported')
        self._typed_fields(raw)
        for field in ACCOUNT_AMOUNTS & raw.keys():
            self._units(raw[field])
        wallet, upnl, equity = self._triple(raw,
            ('totalWalletBalance', 'totalUnrealizedProfit', 'totalMarginBalance'), 'account_amount_missing')
        _require(wallet + upnl == equity, 'account_equity_mismatch')
        assets = raw.get('assets')
        _require(type(assets) is list, 'assets_missing')
        _require(len(assets) <= self.policy.spec['max_assets'], 'observation_resource_limit')
        seen_assets, usdt = set(), None
        for asset in assets:
            _require(type(asset) is dict and isinstance(asset.get('asset'), str)
                and re.fullmatch(r'[A-Z0-9]{1,20}', asset['asset']), 'asset_identity_invalid')
            name = asset['asset']
            _require(name not in seen_assets, 'asset_duplicate')
            seen_assets.add(name)
            _require(set(asset) <= ASSET_FIELDS, 'asset_field_unsupported')
            self._typed_fields(asset)
            optional_amounts = [self._units(asset[field]) for field in ASSET_AMOUNTS & asset.keys()]
            amounts = self._triple(asset, ('walletBalance', 'unrealizedProfit', 'marginBalance'),
                                   'asset_amount_missing')
            _require(amounts[0] + amounts[1] == amounts[2], 'asset_equity_mismatch')
            if name == 'USDT':
                usdt = amounts
            else:
                _require(all(value == 0 for value in optional_amounts), 'non_usdt_asset_nonzero')
        _require(usdt is not None, 'usdt_asset_missing')
        _require(usdt == (wallet, upnl, equity), 'account_asset_mismatch')
        positions = raw.get('positions')
        _require(type(positions) is list, 'positions_missing')
        _require(len(positions) <= self.policy.spec['max_positions'], 'observation_resource_limit')
        seen_positions, total_upnl, side_modes = set(), 0, set()
        for position in positions:
            _require(type(position) is dict and isinstance(position.get('symbol'), str)
                and SYMBOL.fullmatch(position['symbol']), 'position_symbol_unsupported')
            _require(position.get('positionSide') in ('BOTH', 'LONG', 'SHORT'), 'position_side_invalid')
            side_modes.add('oneway' if position['positionSide'] == 'BOTH' else 'hedge')
            _require(len(side_modes) <= 1, 'position_mode_mixed')
            _require(set(position) <= POSITION_FIELDS, 'position_field_unsupported')
            self._typed_fields(position)
            for field in POSITION_AMOUNTS & position.keys():
                self._units(position[field])
            identity = (position['symbol'], position['positionSide'])
            _require(identity not in seen_positions, 'position_duplicate')
            seen_positions.add(identity)
            _require('positionAmt' in position and 'unrealizedProfit' in position, 'position_amount_missing')
            quantity, profit = self._units(position['positionAmt']), self._units(position['unrealizedProfit'])
            _require(position['positionSide'] != 'LONG' or quantity >= 0, 'position_side_quantity_mismatch')
            _require(position['positionSide'] != 'SHORT' or quantity <= 0, 'position_side_quantity_mismatch')
            _require(quantity != 0 or profit == 0, 'flat_position_unrealized_nonzero')
            total_upnl += profit
        _require(total_upnl == upnl, 'positions_unrealized_mismatch')
        return {'wallet_balance_usdt': self._money(wallet), 'unrealized_pnl_usdt': self._money(upnl),
                'net_equity_usdt': self._money(equity)}

    def calculate(self, manifest_ref):
        """读取真实已封存采集运行并复算观察值；此方法不写归档。"""
        result, start, end, rows = self._load(manifest_ref)
        sources = {'start_manifest': start, 'manifest': end, **rows}
        value = {'schema': SCHEMA, 'scope': self.binding.scope,
            'identity_hashes': self.binding.identity_hashes, 'binding_sha256': self.binding.sha256,
            'policy_sha256': self.policy.sha256, 'policy': self.policy.spec,
            'source_collection_id': result['collection_id'],
            'source_refs': {name: row['ref'] for name, row in sources.items()},
            'source_record_hashes': {row['ref']: row['record_sha256'] for row in sources.values()},
            'clock_bracket': deepcopy(result.get('clock_bracket', {})),
            'local_observed_interval_ms': None, 'request_intervals': {},
            'amounts': None, 'positions': [], 'assets': [], 'observed_multi_assets_margin': None,
            'calculation_status': 'not_estimable', 'response_consistent': False,
            'reason_codes': [], 'as_of': None, 'state_at_ms': None,
            'data_complete': False, 'reconciled': False, 'funding_finality_verified': False,
            'boundary_verified': False, 'acceptance_eligible': False, 'production_authorized': False}
        try:
            _require(result.get('collection_status') == 'completed'
                and result.get('capture_complete') is True
                and result.get('transport_evidence_complete') is True, 'observation_capture_incomplete')
            times = []
            for name, row in rows.items():
                stamp = row['payload']['local_observed_ms']
                _require(type(stamp) is int and stamp >= 0, 'observation_time_invalid')
                times.append(stamp)
                meta = row['request_evidence']
                if name != 'positions':
                    first, last = [], []
                    for key in ('started_at_utc', 'received_at_utc'):
                        text = meta.get(key)
                        _require(isinstance(text, str), 'observation_time_invalid')
                        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
                        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'observation_time_invalid')
                        first.append(parsed.astimezone(timezone.utc))
                        last.append(text)
                    _require(first[0] <= first[1], 'observation_time_invalid')
                    value['request_intervals'][name] = {'started_at_utc': last[0], 'received_at_utc': last[1]}
            value['local_observed_interval_ms'] = {'first': min(times), 'last': max(times)}
            raw = rows['account_snapshot']['payload']['data']
            _require(type(raw) is dict, 'account_snapshot_invalid')
            value['observed_multi_assets_margin'] = rows['mode']['payload']['data']
            # 原文保留在归档；失败时不复制未验证或可含任意文字的账户正文。
            value['amounts'] = self._evaluate(rows)
            value['assets'], value['positions'] = deepcopy(raw['assets']), deepcopy(raw['positions'])
            value['calculation_status'], value['response_consistent'] = 'computed', True
        except ObservationError as error:
            value['reason_codes'] = [str(error)]
        except (KeyError, TypeError, ValueError, OverflowError):
            value['reason_codes'] = ['observation_contract_invalid']
        LOGGER.info('Demo账户观察复算完成 计算状态=%s 来源数=%s 资产数=%s 仓位数=%s 原因=%s；未验证时点与最终性',
            value['calculation_status'], len(sources), len(value['assets']), len(value['positions']), value['reason_codes'])
        return value

    def persist(self, manifest_ref):
        """只追加独立观察产物，返回实际提交回执；不是安全 Provider 或验收投影。"""
        value = self.calculate(manifest_ref)
        stable = 'equity-observation:' + _digest(value)
        try:
            receipt = self.archive.append('equity_reconciliation', self.binding.scope, value, {}, stable_key=stable)
            receipt = _receipt(receipt, 'equity_reconciliation', self.binding.scope)
            stored = self.archive.read(receipt['ref'])
            _require(stored['payload'] == value
                and all(stored[key] == receipt[key] for key in
                        ('ref', 'sha256', 'record_sha256', 'sequence', 'committed', 'kind', 'scope')),
                'observation_commit_failed')
        except Exception:
            LOGGER.warning('Demo账户观察提交失败 原因=observation_commit_failed；不签发回执')
            raise ObservationError('observation_commit_failed') from None
        LOGGER.info('Demo账户观察产物已提交 计算状态=%s；未授予交易与验收资格', value['calculation_status'])
        return receipt
