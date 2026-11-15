"""Explicit Demo authorization scoped to a test's temporary storage."""
from pathlib import Path
import uuid

from gate_registry import GateRegistry


def demo_gate_registry(root):
    registry = GateRegistry(
        storage_dir=Path(root) / "demo_gates" / uuid.uuid4().hex, environ={})
    for key in ("execution.demo_enabled", "execution.trading_enabled",
                "execution.entries_enabled"):
        registry.update(key, True, actor="offline-integration-test",
                        reason="isolated offline exchange fixture")
    return registry
