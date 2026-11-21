"""外部暂停控制器的离线生命周期；使用真实 flock 和受控服务边界。"""
from copy import deepcopy
from contextlib import contextmanager
import importlib
import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from demo_command_store import DemoCommandStore, FrozenCommandBinding
from demo_runner_controller import (DemoHandoffError, DemoRunnerController,
                                    SHARED_STATE_KEY, STATE_KEY)
from demo_risk_reservation_store import _hash as risk_hash


class Documents:
    def __init__(self):
        self.values = {}
        self.reads = self.writes = self.checks = 0
        self.fail_save = False
        self.risk_context = {'payload': {'external_risk_usdt': '0', 'external_margin_usdt': '0',
            'external_symbols': []}}
        self.risk_context['context_hash'] = risk_hash(self.risk_context['payload'])
        self.conditional_history = []
        self.fail_readiness = False

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, query, params=()):
        if self.fail_readiness:
            raise RuntimeError('signature=must-never-log')
        if query.startswith('SELECT pg_advisory'):
            rows = []
        elif query.startswith('SELECT * FROM demo_risk_contexts'):
            rows = [self.risk_context] if self.risk_context else []
        elif query.startswith('SELECT * FROM demo_entry_decisions'):
            rows = []
        elif query.startswith('SELECT 1 FROM demo_conditional_intents'):
            rows = self.conditional_history[:1]
        else:
            raise AssertionError('unexpected offline readiness SQL')
        return SimpleNamespace(fetchone=lambda: deepcopy(rows[0]) if rows else None,
                               fetchall=lambda: deepcopy(rows))

    def get_document(self, key, default=None):
        self.reads += 1
        return deepcopy(self.values.get(key, default))

    def put_document(self, key, value):
        self.writes += 1
        if self.fail_save:
            raise RuntimeError('signature=must-never-log')
        self.values[key] = deepcopy(value)

    def check_schema(self):
        self.checks += 1
        return True


class Ledger:
    def __init__(self, data):
        self.store = data
        self.environment = 'binance_demo'
        self.virtual_account_key = 'demo_default'
        self.binding = {'strategy_id': 'paper-1', 'rule_hash': '1' * 64,
                        'execution_hash': '2' * 64, 'demo_policy_hash': '3' * 64,
                        'forward_boundary_ms': 100, 'last_sequence_id': 1}
        self.net, self.pending, self.orders, self.fills = {}, [], [], []
        self.checks = 0

    def check_schema(self):
        self.checks += 1
        return True

    def get_binding(self):
        return deepcopy(self.binding)

    def net_fill_positions(self):
        return deepcopy(self.net)

    def pending_orders(self):
        return deepcopy(self.pending)

    def runner_orders(self):
        return deepcopy(self.orders)

    def runner_fills(self):
        return deepcopy(self.fills)

    def account_reconciliation_status(self):
        return {'safeToSwitch': True}


class Exchange:
    def __init__(self):
        self.reads = []
        self.positions, self.normal, self.conditional = [], [], []
        self.fail = False

    def _read(self, kind, value):
        self.reads.append(kind)
        if self.fail:
            raise RuntimeError('signature=must-never-log')
        return deepcopy(value)

    def fetch_positions(self):
        return self._read('positions', self.positions)

    def fetch_open_orders(self):
        return self._read('normal', self.normal)

    def fetch_open_conditional_orders(self):
        return self._read('conditional', self.conditional)

    def fetch_position_mode(self):
        return self._read('mode', {'hedged': False})


class PausedService:
    """替换服务的交易所边界；保留初始化同步周期与暂停语义。"""
    def __init__(self, adapter, ledger, _paper, config, **_kwargs):
        self.adapter, self.store, self.config = adapter, ledger, config
        self.policy_hash = '3' * 64
        self.enabled = True
        self.cycles = []
        self.failure = None
        self.start_failure = None
        self.safety_runtime_required = False
        self.safety_runtime = None

    def start(self, *, entries_enabled):
        self.enabled = entries_enabled
        self.cycle()
        if self.start_failure:
            raise self.start_failure
        return self.status()

    def pause_entries_without_cycle(self):
        self.enabled = False
        return self.status()

    def stop(self, **_kwargs):
        self.enabled = False
        self.cycle()
        self.cycle()
        return self.status()

    def cycle(self):
        self.cycles.append(self.enabled)
        return self.status()

    def status(self):
        return {'running': True, 'entries_enabled': self.enabled,
                'status': 'reconciliation_error' if self.failure else 'paused_flat',
                'last_error': self.failure, 'forward_boundary_ms': 100,
                'ownership_conflicts': [], 'risk_alert': None}


