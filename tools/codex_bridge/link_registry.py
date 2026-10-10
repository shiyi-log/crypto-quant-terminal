"""Named per-project links layered on the single legacy default binding.

`desktop.json` stays exactly what it was: one default Codex↔DeepSeek pair. The
running desktop apps, the watchdog and the already-loaded MCP server keep using
it unchanged. `links.json` adds *named* links so several projects can share one
bridge without rebinding each other:

```json
{
  "version": 1,
  "links": {
    "quant":  {"codex_thread_id": "...", "dsh_session_id": "...", "dsh_url": "...", "cookie_db": "..."},
    "wdhash": {"codex_thread_id": "...", "dsh_session_id": "..."}
  }
}
```

Routing is explicit on purpose. Codex's desktop app starts every MCP server with
an identical environment (verified 2026-10-10: two bridge processes differ only
by PID), so the bridge cannot infer which Codex conversation is calling; the
caller passes `link=<name>` instead. On the DeepSeek side the caller's own
`DSH_SESSION_ID` is known, so `send --to codex` can match a link automatically.

Link entries may be partial: a project with no dedicated Codex conversation yet
can carry only `dsh_session_id`, and the Codex-bound operations then fail with a
clear message instead of silently reusing another project's conversation.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

VERSION = 1
REQUIRED_KEYS = ("codex_thread_id", "dsh_session_id")
ALLOWED_KEYS = ("codex_thread_id", "dsh_session_id", "dsh_url", "cookie_db", "note", "label")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class RegistryError(RuntimeError):
    pass


def validate_name(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise RegistryError("link 名称只能是 1-32 位小写字母/数字/下划线/连字符，且以字母或数字开头")
    return name


def default_url():
    return "http://127.0.0.1:19387"


def default_cookie_db():
    return str(Path.home() / "Library/Application Support/@deepseek-ai/dsh-desktop/Cookies")


def empty():
    return {"version": VERSION, "links": {}}


def load(path):
    """Read links.json; a missing file is an empty registry, not an error."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise RegistryError(f"links.json 无法读取：{exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("links"), dict):
        raise RegistryError("links.json 结构无效")
    links = {}
    for name, entry in raw["links"].items():
        links[validate_name(name)] = validate_entry(name, entry)
    return {"version": raw.get("version", VERSION), "links": links}


def validate_entry(name, entry):
    if not isinstance(entry, dict):
        raise RegistryError(f"link `{name}` 必须是对象")
    unknown = sorted(set(entry) - set(ALLOWED_KEYS))
    if unknown:
        raise RegistryError(f"link `{name}` 含未知字段：{', '.join(unknown)}")
    cleaned = {}
    for key, value in entry.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(f"link `{name}` 的 {key} 必须是非空字符串")
        cleaned[key] = value.strip()
    if "codex_thread_id" in cleaned:
        try:
            uuid.UUID(cleaned["codex_thread_id"])
        except ValueError as exc:
            raise RegistryError(f"link `{name}` 的 codex_thread_id 必须是 UUID") from exc
    if "dsh_session_id" in cleaned and not cleaned["dsh_session_id"].startswith("session-"):
        raise RegistryError(f"link `{name}` 的 dsh_session_id 必须以 session- 开头")
    return cleaned


def save(path, data):
    target = Path(path)
    links = {validate_name(name): validate_entry(name, entry) for name, entry in data.get("links", {}).items()}
    payload = {"version": int(data.get("version", VERSION)), "links": links}
    tmp = target.with_name(f".links-{uuid.uuid4().hex}.json")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return payload


def get(links, name):
    validate_name(name)
    entry = links.get("links", {}).get(name)
    if entry is None:
        known = ", ".join(sorted(links.get("links", {}))) or "（无）"
        raise RegistryError(f"未定义的 link `{name}`；现有：{known}")
    return entry


def find_by_dsh_session(links, session_id):
    if not session_id:
        return None
    for name in sorted(links.get("links", {})):
        if links["links"][name].get("dsh_session_id") == session_id:
            return name, links["links"][name]
    return None


def find_by_codex_thread(links, thread_id):
    if not thread_id:
        return None
    for name in sorted(links.get("links", {})):
        if links["links"][name].get("codex_thread_id") == thread_id:
            return name, links["links"][name]
    return None


def describe(links, current_session=None):
    rows = []
    for name in sorted(links.get("links", {})):
        entry = links["links"][name]
        rows.append({
            "name": name,
            "codex_thread_id": entry.get("codex_thread_id"),
            "dsh_session_id": entry.get("dsh_session_id"),
            "codex_bound": bool(entry.get("codex_thread_id")),
            "dsh_bound": bool(entry.get("dsh_session_id")),
            "note": entry.get("note"),
            "current_session": entry.get("dsh_session_id") == current_session if current_session else False,
        })
    return rows
