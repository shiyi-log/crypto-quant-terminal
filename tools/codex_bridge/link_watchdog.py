#!/usr/bin/env python3
"""
联动通道进程守护 —— 只监测、只告警，不擅自重启任何东西。

════════════════════════════════════════════════════════════════════
为什么需要它
════════════════════════════════════════════════════════════════════
当前联动走 `desktop_link.py`（不是 bridge.py 那条独立 CLI 会话方案）。
它是按需运行的 CLI，本身没有常驻进程 —— 但它依赖四个环节，
任一挂掉都会让通道【静默失效】：

    ① Codex 桌面应用（/Applications/ChatGPT.app）—— 原生 IPC owner 可用
    ② DeepSeek 桌面应用与被绑定的 DSH 会话可用
    ③ 绑定健康（desktop.json 指向的会话仍存在且可用，端口可达）
    ④ 消息不堆积 —— accepted 但没有回信、且时间过长，说明对面没在处理

DESKTOP_LINK.md 自己写着：「两个桌面应用需正常运行；电脑休眠或应用关闭时
不能保证即时处理。」——本脚本就是把这句话变成可检测的信号。

════════════════════════════════════════════════════════════════════
设计原则
════════════════════════════════════════════════════════════════════
· 【只报告，不重启】—— 重启 ChatGPT 桌面或 DSH 是有侵入性的动作，
  可能丢失对面正在进行的会话，必须由人决定。
· 【复用官方 CLI】—— 绑定与流水都通过 desktop_link.py 读取，
  不自己去解析 desktop.json/desktop.sqlite 的内部结构，避免与桥接实现脱节。
· 【写审计流水】—— 每次快照追加一行 JSON，可回溯通道何时失效。

用法:
    python link_watchdog.py              # 完整检查
    python link_watchdog.py --quiet      # 定时任务用：健康只回一行
    python link_watchdog.py --history 20 # 看历史
    python link_watchdog.py --stuck-min 15   # 卡住判定阈值（默认 15 分钟）
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP_LINK = os.path.join(_HERE, "desktop_link.py")
BRIDGE_HOME = os.environ.get("CODEX_BRIDGE_HOME", os.path.expanduser("~/.dsh-codex-bridge"))
LEDGER = os.path.join(BRIDGE_HOME, "desktop.sqlite")
LOG = os.path.join(BRIDGE_HOME, "watchdog.jsonl")
CODEX_APP = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
HARNESS_PORT = int(os.environ.get("DSH_PORT", "19387"))


def py():
    """用 uv 环境跑桥接脚本（与项目一致）。"""
    cand = os.path.join(os.path.dirname(os.path.dirname(_HERE)), ".venv", "bin", "python3")
    return cand if os.path.exists(cand) else sys.executable


def run_link(*args, timeout=25):
    try:
        r = subprocess.run([py(), DESKTOP_LINK, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -2, "", f"{type(e).__name__}: {e}"


def proc_running(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
        return bool(out), (out[0] if out else None)
    except Exception:
        return False, None


def codex_uptime_s():
    """Return Codex process uptime in seconds when it can be read."""
    try:
        pids = subprocess.run(["pgrep", "-f", CODEX_APP], capture_output=True,
                              text=True).stdout.split()
        if not pids:
            return None
        elapsed = subprocess.run(["ps", "-o", "etimes=", "-p", pids[0]],
                                 capture_output=True, text=True).stdout.strip()
        return int(elapsed) if elapsed.isdigit() else None
    except Exception:
        return None


def port_open(port):
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def check(stuck_min=15):
    s = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
         "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "problems": [], "warnings": []}

    # ① Codex 桌面
    ok, pid = proc_running(CODEX_APP)
    s["codex_app"] = ok
    s["codex_pid"] = pid
    if not ok:
        s["problems"].append("Codex 桌面应用未运行 —— 原生 IPC 投递不可用")

    # ② DSH 桌面（Harness 进程）
    ok2, pid2 = proc_running("DeepSeek Harness.app")
    s["dsh_app"] = ok2
    s["dsh_pid"] = pid2
    if not ok2:
        s["problems"].append("DSH 桌面应用未运行 —— 入站消息无法投递")

    # ③ Harness 端口
    s["harness_port"] = HARNESS_PORT
    s["harness_port_open"] = port_open(HARNESS_PORT)
    if not s["harness_port_open"]:
        s["problems"].append(f"Harness 端口 {HARNESS_PORT} 不可达 —— 入站投递会失败")

    # ④ 绑定健康（用官方 CLI 读）
    rc, out, err = run_link("status")
    s["status_rc"] = rc
    if rc != 0:
        s["problems"].append(f"desktop_link status 失败 (rc={rc}): {(err or out).strip()[:160]}")
    else:
        try:
            st = json.loads(out)
            s["ready"] = st.get("ready")
            ds = st.get("deepseek") or {}
            s["bound_session"] = ds.get("session_id")
            s["bound_title"] = ds.get("title")
            s["bound_running"] = ds.get("running")
            s["codex_thread"] = st.get("codex_thread_id")
            if not st.get("ready"):
                s["problems"].append("绑定 ready=false")
            if not ds.get("running"):
                s["warnings"].append(
                    f"被绑定会话 {ds.get('session_id')} 当前 running=false —— "
                    "入站消息会等到它下次运行才被处理")
        except Exception as e:
            s["problems"].append(f"status 输出无法解析: {type(e).__name__}")

    # ④b 【发送探针】—— 上面的检查只看进程/端口/绑定，全绿也可能发不出去。
    #   实测教训：direct_codex.py 的旧回执校验曾在对面空闲时恒抛错，
    #   而四项检查全绿 → 监控给出"健康"的假信心。必须真的试发一次。
    import uuid as _uuid
    probe_id = f"watchdog-probe-{_uuid.uuid4().hex[:8]}"
    # Only send once through the shared entry point. A timeout or missing
    # confirmation does not prove rejection, so a second sender could duplicate
    # an already accepted message or start another turn.
    rc_p, out_p, err_p = run_link("send", "--to", "codex", "--id", probe_id,
                                  "--text", "WATCHDOG_PROBE (自动探针，忽略即可)")
    try:
        probe_receipt = json.loads(out_p)
    except (TypeError, ValueError):
        probe_receipt = None
    s["send_probe_via"] = "desktop_link"
    s["send_probe_ok"] = (rc_p == 0 and isinstance(probe_receipt, dict)
                          and probe_receipt.get("accepted") is True)
    if not s["send_probe_ok"]:
        detail = ((err_p or out_p).strip().replace("\n", " ")[:200]
                  or "缺少有效 accepted=true 回执；投递未确认")
        s["send_probe_error"] = detail
        # ── 区分「应用刚重启的启动窗口」与「真的坏了」 ──
        # 实测（2026-10-10 16:28）：Codex 应用 16:28:44 重启，同秒探针报
        # no-client-found；16:31:15 重试成功。启动窗口约 2~3 分钟。
        # 本函数【保持不重试】的既有策略（避免重复投递）。
        # 应用刚重启时可附加提示，但探针未确认仍必须保持 unhealthy，
        # 否则监控会把“实际不可发送”误报为健康。
        uptime = codex_uptime_s()
        s["codex_uptime_s"] = uptime
        if uptime is not None and uptime < 300:
            s["warnings"].append(
                f"发送探针未获确认，但 Codex 应用仅运行 {uptime} 秒 —— "
                f"很可能是重启后的 IPC 注册窗口（约需 2~3 分钟），稍后复验即可: {detail}")
        s["problems"].append(
            f"❌ 发送探针未获确认 —— 未重试，需核对实际是否收到: {detail}")

    # ⑤ 流水：卡住的消息（accepted 但长时间无回复）
    if os.path.exists(LEDGER):
        try:
            c = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
            cols = [r[1] for r in c.execute("PRAGMA table_info(messages)")]
            rows = [dict(zip(cols, r)) for r in c.execute("select * from messages")]
            c.close()
            s["n_messages"] = len(rows)
            replied_to = {r.get("reply_to") for r in rows if r.get("reply_to")}
            now = time.time()
            stuck = []
            # 已知的测试/诊断残留与已被取代的消息 —— 不再重复告警
            # （不改 Codex 的流水库，只在本监控里标注）
            IGNORE_PREFIXES = ("iso-", "bi-", "watchdog-probe-", "dsh-probe-",
                               "probe-quant-link-", "probe-default-link-", "probe-retry-")
            SUPERSEDED = {
                # 该条在通道中断期未送达，内容已换新 ID 重发并成功
                "dsh-ablation-and-fixes-20261009": "dsh-channel-bug-and-ablation-20261010",
            }
            for r in rows:
                mid = r.get("id") or ""
                if r.get("status") == "unconfirmed":
                    if mid.startswith(IGNORE_PREFIXES):
                        continue                      # 诊断探针，非业务消息
                    if mid in SUPERSEDED:
                        s.setdefault("superseded", []).append(
                            f"{mid} → 已由 {SUPERSEDED[mid]} 重发")
                        continue
                    s["problems"].append(
                        f"存在未确认消息 {mid} —— 需人工核对是否已送达")
                # ⚠ 只有【出站】消息才期待回信。入站消息（destination=deepseek）
                #   往往是通知类，本来就不需要回复，算进去会产生假阳性。
                if r.get("destination") != "codex":
                    continue
                # 诊断探针不算「卡住」—— 它们本来就不期待回信
                if (r.get("id") or "").startswith(("iso-", "bi-", "watchdog-probe-")):
                    continue
                if r["id"] in replied_to:
                    continue
                age = (now - float(r.get("created_at") or 0)) / 60
                if age >= stuck_min and r.get("status") == "accepted":
                    stuck.append((r["id"], r["destination"], round(age, 1)))
            s["stuck"] = stuck
            if stuck:
                s["warnings"].append(
                    "以下消息已 accepted 但超过 %d 分钟无回信（对面可能在忙或未处理）: %s"
                    % (stuck_min, "; ".join(f"{i}({d},{a}min)" for i, d, a in stuck)))
            # 最近一条
            if rows:
                last = max(rows, key=lambda r: float(r.get("created_at") or 0))
                s["last_message"] = {"id": last["id"], "status": last.get("status"),
                                     "destination": last.get("destination")}
        except Exception as e:
            s["problems"].append(f"读取流水失败: {type(e).__name__}: {e}")
    else:
        s["problems"].append(f"流水库不存在: {LEDGER}")

    s["healthy"] = not s["problems"]
    return s


def show(s):
    print(f"  [{s['ts']}]")
    print(f"    Codex 桌面  {'✅ ' + str(s.get('codex_pid')) if s.get('codex_app') else '❌ 未运行'}")
    print(f"    DSH 桌面    {'✅ ' + str(s.get('dsh_pid')) if s.get('dsh_app') else '❌ 未运行'}")
    print(f"    Harness 端口 {s.get('harness_port')}  {'✅ 可达' if s.get('harness_port_open') else '❌ 不可达'}")
    if s.get("ready") is not None:
        print(f"    绑定        ready={s['ready']} · {s.get('bound_title')} · "
              f"running={s.get('bound_running')}")
    print(f"    发送探针    {'✅ 可发送' if s.get('send_probe_ok') else '❌ 不可发送'}"
          f"{'（经 ' + s['send_probe_via'] + '）' if s.get('send_probe_via') else ''}")
    print(f"    流水        {s.get('n_messages','?')} 条 · 最近 {s.get('last_message')}")
    if s.get("warnings"):
        print("    ⚠️ 提示:")
        for w in s["warnings"]:
            print(f"       · {w}")
    if s["problems"]:
        print("    ❌ 异常:")
        for p in s["problems"]:
            print(f"       · {p}")
    else:
        print("    ✅ 通道健康")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="健康时只回一行（定时任务用）")
    ap.add_argument("--history", type=int, default=0)
    ap.add_argument("--stuck-min", type=int, default=15)
    args = ap.parse_args()

    if args.history:
        if not os.path.exists(LOG):
            print("  还没有守护快照")
            return
        with open(LOG, encoding="utf-8") as history:
            rows = [json.loads(line) for line in history if line.strip()]
        print(f"  最近 {min(args.history,len(rows))} 条（共 {len(rows)} 条）:")
        for r in rows[-args.history:]:
            mark = "✅" if r.get("healthy") else "❌ " + "; ".join(r.get("problems", []))[:80]
            print(f"    {r['ts']}  codex={'up' if r.get('codex_app') else 'DOWN'} "
                  f"dsh={'up' if r.get('dsh_app') else 'DOWN'} "
                  f"ready={r.get('ready')}  {mark}")
        return

    s = check(stuck_min=args.stuck_min)
    os.makedirs(BRIDGE_HOME, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(s, ensure_ascii=False) + "\n")

    if args.quiet and s["healthy"]:
        print(f"  联动通道健康 · codex={'up' if s.get('codex_app') else 'DOWN'} "
              f"· 绑定 running={s.get('bound_running')} · 探针 ✅ · 流水 {s.get('n_messages')} 条")
        return
    show(s)


if __name__ == "__main__":
    main()
