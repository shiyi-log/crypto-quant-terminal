#!/usr/bin/env python3
"""DSH <-> ChatGPT(Codex Desktop) 桥接：MCP over stdio 服务器。

这是 "ChatGPT -> DSH" 入站方向的主通道：Codex 桌面版（/Applications/ChatGPT.app）
读取 ~/.codex/config.toml 的 [mcp_servers] 段，通过 stdio 调用本进程暴露的工具。

协议：JSON-RPC 2.0，行分隔（每行一个完整 JSON 对象）。
发送走 desktop_link 的原生桌面通道，不写入桥接队列。
bridge_core 仅供读取旧队列，供迁移时检查历史积压。

硬性纪律
--------
* stdout 只允许出现协议报文。任何日志/调试一律走 stderr。
* 启动时把 sys.stdout 换成 sys.stderr，因此任何 stray print 也只会落到 stderr。
* 非法 JSON 行忽略；EOF 干净退出，退出码 0。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 路径与导入

#: 脚本所在目录（bridge_core.py 的位置）。直接 `python3 /abs/mcp_server.py`
#: 启动时 Python 已把该目录放进 sys.path[0]，此处再显式兜底一次。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import bridge_core  # noqa: E402  (必须在 sys.path 调整之后导入)
import desktop_link  # noqa: E402

SERVER_NAME = "dsh-codex-bridge"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

#: 单次查询最多返回多少条消息。
MAX_LIMIT = 1000
DEFAULT_LIMIT = 20

# ---------------------------------------------------------------- stdout 纪律

#: 真正的协议输出流。先把 sys.stdout 指向 stderr，任何意外打印都不会污染协议流。
_PROTOCOL_STDOUT = sys.stdout
sys.stdout = sys.stderr


def _protocol_bytes_stream():
    """优先使用底层二进制 buffer，避免 locale 编码问题。"""
    return getattr(_PROTOCOL_STDOUT, "buffer", None)


def write_message(payload: dict[str, Any]) -> None:
    """写出一整行协议报文到真实 stdout，并 flush。"""
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    raw = _protocol_bytes_stream()
    if raw is not None:
        raw.write(line.encode("utf-8"))
        raw.flush()
    else:  # pragma: no cover - 仅在 stdout 被替换成非标准流时走到
        _PROTOCOL_STDOUT.write(line)
        _PROTOCOL_STDOUT.flush()


def _result_obj(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_obj(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


# ---------------------------------------------------------------- 工具定义

TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_to_dsh",
        "description": (
            "Send directly to the bound DeepSeek desktop conversation via native RPC. "
            "Never enqueue or fall back to a queue. Returns native acceptance and message id. "
            "直接投递给已绑定的 DeepSeek 会话；缺少绑定或投递失败时明确报错。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "消息正文（必填，非空白）。Message body (required, non-blank).",
                },
                "title": {
                    "type": "string",
                    "description": "可选标题，添加在消息正文前。Optional title prepended to the body.",
                },
                "message_id": {
                    "type": "string",
                    "description": "可选稳定消息 ID，用于投递记录和防重。Optional stable message id.",
                },
                "reply_to": {
                    "type": "string",
                    "description": "可选被回复消息 ID。Optional id of the message being replied to.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "send_to_codex",
        "description": (
            "Send directly to the bound Codex desktop conversation via native RPC. "
            "Never enqueue or fall back to a queue. "
            "直接投递给已绑定的 Codex 会话；缺少绑定或投递失败时明确报错。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "消息正文（必填，非空白）。"},
                "title": {"type": "string", "description": "可选标题，添加在消息正文前。"},
                "message_id": {"type": "string", "description": "可选稳定消息 ID。"},
                "reply_to": {"type": "string", "description": "可选被回复消息 ID。"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_my_messages",
        "description": (
            "Legacy queue inspection only: peek old inbound messages in to_dsh. "
            "Does not consume messages or advance the cursor (the cursor belongs to DSH). "
            "仅查看旧桥接队列（不消费消息、不推进游标）；直接发送的消息不会出现在这里。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"最多返回多少条，默认 {DEFAULT_LIMIT}。",
                    "minimum": 1,
                }
            },
        },
    },
    {
        "name": "read_dsh_replies",
        "description": (
            "Legacy queue inspection only: peek old outbound messages in to_chatgpt. "
            "Does not consume messages or advance the cursor (the cursor belongs to the daemon). "
            "仅查看旧桥接队列（不消费消息、不推进游标）；直接发送的消息不会出现在这里。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"最多返回多少条，默认 {DEFAULT_LIMIT}。",
                    "minimum": 1,
                }
            },
        },
    },
    {
        "name": "bridge_status",
        "description": (
            "Read-only native desktop binding summary, without sending or connecting. "
            "Optionally include legacy queue depths. "
            "查看原生桌面绑定；可选附带旧队列积压，不发消息或访问桌面登录态。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_legacy": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否附带旧队列状态，默认不读取。",
                },
            },
        },
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


# ---------------------------------------------------------------- 参数工具


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_limit(raw: Any) -> tuple[int | None, str | None]:
    """把 limit 参数规整成正整数；返回 (值, 错误信息)。"""
    if raw is None:
        return DEFAULT_LIMIT, None
    if isinstance(raw, bool):
        return None, "limit 必须是正整数（不能是布尔值）"
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str) and raw.strip().lstrip("+-").isdigit():
        value = int(raw.strip())
    else:
        return None, f"limit 必须是正整数，收到：{raw!r}"
    if value < 1:
        return None, f"limit 必须 >= 1，收到：{value}"
    return min(value, MAX_LIMIT), None


def _text_result(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}]}


def _error_result(message: str) -> dict[str, Any]:
    """工具自身失败：按 MCP 约定走 result + isError，而不是 JSON-RPC error。"""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------- 工具实现


def _send_direct(arguments: dict[str, Any], destination: str) -> dict[str, Any]:
    tool_name = "send_to_dsh" if destination == "deepseek" else "send_to_codex"
    raw_text = arguments.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return _error_result(
            f"{tool_name} 失败：参数 text 必填，且必须是非空白字符串。"
        )
    for key in ("title", "message_id", "reply_to"):
        value = arguments.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return _error_result(f"{tool_name} 失败：参数 {key} 必须是非空白字符串。")
    raw_title = arguments.get("title")
    text = f"{raw_title}\n\n{raw_text}" if raw_title else raw_text
    try:
        delivery = desktop_link.send(
            desktop_link.load_config(), destination, text,
            message_id=arguments.get("message_id"), reply_to=arguments.get("reply_to"),
        )
    except desktop_link.LinkError as exc:
        return _error_result(f"{tool_name} 直接投递失败（未入队）：{exc}")
    if delivery.get("accepted") is not True:
        return _error_result(f"{tool_name} 未获原生通道确认（未入队）。")
    return _text_result({**delivery, "ok": True, "queued": False, "transport": "native-direct"})


def tool_send_to_dsh(arguments: dict[str, Any]) -> dict[str, Any]:
    return _send_direct(arguments, "deepseek")


def tool_send_to_codex(arguments: dict[str, Any]) -> dict[str, Any]:
    return _send_direct(arguments, "codex")


def _peek_tool(arguments: dict[str, Any], direction: str) -> dict[str, Any]:
    limit, error = _coerce_limit(arguments.get("limit"))
    if error:
        return _error_result(error)
    try:
        messages = bridge_core.peek_new(direction)
    except (OSError, ValueError) as exc:
        return _error_result(f"读取 {direction} 队列失败：{exc}")
    selected = messages[:limit]
    return _text_result(
        {
            "legacy": True,
            "direction": direction,
            "count": len(selected),
            "pending": len(messages),
            "cursor_advanced": False,
            "messages": selected,
        }
    )


def tool_get_my_messages(arguments: dict[str, Any]) -> dict[str, Any]:
    return _peek_tool(arguments, "to_dsh")


def tool_read_dsh_replies(arguments: dict[str, Any]) -> dict[str, Any]:
    return _peek_tool(arguments, "to_chatgpt")


def tool_bridge_status(arguments: dict[str, Any]) -> dict[str, Any]:
    include_legacy = arguments.get("include_legacy", False)
    if not isinstance(include_legacy, bool):
        return _error_result("include_legacy 必须是布尔值。")
    payload: dict[str, Any] = {
        "home": str(bridge_core.home()), "transport": "native-direct", "queued": False,
    }
    try:
        config = desktop_link.load_config()
    except desktop_link.LinkError as exc:
        payload["binding"] = {"bound": False, "error": str(exc)}
    else:
        payload["binding"] = {
            "bound": True,
            "codex_thread_id": config["codex_thread_id"],
            "dsh_session_id": config["dsh_session_id"],
            "dsh_url": config["dsh_url"],
        }
    if include_legacy:
        try:
            payload["legacy"] = {
                "thread": bridge_core.load_thread(),
                "pending_to_dsh": bridge_core.pending_count("to_dsh"),
                "pending_to_chatgpt": bridge_core.pending_count("to_chatgpt"),
            }
        except (OSError, ValueError) as exc:
            return _error_result(f"读取旧队列状态失败：{exc}")
    return _text_result(payload)


TOOL_HANDLERS = {
    "send_to_dsh": tool_send_to_dsh,
    "send_to_codex": tool_send_to_codex,
    "get_my_messages": tool_get_my_messages,
    "read_dsh_replies": tool_read_dsh_replies,
    "bridge_status": tool_bridge_status,
}


# ---------------------------------------------------------------- JSON-RPC


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    protocol_version = (
        requested
        if isinstance(requested, str) and requested.strip()
        else DEFAULT_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list() -> dict[str, Any]:
    return {"tools": TOOLS}


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error_result("tools/call 失败：缺少工具名 name。")
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _error_result(f"tools/call 失败：未知工具 {name!r}。")
    arguments = _as_mapping(params.get("arguments"))
    try:
        return handler(arguments)
    except Exception as exc:  # 工具异常不能拖垮服务器
        return _error_result(f"工具 {name} 执行异常：{type(exc).__name__}: {exc}")


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """把一条 JSON-RPC 消息处理成响应对象；None 表示无需响应。"""
    method = message.get("method")
    is_request = "id" in message

    if not isinstance(method, str):
        if not is_request:
            return None
        return _error_obj(message.get("id"), -32601, "Method not found: <missing method>")

    msg_id = message.get("id")
    params = _as_mapping(message.get("params"))

    if method == "initialize":
        return _result_obj(msg_id, handle_initialize(params))
    if method == "notifications/initialized":
        # 通知：MCP 规范下不产生响应（即使客户端误带了 id）。
        return None
    if method == "ping":
        return _result_obj(msg_id, {})
    if method == "tools/list":
        return _result_obj(msg_id, handle_tools_list())
    if method == "tools/call":
        return _result_obj(msg_id, handle_tools_call(params))
    if is_request:
        return _error_obj(msg_id, -32601, f"Method not found: {method}")
    # 未知通知静默忽略
    return None


def dispatch(message: dict[str, Any]) -> None:
    """处理一条 JSON-RPC 消息，必要时写出响应。"""
    response = handle(message)
    if response is not None:
        write_message(response)


# ---------------------------------------------------------------- 主循环


def serve(stdin_stream=None) -> int:
    """读 stdin 行、写 stdout 协议报文，直到 EOF。返回退出码。"""
    stream = stdin_stream if stdin_stream is not None else sys.stdin.buffer
    if hasattr(stream, "buffer"):
        stream = stream.buffer

    while True:
        try:
            line = stream.readline()
        except (KeyboardInterrupt, InterruptedError):
            continue
        except OSError:
            break
        if not line:
            break  # EOF -> 干净退出
        if isinstance(line, bytes):
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                continue  # 非法编码行：忽略
        else:
            text = line
        text = text.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue  # 非法 JSON 行：忽略，不崩溃
        if not isinstance(message, dict):
            continue  # 非对象（批量请求/标量）：忽略
        try:
            dispatch(message)
        except BrokenPipeError:
            break
        except Exception as exc:  # 任何内部错误都不能让进程崩掉
            print(f"mcp_server: dispatch error: {exc!r}", file=sys.stderr, flush=True)
            if "id" in message:
                try:
                    write_message(
                        _error_obj(message.get("id"), -32603, f"Internal error: {exc}")
                    )
                except OSError:
                    break
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION}", file=sys.stderr)
        return 0
    if argv:
        print(f"mcp_server: ignoring unknown args: {argv}", file=sys.stderr, flush=True)
    try:
        return serve()
    except BrokenPipeError:  # pragma: no cover - 客户端提前断开
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