def frozen_binding():
    return FrozenCommandBinding('binding-1',
        {'environment': 'binance_demo', 'account_id': 'demo-account', 'stage_id': 'stage-1000',
         'strategy_version_id': 'strategy-v1', 'run_id': 'run-1'},
        dict(zip(('code_hash', 'strategy_hash', 'execution_hash', 'risk_policy_hash', 'cost_model_hash'),
                 [char * 64 for char in 'abcde'], strict=True)), 'demo_default')


def frozen_target():
    assert importlib.util.find_spec('demo_writer'), '外部 writer 冻结目标尚未实现'
    api = importlib.import_module('demo_writer')
    return api.FrozenWriterTarget(frozen_binding(), strategy_id='paper-1', rule_hash='1' * 64,
        execution_hash='2' * 64, demo_policy_hash='3' * 64)


def lock_available(path):
    script = ('import fcntl,sys; h=open(sys.argv[1],"a+"); '
              '\ntry: fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)'
              '\nexcept BlockingIOError: sys.exit(9)')
    result = subprocess.run([sys.executable, '-c', script, str(path)],
                            capture_output=True, check=False)
    assert result.returncode in {0, 9}, '独立 flock 探针执行失败'
    return result.returncode == 0


@pytest.fixture
def external(tmp_path, monkeypatch):
    data = Documents()
    ledger = Ledger(data)
    command_store = Mock(spec_set=DemoCommandStore(data, 'demo-account'))
    command_store.account_id = 'demo-account'
    command_store.environment = 'binance_demo'
    command_store.check_schema.return_value = True
    command_store.get_binding.return_value = frozen_binding().to_dict()
    exchange = Exchange()
    paper = SimpleNamespace(account=lambda _key: {
        'strategy_id': 'paper-1', 'rule_hash': '1' * 64, 'execution_hash': '2' * 64,
        'execution': {'risk_sizing_mode': 'entry_atr', 'execution_enabled': True},
        'definition': {}, 'state': {'positions': {}}})
    calls = []
    def credentials():
        calls.append('credentials')
        return SimpleNamespace(api_key='offline-demo-key', secret='offline-demo-secret')
    def adapter_factory(**_kwargs):
        calls.append('adapter')
        return exchange
    created = []
    def service_factory(*args, **kwargs):
        service = PausedService(*args, **kwargs)
        created.append(service)
        return service
    monkeypatch.setattr('demo_runner_controller.DemoExecutionService', service_factory)
    class Environment(SimpleNamespace):
        @property
        def controller(self):
            if self.active_controller is None:
                self.active_controller = DemoRunnerController(data, ledger, credentials, adapter_factory,
                    paper_source=paper, lock_path=self.path, risk_gate_required=False,
                    risk_account_scope='demo-account', driver_mode='external_paused')
            return self.active_controller
    data.values[STATE_KEY] = {'selectedAccountId': 'paper-1', 'virtualAccountKey': 'demo_default',
                              'entriesEnabled': True, 'demoPolicyHash': '3' * 64}
    env = Environment(active_controller=None, data=data, ledger=ledger, command_store=command_store, exchange=exchange,
                      created=created, calls=calls, path=tmp_path / 'account.lock')
    yield env
    # 仅离线测试清理句柄，不作为生产退出接口。
    handle = env.active_controller._file_lock if env.active_controller is not None else None
    if handle is not None and not handle.closed:
        handle.close()


def test_constructor_does_not_acquire_lock_read_documents_or_load_credentials(external):
    env = external
    assert env.controller.service is None
    assert not env.path.exists()
    assert env.data.reads == env.data.writes == env.data.checks == 0
    command_store_calls = env.command_store.mock_calls
    assert command_store_calls == []
    assert env.calls == []
    assert env.created == []


