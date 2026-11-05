"""集中管理量化终端的可见业务门禁。

门禁状态保存在项目 ``auth/gates.json``，每次变更追加到
``auth/gate_audit.jsonl``。基础设施配置（数据库连接、密码、端口）不属于
门禁；会影响功能或风险边界的布尔开关必须在这里注册后才能读取。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class UnknownGateError(KeyError):
    """代码尝试读取未注册的门禁。"""


@dataclass(frozen=True)
class GateDefinition:
    key: str
    name: str
    description: str
    category: str
    default_value: bool
    source: str
    editable: bool
    risk_level: str
    requires_reason: bool = False
    requires_restart: bool = False


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> tuple[bool, str]:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default, "default"
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True, f"env:{name}"
    if value in {"0", "false", "no", "off"}:
        return False, f"env:{name}"
    raise ValueError(f"{name} 必须是布尔值")


_DEFINITIONS = (
    GateDefinition(
        "market_stream.enabled", "行情采集", "允许逐笔、盘口和 K 线采集服务启动。",
        "数据采集", True, "QUANT_STREAM_ENABLED", True, "中", False, True,
    ),
    GateDefinition(
        "breadth_stream.enabled", "宽度行情采集", "允许宽度策略的附加行情流启动。",
        "数据采集", False, "QUANT_BREADTH_STREAM_ENABLED", True, "中", False, True,
    ),
    GateDefinition(
        "integrity.monitor_enabled", "完整性监控", "允许行情完整性巡检服务运行并报告缺口。",
        "运维监控", True, "registry", True, "低",
    ),
    GateDefinition(
        "ops.monitor_enabled", "运维监控", "允许策略运维快照和自动巡检服务运行。",
        "运维监控", True, "registry", True, "低",
    ),
    GateDefinition(
        "strategy_lab.enabled", "策略实验室", "允许多策略模拟服务启动。",
        "策略研究", True, "registry", True, "中", False, True,
    ),
    GateDefinition(
        "iteration.auto_enabled", "模型迭代", "允许自动迭代流程执行研究任务。",
        "策略研究", True, "registry", True, "中",
    ),
    GateDefinition(
        "strategy.paired_execution_enabled", "配对执行模拟", "允许策略实验使用配对执行模拟路径。",
        "策略研究", False, "registry", True, "高", True, True,
    ),
    GateDefinition(
        "risk.required", "风险闸门", "开始 Demo 或执行模拟前必须存在已关闭的研究风险闸门。",
        "风险控制", True, "registry", True, "高", True,
    ),
    GateDefinition(
        "execution.demo_enabled", "Demo 执行", "允许 Demo 会话提交模拟入口订单；不代表真实下单。",
        "执行控制", False, "registry", True, "高", True,
    ),
    GateDefinition(
        "execution.entries_enabled", "新开仓", "允许执行服务接收新的入场信号。",
        "执行控制", False, "registry", True, "高", True,
    ),
    GateDefinition(
        "execution.trading_enabled", "交易执行", "允许执行服务访问交易所下单路径；本项目默认关闭。",
        "执行控制", False, "registry", True, "严重", True,
    ),
    GateDefinition(
        "execution.real_orders", "真实下单", "真实订单提交安全底线，默认关闭且必须显式说明原因。",
        "执行控制", False, "registry", True, "严重", True, True,
    ),
)

_BY_KEY = {definition.key: definition for definition in _DEFINITIONS}


def known_gate_keys() -> tuple[str, ...]:
    return tuple(_BY_KEY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GateRegistry:
    """线程安全、可持久化的门禁注册表。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        storage_dir: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        project_root = Path(root or Path(__file__).resolve().parent)
        self.storage_dir = Path(storage_dir or project_root / "auth")
        self.path = self.storage_dir / "gates.json"
        self.audit_path = self.storage_dir / "gate_audit.jsonl"
        self.environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()
        self._state = self._load_state()

    def _default_state(self) -> dict[str, dict]:
        state = {}
        for definition in _DEFINITIONS:
            default = definition.default_value
            source = definition.source
            if definition.source.startswith("QUANT_"):
                default, source = _env_bool(self.environ, definition.source, default)
            state[definition.key] = {
                "current_value": default,
                "updated_at": None,
                "updated_by": None,
                "source": source,
            }
        return state

    def _load_state(self) -> dict[str, dict]:
        state = self._default_state()
        try:
            persisted = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            persisted = {}
        if isinstance(persisted, dict):
            for key, value in persisted.items():
                if key not in _BY_KEY or not isinstance(value, dict):
                    continue
                if isinstance(value.get("current_value"), bool):
                    state[key].update({
                        "current_value": value["current_value"],
                        "updated_at": value.get("updated_at"),
                        "updated_by": value.get("updated_by"),
                        "source": value.get("source") or state[key]["source"],
                    })
        return state

    def _save_state(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _definition(self, key: str) -> GateDefinition:
        try:
            return _BY_KEY[key]
        except KeyError as exc:
            raise UnknownGateError(f"未注册门禁: {key}") from exc

    def _record(self, definition: GateDefinition) -> dict:
        record = {"key": definition.key, "name": definition.name,
                  "description": definition.description, "category": definition.category,
                  "default_value": definition.default_value, "editable": definition.editable,
                  "risk_level": definition.risk_level,
                  "requires_reason": definition.requires_reason,
                  "requires_restart": definition.requires_restart}
        record.update(copy.deepcopy(self._state[definition.key]))
        return record

    def list_gates(self) -> list[dict]:
        with self._lock:
            self._state = self._load_state()
            return [self._record(definition) for definition in _DEFINITIONS]

    def get(self, key: str) -> dict:
        with self._lock:
            self._state = self._load_state()
            return self._record(self._definition(key))

    def is_enabled(self, key: str) -> bool:
        return bool(self.get(key)["current_value"])

    def assert_registered(self, keys) -> None:
        missing = [str(key) for key in keys if str(key) not in _BY_KEY]
        if missing:
            raise UnknownGateError("未注册门禁: " + ", ".join(missing))

    def update(
        self,
        key: str,
        value: bool,
        *,
        actor: str,
        reason: str = "",
        is_admin: bool = True,
    ) -> dict:
        with self._lock:
            self._state = self._load_state()
            definition = self._definition(key)
            if not is_admin:
                raise PermissionError("没有修改系统门禁的权限")
            if not definition.editable:
                raise PermissionError(f"门禁不可编辑: {key}")
            if not isinstance(value, bool):
                raise ValueError("门禁值必须是布尔值")
            reason = str(reason or "").strip()
            if definition.requires_reason and len(reason) < 4:
                raise ValueError("高风险门禁必须填写至少 4 个字的原因")
            old = self._state[key]["current_value"]
            timestamp = _now()
            self._state[key].update({
                "current_value": value,
                "updated_at": timestamp,
                "updated_by": actor,
                "source": f"ui:{actor}",
            })
            self._save_state()
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as audit:
                audit.write(json.dumps({
                    "timestamp": timestamp,
                    "actor": actor,
                    "key": key,
                    "old_value": old,
                    "new_value": value,
                    "reason": reason,
                }, ensure_ascii=False, sort_keys=True) + "\n")
            return self._record(definition)


_default_registry: GateRegistry | None = None
_default_registry_lock = threading.Lock()


def get_gate_registry(*, root: str | Path | None = None,
                      storage_dir: str | Path | None = None) -> GateRegistry:
    global _default_registry
    if root is not None or storage_dir is not None:
        return GateRegistry(root, storage_dir=storage_dir)
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = GateRegistry()
    return _default_registry

