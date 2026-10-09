#!/usr/bin/env python3
"""DSH <-> ChatGPT(Codex Desktop) 双向桥接守护进程 / CLI。

用法见 PROTOCOL.md 第 5 节：

    bridge.py send "<text>"        出站入队（不做网络调用）
    bridge.py poll                打印新的入站消息
    bridge.py thread              打印线程状态
    bridge.py state               打印游标与队列状态
    bridge.py flush               立即消费一次出站队列
    bridge.py serve               前台守护进程（消费出站 + HTTP 监听）
    bridge.py start / stop        后台启停守护进程
    bridge.py health              查询守护进程
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_core as core  # noqa: E402

try:  # T1 的模块；缺失时降级为"仅入队"，不阻塞其他方向
    import codex_client
except ImportError:  # pragma: no cover
    codex_client = None  # type: ignore[assignment]


MAX_REQUEST_BYTES = 256 * 1024
IDLE_SLEEP_S = 1.0


# ------------------------------------------------------------------ 出站消费


def _extract_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    return text if isinstance(text, str) else ""


def consume_once(*, timeout: float = 300.0, verbose: bool = True) -> list[dict[str, Any]]:
    """消费 to_chatgpt 队列里的所有新消息，把回答写回 to_dsh。

    返回本次处理的结果列表。单条失败不影响后续消息。
    """
    messages = core.read_messages("to_chatgpt")
    if not messages:
        return []

    if codex_client is None:
        # 依赖缺失：把消息放回队列尾部会让顺序彻底乱掉，所以直接报告失败。
        for message in messages:
            core.append_message(
                "to_dsh",
                f"[桥接故障] codex_client 模块不可用，无法投递出站消息 {message.get('id')}",
                source="daemon",
                kind="system",
                meta={"error": "codex_client_missing", "outbound_id": message.get("id")},
            )
        core.log("ERROR codex_client 不可用，出站消息未能投递")
        return [{"ok": False, "error": "codex_client_missing"}]

    results: list[dict[str, Any]] = []
    for message in messages:
        text = _extract_text(message)
        thread = core.load_thread()
        thread_id = message.get("thread_id") or thread.get("thread_id") or None
        started = time.time()

        result = codex_client.run_turn(text, timeout=timeout, thread_id=thread_id)
        elapsed = round(time.time() - started, 2)

        if result.get("ok"):
            new_thread = result.get("thread_id") or thread_id
            core.save_thread(
                thread_id=new_thread,
                turn_count=int(thread.get("turn_count") or 0) + 1,
                last_outbound_id=message.get("id"),
                last_error=None,
                cwd=result.get("cwd") or core.DEFAULT_CODEX_CWD,
            )
            reply = core.append_message(
                "to_dsh",
                result.get("text") or "(空回答)",
                source="chatgpt",
                kind="reply",
                thread_id=new_thread,
                meta={
                    "re": message.get("id"),
                    "duration_s": result.get("duration_s", elapsed),
                },
            )
            core.ledger(
                {
                    "ts": core.now_iso(),
                    "event": "delivered",
                    "direction": "to_chatgpt",
                    "id": message.get("id"),
                    "reply_id": reply.get("id"),
                    "thread_id": new_thread,
                    "duration_s": result.get("duration_s", elapsed),
                }
            )
            if verbose:
                print(f"[bridge] 已投递 {message.get('id')} → 回答 {len(reply['text'])} 字 ({elapsed}s)")
        else:
            error = (result.get("error") or "unknown")[:2000]
            core.save_thread(last_error=error)
            core.append_message(
                "to_dsh",
                f"[投递失败] {error}",
                source="daemon",
                kind="system",
                thread_id=thread_id,
                meta={"error": error, "re": message.get("id")},
            )
            core.ledger(
                {
                    "ts": core.now_iso(),
                    "event": "failed",
                    "direction": "to_chatgpt",
                    "id": message.get("id"),
                    "error": error,
                }
            )
            if verbose:
                print(f"[bridge] 投递失败 {message.get('id')}: {error}", file=sys.stderr)

        results.append(result)
    return results


def _consumer_loop(stop: threading.Event, timeout: float) -> None:
    core.log("consumer loop started")
    while not stop.is_set():
        try:
            consume_once(timeout=timeout, verbose=True)
        except Exception as exc:  # noqa: BLE001 - 守护进程绝不能因单次失败退出
            core.log(f"ERROR consumer loop: {exc!r}")
        stop.wait(IDLE_SLEEP_S)
    core.log("consumer loop stopped")


# ------------------------------------------------------------------ HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "dsh-codex-bridge/1.0"
    protocol_version = "HTTP/1.1"

    # -- 工具

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        core.log("http " + (fmt % args))

    # -- 路由

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            thread = core.load_thread()
            self._send(
                200,
                {
                    "ok": True,
                    "thread_id": thread.get("thread_id"),
                    "turn_count": thread.get("turn_count", 0),
                    "pending_out": core.pending_count("to_chatgpt"),
                    "pending_in": core.pending_count("to_dsh"),
                    "codex_client": codex_client is not None,
                    "home": str(core.home()),
                },
            )
        elif path == "/thread":
            self._send(200, core.load_thread())
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/send", "/inbound"):
            self._send(404, {"ok": False, "error": "not_found"})
            return
        data = self._read_body()
        if data is None:
            self._send(400, {"ok": False, "error": "invalid_json_body"})
            return
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(400, {"ok": False, "error": "text_required"})
            return

        if path == "/send":
            message = core.append_message(
                "to_chatgpt",
                text,
                source="dsh",
                thread_id=data.get("thread_id") or None,
                meta={"via": "http"},
            )
            self._send(202, {"ok": True, "id": message["id"], "queued": True})
        else:
            message = core.append_message(
                "to_dsh",
                text,
                source=str(data.get("source") or "external"),
                kind=str(data.get("kind") or "message"),
                meta={"via": "http"},
            )
            self._send(202, {"ok": True, "id": message["id"]})


def serve(*, port: int, host: str = "127.0.0.1", timeout: float = 300.0) -> int:
    core.ensure_layout()
    stop = threading.Event()
    worker = threading.Thread(target=_consumer_loop, args=(stop, timeout), daemon=True)
    worker.start()

    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    core.pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    core.log(f"http listening on {host}:{port}")

    def shutdown(signum: int, _frame: Any) -> None:
        core.log(f"signal {signum}, shutting down")
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    try:
        print(f"[bridge] listening http://{host}:{port}  home={core.home()}", flush=True)
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
        core.log("stopped")
    return 0


# ------------------------------------------------------------------ CLI


def cmd_send(args: argparse.Namespace) -> int:
    message = core.append_message(
        "to_chatgpt", args.text, source="dsh", thread_id=args.thread_id or None
    )
    print(json.dumps(message, ensure_ascii=False, indent=2))
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    messages = core.read_messages(
        args.direction, advance=not args.peek, limit=args.limit
    )
    if args.json:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return 0
    if not messages:
        print("(没有新消息)")
        return 0
    for message in messages:
        kind = message.get("kind", "message")
        source = message.get("source", "?")
        print(f"--- [{kind}] from={source} id={message.get('id')} {message.get('ts')} ---")
        if message.get("meta"):
            print(f"meta: {json.dumps(message['meta'], ensure_ascii=False)}")
        print(message.get("text", ""))
    return 0


def cmd_thread(_args: argparse.Namespace) -> int:
    print(json.dumps(core.load_thread(), ensure_ascii=False, indent=2))
    return 0


def cmd_state(_args: argparse.Namespace) -> int:
    cursors = core.load_cursors()
    state = {
        "home": str(core.home()),
        "cursors": cursors,
        "pending": {d: core.pending_count(d) for d in core.DIRECTIONS},
        "thread": core.load_thread(),
        "codex_client": codex_client is not None,
        "daemon_pid": _read_pid(),
    }
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    results = consume_once(timeout=args.timeout)
    if not results:
        print("(出站队列为空)")
        return 0
    ok = sum(1 for r in results if r.get("ok"))
    print(f"处理 {len(results)} 条，成功 {ok} 条")
    for r in results:
        if not r.get("ok"):
            print("  失败: " + str(r.get("error"))[:500], file=sys.stderr)
    return 0 if ok == len(results) else 1


def _read_pid() -> int | None:
    try:
        return int(core.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmd_start(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"已在运行 pid={pid}")
        return 0
    core.ensure_layout()
    logfile = core.log_path().open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(args.port)],
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(Path(__file__).resolve().parent),
    )
    time.sleep(1.5)
    if proc.poll() is not None:
        print("启动失败，见 " + str(core.log_path()), file=sys.stderr)
        return 1
    print(f"已启动 pid={proc.pid} port={args.port} log={core.log_path()}")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid or not _alive(pid):
        print("未在运行")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _alive(pid):
            print(f"已停止 pid={pid}")
            return 0
        time.sleep(0.25)
    os.kill(pid, signal.SIGKILL)
    print(f"已强制停止 pid={pid}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{args.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            print(response.read().decode("utf-8"))
        return 0
    except (urllib.error.URLError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("send", help="出站入队：DSH → ChatGPT")
    p.add_argument("text")
    p.add_argument("--thread-id", default=None)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("poll", help="读取入站消息")
    p.add_argument("--direction", choices=core.DIRECTIONS, default="to_dsh")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.add_argument("--peek", action="store_true", help="不推进游标")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("thread", help="打印线程状态")
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("state", help="打印游标与队列状态")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("flush", help="立即消费一次出站队列")
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("serve", help="前台守护进程")
    p.add_argument("--port", type=int, default=core.DEFAULT_HTTP_PORT)
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=lambda a: serve(port=a.port, timeout=a.timeout))

    p = sub.add_parser("start", help="后台启动守护进程")
    p.add_argument("--port", type=int, default=core.DEFAULT_HTTP_PORT)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="停止守护进程")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("health", help="查询守护进程")
    p.add_argument("--port", type=int, default=core.DEFAULT_HTTP_PORT)
    p.set_defaults(func=cmd_health)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
