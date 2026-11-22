"""前台 writer 使用真实命令/证据存储逻辑，SQL 仅替换为事务内存边界。"""
from contextlib import contextmanager
from copy import deepcopy
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from demo_command_store import DemoCommandStore, FrozenCommandBinding
from demo_evidence_store import DemoEvidenceStore
from demo_runner_controller import DemoRunnerController
from tests.test_demo_archive_providers import MemorySQL, Result

SCOPE = {'environment': 'binance_demo', 'account_id': 'writer-account', 'stage_id': 'stage-1000',
         'strategy_version_id': 'strategy-v1', 'run_id': 'run-1'}
HASHES = dict(zip(('code_hash', 'strategy_hash', 'execution_hash', 'risk_policy_hash', 'cost_model_hash'),
                  (x * 64 for x in 'abcde'), strict=True))


def api():
    return importlib.import_module('demo_writer')


class One(Result):
    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None


class CommandSQL(MemorySQL):
    def __init__(self):
        super().__init__()
        self.bindings, self.intents, self.events = [], [], []
        self.fail_claim_commit = False

    @contextmanager
    def _connection(self):
        before = deepcopy((self.bindings, self.intents, self.events, self.rows))
        try:
            yield self
            if self.fail_claim_commit and len(self.events) > len(before[2]) and self.events[-1]['event_type'] == 'claimed':
                raise RuntimeError('private-signature-cannot-log')
        except Exception:
            self.bindings, self.intents, self.events, self.rows = before
            raise

    def execute(self, query, params=()):
        if 'FROM demo_command_bindings' in query:
            return One([r for r in self.bindings if (r['environment'], r['account_id'], r['binding_id']) == params])
        if 'FROM demo_command_intents' in query:
            # unresolved_commands 会额外传 limit；替身只按账户范围过滤，避免把限额误当命令键。
            if 'ORDER BY command_id LIMIT' in query:
                return One([r for r in self.intents if (r['environment'], r['account_id']) == params[:2]])
            return One([r for r in self.intents if (r['environment'], r['account_id'], r['command_id']) == params])
        if 'FROM demo_command_events' in query:
            return One([r for r in self.events if (r['environment'], r['account_id']) == params[:2]])
        tables = {
            'demo_command_bindings': (self.bindings, ('environment', 'account_id', 'binding_id', 'payload_sha256', 'payload')),
            'demo_command_intents': (self.intents, ('environment', 'account_id', 'command_id', 'binding_id', 'request_sha256', 'payload_sha256', 'payload')),
            'demo_command_events': (self.events, ('environment', 'account_id', 'account_sequence', 'command_id', 'event_id', 'event_type', 'observed_ms', 'previous_hash', 'payload_sha256', 'record_sha256', 'payload')),
        }
        for table, (rows, columns) in tables.items():
            if query.startswith('INSERT INTO ' + table + '('):
                value = dict(zip(columns, deepcopy(params), strict=True))
                rows.append(value)
                return One([value])
        return super().execute(query, params)


class Controller(DemoRunnerController):
    """只替换进程控制边界；命令票据、事件重放及证据摘要均使用真实实现。"""
    def __init__(self, store):
        self.driver_mode = 'external_paused'
        self.data_store = store
        self.demo_store = SimpleNamespace(store=store, environment='binance_demo', virtual_account_key='demo_default')
        self.risk_account_scope = SCOPE['account_id']
        self.cap = SimpleNamespace(owner_id='writer-' + '1' * 32)
        self.active = False
        self.cycles = 0
        self.pause_error = False
        self.cycle_ok = True
        self.exits = 0
        self.exit_refused = False
        self.activate_calls = 0

    def activate_external(self, target, *, command_store):
        self.activate_calls += 1
        assert command_store.get_binding(target.binding.binding_id) == target.binding.to_dict()
        self.active = True
        return self.cap

    def assert_external(self, cap):
        if cap is not self.cap or not self.active:
            raise ValueError('external_writer_capability_invalid')

    def pause_external(self, cap):
        self.assert_external(cap)
        if self.pause_error:
            raise RuntimeError('private-signature-cannot-log')
        return {'entries_paused': True, 'pause_persisted': True}

    def cycle_external(self, cap):
        self.assert_external(cap)
        self.cycles += 1
        return {'entries_paused': True, 'pause_persisted': not self.pause_error,
                'cycle_ok': self.cycle_ok, 'reason_code': 'cycle_observed' if self.cycle_ok else 'cycle_incomplete'}

    def yield_external_if_flat(self, cap):
        self.assert_external(cap)
        self.exits += 1
        if self.exit_refused:
            return False
        self.active = False
        return True


