"""DSH ⇄ ChatGPT(Codex Desktop) 桥接：出站 Codex 客户端。

职责单一：把一条 prompt 投递给本机 `codex` CLI，并把结果规范化为一个普通 dict。
协议见 PROTOCOL.md；共享状态（thread.json 等）一律通过 bridge_core 读写，
本模块不直接碰队列/游标/流水文件，也不读写任何凭据（不读 ~/.codex/auth.json）。

设计约束
--------
* 只依赖标准库。
* :func:`run_turn` **绝不抛异常**：超时、CLI 不存在、非零退出、JSONL 损坏，
  全部转换成 ``{"ok": False, "error": "..."}``。
* 超时用 ``Popen`` + ``communicate(timeout=...)`` 实现；超时后杀掉整个进程组。
* 最终回答优先取 ``-o`` 临时文件，回退到 JSONL 里最后一条 agent_message。

已实测事实（PROTOCOL.md 第 0 节）
--------------------------------
* 新建线程：``codex exec --json -o <outfile> "<prompt>"``，stdout 首行是
  ``{"type":"thread.started","thread_id":"<uuid>"}``。
* 续聊：``codex exec --json -o <outfile> resume <uuid> "<prompt>"``；
  **父命令（exec）的选项必须写在 resume 之前**，否则报 unexpected argument。
* 必须在 ``bridge_core.DEFAULT_CODEX_CWD``（trust_level = "trusted"）下运行。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:  # 允许 `python codex_client.py` / 从任意 cwd 直接导入
    sys.path.insert(0, _HERE)

try:  # 作为包的一部分导入时走相对导入
    from . import bridge_core  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 常规脚本式导入
    import bridge_core  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_RAW_EVENTS",
    "ERROR_LIMIT",
    "build_command",
    "codex_session_id",
    "extract_agent_text",
    "extract_thread_id",
    "parse_events",
    "run_turn",
]

#: 单次调用的默认超时（codex 一次调用通常 15~60 秒）。
DEFAULT_TIMEOUT = 300.0

#: raw_events 最多保留的事件条数。
MAX_RAW_EVENTS = 500

#: error 字段的最大字符数（含 stderr 摘要）。
ERROR_LIMIT = 2000

#: 临时 -o 文件前缀。
_TMP_PREFIX = "codex-bridge-out-"


# ---------------------------------------------------------------- 命令组装


def build_command(
    prompt: str,
    *,
    cli: str = "codex",
    model: str | None = None,
    out_path: str | None = None,
    thread_id: str | None = None,
    sandbox: str | None = None,
) -> list[str]:
    """组装 argv。

    顺序（关键）：``cli exec [exec 选项...] [resume <id>] <prompt>``。
    ``-o`` / ``-c`` 都是 exec 的父命令选项，必须排在 ``resume`` 之前。
    """
    argv: list[str] = [cli, "exec", "--json"]
    if out_path:
        argv += ["-o", out_path]
    if model:
        argv += ["-c", f'model="{model}"']
    if sandbox:
        argv += ["-c", f'sandbox_mode="{sandbox}"']
    if thread_id:
        argv += ["resume", thread_id]
    argv.append(prompt)
    return argv


# ---------------------------------------------------------------- JSONL 解析


def parse_events(stdout: str) -> list[dict[str, Any]]:
    """把 stdout 逐行解析成事件列表；坏行/非 JSON 行直接跳过。"""
    events: list[dict[str, Any]] = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def extract_thread_id(events: list[dict[str, Any]]) -> str | None:
    """从事件流里取线程 id：优先 thread.started，其次任意带 thread_id 的事件。"""
    for event in events:
        if event.get("type") == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid.strip():
                return tid.strip()
    for event in events:
        tid = event.get("thread_id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    return None


def extract_agent_text(events: list[dict[str, Any]]) -> str:
    """取最后一条 agent_message 正文；优先 item.completed。"""
    for want_completed in (True, False):
        for event in reversed(events):
            if want_completed and event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
            if event.get("type") == "agent_message":
                text = event.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def _cap_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把事件裁剪到 MAX_RAW_EVENTS 条：保留首条 thread.started + 末尾若干条。

    尾部包含最终的 agent_message，头部包含线程标识，排障时两者都重要。
    """
    if len(events) <= MAX_RAW_EVENTS:
        return list(events)
    head: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "thread.started":
            head = [event]
            break
    if not head:
        head = events[:1]
    tail = events[-(MAX_RAW_EVENTS - len(head)) :]
    return head + tail


