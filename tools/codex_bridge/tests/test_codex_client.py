"""codex_client 的离线单元测试（纯标准库 unittest，默认不联网）。

策略：不真调模型 —— 在临时目录里生成假的 `codex` 可执行脚本（/bin/sh），
用它们驱动 run_turn，覆盖 thread_id 解析、回答回退、超时、非零退出、
CLI 不存在、argv 顺序（resume 的父命令选项必须在 resume 之前）等。

真实联网测试默认跳过，只在 CODEX_BRIDGE_LIVE=1 时运行。

运行：
    cd tools/codex_bridge && ../../../.venv/bin/python3 -m unittest tests.test_codex_client -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bridge_core  # noqa: E402
import codex_client  # noqa: E402

LIVE_FLAG = os.environ.get("CODEX_BRIDGE_LIVE") == "1"

THREAD_NEW = "aaaaaaaa-1111-2222-3333-444444444444"
THREAD_FROM_RESUME_STREAM = "zzzzzzzz-9999-8888-7777-666666666666"
THREAD_GARBAGE = "garbage-thread-0001"
THREAD_CAP = "cap-thread-0001"
THREAD_EMPTY = "empty-thread-0001"

# ---------------------------------------------------------------- 假 CLI 脚本

#: 正常新建线程：JSONL 里给 FALLBACK_TEXT，同时把 FILE_TEXT 写进 -o 文件。
#: 用于验证 "-o 文件优先于 JSONL"。
SCRIPT_OK_WITH_FILE = r"""
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_NEW__"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"FALLBACK_TEXT"}}'
if [ -n "$out" ]; then printf '%s\n' 'FILE_TEXT' > "$out"; fi
exit 0
"""

#: 正常新建线程但不往 -o 文件写内容 ⇒ 必须回退到 JSONL。
SCRIPT_OK_NO_FILE = r"""
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_NEW__"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"FALLBACK_TEXT"}}'
if [ -n "$out" ]; then : > "$out"; fi
exit 0
"""

#: resume：把收到的 argv 落到文件，同时故意输出一个不同的 thread_id，
#: 用来断言 run_turn 沿用调用方传入的 thread_id。
SCRIPT_RESUME = r"""
printf '%s\n' "$@" > "__ARGS_LOG__"
printf '%s\n' '{"type":"thread.started","thread_id":"__STREAM_THREAD__"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"RESUMED"}}'
exit 0
"""

#: 非零退出。
SCRIPT_FAIL = r"""
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_NEW__"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"IGNORED"}}'
printf '%s\n' 'BOOM_DETAIL' >&2
exit 3
"""

#: 卡住不返回（记录 pid 供测试确认进程已被杀）。
SCRIPT_HANG = r"""
echo $$ > "__PID_FILE__"
sleep 30
printf '%s\n' '{"type":"thread.started","thread_id":"never"}'
exit 0
"""

#: JSONL 损坏：坏行必须被跳过，好行仍要解析。
SCRIPT_GARBAGE = r"""
printf '%s\n' 'this is not json at all'
printf '%s\n' '{ truncated'
printf '\n'
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_GARBAGE__"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"SURVIVED"}}'
exit 0
"""

#: 事件洪水：702 条事件 ⇒ raw_events 必须被裁到 500 条，但尾部 agent_message 仍要能取到。
SCRIPT_MANY_EVENTS = r"""
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_CAP__"}'
i=0
while [ "$i" -lt 700 ]; do
  printf '{"type":"noise","n":%s}\n' "$i"
  i=$((i + 1))
done
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"TAIL_TEXT"}}'
exit 0
"""

#: rc=0 但没有任何回答文本。
SCRIPT_EMPTY = r"""
printf '%s\n' '{"type":"thread.started","thread_id":"__THREAD_EMPTY__"}'
exit 0
"""


def make_fake_cli(directory: Path, script: str, name: str = "codex") -> str:
    """在 directory 下写一个可执行的假 codex 脚本，返回其路径。"""
    path = directory / name
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 别人的进程
        return True
    return True


class FakeCodexTestCase(unittest.TestCase):
    """所有假 CLI 测试的公共夹具：一个临时目录 + 可用的默认 cwd。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-client-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def spawn(self, script: str, name: str = "codex", **subs: str) -> str:
        body = script
        for key, value in subs.items():
            body = body.replace(f"__{key.upper()}__", str(value))
        return make_fake_cli(self.tmp, body, name=name)


