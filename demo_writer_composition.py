"""显式组装默认暂停的 Demo writer，不推导授权或启动运行时。"""

from __future__ import annotations

import logging
from pathlib import Path

from data_store import DataStore
from demo_command_store import DemoCommandStore
from demo_evidence_store import DemoEvidenceStore
from demo_runner_controller import DemoRunnerController
from demo_store import DemoStore
from demo_writer import DemoWriterDriver, FrozenWriterTarget

LOGGER = logging.getLogger(__name__)


def create_demo_writer_driver(data_store, target, *, credential_provider, lock_path=None,
                              risk_account_scope=None, paper_source=None, clock_ms=None,
                              gate_registry=None):
    """组装显式冻结身份；本函数不连库、不取凭据、不建锁、不激活。"""
    if not isinstance(data_store, DataStore):
        raise ValueError('explicit Demo DataStore required')
    if type(target) is not FrozenWriterTarget:
        raise ValueError('explicit frozen writer target required')
    if not callable(credential_provider):
        raise ValueError('explicit Demo credential provider required')

    frozen = FrozenWriterTarget(
        target.binding, strategy_id=target.strategy_id, rule_hash=target.rule_hash,
        execution_hash=target.execution_hash, demo_policy_hash=target.demo_policy_hash,
    )
    scope = frozen.binding.to_dict()['scope']
    if scope['environment'] != 'binance_demo':
        raise ValueError('Demo writer target environment required')
    if not isinstance(risk_account_scope, str) or not risk_account_scope.strip():
        raise ValueError('explicit Demo risk account scope required')
    if risk_account_scope != scope['account_id']:
        raise ValueError('Demo risk account scope mismatch')

    def store_factory(namespace):
        return DemoStore(data_store, virtual_account_key=namespace)
    ledger = store_factory(frozen.binding.namespace)
    commands = DemoCommandStore(data_store, scope['account_id'])
    archive = DemoEvidenceStore(data_store, scope['account_id'], scope['stage_id'])
    controller = DemoRunnerController(
        data_store, ledger, credential_provider, paper_source=paper_source,
        lock_path=Path(lock_path) if lock_path is not None else None,
        clock_ms=clock_ms, ledger_factory=store_factory,
        risk_gate_required=True, risk_account_scope=risk_account_scope,
        gate_registry=gate_registry, acceptance_round_required=True,
        driver_mode='external_paused',
    )
    driver = DemoWriterDriver(
        controller, commands, archive, frozen,
        clock_ms=clock_ms, composition_only=True,
    )
    LOGGER.info('Demo writer 暂停组合构造完成 环境=binance_demo 检查阶段=仅组装 凭据读取=false writer激活=false 新增开仓=false')
    return driver
