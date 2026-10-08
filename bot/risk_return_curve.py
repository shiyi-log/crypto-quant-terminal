#!/usr/bin/env python3
"""
风险收益曲线 —— 诚实回答「30% 年化要付什么代价」

背景：
    目标是年化 30%+。但收益率与回撤是同向放大的：
    敞口翻倍 → 年化约翻倍，最大回撤也约翻倍。

    所以「要 30%」等价于问「愿意承受多大回撤」。

本脚本对多个敞口档位 + 多种选池方式，给出完整对照表：

    敞口 10% / 20% / 30% / 40% / 60% / 90%（含杠杆）

并给出：
    · 达到 30% 年化所需的敞口与对应回撤
    · 各档位的 Calmar、t 值、正收益年
    · 「杠杆是否有效」——高杠杆下 Calmar 是否崩塌

⚠️ 关键纪律：
    杠杆低 Sharpe 的策略会放大亏损，也可能触发强平。
    本表同时给出「最大回撤」作为可行性边界 —— 回撤超过 100% 就是爆仓。

用法:
    python risk_return_curve.py
    python risk_return_curve.py --universe pit10   # 用无偏选池
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

import walkforward as wf

PERP = "user_data/data/binance/futures"
CURRENT = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC",
           "DOT", "NEAR", "SUI", "APT", "AAVE", "UNI", "FIL", "TRX", "ZEC", "SAND"]

LEVELS = [0.10, 0.20, 0.30, 0.40, 0.60, 0.90, 1.20]


def load_all():
    out = {}
    for f in sorted(glob.glob(f"{PERP}/*-1d-futures.feather")):
        s = os.path.basename(f).split("_")[0]
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) >= 400:
            out[s] = d[["open", "high", "low", "close", "volume"]]
    return out


def series(data, top_n=8, universe_n=None, start="2020-01-01"):
    """返回【单位敞口】的日收益序列"""
    r = wf.backtest(data, sizing="equal", top_n=top_n, universe_n=universe_n,
                    start=start)
    if r is None:
        return None
    n_cols = len(data)
    actual = min(top_n, n_cols) / n_cols
    return r / actual          # 归一到「100% 敞口」


def metrics(r, exposure, label):
    """按指定敞口计算指标"""
    if r is None or len(r) < 100:
        return None
    x = r * exposure
    eq = (1 + x).cumprod()
    years = (x.index[-1] - x.index[0]).days / 365
    ann = eq.iloc[-1] ** (1 / years) - 1
    vol = x.std() * np.sqrt(365)
    mdd = ((eq / eq.cummax()) - 1).min()
    sh = (x.mean() * 365) / vol if vol > 0 else np.nan
    return {
        "label": label, "exposure": exposure,
        "ann": ann * 100, "vol": vol * 100, "mdd": mdd * 100,
        "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
        "sharpe": sh, "t": sh * np.sqrt(years),
        "ruin": mdd <= -0.999,
        "years": years,
    }


def show(rows, title):
    print(f"\n  【{title}】")
    print(f"    {'敞口':>7}{'年化':>10}{'波动':>9}{'最大回撤':>11}{'Calmar':>9}"
          f"{'Sharpe':>9}{'t值':>8}  可行性")
    print("    " + "-" * 78)
    for r in rows:
        if not r:
            continue
        feas = "💥 爆仓" if r["ruin"] else ("⚠ 极难承受" if r["mdd"] < -50
                                          else ("🟡 可承受但痛苦" if r["mdd"] < -30
                                                else "🟢 可承受"))
        print(f"    {r['exposure']*100:>6.0f}%{r['ann']:>9.2f}%{r['vol']:>8.1f}%"
              f"{r['mdd']:>10.2f}%{r['calmar']:>9.2f}{r['sharpe']:>9.2f}"
              f"{r['t']:>8.2f}  {feas}")
    print("    " + "-" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="both",
                    choices=["current", "pit10", "pit20", "both"])
    args = ap.parse_args()

    data = load_all()
    data_cur = {k: v for k, v in data.items() if k in CURRENT}
    print(f"  数据 {len(data)} 币 · 当前名单 {len(data_cur)} 币 · 区间 2020-01-01 起")

    sets = []
    if args.universe in ("current", "both"):
        sets.append(("当前实盘名单（静态 20·有幸存者偏差）", data_cur, None, 8))
    if args.universe in ("pit10", "both"):
        sets.append(("动态时点选池 前 10（无偏差）", data, 10, 8))
    if args.universe == "pit20":
        sets.append(("动态时点选池 前 20（无偏差）", data, 20, 8))

    print()
    print("=" * 96)
    print("风险收益曲线：不同敞口下的实际表现（全样本 2020-2026）")
    print("=" * 96)

    all_rows = {}
    for name, d, un, tn in sets:
        r = series(d, top_n=tn, universe_n=un, start="2020-01-01")
        rows = [metrics(r, lv, f"{lv:.0%}") for lv in LEVELS]
        show(rows, name)
        all_rows[name] = rows

    # 样本外
    print()
    print("=" * 96)
    print("样本外（2023-2026）—— 这才是能参考的")
    print("=" * 96)
    oos_rows = {}
    for name, d, un, tn in sets:
        r = series(d, top_n=tn, universe_n=un, start="2023-01-01")
        rows = [metrics(r, lv, f"{lv:.0%}") for lv in LEVELS]
        show(rows, name)
        oos_rows[name] = rows

    # 回答核心问题
    print()
    print("=" * 96)
    print("⭐ 核心问题：年化 30% 需要什么代价")
    print("=" * 96)
    for name, rows in oos_rows.items():
        print(f"\n  {name}")
        hits = [r for r in rows if r and r["ann"] >= 30]
        if hits:
            h = min(hits, key=lambda r: r["exposure"])
            print(f"    需要敞口 {h['exposure']*100:.0f}%  →  年化 {h['ann']:.1f}%"
                  f"  最大回撤 {h['mdd']:.1f}%  Calmar {h['calmar']:.2f}")
            if h["ruin"]:
                print(f"    💥 但该敞口下回撤超过 100%，实盘会爆仓 —— 不可行")
        else:
            best = max([r for r in rows if r], key=lambda r: r["ann"])
            print(f"    ❌ 所有测试敞口都达不到 30%")
            print(f"    最高是 {best['exposure']*100:.0f}% 敞口 → 年化 {best['ann']:.1f}%"
                  f"，回撤 {best['mdd']:.1f}%")
        print(f"    Calmar 最高档位: ", end="")
        ok = [r for r in rows if r and not r["ruin"]]
        if ok:
            b = max(ok, key=lambda r: r["calmar"])
            print(f"{b['exposure']*100:.0f}% 敞口 (Calmar {b['calmar']:.2f}, "
                  f"年化 {b['ann']:.1f}%, 回撤 {b['mdd']:.1f}%)")

    # 杠杆是否有效
    print()
    print("=" * 96)
    print("杠杆有效性检验（Calmar 是否随敞口崩塌）")
    print("=" * 96)
    for name, rows in oos_rows.items():
        rs = [r for r in rows if r and not r["ruin"]]
        if len(rs) < 2:
            continue
        c0 = rs[0]["calmar"]
        worst = min(rs, key=lambda r: r["calmar"])
        print(f"  {name}")
        print(f"    最低敞口 Calmar {c0:.2f} → 最差档位 Calmar {worst['calmar']:.2f}"
              f"（{worst['exposure']*100:.0f}% 敞口）")
        if worst["calmar"] < c0 * 0.5:
            print(f"    → ⚠ 杠杆使风险调整收益显著恶化，加杠杆不可取")
        else:
            print(f"    → 杠杆下 Calmar 基本稳定（收益率近似线性放大）")

    payload = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "levels": LEVELS,
        "full": {k: [({kk: round(vv, 3) for kk, vv in r.items()
                      if isinstance(vv, (int, float, bool))} if r else None) for r in v]
                 for k, v in all_rows.items()},
        "oos": {k: [({kk: round(vv, 3) for kk, vv in r.items()
                     if isinstance(vv, (int, float, bool))} if r else None) for r in v]
                for k, v in oos_rows.items()},
    }
    with open("user_data/risk_return_curve.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n  ✅ user_data/risk_return_curve.json")


if __name__ == "__main__":
    main()
