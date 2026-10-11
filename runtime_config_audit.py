"""Append-only audit records for production service configuration.

Configuration values are deliberately kept out of the ledger.  A snapshot
contains only canonical content fingerprints and non-sensitive file metadata,
so it can prove which configuration was observed without becoming a second
secret store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SNAPSHOT_EVENT = "production_config_snapshot"
DEFAULT_ENVIRONMENT = "production"
_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")


class ConfigAuditError(ValueError):
    """Raised when a configuration cannot be safely fingerprinted."""


@dataclass(frozen=True)
class ConfigFileSnapshot:
    """The safe evidence retained for one configuration file."""

    path: str
    sha256: str
    size: int
    mtime_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


def canonical_json(value: Any) -> str:
    """Serialize JSON with one stable representation and reject non-finite numbers."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigAuditError("configuration contains values that are not canonical JSON") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _root_path(root: str | os.PathLike[str]) -> Path:
    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve(strict=True)
    except OSError as exc:
        raise ConfigAuditError(f"project root is unavailable: {root}") from exc
    if not root_path.is_dir():
        raise ConfigAuditError("project root must be a directory")
    return root_path


def _contains_symlink(path: Path, root: Path) -> bool:
    """Check every existing component below root, including the file itself."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def validate_config_path(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> Path:
    """Resolve one config path, requiring a regular non-symlink file under root."""
    root_path = _root_path(root)
    raw_path = Path(path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else root_path / raw_path
    # ``resolve`` catches ``..`` escapes and symlink targets.  The explicit
    # component check rejects symlinks even when they point back inside root.
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigAuditError(f"configuration file is unavailable: {path}") from exc
    if resolved == root_path or root_path not in resolved.parents:
        raise ConfigAuditError("configuration path must remain inside the project root")
    if _contains_symlink(candidate, root_path):
        raise ConfigAuditError("configuration paths must not contain symbolic links")
    if not resolved.is_file():
        raise ConfigAuditError("configuration path must be a regular file")
    return resolved


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_config(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
        # Parsing allows NaN by default; canonical_json intentionally rejects it.
        canonical_json(value)
        return value
    except UnicodeDecodeError as exc:
        raise ConfigAuditError(f"configuration is not UTF-8 JSON: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigAuditError(f"configuration is not valid JSON: {path}") from exc


def fingerprint_config_file(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> ConfigFileSnapshot:
    """Fingerprint parsed JSON, rather than formatting or key order on disk."""
    root_path = _root_path(root)
    resolved = validate_config_path(path, root_path)
    value = _read_config(resolved)
    digest = _sha256_text(canonical_json(value))
    stat = resolved.stat()
    return ConfigFileSnapshot(_relative_path(resolved, root_path), digest, stat.st_size, stat.st_mtime_ns)


def canonical_config_hash(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """Return the canonical SHA-256 fingerprint for one JSON config file."""
    return fingerprint_config_file(path, root).sha256


def _validate_text(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigAuditError(f"{name} must be a non-empty safe string")
    return value


def _normalise_files(config_paths: Iterable[str | os.PathLike[str]], root: Path) -> list[ConfigFileSnapshot]:
    if isinstance(config_paths, (str, bytes, os.PathLike)):
        raise ConfigAuditError("config_paths must be an iterable of paths")
    try:
        paths = list(config_paths)
    except TypeError as exc:
        raise ConfigAuditError("config_paths must be an iterable of paths") from exc
    if not paths:
        raise ConfigAuditError("at least one configuration path is required")
    snapshots = [fingerprint_config_file(path, root) for path in paths]
    snapshots.sort(key=lambda item: item.path)
    if len({item.path for item in snapshots}) != len(snapshots):
        raise ConfigAuditError("configuration paths must be unique")
    return snapshots


def build_snapshot(*, root: str | os.PathLike[str], service: str,
                   config_paths: Iterable[str | os.PathLike[str]],
                   trigger: str, actor: str = "system",
                   environment: str = DEFAULT_ENVIRONMENT,
                   observed_at_utc: str | None = None) -> dict[str, Any]:
    """Build a non-sensitive snapshot payload without writing to the ledger."""
    service = _validate_text(service, "service", _SERVICE_NAME)
    trigger = _validate_text(trigger, "trigger", _SAFE_TEXT)
    actor = _validate_text(actor, "actor", _SAFE_TEXT)
    environment = _validate_text(environment, "environment", _SERVICE_NAME)
    root_path = _root_path(root)
    files = _normalise_files(config_paths, root_path)
    path_hashes = {item.path: item.sha256 for item in files}
    service_hash = _sha256_text(canonical_json(path_hashes))
    if observed_at_utc is None:
        observed_at_utc = datetime.now(timezone.utc).isoformat()
    else:
        _validate_text(observed_at_utc, "observed_at_utc", _SAFE_TEXT)
    return {
        "schema_version": 1,
        "environment": environment,
        "service": service,
        "config_paths": [item.path for item in files],
        "config_sha256": service_hash,
        "previous_sha256": None,
        "trigger": trigger,
        "actor": actor,
        "file_metadata": [item.as_dict() for item in files],
        "observed_at_utc": observed_at_utc,
    }


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def _json_for_store(store: Any, payload: Any) -> Any:
    serializer = getattr(store, "_json", None)
    return serializer(payload) if callable(serializer) else payload


def _source_id(service: str) -> str:
    return f"config/production/{service}"


def _event_id(service: str, config_hash: str, previous_hash: str | None = None) -> str:
    if previous_hash is None:
        return f"config-snapshot:{service}:{config_hash}"
    identity = canonical_json({"service": service, "config_sha256": config_hash,
                               "previous_sha256": previous_hash})
    return f"config-snapshot:{service}:" + _sha256_text(identity)


def _latest_snapshot(conn: Any, source_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT event_id,payload FROM research_ledger_events WHERE source_id=%s
           AND event_type=%s
           ORDER BY sequence_id DESC LIMIT 1""", (source_id, SNAPSHOT_EVENT),
    ).fetchone()
    # Lightweight test doubles may return the full result list from fetchone;
    # psycopg returns one mapping/tuple. Accept both shapes for callers that
    # wrap DataStore with an in-memory ledger.
    if isinstance(row, list):
        row = row[-1] if row else None
    if row is None:
        return None
    event_id = _row_value(row, "event_id", 0)
    payload = _row_value(row, "payload", 1)
    if not isinstance(payload, dict):
        return None
    return {"event_id": event_id, "payload": payload}


