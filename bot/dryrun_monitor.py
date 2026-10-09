#!/usr/bin/env python3
"""
干跑监控快照 —— 一次检查，同时写入持久化审计日志。

════════════════════════════════════════════════════════════════════
为什么需要它
════════════════════════════════════════════════════════════════════
盯盘是持续动作，但如果每次检查只输出到对话里，历史就丢了 ——
看不到「持仓如何变化」「什么时候出现第一笔平仓」「敞口是否漂移」。

本脚本每次运行做完整检查，并把结果【追加】成一行 JSON 到
    user_data/dryrun_monitor.jsonl
这样能回溯任意时刻的状态，也能算变化量。

检查项（与每日校验的口径一致）：
  ① 进程：trade / webserver 是否都在
  ② 交易库：mtime、总笔数、持仓、平仓、方向、敞口
  ③ 异常检测：
     · 新出现的 .bak- 重置文件
     · exit_reason 非 exit_signal 的平仓
     · 库超过 26 小时未更新
     · 与上次快照相比的持仓变化
  ④ 白名单数据新鲜度（20 对是否都到最新日线）
  ⑤ 每个持仓的离场距离

⚠️ 时区铁律：trades 表里的 open_date/close_date 是【UTC】，北京 = UTC + 8。

用法:
    python dryrun_monitor.py              # 检查并写日志
    python dryrun_monitor.py --history 20 # 看最近 20 条快照
    python dryrun_monitor.py --quiet      # 只在异常时输出
"""
import argparse
import glob
import json
import math
import os
import sqlite3
import subprocess
import sys
import time

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_HERE, "tradesv3.dryrun.sqlite")
LOG = os.path.join(_HERE, "user_data", "dryrun_monitor.jsonl")
STALE_HOURS = 26
STRATEGY_FILE = os.path.join(_HERE, "user_data", "strategies", "TrendFollowing.py")


def _file_fingerprint(path):
    """返回 (mtime, md5前12位, 字节数) —— 用于判断策略文件是否被改动。"""
    import hashlib
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "md5": h.hexdigest()[:12], "bytes": st.st_size}


def _proc_start(pid):
    """进程启动时间 —— 用于判断 trade 进程是否被重启过。
    ⚠ 失败时返回 'ERR:<原因>' 而不是 None —— 否则比对条件恒假、告警永不触发
      （这正是第一版的问题：subprocess 没在模块级 import，NameError 被静默吞掉）。"""
    if not pid:
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return f"ERR:ps 无输出(pid={pid})"
        return str(pd.Timestamp(out))
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def prog(name):
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True).stdout.split()
        return out[0] if out else None
    except Exception:
        return None


def whitelist_freshness(data, whitelist, now=None):
    """检查完整白名单的最新已收盘 UTC 日线；缺数据不能被子集覆盖掩盖。"""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    expected = now.normalize() - pd.Timedelta(days=1)
    fresh, missing = {}, {}
    for coin in dict.fromkeys(whitelist):
        frame = data.get(coin)
        if frame is None or frame.empty:
            missing[coin] = "missing_or_empty_history"
            continue
        if "close" not in frame:
            missing[coin] = "missing_close"
            continue
        dates = pd.to_datetime(frame.index, utc=True, errors="coerce")
        closed = frame.loc[(dates <= expected) & dates.notna()]
        if closed.empty:
            missing[coin] = "missing_closed_candle"
            continue
        last_date = pd.to_datetime(closed.index, utc=True).max()
        rows = closed.loc[pd.to_datetime(closed.index, utc=True) == last_date]
        if len(rows) != 1:
            missing[coin] = "ambiguous_closed_candle"
            continue
        try:
            price = float(rows["close"].iloc[0])
        except (TypeError, ValueError):
            price = float("nan")
        if not math.isfinite(price) or price <= 0:
            missing[coin] = "invalid_closed_close"
            continue
        fresh[coin] = last_date.date().isoformat()
    stale = {coin: date for coin, date in fresh.items()
             if date < expected.date().isoformat()}
    return {"fresh": fresh, "missing": missing, "stale": stale,
            "expected_closed_candle": expected.isoformat(),
            "fresh_count": len(fresh) - len(stale),
            "total": len(dict.fromkeys(whitelist)),
            "ready": bool(whitelist) and not missing and not stale}