@pytest.mark.parametrize('name,args,kwargs', [
    ('start', ('paper-1',), {}), ('stop', (), {}), ('stop', (), {'flatten': True}),
    ('attach', ('paper-1',), {}), ('adopt_existing_session', ('demo_default',), {}),
    ('start_session', ('demo_default',), {}), ('stop_session', ('demo_default',), {}),
    ('stop_session', ('demo_default',), {'flatten': True}),
    ('switch', ('paper-1',), {}), ('resume_protection', (), {}), ('close_if_flat', (), {}),
])
def test_legacy_mutations_refuse_before_io_in_external_mode(external, name, args, kwargs):
    env = external
    with pytest.raises(DemoHandoffError) as refused:
        getattr(env.controller, name)(*args, **kwargs)
    assert refused.value.code == 'demo_external_driver_required'
    assert env.data.reads == env.data.writes == 0
    assert env.command_store.mock_calls == []
    assert env.calls == [] and env.created == []
    assert not env.path.exists()


def test_existing_legacy_lock_refuses_activation_before_credentials(external):
    import fcntl
    env = external
    with env.path.open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((DemoHandoffError, RuntimeError)):
            env.controller.activate_external(frozen_target(), command_store=env.command_store)
    assert env.calls == [] and env.created == []
    assert env.data.writes == 0
    assert env.command_store.mock_calls == []


@pytest.mark.parametrize('change', ['shared', 'yielded', 'missing', 'namespace',
                                   'strategy', 'rule', 'execution', 'policy', 'risk_account'])
def test_invalid_existing_identity_refuses_before_credentials(external, change):
    env = external
    if change == 'shared':
        env.data.values[SHARED_STATE_KEY] = {'sessions': [{'virtualAccountKey': 'demo_default'}]}
    elif change == 'yielded':
        env.data.values[STATE_KEY]['yieldedFlat'] = True
    elif change == 'missing':
        env.ledger.binding = None
    elif change == 'namespace':
        env.data.values[STATE_KEY]['virtualAccountKey'] = 'demo_other'
    elif change == 'risk_account':
        env.controller.risk_account_scope = 'other-account'
    else:
        field = {'strategy': 'strategy_id', 'rule': 'rule_hash', 'execution': 'execution_hash',
                 'policy': 'demo_policy_hash'}[change]
        env.ledger.binding[field] = 'other' if change == 'strategy' else 'f' * 64
    with pytest.raises((DemoHandoffError, ValueError)):
        env.controller.activate_external(frozen_target(), command_store=env.command_store)
    assert env.calls == [] and env.created == []
    assert env.data.writes == 0
    assert lock_available(env.path)


def test_activation_restores_paused_without_daemon_and_holds_lock_between_cycles(external):
    env = external
    def check_commands():
        assert not lock_available(env.path)
        assert env.calls == []
        return True
    def read_binding(binding_id):
        assert binding_id == 'binding-1'
        assert not lock_available(env.path)
        assert env.calls == []
        return frozen_binding().to_dict()
    env.command_store.check_schema.side_effect = check_commands
    env.command_store.get_binding.side_effect = read_binding
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    assert env.calls == ['credentials', 'adapter']
    assert len(env.created) == 1
    service = env.created[0]
    assert service.cycles == [False]
    assert env.controller._thread is None and env.controller._workers == set()
    assert env.data.values[STATE_KEY]['entriesEnabled'] is False
    assert not lock_available(env.path)
    env.command_store.check_schema.assert_called_once_with()
    env.command_store.get_binding.assert_called_once_with('binding-1')
    env.controller.cycle_external(capability)
    env.controller.cycle_external(capability)
    assert service.cycles == [False, False, False]
    assert env.controller.status()['entriesEnabled'] is False
    assert env.controller.status()['driverMode'] == 'external_paused'
    assert env.controller.status()['externalDriverActive'] is True
    assert not lock_available(env.path)


@pytest.mark.parametrize('changed', ['account', 'environment', 'scope', 'hashes', 'extra'])
def test_wrong_persistent_command_binding_refuses_before_credentials(external, changed):
    env = external
    body = frozen_binding().to_dict()
    if changed == 'account':
        env.command_store.account_id = 'other-account'
    elif changed == 'environment':
        env.command_store.environment = 'binance_live'
    elif changed == 'scope':
        body['scope']['run_id'] = 'other-run'
    elif changed == 'hashes':
        body['identity_hashes']['code_hash'] = 'f' * 64
    else:
        body['unexpected_payload'] = {'truth': True}
    env.command_store.get_binding.return_value = body
    with pytest.raises((DemoHandoffError, ValueError)):
        env.controller.activate_external(frozen_target(), command_store=env.command_store)
    assert env.calls == [] and env.created == []
    assert env.data.writes == 0
    assert lock_available(env.path)