def setup(kind='pause_entries'):
    sql = CommandSQL()
    commands = DemoCommandStore(sql, SCOPE['account_id'])
    binding = FrozenCommandBinding('binding-1', SCOPE, HASHES, 'demo_default')
    commands.register_binding(binding)
    commands.enqueue(binding_id='binding-1', command_id='command-1', kind=kind, actor='operator-1',
                     reason_code='operator_pause' if kind == 'pause_entries' else 'operator_reconcile', submitted_ms=1000)
    archive = DemoEvidenceStore(sql, SCOPE['account_id'], SCOPE['stage_id'])
    controller = Controller(sql)
    target = api().FrozenWriterTarget(binding, strategy_id='paper-1', rule_hash='1' * 64,
        execution_hash='2' * 64, demo_policy_hash='3' * 64)
    writer = api().DemoWriterDriver(controller, commands, archive, target, clock_ms=lambda: 1001)
    return writer, controller, commands, archive, sql


def test_target_frozen_full_binding_cannot_be_fabricated_from_legacy_hashes():
    binding = FrozenCommandBinding('binding-1', SCOPE, HASHES, 'demo_default')
    target = api().FrozenWriterTarget(binding, strategy_id='paper-1', rule_hash='1' * 64,
        execution_hash='2' * 64, demo_policy_hash='3' * 64)
    exported = target.to_dict()
    exported['binding']['scope']['run_id'] = 'other'
    assert target.to_dict()['binding']['scope']['run_id'] == 'run-1'
    with pytest.raises((AttributeError, TypeError)):
        target.strategy_id = 'other'
    with pytest.raises(ValueError):
        api().FrozenWriterTarget({'strategy_id': 'paper-1'}, strategy_id='paper-1',
            rule_hash='1' * 64, execution_hash='2' * 64, demo_policy_hash='3' * 64)


@pytest.mark.parametrize('kind', ['pause_entries', 'reconcile_owned'])
def test_first_committed_claim_drives_one_paused_cycle_then_real_archive_before_applied(kind):
    writer, controller, commands, archive, _ = setup(kind)
    assert controller.activate_calls == 0 and controller.cycles == 0
    with pytest.raises(ValueError):
        writer.cycle_once('command-1')
    writer.activate()
    result = writer.cycle_once('command-1')
    assert result['reason_code'] == 'command_applied'
    assert controller.cycles == 1
    state = commands.get('command-1')
    assert state['status'] == 'applied' and state['execution_complete'] is False
    event = commands.events('command-1')[-1]
    proof = archive.read(event['evidence_refs'][0]['ref'])
    assert proof['payload']['schema'] == 'demo-writer-command-observation-v1'
    assert proof['payload']['identity_hashes'] == HASHES
    assert proof['payload']['intent_sha256'] == state['intent_sha256']
    assert proof['scope'] == SCOPE
    assert result['acceptance_eligible'] is False
    writer.cycle_once('command-1')
    assert controller.cycles == 2
    assert len(commands.events('command-1')) == 3
    assert len(archive.records()) == 1


def test_cycle_discovers_one_committed_command_for_its_frozen_binding_per_cycle():
    writer, controller, commands, _, _ = setup()
    commands.enqueue(binding_id='binding-1', command_id='command-0', kind='pause_entries',
                     actor='operator-2', reason_code='operator_pause', submitted_ms=1000)
    writer.activate()

    first = writer.cycle_once()
    second = writer.cycle_once()

    assert first['reason_code'] == second['reason_code'] == 'command_applied'
    assert commands.get('command-0')['status'] == 'applied'
    assert commands.get('command-1')['status'] == 'applied'
    assert controller.cycles == 2


