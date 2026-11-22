"""显式内部组合根驱动的前台 Demo 保护进程；默认不启动、不取凭据。"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time

from demo_command_store import DemoCommandStore, FrozenCommandBinding, _hash, _id, _stamp, SHA
from demo_evidence_store import DemoEvidenceStore
from demo_runner_controller import DemoRunnerController

LOGGER = logging.getLogger(__name__)
FALSE_FLAGS = {'execution_complete': False, 'source_authenticated': False,
               'acceptance_eligible': False, 'production_authorized': False}


@dataclass(frozen=True, init=False)
class FrozenWriterTarget:
    """完整运行身份与既有会话的交叉核对值；结构一致不认证来源或批准。"""
    binding: FrozenCommandBinding
    strategy_id: str
    rule_hash: str
    execution_hash: str
    demo_policy_hash: str

    def __init__(self, binding, *, strategy_id, rule_hash, execution_hash, demo_policy_hash):
        if type(binding) is not FrozenCommandBinding:
            raise ValueError('complete frozen writer binding required')
        body = binding.to_dict()
        if (any(type(value) is not str for value in (body['binding_id'], body['namespace'],
                *body['scope'].values(), *body['identity_hashes'].values()))):
            raise ValueError('plain immutable writer identities required')
        checked = FrozenCommandBinding(body['binding_id'], body['scope'], body['identity_hashes'], body['namespace'])
        if checked.to_dict() != body or checked.sha256 != binding.sha256:
            raise ValueError('frozen writer binding integrity failed')
        if type(strategy_id) is not str:
            raise ValueError('plain writer strategy identity required')
        _id(strategy_id)
        for value in (rule_hash, execution_hash, demo_policy_hash):
            if type(value) is not str or not SHA.fullmatch(value):
                raise ValueError('frozen legacy writer hashes required')
        object.__setattr__(self, 'binding', checked)
        for key, value in (('strategy_id', strategy_id), ('rule_hash', rule_hash),
                           ('execution_hash', execution_hash), ('demo_policy_hash', demo_policy_hash)):
            object.__setattr__(self, key, value)

    def to_dict(self):
        return {'binding': self.binding.to_dict(), 'strategy_id': self.strategy_id,
                'rule_hash': self.rule_hash, 'execution_hash': self.execution_hash,
                'demo_policy_hash': self.demo_policy_hash}


def _command_ids(values):
    if not isinstance(values, (tuple, list)) or len(values) > 64:
        raise ValueError('bounded internal writer command ids required')
    checked = tuple(_id(value) for value in values)
    if len(set(checked)) != len(checked):
        raise ValueError('duplicate writer command id')
    return checked


class DemoWriterDriver:
    """持内部能力同步运行；处理票据与普通保护分别记录，不重复派发未知动作。"""
    def __setattr__(self, name, value):
        if name in {'controller', 'commands', 'archive', 'target', 'clock_ms'} and hasattr(self, name):
            raise AttributeError('frozen writer dependencies')
        object.__setattr__(self, name, value)

    def __init__(self, controller, command_store, archive, target, *, clock_ms=None):
        if (not isinstance(controller, DemoRunnerController)
                or not isinstance(command_store, DemoCommandStore)
                or not isinstance(archive, DemoEvidenceStore)
                or type(target) is not FrozenWriterTarget):
            raise ValueError('internal writer dependencies required')
        checked = FrozenWriterTarget(target.binding, strategy_id=target.strategy_id,
            rule_hash=target.rule_hash, execution_hash=target.execution_hash,
            demo_policy_hash=target.demo_policy_hash)
        scope = checked.binding.to_dict()['scope']
        if (controller.driver_mode != 'external_paused' or command_store.account_id != scope['account_id']
                or archive.account_id != scope['account_id'] or archive.stage_id != scope['stage_id']
                or archive.environment != scope['environment'] or archive.store is not command_store.store
                or command_store.environment != scope['environment']
                or controller.data_store is not command_store.store
                or controller.demo_store.store is not command_store.store
                or controller.demo_store.environment != scope['environment']
                or controller.demo_store.virtual_account_key != checked.binding.namespace
                or controller.risk_account_scope != scope['account_id']):
            raise ValueError('writer dependency identity mismatch')
        self.controller, self.commands, self.archive, self.target = controller, command_store, archive, checked
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._capability = None
        self._stop_requested = False
        self._cycle_lock = threading.Lock()

    def activate(self):
        started = time.monotonic()
        LOGGER.info('Demo 前台 writer 激活开始 driver_mode=%s；新增开仓保持关闭',
                    self.controller.driver_mode)
        if not self._cycle_lock.acquire(blocking=False):
            LOGGER.warning('Demo 前台 writer 激活拒绝 原因码=writer_operation_active')
            raise ValueError('writer operation already active')
        try:
            if self._capability is not None:
                self.controller.assert_external(self._capability)
                LOGGER.info('Demo 前台 writer 激活复用现有能力 耗时毫秒=%d',
                            int((time.monotonic() - started) * 1000))
                return
            self._capability = self.controller.activate_external(self.target, command_store=self.commands)
            self.controller.assert_external(self._capability)
        except Exception as exc:
            LOGGER.error('Demo 前台 writer 激活失败 异常类型=%s 耗时毫秒=%d；保留安全门禁',
                         type(exc).__name__, int((time.monotonic() - started) * 1000))
            raise
        finally:
            self._cycle_lock.release()
        LOGGER.info('Demo 前台保护已激活 耗时毫秒=%d；新增入口关闭，尚未取得验收资格',
                    int((time.monotonic() - started) * 1000))

    def _require_active(self):
        if self._capability is None:
            raise ValueError('external writer not activated')
        self.controller.assert_external(self._capability)

    def _event(self, state, kind, reason, now, refs=()):
        owner = self._capability.owner_id
        event_id = kind + '-' + _hash({'intent': state['intent_sha256'], 'owner': owner})
        return self.commands.append_event(state['command_id'], event_id=event_id, event_type=kind,
            owner_id=owner, observed_ms=now, reason_code=reason, evidence_refs=list(refs))

    def _unknown(self, state, now, reason='outcome_unknown'):
        try:
            self._event(state, 'unknown', reason, now)
        except Exception:
            LOGGER.warning('Demo 命令观察未能提交 原因码=command_unknown_commit_unavailable；不得重派动作')

    def _matches(self, state):
        body = self.target.binding.to_dict()
        return (isinstance(state, dict) and state.get('binding') == body
                and state.get('intent', {}).get('binding_sha256') == self.target.binding.sha256
                and state.get('intent', {}).get('kind') in {'pause_entries', 'reconcile_owned'})

    def cycle_once(self, command_id=None):
        self._require_active()
        if command_id is not None:
            _id(command_id)
        if not self._cycle_lock.acquire(blocking=False):
            raise ValueError('writer cycle already active')
        try:
            return self._cycle(command_id)
        finally:
            self._cycle_lock.release()

    def _cycle(self, command_id):
        # 同步循环独占能力；命令读取/提交失败不能跳过本轮普通保护。
        self._require_active()
        cycle_started = time.monotonic()
        stage_started = cycle_started
        discovered = False
        discovered_count = 0
        if command_id is None:
            try:
                accepted = self.commands.accepted_commands(self.target.binding.binding_id)
                discovered_count = len(accepted)
                command_id = accepted[0]['command_id'] if accepted else None
                discovered = command_id is not None
            except Exception:
                LOGGER.warning('Demo 待处理命令发现失败 原因码=command_read_or_claim_unavailable；本轮继续账户保护')
                command_id = None
                discovery_failed = True
            else:
                discovery_failed = False
        else:
            discovery_failed = False
        LOGGER.info('Demo 待处理命令发现结束 冻结绑定候选数=%d 自动选择=%s 读取耗时毫秒=%d',
                    discovered_count, discovered, int((time.monotonic() - stage_started) * 1000))
        LOGGER.info('Demo 前台周期开始 命令存在=%s；本轮最多处理一个命令和一次保护周期',
                    command_id is not None)
        try:
            now = _stamp(self.clock_ms())
        except Exception:
            # 命令时间不可用时不读写票据，仍执行本轮普通暂停保护。
            now = None
        state, dispatch, paused = None, False, False
        reason = ('command_read_or_claim_unavailable' if discovery_failed else
                  'command_discovered' if discovered else 'protection_observed')
        if now is None:
            reason = 'command_clock_unavailable'
        elif command_id is not None:
            try:
                state = self.commands.get(command_id)
                if state is None:
                    reason = 'command_unavailable'
                elif not self._matches(state):
                    reason = 'command_binding_mismatch'
                else:
                    now = max(now, state['last_observed_ms'])
                    if state['status'] == 'accepted':
                        receipt = self.commands.claim(command_id, owner_id=self._capability.owner_id, claimed_ms=now)
                        dispatch = receipt.get('committed') is True and receipt.get('action_ticket') is True
                        state = receipt['command']
                        reason = 'command_claimed' if dispatch else 'command_already_claimed'
                    elif state['status'] == 'claimed':
                        self._unknown(state, now, 'owner_restarted')
                        reason = 'command_outcome_unknown'
                    elif state['status'] == 'unknown':
                        # 每实例固定事件只记录首次恢复观察，精确重试不改时间。
                        event_key = 'observed-' + _hash({'intent': state['intent_sha256'], 'owner': self._capability.owner_id})
                        if not any(row['event_id'] == event_key for row in self.commands.events(command_id)):
                            self._event(state, 'observed', 'recovery_observed', now)
                        reason = 'command_outcome_unknown'
                    else:
                        reason = 'command_terminal'
            except Exception:
                reason = 'command_read_or_claim_unavailable'
                LOGGER.warning('Demo 命令读取或处理权不可用 命令读取及领取耗时毫秒=%d；本轮继续暂停保护，不重派动作',
                               int((time.monotonic() - stage_started) * 1000))
        LOGGER.info('Demo 命令处理结束 状态=%s 首次派发票据=%s 原因码=%s 阶段耗时毫秒=%d',
                    state.get('status') if isinstance(state, dict) else 'unavailable',
                    dispatch, reason, int((time.monotonic() - stage_started) * 1000))
        if dispatch:
            stage_started = time.monotonic()
            LOGGER.info('Demo 暂停入口检查开始；先持久关闭新增开仓')
            pause = {}
            try:
                pause = self.controller.pause_external(self._capability)
                paused = pause.get('entries_paused') is True and pause.get('pause_persisted') is True
            except Exception:
                reason = 'command_pause_commit_unavailable'
                LOGGER.warning('Demo 暂停入口检查失败 原因码=command_pause_commit_unavailable；继续执行只保护周期')
            LOGGER.info('Demo 暂停入口检查结束 暂停确认=%s 持久化确认=%s 阶段耗时毫秒=%d',
                        paused, isinstance(pause, dict) and pause.get('pause_persisted') is True,
                        int((time.monotonic() - stage_started) * 1000))
        stage_started = time.monotonic()
        LOGGER.info('Demo 账户保护周期开始；保持已有保护，入口状态为暂停')
        observation = {}
        try:
            observation = self.controller.cycle_external(self._capability)
            cycle_ok = (observation.get('entries_paused') is True and observation.get('cycle_ok') is True
                        and observation.get('pause_persisted') is True)
        except Exception as exc:
            cycle_ok = False
            LOGGER.warning('Demo 前台保护周期不可用 原因码=protection_cycle_incomplete 异常类型=%s；保留账户锁',
                           type(exc).__name__)
        LOGGER.info('Demo 账户保护周期结束 周期完整=%s 入口暂停=%s 原因码=%s 阶段耗时毫秒=%d',
                    cycle_ok, isinstance(observation, dict) and observation.get('entries_paused') is True,
                    observation.get('reason_code', 'unknown') if isinstance(observation, dict) else 'unknown',
                    int((time.monotonic() - stage_started) * 1000))
        if dispatch:
            if not paused or not cycle_ok:
                self._unknown(state, now)
                reason = 'command_outcome_unknown'
            else:
                stage_started = time.monotonic()
                try:
                    self._archive_applied(state, now)
                    reason = 'command_applied'
                    LOGGER.info('Demo 命令证据归档完成 观察状态=applied 阶段耗时毫秒=%d；不表示交易或验收完成',
                                int((time.monotonic() - stage_started) * 1000))
                except Exception as exc:
                    self._unknown(state, now)
                    reason = 'command_outcome_unknown'
                    LOGGER.warning('Demo 命令证据归档失败 原因码=command_observation_unavailable 异常类型=%s 阶段耗时毫秒=%d；结果标记未知且不重派',
                                   type(exc).__name__, int((time.monotonic() - stage_started) * 1000))
        LOGGER.info('Demo 前台周期结束 原因码=%s 周期完整=%s 总耗时毫秒=%d；执行与验收资格保持关闭',
                    reason, cycle_ok, int((time.monotonic() - cycle_started) * 1000))
        return {'reason_code': reason, 'cycle_ok': cycle_ok, **FALSE_FLAGS}

    def _archive_applied(self, state, now):
        binding = self.target.binding.to_dict()
        payload = {'schema': 'demo-writer-command-observation-v1',
            'identity_hashes': binding['identity_hashes'], 'binding_sha256': self.target.binding.sha256,
            'command_id': state['command_id'], 'intent_sha256': state['intent_sha256'],
            'kind': state['intent']['kind'], 'entries_paused': True, 'pause_persisted': True,
            'cycle_observed': True, 'observed_ms': now, **FALSE_FLAGS}
        receipt = self.archive.append('equity_reconciliation', binding['scope'], payload, {},
            stable_key='writer-command:' + _hash({'intent': state['intent_sha256'], 'owner': self._capability.owner_id}))
        if not isinstance(receipt, dict) or receipt.get('committed') is not True:
            raise ValueError('writer observation commit unavailable')
        proof = self.archive.read(receipt['ref'])
        if (proof['scope'] != binding['scope'] or proof['kind'] != 'equity_reconciliation'
                or proof['payload'] != payload or proof['record_sha256'] != receipt['record_sha256']):
            raise ValueError('writer observation integrity failed')
        self._event(state, 'applied', 'control_applied', now,
                    [{'ref': proof['ref'], 'record_sha256': proof['record_sha256']}])

    def request_stop(self):
        self._stop_requested = True

    def try_safe_exit(self, command_ids=()):
        self._require_active()
        if not self._stop_requested:
            return False
        started = time.monotonic()
        LOGGER.info('Demo 前台安全退出检查开始 命令数=%d；将复核未决命令和账户空仓边界',
                    len(command_ids) if isinstance(command_ids, (tuple, list)) else -1)
        _command_ids(command_ids)
        if not self._cycle_lock.acquire(blocking=False):
            LOGGER.info('Demo 前台安全退出暂缓 原因码=writer_cycle_active')
            return False
        try:
            # 与命令读取、claim 和周期共用锁，退出核对后不能插入新的处理票据。
            self._require_active()
            pending = self.commands.pending_commands()
            if type(pending) is not list or pending:
                LOGGER.warning('Demo 前台安全退出拒绝 原因码=pending_commands count=%s 耗时毫秒=%d；继续持锁保护',
                               len(pending) if isinstance(pending, list) else 'unknown',
                               int((time.monotonic() - started) * 1000))
                return False
            result = self.controller.yield_external_if_flat(self._capability)
            exited = (result is True or isinstance(result, dict) and result.get('accountLockOwned') is False
                      and result.get('entriesEnabled') is False and result.get('status') == 'yielded_flat')
            LOGGER.info('Demo 前台安全退出检查结束 结果=%s 耗时毫秒=%d；退出仅在账户锁安全交接后确认',
                        '已交接' if exited else '仍持锁', int((time.monotonic() - started) * 1000))
            return exited
        except Exception as exc:
            LOGGER.warning('Demo 前台安全退出被拒绝 原因码=flat_handoff_unavailable 异常类型=%s 耗时毫秒=%d；继续持锁保护',
                           type(exc).__name__, int((time.monotonic() - started) * 1000))
            return False
        finally:
            self._cycle_lock.release()


def _wait_interval(seconds):
    # 停止请求已设置也需要实际等待，避免无法交接时忙循环。
    threading.Event().wait(seconds)


def run_foreground(driver, *, command_ids=(), stop_event=None, poll_interval_ms=5000):
    """真正的同步前台循环；停止请求须经过安全退出，不在 finally 中释放锁。"""
    if type(driver) is not DemoWriterDriver:
        raise ValueError('internal foreground writer required')
    identities = _command_ids(command_ids)
    if type(poll_interval_ms) is not int or not 100 <= poll_interval_ms <= 60_000:
        raise ValueError('bounded writer polling interval required')
    stop_event = stop_event or threading.Event()
    started = time.monotonic()
    cycle_count = 0
    LOGGER.info('Demo 前台保护进程进入同步循环 命令数=%d 轮询间隔毫秒=%d',
                len(identities), poll_interval_ms)
    driver.activate()
    index = 0
    while True:
        if stop_event.is_set():
            driver.request_stop()
        driver.cycle_once(identities[index % len(identities)] if identities else None)
        index += 1
        cycle_count += 1
        if driver.try_safe_exit(identities):
            LOGGER.info('Demo 前台保护进程安全退出 已完成周期数=%d 总运行毫秒=%d',
                        cycle_count, int((time.monotonic() - started) * 1000))
            return
        _wait_interval(poll_interval_ms / 1000)
