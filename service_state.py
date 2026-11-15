"""Persist operator intent separately from business and execution gates."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid


SCHEMA = "quant-service-control-v1"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def control_path(root: Path) -> Path:
    return root / "database/runtime/service_control.json"


def read_control(root: Path) -> dict:
    path = control_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "services": {}}
    except (OSError, ValueError) as exc:
        raise RuntimeError("service_control_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA \
            or not isinstance(payload.get("services"), dict):
        raise RuntimeError("service_control_invalid")
    for name, item in payload["services"].items():
        if not isinstance(name, str) or not isinstance(item, dict) \
                or item.get("desired_state") not in {"running", "stopped"}:
            raise RuntimeError("service_control_invalid")
    return payload


@contextmanager
def lifecycle_lock(root: Path):
    """Serialize reconciliation with stop intent before a replacement can start."""
    directory = control_path(root).parent
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "service_control.lock").open("a") as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def set_desired(root: Path, names: list[str], desired: str) -> None:
    """Caller holds lifecycle_lock; malformed control is never reset silently."""
    if desired not in {"running", "stopped"}:
        raise ValueError("invalid desired state")
    payload = read_control(root)
    updated = timestamp()
    for name in names:
        payload["services"][name] = {"desired_state": desired, "updated_at": updated}
    payload["updated_at"] = updated
    atomic_json(control_path(root), payload)


def desired_state(root: Path, name: str, *, default: str = "running") -> tuple[str, str | None]:
    payload = read_control(root)
    item = payload["services"].get(name, {})
    return item.get("desired_state", default), item.get("updated_at")