def test_command_discovery_failure_does_not_skip_protection_cycle():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    commands.accepted_commands = Mock(side_effect=RuntimeError('private-signature-cannot-log'))

    result = writer.cycle_once()

    assert result['reason_code'] == 'command_read_or_claim_unavailable'
    assert result['cycle_ok'] is True
    assert controller.cycles == 1
    assert commands.get('command-1')['status'] == 'accepted'


def test_safe_exit_refuses_committed_but_unclaimed_command():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    writer.request_stop()

    assert writer.try_safe_exit() is False
    assert controller.active and controller.exits == 0
    assert commands.get('command-1')['status'] == 'accepted'


def test_cycle_logs_chinese_stage_timing_without_sensitive_command_or_account_values(caplog):
    writer, _, _, _, _ = setup()
    writer.activate()

    with caplog.at_level('INFO'):
        writer.cycle_once('command-1')

    for label in ('周期开始', '命令处理结束', '暂停入口检查开始', '保护周期结束',
                  '命令证据归档完成', '阶段耗时毫秒', '周期结束'):
        assert label in caplog.text
    assert 'writer-account' not in caplog.text
    assert 'operator-1' not in caplog.text
    assert 'command-1' not in caplog.text
    assert 'private-signature-cannot-log' not in caplog.text


@pytest.mark.parametrize('failure', ['claim_commit', 'cycle_status', 'pause_save', 'archive', 'event_commit'])
def test_command_failure_does_not_repeat_action_or_suppress_ordinary_protection(failure, caplog):
    writer, controller, commands, archive, sql = setup()
    writer.activate()
    if failure == 'claim_commit':
        sql.fail_claim_commit = True
    elif failure == 'cycle_status':
        controller.cycle_ok = False
    elif failure == 'pause_save':
        controller.pause_error = True
    elif failure == 'archive':
        archive.append = Mock(side_effect=RuntimeError('private-signature-cannot-log'))
    else:
        original = commands.append_event
        def fail_applied(*args, **kwargs):
            if kwargs['event_type'] == 'applied':
                raise RuntimeError('private-signature-cannot-log')
            return original(*args, **kwargs)
        commands.append_event = fail_applied
    with caplog.at_level('INFO'):
        result = writer.cycle_once('command-1')
    assert controller.cycles == 1
    assert result['reason_code'] != 'command_applied'
    assert commands.get('command-1')['status'] == ('accepted' if failure == 'claim_commit' else 'unknown')
    assert 'private-signature-cannot-log' not in caplog.text
    assert 'writer-account' not in caplog.text
    assert 'operator-1' not in caplog.text
    if failure != 'claim_commit':
        writer.cycle_once('command-1')
        assert controller.cycles == 2
        assert commands.get('command-1')['status'] == 'unknown'
        assert len(archive.records()) <= 1


def test_restart_claimed_and_unknown_only_observe_never_redispatch_or_issue_ticket():
    writer, controller, commands, archive, _ = setup()
    commands.claim('command-1', owner_id='writer-' + '2' * 32, claimed_ms=1001)
    writer.activate()
    writer.cycle_once('command-1')
    assert commands.get('command-1')['status'] == 'unknown'
    assert controller.cycles == 1
    writer.cycle_once('command-1')
    assert commands.get('command-1')['submission_attempts'] == 1
    assert controller.cycles == 2 and archive.records() == []
    assert commands.claim('command-1', owner_id=controller.cap.owner_id, claimed_ms=1002)['action_ticket'] is False


def test_foreign_binding_not_claimed_and_protection_still_runs():
    writer, controller, commands, _, _ = setup()
    other = FrozenCommandBinding('binding-2', {**SCOPE, 'run_id': 'run-2'}, HASHES, 'demo_default')
    commands.register_binding(other)
    commands.enqueue(binding_id='binding-2', command_id='foreign-1', kind='pause_entries',
                     actor='operator-1', reason_code='operator_pause', submitted_ms=1000)
    writer.activate()
    assert writer.cycle_once('foreign-1')['reason_code'] == 'command_binding_mismatch'
    assert commands.get('foreign-1')['status'] == 'accepted'
    assert controller.cycles == 1


