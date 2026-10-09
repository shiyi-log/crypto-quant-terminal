"""DSH <-> ChatGPT(Codex Desktop) 双向桥接：共享协议层。

本模块是 PROTOCOL.md 的唯一实现，供 bridge.py / mcp_server.py / dsh_plugin 复用。
只依赖标准库。写入纪律见 PROTOCOL.md 第 1 节。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------- 常量

MAX_LINE_BYTES = 64 * 1024
MAX_CONSUMED_IDS = 500

DIRECTIONS = ("to_chatgpt", "to_dsh")

#: 默认 cwd：Codex 只在该目录下持久化会话（config.toml 中 trust_level = "trusted"）。
DEFAULT_CODEX_CWD = "/Users/shiyi/DeepSeek/量化"

#: 默认监听端口（仅 127.0.0.1）。
DEFAULT_HTTP_PORT = 8899


# ---------------------------------------------------------------- 路径


def home() -> Path:
    """桥接根目录，可由 CODEX_BRIDGE_HOME 覆盖。"""
    override = os.environ.get("CODEX_BRIDGE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dsh-codex-bridge"


def queue_dir() -> Path:
    return home() / "queue"


def queue_path(direction: str) -> Path:
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction: {direction!r}")
    return queue_dir() / f"{direction}.jsonl"


def thread_path() -> Path:
    return home() / "thread.json"


def ledger_path() -> Path:
    return home() / "ledger.jsonl"


def cursor_path() -> Path:
    return home() / "state" / "cursor.json"


def log_path() -> Path:
    return home() / "daemon.log"


def pid_path() -> Path:
    return home() / "daemon.pid"


def ensure_layout() -> None:
    """幂等创建目录结构。"""
    for path in (queue_dir(), home() / "state"):
        path.mkdir(parents=True, exist_ok=True)
    for direction in DIRECTIONS:
        target = queue_path(direction)
        if not target.exists():
            target.touch()


# ---------------------------------------------------------------- 基础工具


def now_iso() -> str:
    """本地时区 RFC3339 时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def new_id() -> str:
    return "m-" + uuid.uuid4().hex


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_line(path: Path, text: str) -> int:
    """以 O_APPEND 追加一行，返回写入的字节数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        return os.write(fd, payload)
    finally:
        os.close(fd)


def log(message: str) -> None:
    """写一行守护进程日志（失败不影响主流程）。"""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _append_line(log_path(), f"{stamp} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 消息


def make_message(
    direction: str,
    text: str,
    *,
    source: str,
    kind: str = "message",
    thread_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条消息对象；超长正文按 MAX_LINE_BYTES 截断。"""
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction: {direction!r}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    truncated = False
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_LINE_BYTES:
        text = encoded[:MAX_LINE_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    message: dict[str, Any] = {
        "id": new_id(),
        "ts": now_iso(),
        "direction": direction,
        "source": source,
        "text": text,
        "kind": kind,
        "meta": dict(meta or {}),
        "truncated": truncated,
    }
    if thread_id:
        message["thread_id"] = thread_id
    return message


def append_message(
    direction: str,
    text: str,
    *,
    source: str,
    kind: str = "message",
    thread_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加一条消息到队列，并记入 ledger。"""
    ensure_layout()
    message = make_message(
        direction,
        text,
        source=source,
        kind=kind,
        thread_id=thread_id,
        meta=meta,
    )
    _append_line(queue_path(direction), json.dumps(message, ensure_ascii=False) + "\n")
    ledger(
        {
            "ts": message["ts"],
            "event": "queued",
            "direction": direction,
            "id": message["id"],
            "kind": kind,
            "source": source,
            "bytes": len(message["text"].encode("utf-8")),
        }
    )
    return message


def _iter_jsonl(path: Path, offset: int = 0) -> Iterable[tuple[int, dict[str, Any]]]:
    """从 offset 起逐行产出 (行尾偏移, 对象)；坏行跳过。"""
    if not path.exists():
        return
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                return
            end = handle.tell()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield end, json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue


# ---------------------------------------------------------------- 游标


def load_cursors() -> dict[str, Any]:
    path = cursor_path()
    if not path.exists():
        return {direction: {"offset": 0, "consumed_ids": []} for direction in DIRECTIONS}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    cursors: dict[str, Any] = {}
    for direction in DIRECTIONS:
        entry = data.get(direction) or {}
        cursors[direction] = {
            "offset": int(entry.get("offset") or 0),
            "consumed_ids": list(entry.get("consumed_ids") or [])[-MAX_CONSUMED_IDS:],
        }
    return cursors


def save_cursors(cursors: dict[str, Any]) -> None:
    _atomic_write(
        cursor_path(), json.dumps(cursors, ensure_ascii=False, indent=2) + "\n"
    )


def read_messages(
    direction: str,
    *,
    advance: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """读取游标之后的**新**消息。

    advance=True 时推进游标并记录已消费 id（幂等去重）；
    advance=False（peek）不改变任何状态。
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction: {direction!r}")
    ensure_layout()
    cursors = load_cursors()
    state = cursors[direction]
    seen: list[str] = list(state["consumed_ids"])
    seen_set = set(seen)

    fresh: list[dict[str, Any]] = []
    last_end = int(state["offset"])
    for end, message in _iter_jsonl(queue_path(direction), last_end):
        last_end = end
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id in seen_set:
            continue
        fresh.append(message)
        if isinstance(message_id, str):
            seen.append(message_id)
            seen_set.add(message_id)
        if len(fresh) >= limit:
            break

    if advance:
        state["offset"] = last_end
        state["consumed_ids"] = seen[-MAX_CONSUMED_IDS:]
        save_cursors(cursors)
    return fresh


def peek_new(direction: str) -> list[dict[str, Any]]:
    """不推进游标地查看新消息。"""
    return read_messages(direction, advance=False)


def pending_count(direction: str) -> int:
    """尚无消费者的消息条数。"""
    try:
        return len(peek_new(direction))
    except OSError:
        return 0


# ---------------------------------------------------------------- 线程状态


def load_thread() -> dict[str, Any]:
    path = thread_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_thread(**fields: Any) -> dict[str, Any]:
    """原子更新 thread.json 并返回新值。"""
    ensure_layout()
    current = load_thread()
    current.update({k: v for k, v in fields.items() if v is not _UNSET})
    current.setdefault("created_at", now_iso())
    current["updated_at"] = now_iso()
    _atomic_write(thread_path(), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return current


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<unset>"


_UNSET = _Unset()

UNSET = _UNSET


def ledger(entry: dict[str, Any]) -> None:
    """追加一条流水（永不修改历史）。"""
    _append_line(
        ledger_path(), json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    )


def read_ledger(limit: int = 50) -> list[dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    return rows[-limit:]