# ---------------------------------------------------------------- 小工具


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[-limit:]


def _compose_error(message: str, stderr: str) -> str:
    """拼 error：原因 + stderr 摘要（尾部最多 ERROR_LIMIT 字符），整体不超过限制。"""
    tail = _truncate((stderr or "").strip(), ERROR_LIMIT)
    combined = f"{message} | stderr: {tail}" if tail else message
    return combined[:ERROR_LIMIT]


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """杀掉整个进程组（Popen 时用了 start_new_session=True）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - 进程已消失
        pass


def _drain(proc: subprocess.Popen[str], grace: float) -> tuple[str, str]:
    """尽力收尸并取回已缓冲的输出；再超时就放弃，绝不阻塞。"""
    try:
        out, err = proc.communicate(timeout=grace)
        return out or "", err or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.output or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):  # pragma: no cover - text 模式下不会发生
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):  # pragma: no cover
            err = err.decode("utf-8", errors="replace")
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:  # pragma: no cover
                pass
        return out, err


def _new_result(
    *,
    started: float,
    text: str = "",
    thread_id: str | None = None,
    error: str | None = None,
    raw_events: list[dict[str, Any]] | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": error is None,
        "text": text,
        "thread_id": thread_id,
        "duration_s": round(time.monotonic() - started, 3),
        "error": error,
        "raw_events": raw_events or [],
        "command": command or [],
    }


# ---------------------------------------------------------------- 线程 id


def _resolve_explicit_cli(cli: str, cwd: str | None) -> bool:
    """cli 是路径形式时校验其存在性；是裸命令名时不校验（交给 PATH）。"""
    if not isinstance(cli, str) or not cli:
        return False
    looks_like_path = os.sep in cli or (os.altsep is not None and os.altsep in cli)
    if not looks_like_path:
        return True
    candidate = cli
    if not os.path.isabs(candidate) and cwd:
        candidate = os.path.join(cwd, candidate)
    return os.path.isfile(candidate) and os.access(candidate, os.X_OK)


def codex_session_id(cli: str = "codex", *, cwd: str | None = None) -> str | None:
    """返回当前应续聊的 Codex 线程 id，没有则 None（表示下一条走新建线程）。

    数据来源是桥接状态 ``thread.json``（经 bridge_core.load_thread()），
    因为只有桥接层知道"当前会话"是哪一个。

    语义
    ----
    * ``thread.json`` 里有非空 ``thread_id`` ⇒ 返回它（下一条出站用 resume）。
    * 否则 ⇒ None（下一条出站新建线程）。
    * ``cli`` 传入明确的**路径**但不可执行 ⇒ None。
      裸命令名（默认 ``"codex"``）不做 PATH 探测，避免环境差异造成误判。
    * ``cwd`` 用于解析相对路径形式的 ``cli``。

    本函数绝不抛异常。
    """
    try:
        if not _resolve_explicit_cli(cli, cwd):
            return None
        thread = bridge_core.load_thread()
        if not isinstance(thread, dict):
            return None
        tid = thread.get("thread_id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
        return None
    except Exception:  # noqa: BLE001 - 契约：永不抛异常
        return None


# ---------------------------------------------------------------- 主入口


def run_turn(
    prompt: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    model: str | None = None,
    cwd: str | None = None,
    thread_id: str | None = None,
    cli: str = "codex",
) -> dict[str, Any]:
    """跑一轮 codex 对话，返回统一结果 dict（永不抛异常）。

    ``thread_id`` 为空 → 新建线程；否则 ``codex exec ... resume <uuid>``。

    返回::

        {
          "ok": bool,          # 是否拿到回答（rc==0 且正文非空）
          "text": str,         # 回答正文；失败时为空串
          "thread_id": str|None,   # 新建时来自 thread.started；resume 时沿用传入值
          "duration_s": float,
          "error": str|None,   # 失败原因（含 stderr 摘要，最多 2000 字符）
          "raw_events": list,  # JSONL 事件，最多 500 条
          "command": list[str] # 实际执行的 argv
        }
    """
    try:
        return _run_turn(
            prompt,
            timeout=timeout,
            model=model,
            cwd=cwd,
            thread_id=thread_id,
            cli=cli,
        )
    except Exception as exc:  # noqa: BLE001 - 契约：run_turn 绝不抛异常
        return {
            "ok": False,
            "text": "",
            "thread_id": thread_id if isinstance(thread_id, str) else None,
            "duration_s": 0.0,
            "error": _truncate(f"unexpected error: {exc!r}", ERROR_LIMIT),
            "raw_events": [],
            "command": [],
        }


def _run_turn(
    prompt: str,
    *,
    timeout: float,
    model: str | None,
    cwd: str | None,
    thread_id: str | None,
    cli: str,
) -> dict[str, Any]:
    started = time.monotonic()
    workdir = cwd if cwd is not None else bridge_core.DEFAULT_CODEX_CWD
    normalized_thread = (
        thread_id.strip()
        if isinstance(thread_id, str) and thread_id.strip()
        else None
    )

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT

    out_path: str | None = None
    command: list[str] = []
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    spawn_error: str | None = None

    try:
        fd, out_path = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=".txt")
        os.close(fd)
    except OSError as exc:
        return _new_result(
            started=started,
            thread_id=normalized_thread,
            error=_truncate(f"cannot create output file: {exc}", ERROR_LIMIT),
            command=[],
        )

    file_text = ""
    try:
        try:
            command = build_command(
                prompt,
                cli=cli,
                model=model,
                out_path=out_path,
                thread_id=normalized_thread,
            )
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_value)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                stdout = exc.output or ""
                stderr = exc.stderr or ""
                if isinstance(stdout, bytes):  # pragma: no cover - text 模式
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):  # pragma: no cover - text 模式
                    stderr = stderr.decode("utf-8", errors="replace")
                _kill_process_tree(proc)
                trailing_out, trailing_err = _drain(proc, 5.0)
                stdout += trailing_out
                stderr += trailing_err
            returncode = proc.returncode
        except FileNotFoundError as exc:
            spawn_error = f"codex CLI not found: {exc}"
        except NotADirectoryError as exc:
            spawn_error = f"invalid cwd {workdir!r}: {exc}"
        except PermissionError as exc:
            spawn_error = f"cannot execute codex CLI: {exc}"
        except OSError as exc:
            spawn_error = f"failed to start codex CLI: {exc}"
        # 必须"先读后删"：-o 文件是最终回答的首选来源。
        file_text = _read_text(out_path).strip()
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    events = parse_events(stdout)
    return _build_result(
        started=started,
        events=events,
        file_text=file_text,
        command=command,
        returncode=returncode,
        timed_out=timed_out,
        spawn_error=spawn_error,
        timeout_value=timeout_value,
        thread_id=normalized_thread,
        stderr=stderr,
    )


def _build_result(
    *,
    started: float,
    events: list[dict[str, Any]],
    file_text: str,
    command: list[str],
    returncode: int | None,
    timed_out: bool,
    spawn_error: str | None,
    timeout_value: float,
    thread_id: str | None,
    stderr: str,
) -> dict[str, Any]:
    raw_events = _cap_events(events)
    resolved_thread = thread_id or extract_thread_id(events)

    if spawn_error:
        return _new_result(
            started=started,
            thread_id=resolved_thread,
            error=_truncate(spawn_error, ERROR_LIMIT),
            raw_events=raw_events,
            command=command,
        )

    if timed_out:
        return _new_result(
            started=started,
            thread_id=resolved_thread,
            error=_compose_error(
                f"codex timed out after {timeout_value:g}s", stderr
            ),
            raw_events=raw_events,
            command=command,
        )

    if returncode != 0:
        return _new_result(
            started=started,
            thread_id=resolved_thread,
            error=_compose_error(f"codex exited with code {returncode}", stderr),
            raw_events=raw_events,
            command=command,
        )

    text = file_text or extract_agent_text(events).strip()
    if not text:
        return _new_result(
            started=started,
            thread_id=resolved_thread,
            error=_compose_error("codex returned no answer text", stderr),
            raw_events=raw_events,
            command=command,
        )

    return _new_result(
        started=started,
        text=text,
        thread_id=resolved_thread,
        error=None,
        raw_events=raw_events,
        command=command,
    )
