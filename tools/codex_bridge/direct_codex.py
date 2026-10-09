"""Direct messages to an existing Codex desktop conversation.

The desktop owns its stdio app-server. Its local IPC follower methods relay to
that same server's turn/steer and turn/start, inheriting the thread settings.
This module never spawns a CLI, resumes a thread, or writes a queued prompt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time
import uuid


MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 48 * 1024


class DirectCodexError(RuntimeError):
    pass


class CodexRPCError(DirectCodexError):
    """An explicit rejection is distinct from a lost delivery confirmation."""


def _is_active_turn_ended_error(message, thread_id):
    """Recognize only explicit inactive-turn errors tied to this exact thread."""
    if not isinstance(message, str):
        return False
    thread = rf"(?<![0-9A-Za-z-]){re.escape(thread_id)}(?![0-9A-Za-z-])"
    ended = r"(?:(?:has|is)\s+)?(?:already\s+)?(?:ended|finished|completed)|(?:is\s+)?no\s+longer\s+active"
    link = r"(?:\s+(?:because|as|since)\s+(?:(?:its|the)\s+)?|\s*[:,\-]\s*(?:(?:its|the)\s+)?|\s+(?:(?:its|the)\s+)?|'s\s+)"
    patterns = (
        # Only a direct affirmative statement about this active turn permits
        # fallback. Skipping arbitrary words would also match "has not ended"
        # or "still running, but a previous turn ended".
        rf"\b(?:conversation|thread)\s+{thread}\b{link}active\s+turn\s+(?:{ended})\b",
        rf"\bactive\s+turn\s+(?:for|of|on|in)\s+(?:the\s+)?(?:conversation|thread)\s+{thread}\b\s+(?:{ended})\b",
    )
    return any(re.search(pattern, message, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def default_socket_path():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "ipc/ipc.sock"


class DesktopRPC:
    """The desktop's length-prefixed local JSON RPC protocol."""

    def __init__(self, timeout=20, socket_path=None):
        if timeout <= 0:
            raise DirectCodexError("Codex RPC 超时必须大于 0")
        self.deadline = time.monotonic() + timeout
        self.socket_path = Path(socket_path or default_socket_path()).expanduser()
        self.client_id = "initializing-client"
        self.sock = None

    def __enter__(self):
        try:
            info = self.socket_path.stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise DirectCodexError("Codex IPC 必须是当前用户的本机 Unix socket")
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self._remaining())
            self.sock.connect(str(self.socket_path))
            result = self.call("initialize", {"clientType": "desktop"}, version=0)
            if not isinstance(result, dict) or not isinstance(result.get("clientId"), str) or not result["clientId"]:
                raise DirectCodexError("Codex IPC 初始化回执格式错误")
            self.client_id = result["clientId"]
            return self
        except (OSError, ValueError) as exc:
            self.close()
            raise DirectCodexError("Codex 桌面 IPC 连接失败；未确认投递，不会退回队列") from exc
        except DirectCodexError:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DirectCodexError("Codex RPC 超时；投递未确认，勿自动重发")
        return remaining

    def _receive_exact(self, length):
        result = bytearray()
        while len(result) < length:
            self.sock.settimeout(self._remaining())
            chunk = self.sock.recv(length - len(result))
            if not chunk:
                raise DirectCodexError("Codex IPC 连接中断；投递未确认，勿自动重发")
            result.extend(chunk)
        return bytes(result)

    def call(self, method, params, version, target=None, with_envelope=False):
        request_id = str(uuid.uuid4())
        request = {"type": "request", "requestId": request_id,
                   "sourceClientId": self.client_id, "version": version,
                   "method": method, "params": params}
        if target:
            request["targetClientId"] = target
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_FRAME_BYTES:
            raise DirectCodexError("Codex RPC 消息过大")
        try:
            self.sock.settimeout(self._remaining())
            self.sock.sendall(struct.pack("<I", len(raw)) + raw)
            while True:
                length = struct.unpack("<I", self._receive_exact(4))[0]
                if not 0 < length <= MAX_FRAME_BYTES:
                    raise DirectCodexError("Codex IPC 回执长度异常；投递未确认")
                response = json.loads(self._receive_exact(length))
                if not isinstance(response, dict):
                    raise DirectCodexError("Codex IPC 回执格式错误；投递未确认")
                if response.get("requestId") != request_id:
                    continue
                if response.get("type") != "response":
                    raise DirectCodexError("Codex IPC 回执类型错误；投递未确认")
                if response.get("error") is not None:
                    error = response["error"]
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise CodexRPCError(f"Codex RPC 拒绝请求：{message}")
                # Successful responses carry routing metadata. Error responses
                # from the desktop may omit both `method` and `handledByClientId`.
                if response.get("method") != method:
                    raise DirectCodexError("Codex IPC 回执方法不匹配；投递未确认")
                if target and response.get("handledByClientId") != target:
                    raise DirectCodexError("Codex IPC 回执 owner 不匹配；投递未确认")
                if response.get("resultType") != "success" or "result" not in response:
                    raise DirectCodexError("Codex IPC 未返回投递回执")
                return response if with_envelope else response["result"]
        except (OSError, ValueError, UnicodeError) as exc:
            raise DirectCodexError("Codex IPC 响应丢失或无效；投递未确认，勿自动重发") from exc

    def owner(self, thread_id):
        result = self.call("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id},
                           version=1, with_envelope=True)
        if not isinstance(result, dict) or not result.get("handledByClientId"):
            raise DirectCodexError("目标 Codex 会话没有可用的桌面 owner；不会另开会话或进入队列")
        return result["handledByClientId"]


