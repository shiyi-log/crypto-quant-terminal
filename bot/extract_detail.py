#!/usr/bin/env python3
"""
回测交易明细提取器

把回测结果 zip 里的成交记录，和 FreqAI 的逐根预测关联起来，
回答「这轮赚了多少、怎么下的单、当时模型怎么判断的」。

输出: user_data/models/<identifier>/backtest_detail.json

用法:
    python extract_detail.py --identifier universe15-mlp-1h-7d --zip <结果zip>
    python extract_detail.py --auto          # 自动按成交数/盈亏匹配 zip 与 identifier
"""

import argparse
import glob
import json
import os
import zipfile

import numpy as np
import pandas as pd

MODELS = "user_data/models"
RESULTS = "user_data/backtest_results"


# ═══════════════════ 读回测结果 ═══════════════════

def read_result(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        inner = [n for n in z.namelist() if n.endswith(".json") and "meta" not in n]
        data = json.loads(z.read(inner[0]))
    name = list(data["strategy"].keys())[0]
    return name, data["strategy"][name]


# ═══════════════════ 读预测（决策依据） ═══════════════════

def load_predictions(identifier, symbol):
    """返回 {naive_datetime_str: {'pred':.., 'do_predict':.., 'di':..}}"""
    out = {}
    pattern = os.path.join(MODELS, identifier, "backtesting_predictions",
                           f"cb_{symbol.lower()}_*_prediction.feather")
    for f in sorted(glob.glob(pattern)):
        try:
            d = pd.read_feather(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        for _, r in d.iterrows():
            out[r["date"].strftime("%Y-%m-%d %H:%M:%S")] = {
                "pred": float(r["&-s_close"]),
                "do_predict": int(r["do_predict"]),
                "di": round(float(r["DI_values"]), 4) if pd.notna(r.get("DI_values")) else None,
            }
    return out


# ═══════════════════ 主逻辑 ═══════════════════

def build_detail(identifier, zip_path, entry_threshold=0.02, exit_threshold=-0.01):
    strategy_name, s = read_result(zip_path)
    trades = s.get("trades") or []
    cache = {}

    rows = []
    for t in trades:
        sym = t["pair"].split("/")[0]
        if sym not in cache:
            cache[sym] = load_predictions(identifier, sym)
        od = pd.to_datetime(t["open_date"], utc=True).tz_localize(None)
        key = od.strftime("%Y-%m-%d %H:%M:%S")
        dec = cache[sym].get(key) or {}

        pred = dec.get("pred")
        side = "空" if t.get("is_short") else "多"
        # 决策描述：模型预测值与阈值的比较
        if pred is None:
            reason = "无对应预测记录"
        elif t.get("is_short"):
            reason = f"预测 {pred*100:+.2f}% < -{entry_threshold*100:.1f}% → 开空"
        else:
            reason = f"预测 {pred*100:+.2f}% > +{entry_threshold*100:.1f}% → 开多"

        rows.append({
            "pair": t["pair"],
            "side": side,
            "is_short": bool(t.get("is_short")),
            "open_date": t["open_date"],
            "close_date": t.get("close_date"),
            "open_rate": round(t.get("open_rate") or 0, 6),
            "close_rate": round(t.get("close_rate") or 0, 6),
            "amount": t.get("amount"),
            "stake_amount": round(t.get("stake_amount") or 0, 2),
            "profit_pct": round((t.get("profit_ratio") or 0) * 100, 2),
            "profit_abs": round(t.get("profit_abs") or 0, 2),
            "exit_reason": t.get("exit_reason"),
            "enter_tag": t.get("enter_tag"),
            "leverage": t.get("leverage"),
            "duration_h": round((t.get("trade_duration") or 0) / 60, 1),
            # 决策依据
            "model_pred_pct": round(pred * 100, 3) if pred is not None else None,
            "do_predict": dec.get("do_predict"),
            "di": dec.get("di"),
            "decision": reason,
            "outcome": "盈利" if (t.get("profit_abs") or 0) > 0 else "亏损",
        })

    # —— 汇总 ——
    rets = np.array([r["profit_pct"] for r in rows]) if rows else np.array([])
    wins = [r for r in rows if r["profit_abs"] > 0]
    losses = [r for r in rows if r["profit_abs"] <= 0]

    by_pair = {}
    for r in rows:
        d = by_pair.setdefault(r["pair"], {"trades": 0, "profit_abs": 0.0, "wins": 0})
        d["trades"] += 1
        d["profit_abs"] = round(d["profit_abs"] + r["profit_abs"], 2)
        d["wins"] += 1 if r["profit_abs"] > 0 else 0

    by_exit = {}
    for r in rows:
        k = r["exit_reason"] or "unknown"
        d = by_exit.setdefault(k, {"trades": 0, "profit_abs": 0.0})
        d["trades"] += 1
        d["profit_abs"] = round(d["profit_abs"] + r["profit_abs"], 2)

    # 预测值与实际收益的关系（模型的判断有没有用）
    preds = np.array([r["model_pred_pct"] for r in rows if r["model_pred_pct"] is not None])
    acts = np.array([r["profit_pct"] for r in rows if r["model_pred_pct"] is not None])
    ic = float(np.corrcoef(preds, acts)[0, 1]) if len(preds) > 5 and preds.std() > 0 and acts.std() > 0 else None
    hit = float((np.sign(preds) == np.sign(acts)).mean()) if len(preds) > 0 else None

    # 资金曲线
    equity, bal = [], s["starting_balance"]
    for t in sorted(trades, key=lambda x: x["close_timestamp"]):
        bal += t.get("profit_abs", 0.0)
        day = pd.to_datetime(t["close_timestamp"], unit="ms", utc=True).strftime("%Y-%m-%d")
        equity.append([day, round(bal, 2)])
    # 按天去重取最后值
    dedup = {}
    for d, v in equity:
        dedup[d] = v
    equity = [[d, v] for d, v in sorted(dedup.items())]

    return {
        "identifier": identifier,
        "strategy": strategy_name,
        "generated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {"entry": entry_threshold, "exit": exit_threshold},
        "summary": {
            "start": s["backtest_start"],
            "end": s["backtest_end"],
            "days": round(s.get("backtest_days", 0)),
            "starting_balance": round(s["starting_balance"], 2),
            "final_balance": round(s["final_balance"], 2),
            "profit_abs": round(s["profit_total_abs"], 2),
            "profit_pct": round(s["profit_total"] * 100, 2),
            "trades": len(rows),
            "trades_long": s.get("trade_count_long", 0),
            "trades_short": s.get("trade_count_short", 0),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": round(s["winrate"] * 100, 2),
            "avg_win": round(np.mean([r["profit_pct"] for r in wins]), 2) if wins else None,
            "avg_loss": round(np.mean([r["profit_pct"] for r in losses]), 2) if losses else None,
            "best_trade": round(rets.max(), 2) if len(rets) else None,
            "worst_trade": round(rets.min(), 2) if len(rets) else None,
            "max_drawdown_pct": round(s["max_drawdown_account"] * 100, 2),
            "sharpe": round(s["sharpe"], 3),
            "profit_factor": round(s["profit_factor"], 3),
            "cagr": round(s["cagr"] * 100, 2),
            "trades_per_day": round(s.get("trades_per_day", 0), 2),
            # 决策质量
            "pred_vs_actual_ic": round(ic, 4) if ic is not None else None,
            "sign_hit_rate": round(hit * 100, 2) if hit is not None else None,
        },
        "by_pair": [{"pair": k, **v} for k, v in sorted(by_pair.items(), key=lambda x: x[1]["profit_abs"])],
        "by_exit": [{"reason": k, **v} for k, v in sorted(by_exit.items(), key=lambda x: x[1]["profit_abs"])],
        "equity": equity,
        "trades": sorted(rows, key=lambda x: x["open_date"]),
    }


def auto_match():
    """把结果 zip 与 identifier 按「成交数 + 盈亏」匹配"""
    zips = sorted(glob.glob(f"{RESULTS}/*.zip"), key=os.path.getmtime, reverse=True)
    pairs = {}
    for ident_dir in sorted(glob.glob(f"{MODELS}/*/")):
        ident = os.path.basename(ident_dir.rstrip("/"))
        if not glob.glob(f"{ident_dir}backtesting_predictions/*.feather"):
            continue
        arch = os.path.join(ident_dir, "backtest_result.json")
        want = None
        if os.path.exists(arch):
            r = json.load(open(arch))["result"]
            want = (r["trades"], round(r["profit_abs"], 2))
        for z in zips:
            if z in pairs:
                continue
            try:
                _, s = read_result(z)
            except Exception:
                continue
            got = (len(s.get("trades") or []), round(s["profit_total_abs"], 2))
            if want and got == want:
                pairs[z] = ident
                break
            if not want and got[0] > 0 and abs(got[1] - s["starting_balance"] * -6.5) < 1:
                pass
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--identifier")
    ap.add_argument("--zip")
    ap.add_argument("--entry-threshold", type=float, default=0.02)
    ap.add_argument("--exit-threshold", type=float, default=-0.01)
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()

    jobs = []
    if args.auto:
        jobs = list(auto_match().items())
        if not jobs:
            print("未匹配到任何 zip 与 identifier（需要先有 backtest_result.json 归档）")
            return
    elif args.identifier and args.zip:
        jobs = [(args.zip, args.identifier)]
    else:
        print("用法: --auto  或  --identifier X --zip Y")
        return

    for zip_path, ident in jobs:
        detail = build_detail(ident, zip_path, args.entry_threshold, args.exit_threshold)
        out = os.path.join(MODELS, ident, "backtest_detail.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False)
        sm = detail["summary"]
        print(f"✅ {ident}")
        print(f"   {sm['trades']} 笔 | 盈亏 {sm['profit_abs']:+.2f} ({sm['profit_pct']:+.2f}%) | "
              f"胜率 {sm['winrate']}% | 平均盈 {sm['avg_win']}% / 平均亏 {sm['avg_loss']}%")
        print(f"   预测-实际 IC {sm['pred_vs_actual_ic']} | 方向命中率 {sm['sign_hit_rate']}%")
        print(f"   → {out}  ({os.path.getsize(out)//1024} KB)")


if __name__ == "__main__":
    main()
