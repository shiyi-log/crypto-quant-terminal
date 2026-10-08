#!/usr/bin/env python3
"""策略研究数据刷新守护进程

为什么需要它（真实发生过的问题）：
    `build_research.py` 是一次性脚本，start.sh / auto_iterate.py / auto_research.py
    都没有调用它 —— 于是「策略研究」页长期停在最后一次手工运行的时点
    （实测停在半天前），看起来就是"很久没更新"。

    这里按固定间隔重建 `user_data/research_summary.json`：
      · 失败时保留上一份 summary（不写坏文件），并把失败原因写进状态文件；
      · 状态文件供 /locals/research_status 读取，页面可显示"最近一次刷新失败"。

用法:
    python refresh_research.py                 # 只跑一次
    python refresh_research.py --daemon        # 常驻，默认每 6 小时
    python refresh_research.py --interval 3600
"""

import argparse
import contextlib
import io
import json
import os
import sys
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

SUMMARY = "user_data/research_summary.json"
STATUS = "user_data/research_refresh.json"
DEFAULT_INTERVAL = 6 * 3600


def _read_summary_generated_at():
    try:
        with open(SUMMARY, encoding="utf-8") as f:
            return json.load(f).get("generated_at")
    except Exception:
        return None


def refresh_once(verbose=True):
    """重建一次策略研究汇总；返回状态记录"""
    t0 = time.time()
    rec = {
        "attempt_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": False,
    }
    try:
        import build_research

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            build_research.main()
        tail = [x for x in buf.getvalue().strip().splitlines() if x.strip()]
        rec["ok"] = True
        rec["log"] = tail[-3:]
        if verbose:
            for line in tail:
                print(f"  {line}")
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()[-1200:]
        if verbose:
            print(f"  ⚠ 刷新失败: {rec['error']}", file=sys.stderr)

    rec["duration_s"] = round(time.time() - t0, 1)
    rec["summary_generated_at"] = _read_summary_generated_at()
    try:
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return rec


def main():
    ap = argparse.ArgumentParser(description="策略研究数据刷新")
    ap.add_argument("--daemon", action="store_true", help="常驻模式")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="刷新间隔秒数（默认 6 小时）")
    args = ap.parse_args()

    if not args.daemon:
        print("=" * 88)
        print("策略研究数据刷新")
        print("=" * 88)
        rec = refresh_once()
        mark = "✅ 成功" if rec["ok"] else "❌ 失败"
        print(f"  {mark}  {rec['duration_s']}s  → {rec.get('summary_generated_at')}")
        return 0 if rec["ok"] else 1

    print(f"  策略研究刷新启动，每 {args.interval / 3600:g} 小时一轮")
    while True:
        print(f"\n── {time.strftime('%Y-%m-%d %H:%M')} ──")
        rec = refresh_once()
        print(f"  {'✅ 已刷新' if rec['ok'] else '❌ 刷新失败'}  {rec['duration_s']}s")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
