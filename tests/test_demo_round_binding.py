"""Acceptance rounds cannot reuse shared research entry paths."""
import copy
from types import SimpleNamespace

import pytest

from demo_runner_controller import DemoHandoffError, DemoRunnerController


class Documents:
    def __init__(self):
        self.items = {}

    def get_document(self, key, default=None):
        return copy.deepcopy(self.items.get(key, default))

    def put_document(self, key, value):
        self.items[key] = copy.deepcopy(value)


class Ledger:
    virtual_account_key = 'demo_default'

    def get_binding(self):
        return None

    def runner_orders(self):
        return []


class Paper:
    def account(self, strategy):
        return {'strategy_id': strategy, 'rule_hash': 'a' * 64,
                'execution_hash': 'b' * 64,
                'execution': {'execution_enabled': True, 'risk_sizing_mode': 'entry_atr'}}


class Service:
    def __init__(self, strategy='s1'):
        self.config = SimpleNamespace(strategy_id=strategy, virtual_account_key='demo_default',
                                      max_notional_usdt='1000', poll_interval_ms=5000)
        self.policy_hash = 'policy'
        self.enabled = False

    def enable_entries(self):
        self.enabled = True

    def stop(self, **kwargs):
        self.enabled = False

    def status(self):
        return {'entries_enabled': self.enabled, 'status': 'paused_flat'}


def approved(**updates):
    return {'round_id': 'round-1', 'environment': 'binance_demo',
            'account_scope': 'account-a', 'strategy_id': 's1',
            'rule_hash': 'a' * 64, 'execution_hash': 'b' * 64,
            'status': 'active', 'approval_id': 'approval-1', 'approved_by': 'operator',
            **updates}


def controller(tmp_path, document=None, *, required=True):
    value = {'document': approved() if document is None else document}
    # Assigning these explicitly also exercises old controller behavior in RED.
    c = DemoRunnerController(Documents(), Ledger(), lambda: None,
                             paper_source=Paper(), lock_path=tmp_path / 'demo.lock',
                             risk_gate_required=False, risk_account_scope='account-a')
    c.acceptance_round_required = required
    c.acceptance_round_provider = lambda: copy.deepcopy(value['document'])
    c.service = Service()
    c._start_worker = lambda: None
    return c, value


def test_required_round_rejects_missing_authority_before_enabling_entries(tmp_path):
    c, value = controller(tmp_path)
    value['document'] = None
    with pytest.raises(DemoHandoffError, match='approval'):
        c.start('s1')
    assert c.service.enabled is False


@pytest.mark.parametrize('fields', [
    {'environment': 'live'}, {'account_scope': 'other'}, {'strategy_id': 's2'},
    {'rule_hash': 'c' * 64}, {'execution_hash': 'c' * 64},
    {'status': 'completed'}, {'approval_id': ''}, {'approved_by': ''},
])
def test_unapproved_or_mismatched_round_cannot_open_entries(tmp_path, fields):
    c, _ = controller(tmp_path, approved(**fields))
    with pytest.raises(DemoHandoffError):
        c.start('s1')
    assert c.service.enabled is False


def test_valid_round_is_frozen_in_recovery_document(tmp_path):
    c, _ = controller(tmp_path)
    state = c.start('s1')
    assert c.service.enabled is True
    assert state['acceptanceRound']['round_id'] == 'round-1'
    assert c.data_store.items['binance_demo_runner_controller.json']['acceptanceRound'] == approved()


def test_round_approval_change_cannot_rebind_an_existing_session(tmp_path):
    c, value = controller(tmp_path)
    c.start('s1')
    c.stop()
    value['document'] = approved(round_id='round-2', approval_id='approval-2')
    with pytest.raises(DemoHandoffError):
        c.start('s1')
    assert c.service.enabled is False


def test_active_acceptance_round_forbids_switch_before_pausing_or_rebinding(tmp_path):
    import test_demo_controller_switch as switch_fixture
    case = switch_fixture.SwitchTests()
    case.setUp()
    c = case.controller
    c.acceptance_round_required = True
    c.acceptance_round_provider = lambda: approved(strategy_id='old')
    c.risk_account_scope = 'account-a'
    try:
        with pytest.raises(DemoHandoffError) as error:
            c.switch('new', join_timeout=0)
        assert error.value.code == 'demo_acceptance_round_exclusive'
        assert c.service is case.service
        assert c.service.entries_enabled
    finally:
        case.doCleanups()


