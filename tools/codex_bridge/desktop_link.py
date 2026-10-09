#!/usr/bin/env python3
"""Message the existing Codex and DeepSeek desktop conversations.

Uses Harness's authenticated native RPC and Codex's native desktop RPC. No new
model session, periodic agent runs, global config changes, or app restart.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing

from direct_codex import DirectCodexError, deliver_codex, inspect_codex

DEFAULT_URL = "http://127.0.0.1:19387"
DEFAULT_COOKIE_DB = Path.home() / "Library/Application Support/@deepseek-ai/dsh-desktop/Cookies"
DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
MAX_TEXT_BYTES = 48 * 1024


class LinkError(RuntimeError):
    pass


def checked_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise LinkError("消息正文不能为空")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise LinkError("消息超过 48 KiB；请发送文件路径或缩短正文")
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HarnessClient:
    def __init__(self, base_url=DEFAULT_URL, cookie_db=DEFAULT_COOKIE_DB, timeout=15):
        try:
            url = urllib.parse.urlsplit(base_url)
            port = url.port or 80
        except ValueError as exc:
            raise LinkError("无效的 Harness 地址") from exc
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost")
                or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/")):
            raise LinkError("Harness 地址必须是本机 http://127.0.0.1:<port>")
        # Canonical authority must match the desktop host's authentication cookie.
        self.base_url = f"http://127.0.0.1:{port}"
        self.cookie_db = Path(cookie_db).expanduser()
        self.timeout = timeout
        digest = hashlib.sha256(f"127.0.0.1:{port}".encode()).digest()
        self.cookie_name = "dsh-auth-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _cookie(self):
        try:
            with closing(sqlite3.connect(self.cookie_db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute(
                    "SELECT value FROM cookies WHERE host_key=? AND name=?",
                    ("127.0.0.1", self.cookie_name),
                ).fetchall()
        except sqlite3.Error as exc:
            raise LinkError("无法只读访问 DeepSeek 桌面登录态；请确认应用已打开") from exc
        values = [row[0] for row in rows if row[0]]
        if len(values) != 1 or any(c in values[0] for c in "\r\n;"):
            raise LinkError("未找到当前 Harness 端口的可用登录态；请在 DeepSeek 桌面正常登录")
        return f"{self.cookie_name}={values[0]}"

    def call(self, method, args):
        if method not in ("session/list", "session/prompt", "session/projections", "session/page"):
            raise LinkError("未支持的 Harness 方法")
        envelope = {"type": "client-request", "rpcId": str(uuid.uuid4()),
                    "method": method, "payload": {"args": args}}
        request = urllib.request.Request(
            self.base_url + "/api/" + method,
            data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": self._cookie()},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LinkError(f"Harness HTTP {exc.code}；未确认投递") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LinkError("Harness 连接失败；未确认投递") from exc
        except (ValueError, UnicodeError) as exc:
            raise LinkError("Harness 返回了无效 JSON") from exc
        if not isinstance(result, dict) or result.get("type") != "server-response":
            raise LinkError("Harness 返回了无效 RPC 响应")
        if result.get("rpcId") != envelope["rpcId"]:
            raise LinkError("Harness RPC 响应身份不匹配")
        inner = result.get("result")
        if not isinstance(inner, dict) or inner.get("ok") is not True:
            raise LinkError("Harness RPC 拒绝了请求；未确认投递")
        return inner.get("value")

    def sessions(self):
        result = self.call("session/list", {"_request": {}})
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise LinkError("Harness 会话列表格式错误")
        return result["items"]

    def prompt(self, session_id, text, request_id, mode="steer"):
        checked_text(text)
        if mode != "steer" or not session_id or not request_id:
            raise LinkError("缺少消息或会话身份；跨助手消息仅支持直接发送（steer）")
        result = self.call("session/prompt", {"request": {
            "requestId": request_id, "sessionId": session_id, "mode": mode,
            "content": [{"type": "text", "text": text}], "clientTimeZone": "Asia/Shanghai",
        }})
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise LinkError("Harness 未确认接受此消息")
        return result


def queue_codex(thread_id, text, binary=None, timeout=20):
    """Compatibility name; delivery uses native RPC and never queues a message."""
    checked_text(text)
    try:
        uuid.UUID(thread_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise LinkError("Codex 会话 ID 必须是 UUID") from exc
    try:
        return deliver_codex(thread_id, text, timeout=timeout)
    except DirectCodexError as exc:
        raise LinkError(str(exc)) from exc


def state_home():
    path = Path(os.environ.get("CODEX_BRIDGE_HOME", str(Path.home() / ".dsh-codex-bridge"))).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def config_path():
    return state_home() / "desktop.json"


def load_config():
    try:
        result = json.loads(config_path().read_text())
    except (OSError, ValueError) as exc:
        raise LinkError("请先运行 desktop_link.py bind 绑定现有双方会话") from exc
    for key in ("codex_thread_id", "dsh_session_id", "dsh_url", "cookie_db"):
        if not result.get(key):
            raise LinkError("desktop.json 绑定不完整")
    return result


def save_config(config):
    path = config_path()
    tmp = path.with_name(f".desktop-{uuid.uuid4().hex}.json")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def client(config):
    return HarnessClient(config["dsh_url"], config["cookie_db"])


def journal():
    path = state_home() / "desktop.sqlite"
    db = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, target TEXT NOT NULL,
        text TEXT NOT NULL, reply_to TEXT, status TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT)""")
    db.commit()
    return db


