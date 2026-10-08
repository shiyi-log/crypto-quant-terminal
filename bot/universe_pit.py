#!/usr/bin/env python3
"""
时点（Point-in-Time）选池 vs 幸存者偏差名单

问题：
    现在实盘用的 20 币名单是【用今天的知识人工挑的】——
    BTC/ETH/SOL/SUI/APT/AAVE/UNI/SAND/NEAR/AVAX 里有一半在 2020 年还不存在。
    用这份名单回测 2020 年 = 严重幸存者偏差，收益会被系统性高估。

本脚本对比四种选池方式，量化偏差有多大：

    A. 静态 20（当前实盘名单）      —— 幸存者偏差最重
    B. 静态「2020年就存在的」21币    —— 无幸存者偏差，但固定不变
    C. 动态 PIT 前 N 名             —— 每月按时点流动性重选，无前视
    D. 静态 全部 57 币              —— 同样有偏差（含未来才上市的币）

时点选池规则：
    · 每个时点按【滚动 90 天成交额中位数】排名（liq.shift(1)，无前视）
    · 只取前 N 名
    · 尚未上市的币自然不在面板里 → 排名为 NaN → 自动排除
    · 月度换池（避免日度换手）

用法:
    python universe_pit.py
    python universe_pit.py --detail     # 打印逐年明细
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

import walkforward as wf

PERP = "user_data/data/binance/futures"
LIVE = "user_data/config_trend_live.json"
CURRENT = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC",
           "DOT", "NEAR", "SUI", "APT", "AAVE", "UNI", "FIL", "TRX", "ZEC", "SAND"]


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


def existing_at(data, date):
    """在指定日期已经上市的币"""
    return {s for s, d in data.items() if d.index[0] <= date}


def run(data, label, top_n=8, sizing="equal", universe_n=None,
        start="2020-01-01", end=None, target_exposure=0.40):
    """
    target_exposure: 把组合缩放到统一敞口，否则不同池宽度的暴露度不同
                     （静态 20 币是 8/20=40%，动态池是 8/51=15.7%），无法直接比。
                     缩放不改变 Calmar（年化与回撤同比例变化），但让年化可比。
    """
    r = wf.backtest(data, sizing=sizing, top_n=top_n, universe_n=universe_n,
                    start=start)
    if r is None:
        return None
    if end:
        r = r[r.index < end]
    # 估算实际平均暴露度并缩放
    n_cols = len(data)
    actual = min(top_n, n_cols) / n_cols
    if actual > 0:
        r = r * (target_exposure / actual)
    e = wf.evaluate(r, label)
    if e:
        e["exposure"] = round(actual * 100, 1)
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    data = load_all()
    print(f"  全部数据 {len(data)} 币")

    # ⚠️ 用 2021-01-01 而不是 2020-01-01：
    #    日线数据从 2019-09 才开始，2020-01-01 时只有 3 个币有数据，
    #    拿 3 币池做对比没有意义（波动 74%）。
    early = sorted(existing_at(data, pd.Timestamp("2021-01-01")))
    early = [c for c in early if c in data]
    print(f"  2021-01-01 已上市 {len(early)} 币: {', '.join(early)}")
    print()

    data_cur = {k: v for k, v in data.items() if k in CURRENT}
    data_early = {k: v for k, v in data.items() if k in early}

    print("=" * 108)
    print("① 选池方式对比（样本内 2020-2022）—— 已统一缩放到 40% 敞口")
    print("=" * 108)
    print(f"  {'选池方式':<34}{'原敞口':>8}{'年化':>9}{'波动':>8}{'回撤':>9}{'Calmar':>8}{'t值':>7}{'正年':>7}")
    print("  " + "-" * 90)

    ins_rows = []

    def show(label, e):
        if not e:
            print(f"  {label:<40} 无数据")
            return None
        print(f"  {label:<34}{e.get('exposure',0):>7.0f}%{e['ann']:>8.2f}%{e['vol']:>7.1f}%"
              f"{e['mdd']:>8.2f}%{e['calmar']:>8.2f}{e['t']:>7.2f}"
              f"{e['pos_years']:>4}/{e['n_years']}")
        return e

    a = show("A 静态 20（当前实盘名单·有偏差）",
             run(data_cur, "A", start="2020-01-01", end="2023-01-01"))
    b = show("B 静态 21（2020 就存在的·无偏差）",
             run(data_early, "B", start="2020-01-01", end="2023-01-01"))
    for n in [10, 20, 30]:
        e = run(data, f"C{n}", universe_n=n, start="2020-01-01", end="2023-01-01")
        show(f"C 动态 PIT 前 {n} 名", e)
        ins_rows.append((n, e))
    show("D 静态 全部 57（含未来上市·有偏差）",
         run(data, "D", start="2020-01-01", end="2023-01-01"))
    print("  " + "-" * 92)

    print()
    print("=" * 108)
    print("② 样本外验证（2023-2026）—— 已统一缩放到 40% 敞口")
    print("=" * 108)
    print(f"  {'选池方式':<34}{'原敞口':>8}{'年化':>9}{'波动':>8}{'回撤':>9}{'Calmar':>8}{'t值':>7}{'正年':>7}")
    print("  " + "-" * 90)
    oos_rows = []
    show("A 静态 20（当前实盘名单）",
         run(data_cur, "A", start="2023-01-01"))
    show("B 静态 21（2020 就存在的）",
         run(data_early, "B", start="2023-01-01"))
    for n in [10, 20, 30]:
        e = run(data, f"C{n}", universe_n=n, start="2023-01-01")
        show(f"C 动态 PIT 前 {n} 名", e)
        oos_rows.append((n, e))
    show("D 静态 全部 57", run(data, "D", start="2023-01-01"))
    print("  " + "-" * 92)

    print()
    print("=" * 108)
    print("③ 幸存者偏差有多大")
    print("=" * 108)
    if a and b:
        print(f"  样本内（2020-2022）:")
        print(f"    静态 20（有偏差）  Calmar {a['calmar']:.2f}  年化 {a['ann']:.2f}%")
        print(f"    静态 21（无偏差）  Calmar {b['calmar']:.2f}  年化 {b['ann']:.2f}%")
        d = (a['calmar'] - b['calmar']) / abs(b['calmar']) * 100 if b['calmar'] else float('nan')
        print(f"    → 幸存者偏差虚高 Calmar {d:+.0f}%")

    print()
    print("=" * 108)
    print("④ 逐年明细（动态 PIT 前 20）")
    print("=" * 108)
    if args.detail:
        r = wf.backtest(data, sizing="equal", top_n=8, universe_n=20,
                        start="2020-01-01")
        e = wf.evaluate(r, "PIT20")
        if e:
            print(f"  {'年份':<8}{'收益':>10}")
            print("  " + "-" * 20)
            for y, v in sorted(e["yearly"].items()):
                print(f"  {y:<8}{v:>9.1f}%")
        # 池子随时间变化
        liq = wf.liquidity_panel(data)
        top = wf.top_n_at(liq, 20)
        print()
        print("  池子成员变化（每半年取一次）:")
        for dt in pd.date_range("2020-01-01", "2026-07-01", freq="6MS"):
            if dt in top.index:
                members = [c for c in top.columns if top.loc[dt, c]]
                n_exist = len(existing_at(data, dt))
                print(f"    {dt.date()}  实际存在 {n_exist:>2} 币 · "
                      f"选中 {len(members):>2} 币: {', '.join(members[:10])}")
    else:
        print("  （加 --detail 查看逐年与池子变化）")

    # 保存
    payload = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "universe_comparison": {
            "static20_ins": {k: round(v, 3) for k, v in (a or {}).items()
                             if isinstance(v, (int, float))},
            "static21_ins": {k: round(v, 3) for k, v in (b or {}).items()
                             if isinstance(v, (int, float))},
            "pit": [{"n": n, **(  {k: round(v, 3) for k, v in e.items()
                                    if isinstance(v, (int, float))} if e else {})}
                    for n, e in ins_rows],
        },
    }
    with open("user_data/universe_comparison.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n  ✅ user_data/universe_comparison.json")


if __name__ == "__main__":
    main()
