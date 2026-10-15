#!/usr/bin/env python3
"""安全进程检查（Round 316）—— 从根上消除「grep 自匹配」这一反复出现的错误。

问题
----
我用 `ps ... | grep -c "pattern"` 统计进程时，**grep 自身（以及调用它的 shell）
的命令行里就含有该模式** ⇒ 计数被虚增 1（或更多）。

本会话已犯【4 次】：
  · R295 `ps aux | grep "[f]etch_new_sources"` —— 匹配到自己的 bash 命令 ⇒ 误判"进程在运行"
  · R299 / R309 / R316 采集器计数 —— 同为自匹配

做法
----
用 Python 拿 `ps` 输出后**在进程内过滤**：
  ① 排除本进程及其祖先链（它们一定含命令行参数）
  ② 排除任何 `grep` / `pgrep` / 本脚本路径出现其中的行
  ③ 只匹配 `command` 字段，不匹配别的列

只读。退出码 0 = 正常；1 = 参数问题。

用法
----
    .venv/bin/python3 tools/check_processes.py market_stream.py
    .venv/bin/python3 tools/check_processes.py --require-intervals 1m,5m,15m,1h,1d market_stream.py
    .venv/bin/python3 tools/check_processes.py --json market_stream.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SELF = os.path.realpath(__file__)


def _ancestors() -> set[int]:
    """本进程 + 全部祖先 pid（它们的命令行一定含脚本名/参数）。"""
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(12):
        if pid <= 1:
            break
        pids.add(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            pid = int(out) if out else 0
        except Exception:
            break
    return pids


def list_processes(pattern: str) -> list[dict]:
    excluded = _ancestors()
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,lstart=,command="],
                         capture_output=True, text=True).stdout
    rows: list[dict] = []
    for line in out.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$", line)
        if not m:
            continue
        pid, ppid, started, cmd = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        # ① 祖先链排除
        if pid in excluded or ppid in excluded:
            continue
        # ② grep/pgrep/本脚本 排除
        if re.search(r"\b(p?grep)\b", cmd) or SELF in cmd or "check_processes.py" in cmd:
            continue
        # ③ 只在 command 里匹配
        if pattern not in cmd:
            continue
        rows.append({"pid": pid, "started": started.strip(), "command": cmd})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pattern", help="在 command 字段中匹配的子串")
    ap.add_argument("--require-intervals", default=None,
                    help="只统计 command 同时含该子串的进程（例如 1m,5m,15m,1h,1d）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = list_processes(args.pattern)
    managed = rows
    if args.require_intervals:
        managed = [r for r in rows if args.require_intervals in r["command"]]

    if args.json:
        print(json.dumps({"all": rows, "filtered": managed,
                          "count_all": len(rows), "count_filtered": len(managed)},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"  ══ 进程检查: pattern={args.pattern!r} "
          f"{'· 附带 ' + args.require_intervals if args.require_intervals else ''} ══")
    print(f"    匹配总数（已排除自身/祖先/grep）: 【{len(rows)}】")
    for r in rows:
        tag = "✅" if (not args.require_intervals or args.require_intervals in r["command"]) else "○"
        print(f"      {tag} pid {r['pid']:<7}{r['started']:<26}{r['command'][:96]}")
    if args.require_intervals:
        print(f"    ⇒ 满足「{args.require_intervals}」的: 【{len(managed)}】")
    print()
    print("  ⚠️ 本工具的价值: `ps | grep -c` 会把 grep 自身算进去（本会话已犯 4 次）。")
    print("     凡是统计进程，请用本工具，不要用 shell 管道。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