class CommandAssemblyTests(unittest.TestCase):
    """argv 组装（纯函数，无子进程）。"""

    def test_new_thread_command_order(self) -> None:
        argv = codex_client.build_command(
            "hello world", cli="/bin/echo", out_path="/tmp/o.txt"
        )
        self.assertEqual(
            argv, ["/bin/echo", "exec", "--json", "-o", "/tmp/o.txt", "hello world"]
        )

    def test_resume_puts_parent_options_before_resume(self) -> None:
        argv = codex_client.build_command(
            "hi",
            cli="codex",
            model="gpt-6.1-sol",
            out_path="/tmp/o.txt",
            thread_id=THREAD_NEW,
        )
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[2], "--json")
        resume_index = argv.index("resume")
        # 父命令（exec）选项 -o / -c 必须全部排在 resume 之前。
        self.assertLess(argv.index("-o"), resume_index)
        self.assertEqual(argv[argv.index("-c") : argv.index("-c") + 2], ["-c", 'model="gpt-6.1-sol"'])
        self.assertEqual(argv[resume_index:], ["resume", THREAD_NEW, "hi"])
        # resume 之后除了 <id> <prompt> 不应再有别的选项
        self.assertEqual(argv[resume_index + 1 :], [THREAD_NEW, "hi"])

    def test_sandbox_option_also_before_resume(self) -> None:
        argv = codex_client.build_command(
            "hi", out_path="/tmp/o.txt", thread_id=THREAD_NEW, sandbox="read-only"
        )
        resume_index = argv.index("resume")
        self.assertLess(argv.index("-c"), resume_index)
        self.assertIn('-c', argv)
        self.assertEqual(argv[argv.index("-c") + 1], 'sandbox_mode="read-only"')


class ParseHelperTests(unittest.TestCase):
    """JSONL 解析与提取。"""

    def test_parse_events_skips_bad_lines(self) -> None:
        events = codex_client.parse_events(
            "not json\n{\"a\": 1}\n\n{bad\n[1,2]\n"
        )
        self.assertEqual(events, [{"a": 1}])

    def test_extract_thread_id_prefers_thread_started(self) -> None:
        events = [
            {"type": "noise", "thread_id": "wrong"},
            {"type": "thread.started", "thread_id": THREAD_NEW},
        ]
        self.assertEqual(codex_client.extract_thread_id(events), THREAD_NEW)

    def test_extract_agent_text_takes_last_completed(self) -> None:
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "one"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "two"}},
        ]
        self.assertEqual(codex_client.extract_agent_text(events), "two")