def test_external_start_worker_never_spawns_even_during_failure_recovery(external):
    env = external
    env.controller._start_worker()
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    env.ledger.net = {'BTC/USDT:USDT': '0.001'}
    with pytest.raises(DemoHandoffError):
        env.controller.yield_external_if_flat(capability)
    env.controller._start_worker()
    env.controller.cycle_external(capability)
    assert env.controller._thread is None and env.controller._workers == set()
    assert env.created[0].cycles[-1] is False
    assert not lock_available(env.path)


@pytest.mark.parametrize('forged', ['owner-string', None, object()])
def test_forged_capability_is_rejected_before_documents_or_cycle(external, forged):
    env = external
    env.controller.activate_external(frozen_target(), command_store=env.command_store)
    before = (env.data.reads, env.data.writes, len(env.created[0].cycles))
    with pytest.raises(DemoHandoffError):
        env.controller.cycle_external(forged)
    assert (env.data.reads, env.data.writes, len(env.created[0].cycles)) == before


@pytest.mark.parametrize('damage', ['closed', 'replacement'])
def test_lost_file_identity_is_rejected_before_cycle_or_documents(external, damage):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    if damage == 'closed':
        env.controller._file_lock.close()
    else:
        env.path.rename(env.path.with_suffix('.old'))
        env.path.write_text('')
    before = (env.data.reads, env.data.writes, len(env.created[0].cycles))
    with pytest.raises(DemoHandoffError):
        env.controller.cycle_external(capability)
    assert (env.data.reads, env.data.writes, len(env.created[0].cycles)) == before


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='需要 POSIX fork')
def test_forked_process_cannot_use_inherited_capability(external):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    pid = os.fork()
    if pid == 0:
        before = (env.data.reads, env.data.writes, len(env.created[0].cycles))
        try:
            env.controller.cycle_external(capability)
        except DemoHandoffError:
            unchanged = before == (env.data.reads, env.data.writes, len(env.created[0].cycles))
            os._exit(0 if unchanged else 2)
        except Exception:
            os._exit(3)
        os._exit(4)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert not lock_available(env.path)


def test_pause_is_persisted_without_running_an_extra_cycle(external):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    service = env.created[0]
    service.enabled = True
    before = len(service.cycles)
    env.controller.pause_external(capability)
    assert service.enabled is False
    assert len(service.cycles) == before
    assert env.data.values[STATE_KEY]['entriesEnabled'] is False


@pytest.mark.parametrize('blocked', ['owned', 'unknown_order', 'conditional', 'exchange_failure', 'save_failure'])
def test_failed_yield_retains_paused_protection_and_real_lock(external, blocked):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    if blocked == 'owned':
        env.ledger.net = {'BTC/USDT:USDT': '0.001'}
    elif blocked == 'unknown_order':
        env.ledger.pending = [{'status': 'unknown'}]
    elif blocked == 'conditional':
        env.exchange.conditional = [{'symbol': 'BNBUSDT', 'client_algo_id': 'foreign'}]
    elif blocked == 'exchange_failure':
        env.exchange.fail = True
    else:
        env.data.fail_save = True
    with pytest.raises((DemoHandoffError, RuntimeError)):
        env.controller.yield_external_if_flat(capability)
    assert not lock_available(env.path)
    assert env.controller.service is env.created[0]
    assert env.controller.service.enabled is False
    assert env.controller._thread is None and env.controller._workers == set()
    env.data.fail_save = False
    env.controller.cycle_external(capability)
    assert env.created[0].cycles[-1] is False
    assert not lock_available(env.path)


def test_successful_yield_rechecks_all_exchange_sources_then_releases_capability(external):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    result = env.controller.yield_external_if_flat(capability)
    assert set(env.exchange.reads) >= {'positions', 'normal', 'conditional', 'mode'}
    assert result['entriesEnabled'] is False
    assert env.data.values[STATE_KEY]['yieldedFlat'] is True
    assert env.controller.service is None
    assert lock_available(env.path)
    with pytest.raises(DemoHandoffError):
        env.controller.cycle_external(capability)