def snapshot():
    s = {"ts": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
         "ts_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
         "problems": []}

    s["pid_trade"] = prog("freqtrade trade")
    s["pid_ws"] = prog("freqtrade webserver")
    # 策略文件指纹 —— Codex 的三项修复落地时会变
    s["strategy_file"] = _file_fingerprint(STRATEGY_FILE)
    # trade 进程启动时间 —— 变了说明重启过（新代码自此生效）
    s["trade_start"] = _proc_start(s["pid_trade"])
    if not s["pid_trade"]:
        s["problems"].append("trade 进程不在 —— 信号会漏、持仓失去管理")
    if not s["pid_ws"]:
        s["problems"].append("webserver 进程不在")

    if not os.path.exists(DB):
        s["problems"].append(f"找不到交易库 {DB}")
        return s

    s["db_mtime_age_min"] = round((time.time() - os.path.getmtime(DB)) / 60, 1)
    if s["db_mtime_age_min"] > STALE_HOURS * 60:
        s["problems"].append(f"交易库 {s['db_mtime_age_min']/60:.0f} 小时未更新（>={STALE_HOURS}h）")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tr = pd.read_sql_query("select * from trades", con)
    con.close()
    tr["is_open"] = tr["is_open"].astype(bool)
    op = tr[tr["is_open"]]
    cl = tr[~tr["is_open"]]

    s["n_total"] = int(len(tr))
    s["n_open"] = int(len(op))
    s["n_closed"] = int(len(cl))
    s["long"] = int((~op["is_short"].astype(bool)).sum())
    s["short"] = int(op["is_short"].astype(bool).sum())
    s["stake_sum"] = round(float(op["stake_amount"].sum()), 2)
    s["exposure_pct"] = round(float(op["stake_amount"].sum()) / 9000 * 100, 2)
    s["pairs"] = sorted(op["pair"].str.split("/").str[0].tolist())

    # 平仓原因
    if len(cl):
        reasons = cl["exit_reason"].value_counts().to_dict()
        s["exit_reasons"] = {str(k): int(v) for k, v in reasons.items()}
        bad = [r for r in reasons if r not in ("exit_signal", None)]
        if bad:
            s["problems"].append(f"❌ 出现非信号离场: {bad}（应为 exit_signal）")
        s["realized_pnl"] = round(float(cl["close_profit_abs"].sum()), 2)

    # 残留备份（重置痕迹）
    baks = sorted(glob.glob(DB + ".bak-*"))
    s["n_bak"] = len(baks)
    if baks:
        s["latest_bak"] = os.path.basename(baks[-1])

    # 白名单数据新鲜度
    try:
        import ml_lab as ml
        cfgp = os.path.join(_HERE, "user_data", "config_trend_live.json")
        wl = [p.split("/")[0] for p in json.load(open(cfgp))["exchange"]["pair_whitelist"]]
        data = ml.load_ohlcv()
        coverage = whitelist_freshness(data, wl)
        s["wl_fresh"] = coverage["fresh_count"]
        s["wl_total"] = coverage["total"]
        s["wl_missing"] = coverage["missing"]
        s["wl_stale"] = coverage["stale"]
        s["wl_ready"] = coverage["ready"]
        s["wl_expected_closed_candle"] = coverage["expected_closed_candle"]
        if not coverage["ready"]:
            # ⚠ 因果升级：修复后的 confirm_trade_entry 要求【全白名单】数据就绪
            #   （snapshot["ready"] 才放行）。任意一个白名单币缺数据/数据陈旧
            #   → ready=False → 整个策略【静默停止开仓】，不会报错。
            #   所以这里不是"某个币数据旧"的提示，而是"策略可能已完全停止交易"的告警。
            s["problems"].append(
                f"❌ 白名单已收盘数据不完整: missing={coverage['missing']} "
                f"stale={coverage['stale']} expected={coverage['expected_closed_candle']} "
                "—— 修复后的门控要求全池就绪，策略可能已完全停止开仓，必须处理")
        # 每个持仓的离场距离
        sys.path.insert(0, _HERE)
        from dryrun_verify import signals, coin_of
        ex = []
        for _, r in op.iterrows():
            c = coin_of(r["pair"])
            d = data.get(c)
            if d is None or d.empty or "close" not in d:
                continue
            sg = signals(d)
            cur = float(d["close"].iloc[-1])
            line = float(sg["ll_entry"].iloc[-1]) \
                if not bool(r["is_short"]) else float(sg["hh_entry"].iloc[-1])
            ex.append({"coin": c, "pnl_pct": round((cur / float(r["open_rate"]) - 1) * 100, 2),
                       "to_exit_pct": round((line / cur - 1) * 100, 2)})
        s["positions"] = ex
    except Exception as e:
        s["problems"].append(f"数据新鲜度/离场距离检查失败: {type(e).__name__}: {e}")

    return s


