#!/usr/bin/env python3
"""多模型回测结果横向对比（读取各 identifier 归档的 backtest_result.json）"""
import glob, json, os

rows = []
for f in sorted(glob.glob("user_data/models/*/backtest_result.json")):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    r = d.get("result") or {}
    if not r or "error" in r:
        continue
    rows.append({
        "模型": d.get("model", "?"),
        "identifier": d.get("identifier", "?"),
        "净利润%": r.get("profit_pct"),
        "净利润": r.get("profit_abs"),
        "交易": r.get("trades"),
        "胜率%": r.get("winrate"),
        "回撤%": r.get("max_drawdown_pct"),
        "Sharpe": r.get("sharpe"),
        "盈亏比": r.get("profit_factor"),
        "CAGR%": r.get("cagr"),
        "耗时s": d.get("elapsed_seconds"),
    })

if not rows:
    print("暂无归档结果（跑完一个模型后生成 user_data/models/<id>/backtest_result.json）")
    raise SystemExit

cols = list(rows[0].keys())
w = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
print("=" * (sum(w.values()) + 3 * len(cols)))
print("  ".join(c.ljust(w[c]) for c in cols))
print("-" * (sum(w.values()) + 3 * len(cols)))
for r in sorted(rows, key=lambda x: (x["净利润%"] is None, -(x["净利润%"] or 0))):
    print("  ".join(str(r[c]).ljust(w[c]) for c in cols))
print("=" * (sum(w.values()) + 3 * len(cols)))
