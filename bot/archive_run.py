#!/usr/bin/env python3
"""
回测结果补齐归档

背景：run_backtest_task.py 的归档逻辑是在几个训练任务启动【之后】才加上的，
所以早期完成的模型只有 zip 文件，没有归档到 user_data/models/<id>/。

本脚本扫描所有回测 zip，按「成交数 + 盈亏」与各 identifier 的已有结果匹配，
把缺失的 backtest_result.json / backtest_detail.json 补齐。

对于无法匹配的 zip，可用 --zip + --identifier 显式指定。

用法:
    python archive_run.py --list          # 看现状
    python archive_run.py --auto          # 自动补齐能匹配的
    python archive_run.py --zip <path> --identifier <id>
"""

import argparse
import glob
import json
import os
import zipfile

import pandas as pd

import extract_detail as ed

MODELS = "user_data/models"
RESULTS = "user_data/backtest_results"


def zip_summary(zip_path):
    """从 zip 读成交数与盈亏，用于匹配"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            inner = [n for n in z.namelist() if n.endswith(".json") and "meta" not in n]
            if not inner:
                return None
            data = json.loads(z.read(inner[0]))
        s = list(data["strategy"].values())[0]
        return {
            "trades": len(s.get("trades") or []),
            "profit_abs": round(s["profit_total_abs"], 2),
            "strategy": list(data["strategy"].keys())[0],
            "end": s["backtest_end"],
        }
    except Exception:
        return None


def archive(identifier, zip_path, entry_threshold=0.02):
    """写 backtest_result.json（摘要） + backtest_detail.json（逐笔）"""
    out_dir = os.path.join(MODELS, identifier)
    os.makedirs(out_dir, exist_ok=True)

    detail = ed.build_detail(identifier, zip_path, entry_threshold)
    dp = os.path.join(out_dir, "backtest_detail.json")
    with open(dp, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False)

    # 摘要文件（供旧接口/面板使用）
    sm = detail["summary"]
    rp = os.path.join(out_dir, "backtest_result.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({
            "identifier": identifier,
            "model": identifier.split("-")[-3] if "-" in identifier else identifier,
            "strategy": detail["strategy"],
            "zip": os.path.basename(zip_path),
            "result": {
                "trades": sm["trades"],
                "profit_abs": sm["profit_abs"],
                "profit_pct": sm["profit_pct"],
                "winrate": sm["winrate"],
                "max_drawdown_pct": sm["max_drawdown_pct"],
                "sharpe": sm["sharpe"],
                "profit_factor": sm["profit_factor"],
                "cagr": sm["cagr"],
                "equity": detail["equity"],
            },
        }, f, ensure_ascii=False)

    return dp, rp, sm


def cmd_list():
    print("  已有归档:")
    for d in sorted(glob.glob(f"{MODELS}/*/")):
        ident = os.path.basename(d.rstrip("/"))
        r = os.path.exists(f"{d}backtest_result.json")
        t = os.path.exists(f"{d}backtest_detail.json")
        n = len(glob.glob(f"{d}backtesting_predictions/*.feather"))
        if n == 0 and not r and not t:
            continue
        print(f"    {ident:<30} 预测{n:>4}个  摘要{'✅' if r else '❌'}  逐笔{'✅' if t else '❌'}")
    print()
    print("  回测 zip（按时间倒序，前 8 个）:")
    for f in sorted(glob.glob(f"{RESULTS}/*.zip"), key=os.path.getmtime, reverse=True)[:8]:
        s = zip_summary(f)
        if s:
            ts = pd.Timestamp(os.path.getmtime(f), unit="s", tz="UTC").tz_convert("Asia/Shanghai")
            print(f"    {os.path.basename(f):<46} {s['trades']:>4}笔 {s['profit_abs']:>9.2f}  {ts:%H:%M:%S}")


def cmd_auto():
    zips = sorted(glob.glob(f"{RESULTS}/*.zip"), key=os.path.getmtime, reverse=True)
    used = set()
    done = 0
    for d in sorted(glob.glob(f"{MODELS}/*/")):
        ident = os.path.basename(d.rstrip("/"))
        if not (os.path.exists(f"{d}backtest_result.json") or
                glob.glob(f"{d}backtesting_predictions/*.feather")):
            continue
        # 已有逐笔就跳过
        if os.path.exists(f"{d}backtest_detail.json"):
            continue
        # 用摘要里的成交数/盈亏匹配 zip
        want = None
        rp = f"{d}backtest_result.json"
        if os.path.exists(rp):
            r = json.load(open(rp))["result"]
            want = (r["trades"], round(r["profit_abs"], 2))
        target = None
        for z in zips:
            if z in used:
                continue
            s = zip_summary(z)
            if not s:
                continue
            if want and (s["trades"], s["profit_abs"]) == want:
                target = z
                break
        if not target:
            print(f"  ⚠ {ident}: 未匹配到 zip（可用 --zip 显式指定）")
            continue
        try:
            _, _, sm = archive(ident, target)
            used.add(target)
            done += 1
            print(f"  ✅ {ident}  ← {os.path.basename(target)}  "
                  f"{sm['trades']}笔 {sm['profit_abs']:+.2f} ({sm['profit_pct']:+.2f}%)")
        except Exception as exc:
            print(f"  ❌ {ident}: {exc}")
    print(f"\n  补齐 {done} 个")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--zip")
    ap.add_argument("--identifier")
    args = ap.parse_args()

    if args.list or not (args.auto or (args.zip and args.identifier)):
        cmd_list()
        if not args.auto and not args.zip:
            return
    if args.auto:
        cmd_auto()
    if args.zip and args.identifier:
        dp, rp, sm = archive(args.identifier, args.zip)
        print(f"✅ {args.identifier}")
        print(f"   逐笔 → {dp}")
        print(f"   摘要 → {rp}")
        print(f"   {sm['trades']} 笔 | {sm['profit_pct']:+.2f}% | 胜率 {sm['winrate']}% | "
              f"IC {sm['pred_vs_actual_ic']} | 命中 {sm['sign_hit_rate']}%")


if __name__ == "__main__":
    main()