class RunTurnTests(FakeCodexTestCase):
    """run_turn 的端到端离线行为。"""

    def test_new_thread_parses_thread_id_and_prefers_out_file(self) -> None:
        cli = self.spawn(SCRIPT_OK_WITH_FILE, thread_new=THREAD_NEW)
        result = codex_client.run_turn(
            "ping", cli=cli, cwd=str(self.tmp), timeout=60.0
        )
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "FILE_TEXT")
        self.assertEqual(result["thread_id"], THREAD_NEW)
        self.assertIsNone(result["error"])
        self.assertIsInstance(result["duration_s"], float)
        self.assertGreaterEqual(result["duration_s"], 0.0)
        self.assertTrue(result["raw_events"])
        # 新建线程：不含 resume
        self.assertNotIn("resume", result["command"])
        self.assertEqual(result["command"][0], cli)
        self.assertEqual(result["command"][-1], "ping")
        # 临时 -o 文件必须已清理
        out_path = result["command"][result["command"].index("-o") + 1]
        self.assertFalse(os.path.exists(out_path), f"temp file leaked: {out_path}")

    def test_answer_falls_back_to_jsonl_when_out_file_empty(self) -> None:
        cli = self.spawn(SCRIPT_OK_NO_FILE, thread_new=THREAD_NEW)
        result = codex_client.run_turn("ping", cli=cli, cwd=str(self.tmp), timeout=60.0)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "FALLBACK_TEXT")
        self.assertEqual(result["thread_id"], THREAD_NEW)

    def test_resume_reuses_passed_thread_id_and_argv_order(self) -> None:
        args_log = self.tmp / "args.txt"
        cli = self.spawn(
            SCRIPT_RESUME,
            args_log=str(args_log),
            stream_thread=THREAD_FROM_RESUME_STREAM,
        )
        result = codex_client.run_turn(
            "continue please",
            cli=cli,
            cwd=str(self.tmp),
            thread_id=THREAD_NEW,
            model="gpt-6.1-sol",
            timeout=60.0,
        )
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "RESUMED")
        # resume 必须沿用调用方传入的 thread_id（而不是 stdout 里的那个）
        self.assertEqual(result["thread_id"], THREAD_NEW)

        recorded = args_log.read_text(encoding="utf-8").splitlines()
        # 假 CLI 记录的是 shell 的 "$@"，即 argv[1:]（$0 是脚本自身路径）
        self.assertEqual(recorded, result["command"][1:])
        self.assertEqual(recorded[0:2], ["exec", "--json"])
        self.assertEqual(recorded[2], "-o")
        self.assertEqual(recorded[4:6], ["-c", 'model="gpt-6.1-sol"'])
        resume_index = recorded.index("resume")
        self.assertLess(recorded.index("-o"), resume_index)
        self.assertLess(recorded.index("-c"), resume_index)
        self.assertEqual(
            recorded[resume_index:], ["resume", THREAD_NEW, "continue please"]
        )

    def test_timeout_returns_not_ok_and_kills_process(self) -> None:
        pid_file = self.tmp / "child.pid"
        cli = self.spawn(SCRIPT_HANG, pid_file=str(pid_file))
        started = time.monotonic()
        result = codex_client.run_turn(
            "hang forever", cli=cli, cwd=str(self.tmp), timeout=1.0
        )
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIn("timed out", result["error"])
        self.assertLess(elapsed, 15.0, "timeout 没有及时生效")
        self.assertEqual(result["command"][-1], "hang forever")

        self.assertTrue(pid_file.exists(), "假 CLI 未启动")
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5.0
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(pid_alive(pid), f"超时后子进程 {pid} 仍在运行")

    def test_nonzero_exit_returns_not_ok_with_stderr(self) -> None:
        cli = self.spawn(SCRIPT_FAIL, thread_new=THREAD_NEW)
        result = codex_client.run_turn("ping", cli=cli, cwd=str(self.tmp), timeout=60.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIn("exited with code 3", result["error"])
        self.assertIn("BOOM_DETAIL", result["error"])
        self.assertLessEqual(len(result["error"]), codex_client.ERROR_LIMIT)
        # 即便失败，也把可解析出的线程 id 带回来（便于排障/续聊）
        self.assertEqual(result["thread_id"], THREAD_NEW)

    def test_missing_cli_returns_not_ok(self) -> None:
        missing = str(self.tmp / "definitely-not-here" / "codex")
        result = codex_client.run_turn(
            "ping", cli=missing, cwd=str(self.tmp), timeout=10.0
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["thread_id"])
        self.assertIn("not found", result["error"].lower())
        self.assertEqual(result["command"][0], missing)
        self.assertIsInstance(result["raw_events"], list)

    def test_corrupt_jsonl_is_survivable(self) -> None:
        cli = self.spawn(SCRIPT_GARBAGE, thread_garbage=THREAD_GARBAGE)
        result = codex_client.run_turn("ping", cli=cli, cwd=str(self.tmp), timeout=60.0)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "SURVIVED")
        self.assertEqual(result["thread_id"], THREAD_GARBAGE)
        self.assertEqual(len(result["raw_events"]), 2)

    def test_raw_events_are_capped_but_extraction_uses_all(self) -> None:
        cli = self.spawn(SCRIPT_MANY_EVENTS, thread_cap=THREAD_CAP)
        result = codex_client.run_turn("ping", cli=cli, cwd=str(self.tmp), timeout=60.0)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "TAIL_TEXT")
        self.assertEqual(len(result["raw_events"]), codex_client.MAX_RAW_EVENTS)
        self.assertEqual(result["raw_events"][0]["type"], "thread.started")
        self.assertEqual(result["raw_events"][-1]["type"], "item.completed")
        self.assertEqual(result["thread_id"], THREAD_CAP)

    def test_success_without_any_text_is_not_ok(self) -> None:
        cli = self.spawn(SCRIPT_EMPTY, thread_empty=THREAD_EMPTY)
        result = codex_client.run_turn("ping", cli=cli, cwd=str(self.tmp), timeout=60.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIn("no answer text", result["error"])
        self.assertEqual(result["thread_id"], THREAD_EMPTY)

    def test_default_cwd_is_trusted_project_dir(self) -> None:
        cli = self.spawn(SCRIPT_OK_NO_FILE, thread_new=THREAD_NEW)
        self.assertTrue(os.path.isdir(bridge_core.DEFAULT_CODEX_CWD))
        result = codex_client.run_turn("ping", cli=cli, timeout=60.0)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["text"], "FALLBACK_TEXT")

    def test_unexpected_input_never_raises(self) -> None:
        # 空 prompt / None：契约要求返回 dict 而不是抛异常。
        for bad_prompt in ("", None, 123):
            result = codex_client.run_turn(
                bad_prompt,  # type: ignore[arg-type]
                cli=str(self.tmp / "nope"),
                cwd=str(self.tmp),
                timeout=5.0,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(set(result), {
                "ok", "text", "thread_id", "duration_s", "error", "raw_events", "command",
            })


class CodexSessionIdTests(unittest.TestCase):
    """codex_session_id：读 bridge_core 的 thread.json，绝不抛异常。"""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="codex-bridge-home-"))
        self.addCleanup(shutil.rmtree, str(self.home), ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CODEX_BRIDGE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_none_without_thread_file(self) -> None:
        self.assertIsNone(codex_client.codex_session_id())

    def test_returns_persisted_thread_id(self) -> None:
        bridge_core.save_thread(thread_id=THREAD_NEW, model="gpt-6.1-sol")
        self.assertEqual(codex_client.codex_session_id(), THREAD_NEW)
        self.assertEqual(
            codex_client.codex_session_id("codex", cwd=bridge_core.DEFAULT_CODEX_CWD),
            THREAD_NEW,
        )

    def test_blank_thread_id_returns_none(self) -> None:
        bridge_core.save_thread(thread_id="   ")
        self.assertIsNone(codex_client.codex_session_id())

    def test_corrupt_thread_json_returns_none(self) -> None:
        bridge_core.ensure_layout()
        bridge_core.thread_path().write_text("{not json", encoding="utf-8")
        self.assertIsNone(codex_client.codex_session_id())

    def test_explicit_missing_cli_path_returns_none(self) -> None:
        bridge_core.save_thread(thread_id=THREAD_NEW)
        self.assertIsNone(
            codex_client.codex_session_id(str(self.home / "no-such-codex"))
        )

    def test_relative_cli_path_resolves_against_cwd(self) -> None:
        bridge_core.save_thread(thread_id=THREAD_NEW)
        make_fake_cli(self.home, "exit 0\n")
        self.assertEqual(
            codex_client.codex_session_id("./codex", cwd=str(self.home)), THREAD_NEW
        )
        self.assertIsNone(codex_client.codex_session_id("./missing", cwd=str(self.home)))


# ---------------------------------------------------------------- 真实联网（默认跳过）


@unittest.skipUnless(
    LIVE_FLAG,
    "真实联网测试默认跳过；设 CODEX_BRIDGE_LIVE=1 才运行（会真的调用本机 codex，约 15~60s/次）",
)
class LiveCodexTests(unittest.TestCase):
    """真调一次本机 codex（走 127.0.0.1:15721 代理）。"""

    def test_live_new_thread(self) -> None:
        result = codex_client.run_turn("Reply with exactly: LIVE_OK", timeout=300.0)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["thread_id"], "thread_id 必须非空")
        self.assertIn("LIVE_OK", result["text"])

    def test_live_resume_keeps_context_and_thread_id(self) -> None:
        first = codex_client.run_turn(
            "Remember this codeword: LIVE_MEMORY_TOKEN. Reply with exactly: STORED",
            timeout=300.0,
        )
        self.assertTrue(first["ok"], first.get("error"))
        self.assertTrue(first["thread_id"])

        second = codex_client.run_turn(
            "What codeword did I ask you to remember? Reply with exactly that codeword.",
            thread_id=first["thread_id"],
            timeout=300.0,
        )
        self.assertTrue(second["ok"], second.get("error"))
        self.assertEqual(second["thread_id"], first["thread_id"])
        self.assertIn("LIVE_MEMORY_TOKEN", second["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