@pytest.mark.parametrize('operation', ['attach', 'adopt'])
def test_active_acceptance_round_forbids_new_shared_membership(tmp_path, operation):
    import test_demo_controller_parallel as parallel_fixture
    case = parallel_fixture.ParallelTests()
    case.setUp()
    c = case.controller
    c.acceptance_round_required = True
    c.acceptance_round_provider = lambda: approved(strategy_id='s35')
    c.risk_account_scope = 'account-a'
    old = case.make_ledger('demo_strategy_old')
    old.binding = {'strategy_id': 's07', 'rule_hash': 'a' * 64,
                   'execution_hash': 'b' * 64}
    try:
        with pytest.raises(DemoHandoffError) as error:
            if operation == 'attach':
                c.attach('s07')
            else:
                c.adopt_existing_session(old.virtual_account_key)
        assert error.value.code == 'demo_acceptance_round_exclusive'
        assert len(c._services()) == 1
    finally:
        case.doCleanups()


def test_companion_session_cannot_start_in_an_acceptance_round(tmp_path):
    c, _ = controller(tmp_path)
    companion = Service('s2')
    companion.config.virtual_account_key = 'demo_strategy_companion'
    c._parallel_sessions[companion.config.virtual_account_key] = companion
    with pytest.raises(DemoHandoffError):
        c.start_session(companion.config.virtual_account_key)
    assert companion.enabled is False


def test_any_existing_companion_blocks_even_primary_entries(tmp_path):
    c, _ = controller(tmp_path)
    c._parallel_sessions['old'] = Service('s2')
    with pytest.raises(DemoHandoffError):
        c.start('s1')
    assert c.service.enabled is False


def test_missing_round_does_not_block_protection_or_pause(tmp_path):
    c, value = controller(tmp_path)
    value['document'] = None
    c.service.enabled = True
    c.stop()
    assert c.service.enabled is False
    assert c.resume_protection()['entriesEnabled'] is False


def test_binding_must_be_durable_before_an_existing_service_can_enable_entries(tmp_path):
    c, _ = controller(tmp_path)
    def fail_write(*args):
        raise OSError('database unavailable')
    c.data_store.put_document = fail_write
    with pytest.raises(OSError):
        c.start('s1')
    assert c.service.enabled is False


def test_restart_preserves_binding_when_optional_mode_is_disabled(tmp_path):
    c, _ = controller(tmp_path)
    c.start('s1')
    saved = c.data_store.items['binance_demo_runner_controller.json']
    restarted, value = controller(tmp_path, required=False)
    restarted._restore_active_store(saved)
    restarted.acceptance_round_provider = None
    with pytest.raises(DemoHandoffError):
        restarted.start('s1')
    assert restarted.service.enabled is False


def test_persisted_round_blocks_attach_before_recovery_loaded(tmp_path):
    import test_demo_controller_parallel as parallel_fixture
    case = parallel_fixture.ParallelTests()
    case.setUp()
    c = case.controller
    c.data_store.items['binance_demo_runner_controller.json']['acceptanceRound'] = approved()
    try:
        with pytest.raises(DemoHandoffError) as error:
            c.attach('s07')
        assert error.value.code == 'demo_acceptance_round_exclusive'
        assert len(c._services()) == 1
    finally:
        case.doCleanups()


def test_revoked_approval_stops_an_already_enabled_service(tmp_path):
    c, value = controller(tmp_path)
    c.start('s1')
    value['document'] = approved(status='completed')
    with pytest.raises(DemoHandoffError):
        c.start('s1')
    assert c.service.enabled is False


def test_each_worker_cycle_checks_round_before_protecting_without_entries(tmp_path):
    c, value = controller(tmp_path)
    c.start('s1')
    value['document'] = approved(status='completed')
    observed = []
    def cycle():
        observed.append(c.service.enabled)
        c._halt_worker.set()
        c._wake.set()
    c.service.cycle = cycle
    c._loop()
    assert observed == [False]


def test_shared_recovery_cannot_erase_primary_round_binding(tmp_path):
    import test_demo_controller_parallel as parallel_fixture
    case = parallel_fixture.ParallelTests()
    case.setUp()
    try:
        namespace, _ = case.attach()
        frozen = approved(strategy_id='s35')
        case.data.items['binance_demo_runner_controller.json']['acceptanceRound'] = frozen
        shared = case.data.items['binance_demo_shared_sessions.json']
        shared.pop('acceptanceRound', None)
        restarted = case.restart()
        restarted.resume_protection()
        assert case.data.items['binance_demo_runner_controller.json']['acceptanceRound'] == frozen
        with pytest.raises(DemoHandoffError):
            restarted.start_session(namespace)
    finally:
        case.doCleanups()