def test_lost_capability_rejects_before_command_read_or_cycle():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    controller.active = False
    commands.get = Mock(side_effect=AssertionError('不能读取命令'))
    with pytest.raises(ValueError):
        writer.cycle_once('command-1')
    assert controller.cycles == 0
    commands.get.assert_not_called()


def test_unknown_command_prevents_normal_exit_even_when_controller_is_flat():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    controller.cycle_ok = False
    writer.cycle_once('command-1')
    writer.request_stop()
    assert writer.try_safe_exit(['command-1']) is False
    assert controller.active and controller.exits == 0
    assert commands.get('command-1')['status'] == 'unknown'


def test_foreground_stop_request_retries_safe_exit_and_keeps_protection_without_busy_spin(monkeypatch):
    writer, controller, _, _, _ = setup()
    writer.activate()
    stop = SimpleNamespace(is_set=lambda: True)
    controller.exit_refused = True
    waits = []
    def wait(seconds):
        waits.append(seconds)
        controller.exit_refused = False
    monkeypatch.setattr(api(), '_wait_interval', wait)
    api().run_foreground(writer, command_ids=(), stop_event=stop, poll_interval_ms=100)
    assert controller.cycles == 2 and controller.exits == 2
    assert waits == [0.1]
    assert controller.active is False


@pytest.mark.parametrize('field,value', [('strategy_id', 123), ('rule_hash', None),
    ('execution_hash', 'F' * 64), ('demo_policy_hash', 'short')])
def test_invalid_legacy_target_never_becomes_driver_authority(field, value):
    values = dict(strategy_id='paper-1', rule_hash='1' * 64,
                  execution_hash='2' * 64, demo_policy_hash='3' * 64)
    values[field] = value
    with pytest.raises(ValueError):
        api().FrozenWriterTarget(FrozenCommandBinding('binding-1', SCOPE, HASHES, 'demo_default'), **values)


def test_behavioral_string_subclasses_cannot_change_frozen_writer_identity():
    class Behavioral(str):
        def encode(self, *args, **kwargs):
            return b'forged-bytes'
    binding = FrozenCommandBinding('binding-1', SCOPE, HASHES, 'demo_default')
    with pytest.raises(ValueError):
        api().FrozenWriterTarget(binding, strategy_id=Behavioral('paper-1'), rule_hash='1' * 64,
                                execution_hash='2' * 64, demo_policy_hash='3' * 64)


def test_false_claim_ticket_never_dispatches_pause_but_keeps_ordinary_protection():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    state = commands.get('command-1')
    commands.claim = Mock(return_value={'committed': True, 'action_ticket': False, 'command': state})
    controller.pause_external = Mock(side_effect=AssertionError('无票据不得执行暂停命令'))
    result = writer.cycle_once('command-1')
    controller.pause_external.assert_not_called()
    assert controller.cycles == 1 and result['reason_code'] == 'command_already_claimed'


def test_uncommitted_claim_receipt_cannot_drive_a_command_action():
    writer, controller, commands, _, _ = setup()
    writer.activate()
    state = commands.get('command-1')
    commands.claim = Mock(return_value={'committed': False, 'action_ticket': True, 'command': state})
    controller.pause_external = Mock(side_effect=AssertionError('未提交不得执行命令'))
    writer.cycle_once('command-1')
    controller.pause_external.assert_not_called()
    assert controller.cycles == 1


def test_missing_archive_commit_receipt_keeps_claim_unknown():
    writer, controller, commands, archive, _ = setup()
    writer.activate()
    archive.append = Mock(return_value={'committed': False})
    assert writer.cycle_once('command-1')['reason_code'] == 'command_outcome_unknown'
    assert controller.cycles == 1 and commands.get('command-1')['status'] == 'unknown'


def test_target_and_dependencies_cannot_be_swapped_after_driver_construction():
    writer, _, _, _, _ = setup()
    for field in ('controller', 'commands', 'archive', 'target', 'clock_ms'):
        with pytest.raises(AttributeError):
            setattr(writer, field, Mock())


@pytest.mark.parametrize('values', [None, ['command-1'] * 2, ['x'] * 65, ['https://private.invalid']])
def test_invalid_foreground_command_set_refuses_before_activation(values):
    writer, controller, _, _, _ = setup()
    with pytest.raises(ValueError):
        api().run_foreground(writer, command_ids=values)
    assert controller.activate_calls == 0