def send(config, destination, text, message_id=None, reply_to=None, mode="steer", retry=False):
    checked_text(text)
    if mode != "steer":
        raise LinkError("跨助手消息仅支持直接发送（steer），禁止进入队列")
    if destination not in ("deepseek", "codex"):
        raise LinkError("消息目标必须是 deepseek 或 codex")
    message_id = message_id or "link-" + uuid.uuid4().hex
    if len(message_id) > 128 or any(c in message_id for c in "\r\n"):
        raise LinkError("消息 ID 不合法")
    target = config["dsh_session_id"] if destination == "deepseek" else config["codex_thread_id"]
    with closing(journal()) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if existing:
            if (existing["destination"], existing["target"], existing["text"], existing["reply_to"]) != (destination, target, text, reply_to):
                raise LinkError("同一消息 ID 的目标或正文已改变")
            if existing["status"] == "accepted":
                db.rollback()
                return {"id": message_id, "accepted": True, "duplicate": True, "destination": destination}
            if not retry:
                raise LinkError("该消息已有投递记录；检查 history 后用 --retry 明确重试")
            if destination == "codex":
                raise LinkError("Codex 不提供此链路的原生去重；先核对是否已收到，再用新的消息 ID")
            db.execute("UPDATE messages SET status='sending',updated_at=?,error=NULL WHERE id=?", (time.time(), message_id))
        else:
            stamp = time.time()
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                       (message_id, destination, target, text, reply_to, "sending", stamp, stamp, None))
        db.commit()
        origin = "Codex" if destination == "deepseek" else "DeepSeek"
        prefix = f"[跨助手消息 · 来自 {origin} · id={message_id}"
        if reply_to:
            prefix += f" · 回复 {reply_to}"
        payload = prefix + "]\n\n" + text
        try:
            if destination == "deepseek":
                result = client(config).prompt(target, payload, message_id, mode)
            else:
                result = queue_codex(target, payload, config.get("codex_binary"))
        except LinkError as exc:
            db.execute("UPDATE messages SET status='unconfirmed',updated_at=?,error=? WHERE id=? AND status!='accepted'",
                       (time.time(), str(exc), message_id))
            db.commit()
            raise
        db.execute("UPDATE messages SET status='accepted',updated_at=? WHERE id=?", (time.time(), message_id))
        db.commit()
    return {"id": message_id, "accepted": True, "destination": destination, "result": result}


def session_summary(item):
    values = item.get("projections", {}).get("values", {})
    return {"session_id": item.get("sessionId"), "title": values.get("title"),
            "cwd": item.get("cwd"), "running": item.get("running"),
            "agent_available": item.get("agentAvailable")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bind", help="绑定现有双方桌面会话")
    bind.add_argument("--codex-thread", required=True)
    bind.add_argument("--dsh-session", required=True)
    bind.add_argument("--dsh-url", default=DEFAULT_URL)
    bind.add_argument("--cookie-db", default=str(DEFAULT_COOKIE_DB))
    bind.add_argument("--codex-binary", default=DEFAULT_CODEX, help="旧绑定兼容字段；直接投递不启动 CLI")
    sessions = commands.add_parser("sessions", help="查看 Harness 根会话")
    sessions.add_argument("--dsh-url", default=DEFAULT_URL)
    sessions.add_argument("--cookie-db", default=str(DEFAULT_COOKIE_DB))
    commands.add_parser("status", help="核对绑定与当前 DeepSeek 会话")
    send_cmd = commands.add_parser("send", help="向已绑定的桌面会话发消息")
    send_cmd.add_argument("--to", choices=("deepseek", "codex"), required=True)
    send_cmd.add_argument("--text", help="未指定时从 stdin 读取")
    send_cmd.add_argument("--id", dest="message_id")
    send_cmd.add_argument("--reply-to")
    send_cmd.add_argument("--mode", choices=("steer",), default="steer")
    send_cmd.add_argument("--retry", action="store_true", help="明确重试未确认的 Harness 消息")
    history = commands.add_parser("history", help="查看投递流水；accepted 不等于已回复")
    history.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        if args.command == "bind":
            try:
                uuid.UUID(args.codex_thread)
            except ValueError as exc:
                raise LinkError("Codex 会话 ID 必须是 UUID") from exc
            config = {"codex_thread_id": args.codex_thread, "dsh_session_id": args.dsh_session,
                      "dsh_url": args.dsh_url, "cookie_db": str(Path(args.cookie_db).expanduser()),
                      "codex_binary": args.codex_binary}
            items = client(config).sessions()
            item = next((s for s in items if s.get("sessionId") == args.dsh_session), None)
            if item is None or item.get("parentSessionId"):
                raise LinkError("目标 Harness 根会话不存在")
            try:
                inspect_codex(args.codex_thread)
            except DirectCodexError as exc:
                raise LinkError(str(exc)) from exc
            save_config(config)
            result = {"bound": True, "codex_thread_id": args.codex_thread, "deepseek": session_summary(item)}
        elif args.command == "sessions":
            result = [session_summary(s) for s in HarnessClient(args.dsh_url, args.cookie_db).sessions()
                      if not s.get("parentSessionId")]
        elif args.command == "status":
            config = load_config()
            item = next((s for s in client(config).sessions() if s.get("sessionId") == config["dsh_session_id"]), None)
            try:
                codex = inspect_codex(config["codex_thread_id"])
            except DirectCodexError as exc:
                codex = {"ready": False, "error": str(exc)}
            result = {"codex_thread_id": config["codex_thread_id"], "deepseek": session_summary(item) if item else None,
                      "codex": codex, "delivery_mode": "direct", "ready": item is not None and codex["ready"]}
        elif args.command == "send":
            text = args.text if args.text is not None else sys.stdin.read()
            result = send(load_config(), args.to, text, args.message_id, args.reply_to, args.mode, args.retry)
        else:
            with closing(journal()) as db:
                result = [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT ?",
                                                    (max(1, min(args.limit, 200)),))]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LinkError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