def _change_alerts(s, prev):
    """与上次快照比对，返回【变更告警】行列表。
    ⚠ 必须让 --quiet 模式也能拿到 —— 否则策略变更/进程重启这类
      最关键的信号会被静默吞掉（它们在 4 小时健康检查里最该出声）。"""
    out = []
    if not prev:
        return out
    a, b = s.get("strategy_file"), prev.get("strategy_file")
    if a and b and a.get("md5") != b.get("md5"):
        out.append("    🔔 【策略文件已变更】TrendFollowing.py")
        out.append(f"       旧 md5={b.get('md5')} ({b.get('mtime')})")
        out.append(f"       新 md5={a.get('md5')} ({a.get('mtime')})")
        out.append("       ⚠ 未重启前不生效；重启后需立即重跑逐笔校验")
    if s.get("trade_start") and prev.get("trade_start") \
       and s["trade_start"] != prev["trade_start"]:
        out.append(f"    🔔 【trade 进程已重启】{prev.get('trade_start')} → {s['trade_start']}")
        out.append("       新代码自此生效 —— 需立即重跑 dryrun_verify.py 确认 fail-closed 生效")
    if prev.get("pid_trade") and not s.get("pid_trade"):
        out.append(f"    🔔 【trade 进程消失】原 PID {prev['pid_trade']}")
    return out


def show(s, prev=None):
    print(f"  [{s['ts']} 北京]")
    print(f"    进程   trade {'✅ '+s['pid_trade'] if s.get('pid_trade') else '❌'} · "
          f"webserver {'✅ '+s['pid_ws'] if s.get('pid_ws') else '❌'}")
    print(f"    交易库 {s.get('n_total','?')} 笔 · 持仓 {s.get('n_open','?')} · "
          f"平仓 {s.get('n_closed','?')} · {s.get('db_mtime_age_min','?')} 分钟前")
    if s.get("n_open"):
        print(f"    持仓   多 {s['long']} / 空 {s['short']} · 敞口 {s['exposure_pct']}% "
              f"({s['stake_sum']} / 9000)")
    if s.get("exit_reasons"):
        print(f"    平仓原因 {s['exit_reasons']} · 已实现盈亏 {s.get('realized_pnl')}")
    if s.get("strategy_file"):
        f = s["strategy_file"]
        print(f"    策略文件 TrendFollowing.py  md5={f['md5']} · {f['mtime']} · {f['bytes']}B")
    if s.get("wl_total"):
        print(f"    白名单数据 {s['wl_fresh']}/{s['wl_total']} 新鲜")
    if prev:
        for line in _change_alerts(s, prev):
            print(line)
        d_open = s.get("n_open", 0) - prev.get("n_open", 0)
        d_closed = s.get("n_closed", 0) - prev.get("n_closed", 0)
        if d_open or d_closed:
            print(f"    🔔 与上次相比: 持仓 {d_open:+d} · 平仓 {d_closed:+d}")
        else:
            print("    （与上次相比无变化）")
    if s.get("positions"):
        print(f"    {'离场距离':<10}" + "  ".join(
            f"{p['coin']}:{p['to_exit_pct']:+.1f}%" for p in s["positions"]))
    if s["problems"]:
        print("    ⚠️ 异常:")
        for p in s["problems"]:
            print(f"       · {p}")
    else:
        print("    ✅ 无异常")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=int, default=0, help="显示最近 N 条历史快照")
    ap.add_argument("--quiet", action="store_true", help="只在异常时输出")
    args = ap.parse_args()

    if args.history:
        if not os.path.exists(LOG):
            print("  还没有历史快照")
            return
        rows = [json.loads(line) for line in open(LOG, encoding="utf-8") if line.strip()]
        print(f"  最近 {min(args.history, len(rows))} 条（共 {len(rows)} 条）:")
        for r in rows[-args.history:]:
            bad = "⚠️ " + "; ".join(r["problems"]) if r["problems"] else "✅"
            print(f"    {r['ts']}  持仓 {r.get('n_open','?'):>2} · 平仓 {r.get('n_closed','?'):>2} "
                  f"· 敞口 {r.get('exposure_pct','?'):>5}%  {bad}")
        return

    prev = None
    if os.path.exists(LOG):
        rows = [json.loads(line) for line in open(LOG, encoding="utf-8") if line.strip()]
        prev = rows[-1] if rows else None

    s = snapshot()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(s, ensure_ascii=False) + "\n")

    alerts = _change_alerts(s, prev)
    if args.quiet and not s["problems"]:
        if alerts:
            # 有变更 → 必须出声（这是最重要的信号）
            print(f"  干跑健康 · 持仓 {s.get('n_open')} · 库 {s.get('db_mtime_age_min')} 分钟前")
            for line in alerts:
                print(line)
        else:
            print(f"  干跑健康 · 持仓 {s.get('n_open')} · 库 {s.get('db_mtime_age_min')} 分钟前")
        return
    show(s, prev)


if __name__ == "__main__":
    main()
