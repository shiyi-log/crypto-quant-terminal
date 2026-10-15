#!/usr/bin/env python3
"""挑选【最空闲】的 Codex 会话来派发任务（用户指示：按空闲配发，优先配发空闲会话）。

背景
----
用户 2026-10-15 指示：
  「codex 所有的会话都可以执行全部任务，不要再把任务塞给一个会话了」
  「不用分工，按空闲配发，优先配发空闲会话」
  「codex 完成任务让他报告」

⇒ DSH 每次派发前应先运行本脚本，取它推荐的 `--link`，
  而不是固定发给 `quant`（该线程曾独自承载 405 条派发）。

判定口径（如实声明其局限）
--------------------------
桥的 `messages` 表【没有】"会话忙/空闲"字段，回信也无法可靠归因：
  · 出站消息的 `target` = Codex 线程 id（可精确归因）
  · 入站消息的 `target` = DSH 会话 id（**四通道相同**）⇒ 无法直接归因到线程
  · 入站消息的 `reply_to` 只有约一半非空（实测 80/164）

⇒ 用三个【可观测代理】：
  ① **闲置时长** = now − 该线程最后一次被我派发的时刻（越大越空闲）← 主排序键
  ② **未回应派发数** = 该线程在"全局最后一次入站"之后收到的派发条数
     （>0 说明它可能还在处理，因为任何入站都会把计数器清零）
  ③ **占用守卫** = 若 **全部** 线程都有未回应派发且最近一次派发很新
     ⇒ **拒绝推荐任何会话**，提示"暂缓派发、先等报告"

⚠️ ②③ 都是代理：全局入站会把所有线程的计数一起清零。
   若某会话长时间不回，请再用 `link_watchdog.py` 观察（它会报 "accepted 但无回信"）。

只读（除 `--record` 会写一行到本地派发日志）。不写库、不改绑定、不发任何消息。

用法
----
    .venv/bin/python3 tools/codex_bridge/pick_idle_session.py
    .venv/bin/python3 tools/codex_bridge/pick_idle_session.py --record quant "退役 5 族"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

DB = Path.home() / ".dsh-codex-bridge" / "desktop.sqlite"
LOG = Path(__file__).resolve().parent / "dispatch_log.jsonl"
LINKS = {
    "quant": "01a11fbf-3414-7b52-9a03-01ef78e5539c",
    "quant-standby": "01a12bd8-c7bb-7f60-8833-be8f42320063",
    "quant-standby-2": "01a12bda-c9c6-79e0-abe7-2e5639cdfe0e",
    "quant-standby-3": "01a12bdd-bffd-7d21-8e56-6d74f18d8bd8",
}
PROBE_PREFIX = "watchdog-probe-"
# 派发后多久仍无回信就视为"可能在忙"
BUSY_AFTER_SECONDS = 600
# 同一线程未回应派发超过这个数，就提示我在堆积
PILEUP_WARN = 3


def _human(seconds: float) -> str:
    if seconds < 0:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.1f} 分"
    return f"{seconds / 3600:.1f} 小时"


def _snapshot() -> tuple[list[dict], float | None]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = conn.execute(
        "select max(created_at) from messages where destination='deepseek'"
    ).fetchone()
    last_inbound = float(row[0]) if row and row[0] else None

    out = []
    for link, thread in LINKS.items():
        r = conn.execute(
            """select id, status, created_at from messages
               where destination='codex' and target like ? and id not like ?
               order by created_at desc limit 1""",
            (f"%{thread}%", f"{PROBE_PREFIX}%"),
        ).fetchone()
        if not r:
            out.append({"link": link, "last_out": None, "status": None,
                        "unanswered": 0, "idle": float("inf")})
            continue
        last_out = float(r[2]) if r[2] else None
        if last_inbound is None:
            unans = conn.execute(
                """select count(*) from messages
                   where destination='codex' and target like ? and id not like ?""",
                (f"%{thread}%", f"{PROBE_PREFIX}%"),
            ).fetchone()[0]
        else:
            unans = conn.execute(
                """select count(*) from messages
                   where destination='codex' and target like ? and id not like ?
                     and created_at > ?""",
                (f"%{thread}%", f"{PROBE_PREFIX}%", last_inbound),
            ).fetchone()[0]
        out.append({"link": link, "last_out": last_out, "status": r[1],
                    "unanswered": int(unans),
                    "idle": (time.time() - last_out) if last_out else float("inf")})
    conn.close()
    return out, last_inbound


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--record", nargs=2, metavar=("LINK", "TASK"),
                    help="记录一次派发到 dispatch_log.jsonl（供事后审计）")
    args = ap.parse_args()

    if not DB.exists():
        print(f"  ❌ 找不到消息库: {DB}", file=sys.stderr)
        return 1

    # ⚠️ --record 必须在守卫/广播检测【之前】处理：
    #    它是"刚派发完"才调用的，而此时广播检测必然触发（刚发过），
    #    若放在后面就永远不可达。
    if args.record:
        link, task = args.record
        if link not in LINKS:
            print(f"  ❌ 未知 link: {link}（可选: {', '.join(LINKS)}）", file=sys.stderr)
            return 1
        rec = {"at": time.time(), "at_local": time.strftime("%Y-%m-%d %H:%M:%S"),
               "link": link, "task": task}
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  ✅ 已记录派发: {link} · {task[:50]}")
        print(f"     日志: {LOG}")
        return 0

    rows, last_inbound = _snapshot()
    now = time.time()

    print("  ══ Codex 会话空闲度（按空闲配发的依据）══")
    if last_inbound:
        print(f"    全局最后一次入站: {time.strftime('%H:%M:%S', time.localtime(last_inbound))}"
              f"（{_human(now - last_inbound)} 前）")
    else:
        print("    全局最后一次入站: 无")
    print()
    print(f"    {'通道':<18}{'最后派发':>10}{'闲置':>10}{'未回应':>8}{'状态':>12}")
    for d in sorted(rows, key=lambda x: x["unanswered"]):
        ts = (time.strftime("%H:%M:%S", time.localtime(d["last_out"]))
              if d["last_out"] else "—")
        idle = _human(d["idle"]) if d["last_out"] else "从未派发"
        warn = " ⚠️堆积" if d["unanswered"] >= PILEUP_WARN else ""
        print(f"    {d['link']:<18}{ts:>10}{idle:>10}{d['unanswered']:>8}"
              f"{str(d['status']):>12}{warn}")

    # ── 广播检测：短时间内给多个线程派发 ⇒ 回信无法归因 ⇒ 占用守卫会失效 ──
    win = now - 900  # 15 分钟
    recent_all = []
    conn2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for link, thread in LINKS.items():
        r = conn2.execute(
            """select max(created_at) from messages
               where destination='codex' and target like ? and id not like ?
                 and created_at > ?""",
            (f"%{thread}%", f"{PROBE_PREFIX}%", win),
        ).fetchone()
        if r and r[0]:
            recent_all.append((link, float(r[0])))
    conn2.close()
    if len(recent_all) > 1:
        print()
        print(f"  ⚠️ 【广播检测】最近 15 分钟内向 {len(recent_all)} 个线程派发过：")
        for link, ts in sorted(recent_all, key=lambda x: x[1]):
            print(f"       {link:<18}{time.strftime('%H:%M:%S', time.localtime(ts))}")
        print("     ⇒ 【回信无法归因到具体线程】⇒ 下面的「未回应」计数会在")
        print("        【任意一条】回信到达时被【全部清零】⇒ 占用守卫因此失效。")
        print("     ⇒ 纪律：【一次一条】—— 发给一个会话、等它报告、再发下一条。")
        print()
        print("  ⇒ 本次【不推荐】任何会话（先等积压的报告回来，再按一次一条继续）。")
        return 0

    # ── 占用守卫：全部线程都有"较新的未回应派发" ⇒ 拒绝推荐 ──
    free = [d for d in rows if d["unanswered"] == 0]
    recently_dispatched = [d for d in rows
                           if d["last_out"] and (now - d["last_out"]) < BUSY_AFTER_SECONDS]
    print()
    if not free and len(recently_dispatched) == len(rows):
        newest = min(now - d["last_out"] for d in rows if d["last_out"])
        print(f"  🛑 【占用守卫：暂缓派发】")
        print(f"     4 个会话全部在 {_human(newest)} 内收到过派发且【都还没有回信】。")
        print(f"     ⇒ 【不要再发新消息】—— 继续发只会把「空闲」信号冲掉，且可能造成排队。")
        print(f"     ⇒ 先用 link_watchdog.py 看是否有 accepted 但无回信的堆积；")
        print(f"        若长时间无回信，报告用户，不要自行重启或改绑定。")
        print()
        print("  ⇒ 本次【不推荐】任何会话。")
        return 0

    if free:
        pick = min(free, key=lambda d: (d["last_out"] or 0))
        why = "尚无未回应派发"
    else:
        pick = min(rows, key=lambda d: (d["unanswered"], d["last_out"] or 0))
        why = "未回应最少且派发最早"
    print(f"  ⇒ 推荐派发到: --link {pick['link']}")
    print(f"     （{why} · 闲置 {_human(pick['idle']) if pick['last_out'] else '从未派发'}"
          f" · 未回应 {pick['unanswered']}）")

    piled = [d for d in rows if d["unanswered"] >= PILEUP_WARN]
    if piled:
        print()
        print("  ⚠️ 堆积提示: " + ", ".join(f"{d['link']}({d['unanswered']} 条未回应)" for d in piled))
        print("     这些会话可能还在处理，或已卡住 —— 先等报告，不要继续加码。")

    print()
    print("  ⚠️ 局限：桥无「会话忙/空闲」字段；以上是【闲置时长 + 未回应条数】两个代理，")
    print("     不是「对面正在执行 turn」的直接证据。")
    print("  ⚠️ 纪律：派发后该会话视为【已占用】，下次另选；")
    print("     并在收到它的【完成报告】后再给它新任务（用户要求：完成必报）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