def _same_snapshot_identity(old: dict[str, Any], new: dict[str, Any]) -> bool:
    # observed_at_utc and file metadata may vary while the canonical config is
    # unchanged (for example, a formatting-only rewrite).
    return (old.get("service") == new.get("service")
            and old.get("environment") == new.get("environment")
            and old.get("config_sha256") == new.get("config_sha256")
            and old.get("previous_sha256") == new.get("previous_sha256"))


def append_config_snapshot(store: Any, *, root: str | os.PathLike[str], service: str,
                           config_paths: Iterable[str | os.PathLike[str]],
                           trigger: str, actor: str = "system",
                           environment: str = DEFAULT_ENVIRONMENT,
                           observed_at_utc: str | None = None) -> str:
    """Append one production config snapshot and return its immutable event ID.

    Repeating a snapshot with the same canonical service hash is idempotent.
    Every changed hash records the immediately preceding snapshot hash.
    Database errors intentionally propagate so production callers can fail
    closed before starting or reloading an unaudited service.
    """
    payload = build_snapshot(root=root, service=service, config_paths=config_paths,
                             trigger=trigger, actor=actor, environment=environment,
                             observed_at_utc=observed_at_utc)
    source_id = _source_id(payload["service"])
    with store._connection() as conn:
        previous = _latest_snapshot(conn, source_id)
        previous_payload = previous["payload"] if previous else None
        previous_hash = previous_payload.get("config_sha256") if previous_payload else None
        if previous and previous_hash == payload["config_sha256"]:
            # Retries are idempotent even when their timestamps or file
            # metadata differ from the first write.
            return previous["event_id"]
        payload["previous_sha256"] = previous_hash
        event_id = _event_id(payload["service"], payload["config_sha256"], previous_hash)
        inserted = conn.execute(
            """INSERT INTO research_ledger_events (event_id,event_type,payload,source_id)
               VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
            (event_id, SNAPSHOT_EVENT, _json_for_store(store, payload), source_id),
        ).fetchone()
        if inserted is not None:
            return _row_value(inserted, "event_id", 0)
        existing = conn.execute(
            """SELECT event_id,event_type,payload,source_id FROM research_ledger_events
               WHERE event_id=%s""", (event_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"ledger event disappeared after conflict: {event_id}")
        old_type = _row_value(existing, "event_type", 1)
        old_payload = _row_value(existing, "payload", 2)
        old_source = _row_value(existing, "source_id", 3)
        if old_type != SNAPSHOT_EVENT or old_source != source_id:
            raise ValueError(f"immutable config audit event conflict: {event_id}")
        if isinstance(old_payload, dict) and _same_snapshot_identity(old_payload, payload):
            return event_id
        raise ValueError(f"immutable config audit event conflict: {event_id}")


record_config_snapshot = append_config_snapshot


__all__ = [
    "ConfigAuditError", "ConfigFileSnapshot", "SNAPSHOT_EVENT",
    "append_config_snapshot", "build_snapshot", "canonical_config_hash",
    "canonical_json", "fingerprint_config_file", "record_config_snapshot",
    "validate_config_path",
]