def deliver_codex(thread_id, text, timeout=20, socket_path=None):
    """Steer the existing turn, or start a turn only after an explicit idle reply."""
    try:
        uuid.UUID(thread_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DirectCodexError("Codex 会话 ID 必须是 UUID") from exc
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise DirectCodexError("Codex 消息必须非空且不超过 48 KiB")
    message_id = str(uuid.uuid4())
    input_items = [{"type": "text", "text": text, "text_elements": []}]
    with DesktopRPC(timeout=timeout, socket_path=socket_path) as rpc:
        owner = rpc.owner(thread_id)
        cwd = str(Path.cwd())
        params = {"conversationId": thread_id, "input": input_items,
                  "restoreMessage": {"id": message_id, "cwd": cwd,
                                     "context": {"workspaceRoots": [cwd], "commentAttachments": []}},
                  "clientUserMessageId": message_id, "attachments": [], "serviceTier": None}
        method = "turn/steer"
        try:
            result = rpc.call("thread-follower-steer-turn", params, version=1, target=owner)
        except CodexRPCError as exc:
            message = str(exc).removeprefix("Codex RPC 拒绝请求：")
            if not _is_active_turn_ended_error(message, thread_id):
                raise
            method = "turn/start"
            result = rpc.call("thread-follower-start-turn", {
                "conversationId": thread_id,
                "turnStart": {"request": {"threadId": thread_id, "input": input_items,
                                          "clientUserMessageId": message_id},
                              "context": {"inheritThreadSettings": True}},
            }, version=2, target=owner)
        inner = result.get("result") if isinstance(result, dict) else None
        turn_id = inner.get("turnId") if isinstance(inner, dict) else None
        if method == "turn/start" and isinstance(inner, dict) and isinstance(inner.get("turn"), dict):
            turn_id = inner["turn"].get("id")
            if inner["turn"].get("status") not in ("inProgress", "completed"):
                raise DirectCodexError("Codex turn/start 未确认正常开始；勿自动重发")
        if not isinstance(turn_id, str) or not turn_id:
            raise DirectCodexError("Codex RPC 缺少有效 turn 回执；投递未确认，勿自动重发")
        return {"accepted": True, "thread_id": thread_id, "turn_id": turn_id,
                "method": method, "transport": "desktop-native-ipc"}


def inspect_codex(thread_id, timeout=5, socket_path=None):
    """Read desktop ownership/settings without loading a thread or sending text."""
    try:
        uuid.UUID(thread_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DirectCodexError("Codex 会话 ID 必须是 UUID") from exc
    with DesktopRPC(timeout=timeout, socket_path=socket_path) as rpc:
        owner = rpc.owner(thread_id)
        result = rpc.call("thread-follower-read-model-settings", {"conversationId": thread_id},
                          version=1, target=owner)
        if not isinstance(result, dict):
            raise DirectCodexError("Codex 会话设置回执无效")
        return {"thread_id": thread_id, "ready": True, "transport": "desktop-native-ipc"}