def test_safe_exit_and_command_claim_cannot_overlap_across_driver_threads():
    import threading
    writer, controller, commands, _, _ = setup()
    writer.activate()
    # 交接前必须先终结启动夹具中的已提交命令。
    writer.cycle_once('command-1')
    writer.request_stop()
    entered, release = threading.Event(), threading.Event()
    def yield_wait(capability):
        controller.assert_external(capability)
        entered.set()
        assert release.wait(2)
        controller.active = False
        return True
    controller.yield_external_if_flat = yield_wait
    result = []
    thread = threading.Thread(target=lambda: result.append(writer.try_safe_exit()))
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(ValueError, match='cycle already active'):
            writer.cycle_once('command-1')
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and result == [True]
    assert commands.get('command-1')['status'] == 'applied'
    assert controller.cycles == 1


@pytest.mark.parametrize('damaged', ['controller_store', 'ledger_store', 'ledger_namespace',
                                   'ledger_environment', 'controller_account'])
def test_cross_store_or_identity_composition_refuses_before_any_io(damaged):
    writer, controller, commands, archive, _ = setup()
    if damaged == 'controller_store':
        controller.data_store = CommandSQL()
    elif damaged == 'ledger_store':
        controller.demo_store.store = CommandSQL()
    elif damaged == 'ledger_namespace':
        controller.demo_store.virtual_account_key = 'demo_other'
    elif damaged == 'ledger_environment':
        controller.demo_store.environment = 'binance_live'
    else:
        controller.risk_account_scope = 'other-account'
    commands.get = Mock(side_effect=AssertionError('构造不应读取命令'))
    archive.records = Mock(side_effect=AssertionError('构造不应读取归档'))
    with pytest.raises(ValueError, match='identity mismatch'):
        api().DemoWriterDriver(controller, commands, archive, writer.target)
    assert controller.activate_calls == controller.cycles == 0
    commands.get.assert_not_called()
    archive.records.assert_not_called()


@pytest.mark.parametrize('status', ['claimed', 'unknown'])
def test_accountwide_unresolved_prevents_exit_with_no_command_ids_after_restart(status):
    writer, controller, commands, _, _ = setup()
    other = FrozenCommandBinding('binding-other', {**SCOPE, 'run_id': 'other-run'}, HASHES, 'demo_other')
    commands.register_binding(other)
    commands.enqueue(binding_id='binding-other', command_id='outside-current-target', kind='reconcile_owned',
                     actor='operator-1', reason_code='operator_reconcile', submitted_ms=1000)
    commands.claim('outside-current-target', owner_id='writer-' + '2' * 32, claimed_ms=1001)
    if status == 'unknown':
        commands.append_event('outside-current-target', event_id='outside-unknown', event_type='unknown',
            owner_id='writer-' + '2' * 32, observed_ms=1002, reason_code='outcome_unknown', evidence_refs=[])
    writer.activate()
    writer.request_stop()
    assert writer.try_safe_exit() is False
    assert controller.exits == 0 and controller.active is True
    assert commands.get('outside-current-target')['status'] == status


def test_activation_and_cycle_share_single_driver_operation_lock():
    import threading
    writer, controller, commands, _, _ = setup()
    entered, release = threading.Event(), threading.Event()
    actual = controller.activate_external
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return actual(*args, **kwargs)
    controller.activate_external = delayed
    errors = []
    def activate():
        try:
            writer.activate()
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=activate)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(ValueError, match='operation already active'):
            writer.activate()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and errors == []
    assert controller.activate_calls == 1 and commands.get('command-1')['status'] == 'accepted'


def test_invalid_command_clock_does_not_skip_ordinary_paused_protection():
    writer, controller, commands, archive, _ = setup()
    writer = api().DemoWriterDriver(controller, commands, archive, writer.target, clock_ms=lambda: 'invalid')
    writer.activate()
    result = writer.cycle_once('command-1')
    assert result['reason_code'] == 'command_clock_unavailable'
    assert controller.cycles == 1
    assert commands.get('command-1')['status'] == 'accepted'