@pytest.mark.parametrize('required,runtime', [(True, None), (False, object())])
def test_missing_durable_safety_readiness_refuses_yield_and_keeps_protection(external, required, runtime):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    service = env.created[0]
    service.safety_runtime_required, service.safety_runtime = required, runtime
    with pytest.raises(DemoHandoffError) as refused:
        env.controller.yield_external_if_flat(capability)
    assert refused.value.code == 'demo_external_exit_readiness_unavailable'
    assert env.data.values[STATE_KEY].get('yieldedFlat') is not True
    assert not lock_available(env.path)
    before = len(service.cycles)
    env.controller.cycle_external(capability)
    assert len(service.cycles) == before + 1
    assert service.cycles[-1] is False
    assert env.controller._thread is None and env.controller._workers == set()


@pytest.mark.parametrize('blocked', ['missing_context', 'damaged_context', 'read_failure', 'conditional_history'])
def test_durable_state_unavailable_never_becomes_permission_to_yield(external, blocked):
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    if blocked == 'missing_context':
        env.data.risk_context = None
    elif blocked == 'damaged_context':
        env.data.risk_context['context_hash'] = '0' * 64
    elif blocked == 'read_failure':
        env.data.fail_readiness = True
    else:
        env.data.conditional_history = [{'virtual_account_key': 'other-namespace', 'status': 'FINISHED'}]
    with pytest.raises(DemoHandoffError) as refused:
        env.controller.yield_external_if_flat(capability)
    assert refused.value.code == 'demo_external_exit_readiness_unavailable'
    assert not lock_available(env.path)
    assert env.controller.service is env.created[0]
    assert env.data.values[STATE_KEY].get('yieldedFlat') is not True


def test_concurrent_activation_is_refused_before_credentials_or_service_creation(external):
    import threading
    env = external
    entered, release = threading.Event(), threading.Event()
    def held_schema():
        entered.set()
        assert release.wait(2)
        return True
    env.command_store.check_schema.side_effect = held_schema
    errors = []
    def activate():
        try:
            env.controller.activate_external(frozen_target(), command_store=env.command_store)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=activate)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(DemoHandoffError) as refused:
            env.controller.activate_external(frozen_target(), command_store=env.command_store)
        assert refused.value.code == 'demo_external_operation_active'
        assert env.calls == [] and env.created == []
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and errors == []
    assert len(env.created) == 1 and env.calls == ['credentials', 'adapter']


def test_controller_cycle_cannot_overlap_flat_checks_or_lock_release(external):
    import threading
    env = external
    capability = env.controller.activate_external(frozen_target(), command_store=env.command_store)
    entered, release = threading.Event(), threading.Event()
    original = env.exchange.fetch_open_conditional_orders
    def held_conditional():
        entered.set()
        assert release.wait(2)
        return original()
    env.exchange.fetch_open_conditional_orders = held_conditional
    result = []
    thread = threading.Thread(target=lambda: result.append(env.controller.yield_external_if_flat(capability)))
    thread.start()
    assert entered.wait(1)
    before = len(env.created[0].cycles)
    try:
        with pytest.raises(DemoHandoffError) as refused:
            env.controller.cycle_external(capability)
        assert refused.value.code == 'demo_external_operation_active'
        assert len(env.created[0].cycles) == before
        assert not lock_available(env.path)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and len(result) == 1
    assert lock_available(env.path)


def test_initial_start_failure_retries_start_before_next_protection_cycle(external):
    env = external
    controller = env.controller
    service_factory = controller._service
    def recoverable(*args, **kwargs):
        service = service_factory(*args, **kwargs)
        service.start_attempts = 0
        service._started = False
        def start(*, entries_enabled):
            assert entries_enabled is False
            service.start_attempts += 1
            if service.start_attempts == 1:
                raise RuntimeError('offline-before-started')
            service._started = True
            service.cycle()
            return service.status()
        original_cycle = service.cycle
        def guarded_cycle():
            if not service._started:
                raise RuntimeError('Demo runner must be explicitly started')
            return original_cycle()
        service.start, service.cycle = start, guarded_cycle
        return service
    controller._service = recoverable
    capability = controller.activate_external(frozen_target(), command_store=env.command_store)
    assert not lock_available(env.path)
    service = env.created[0]
    assert service.start_attempts == 1 and service.cycles == []
    result = controller.cycle_external(capability)
    assert service.start_attempts == 2 and service.cycles == [False]
    assert result['cycle_ok'] is True and controller._error is None
    controller.cycle_external(capability)
    assert service.start_attempts == 2 and service.cycles == [False, False]
