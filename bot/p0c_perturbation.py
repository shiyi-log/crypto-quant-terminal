#!/usr/bin/env python3
"""
P0(c) 未来数据扰动复验 —— run_v2 的因果性抽样验收。

════════════════════════════════════════════════════════════════════
要证明什么
════════════════════════════════════════════════════════════════════
第 i 日的决策只能依赖截至第 i-1 日【收盘】已知的信息。
所以：改动第 D 日的收盘价，不得改变第 D 日的任何决策
（用 state[D] 决策就是前视，因为 D 日收盘在 D 日开盘时未知）。

之前只测了 5 个日期。本脚本按时段分层抽样，逐日报告，并保留边界证据。

═══ 三类检验 ═══
T1 锐利单日扰动：只改第 D 日 close（不动 open），看第 D 日决策是否变
   · 抽样覆盖全部年份，按年分段汇总
T2 未来段扰动：改第 J 日【之后】全部数据，看早于 J 的决策是否变
   · 这是粗粒度检验，能抓跨段泄漏（但对单日前视不敏感，故不能替代 T1）
T3 边界证据：缺开盘价、pending_exit、末端未平仓 —— 逐项留证

═══ 为什么 T1 必须"只改 close、不改 open" ═══
若同时改 open，成交价本身变了，决策"变化"无法归因于前视。
只改 close 才能隔离出"用未知信息做决策"这一件事。

用法:
    python p0c_perturbation.py                # 跨年份抽样 40 天（不是遍历所有日期）
    python p0c_perturbation.py --quick        # 抽样 8 天，先看趋势
    python p0c_perturbation.py --json out.json
"""
import argparse
import json
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shiyi/DeepSeek/量化/bot")

FACTOR = 1.5          # 扰动力度：×1.5；不保证触发每一种边界


def load():
    import ml_lab as ml
    return ml.load_ohlcv()


def decisions(engine, data, **kw):
    """返回 (事件集合, diag)。

    ⚠ 关键修正：不能把「开仓日 == D 的交易」整体当作「D 日的决策」——
      那会把【平仓日】也算进去，而平仓是【未来某天】的决策。
      例：改 D 日收盘 → 影响 state[D] → 影响 D+1 日的平仓判断，这是【合法】的。
      所以必须把 D 日发生的决策拆成两类事件：
        ("in",  pair, D, 开仓价, 方向, stake, signed_qty)
        ("out", pair, D, 平仓价, 方向, stake, signed_qty)
      两类都只应依赖截至 D-1 日收盘的信息。
      金额与方向也是决策；只比较币/日期/价格会漏掉定仓前视。
    """
    tr, eq, ret, diag = engine(data, **kw)
    t = pd.DataFrame(tr)
    ev = set()

    def event(kind, row):
        rate = float(row["open_rate"] if kind == "in" else row["close_rate"])
        entry = float(row["open_rate"])
        stake = float(row["stake"])
        if not (np.isfinite(rate) and rate > 0 and np.isfinite(entry) and entry > 0
                and np.isfinite(stake) and stake > 0):
            raise ValueError(f"无效决策事件：{kind} {row['pair']}")
        side = "short" if bool(row["is_short"]) else "long"
        qty = stake / entry * (-1 if side == "short" else 1)
        date = pd.Timestamp(row["open_date"] if kind == "in" else row["close_date"])
        # 不舍入：确定性引擎的相同历史应得到完全相同的成交与定仓。
        return (kind, row["pair"], date.strftime("%Y-%m-%d"), rate, side, stake, qty)

    for _, row in t.iterrows():
        ev.add(event("in", row))
        ev.add(event("out", row))
    # ⚠ 必须补上【期末仍持仓】的进场事件 —— 否则扰动让某仓位"留到期末"时，
    #   它的进场会凭空消失，被误判成前视（我第一版 T2 就栽在这里）。
    # ⚠ 只补 ["in"]，绝不再加 ("open", ...) 之类的【状态标记】——
    #   那不是决策。我上一版加了它，结果：某仓位在基线里平仓、在扰动后留到期末，
    #   就会一边有 "open" 标记一边没有 → 纯属人为不对称（假前视）。
    #   事件集必须【只含决策】：进场与出场。
    for p in diag.get("open_positions", []) or []:
        ev.add(event("in", p))
    return ev, diag


def day_decisions(dec, day):
    """D 日发生的全部决策事件（进场 + 出场）。"""
    ds = day.strftime("%Y-%m-%d")
    return {x for x in dec if x[2] == ds}


def T1_sharp(engine, data, days, log=print):
    """只改第 D 日 close，看第 D 日决策是否变。"""
    base, _ = decisions(engine, data)
    rows = []
    for n, D in enumerate(days, 1):
        pert = {k: v.copy() for k, v in data.items()}
        for k in pert:
            if D in pert[k].index:
                pert[k].loc[D, "close"] *= FACTOR      # 只改 close，不动 open
        new, _ = decisions(engine, pert)
        a, b = day_decisions(base, D), day_decisions(new, D)
        changed = a != b
        rows.append({"day": D.strftime("%Y-%m-%d"), "n_base": len(a), "n_pert": len(b),
                     "changed": changed,
                     "only_base": len(a - b), "only_pert": len(b - a)})
        log(f"    [{n}/{len(days)}] {D.date()}  基线 {len(a)} 笔 → 扰动 {len(b)} 笔"
            f"  {'❌ 变了' if changed else '✅ 不变'}")
    return rows


def T2_future(engine, data, cuts, log=print):
    """改第 J 日之后全部数据，看早于 J 的决策是否变。"""
    base, _ = decisions(engine, data)
    rows = []
    for J in cuts:
        pert = {k: v.copy() for k, v in data.items()}
        for k in pert:
            m = pert[k].index >= J
            pert[k].loc[m, "close"] *= FACTOR
            pert[k].loc[m, "open"] *= FACTOR
        new, _ = decisions(engine, pert)
        js = J.strftime("%Y-%m-%d")
        a = {x for x in base if x[2] < js}       # 事件日早于 J 的决策
        b = {x for x in new if x[2] < js}
        rows.append({"cut": js, "n_base_before": len(a), "changed": a != b,
                     "has_history_coverage": len(a) > 0,
                     "only_base": len(a - b), "only_pert": len(b - a)})
        log(f"    {J.date()} 之后扰动 · 早于该日的决策 {len(a)} 笔"
            f"  {'❌ 变了' if a != b else '✅ 不变'}")
    return rows


def T3_boundaries(engine, data, log=print):
    """缺开盘退出的硬验收；同一仓位必须挂起并在首个有效开盘成交。"""
    ev = {}
    tr, eq, ret, diag = engine(data)
    ev["diag"] = {k: (v if not isinstance(v, np.generic) else v.item())
                  for k, v in diag.items()}
    t = pd.DataFrame(tr)
    ev["n_closed"] = len(t)
    ev["n_open_at_end"] = diag.get("open_positions_at_end", 0)
    log(f"    已平仓 {ev['n_closed']} 笔 · 末端未平仓 {ev['n_open_at_end']} 个")
    log(f"    缺开盘跳过 {diag.get('missing_open_fill_skips',0)} · "
        f"陈旧估值 {diag.get('stale_valuation',0)} · "
        f"pending_exit 日数 {diag.get('pending_exit_days',0)}")

    if len(t) == 0:
        raise AssertionError("T3 无已平仓样本，无法注入退出日缺开盘；不得判为通过")
    if diag.get("closed_trades") != len(t):
        raise AssertionError("已平仓诊断计数与交易记录不一致")
    if len(diag.get("open_positions", [])) != ev["n_open_at_end"]:
        raise AssertionError("期末未平仓明细与计数不一致")

    row = t.sort_values("close_date").iloc[len(t) // 2]
    # load_ohlcv 返回裸币名；合成数据也可能使用完整 futures pair。
    keys = [k for k in data if k.split("/")[0] == row["pair"]]
    if len(keys) != 1:
        raise AssertionError(f"T3 无法唯一匹配行情 key：{row['pair']} ({keys})")
    coin = keys[0]
    D = pd.Timestamp(row["close_date"])
    if D not in data[coin].index:
        raise AssertionError("T3 退出日不在行情中")
    base_open = data[coin].loc[D, "open"]
    if not (np.isfinite(base_open) and base_open > 0):
        raise AssertionError("T3 基线退出日必须有有效开盘价")

    pert = {k: v.copy() for k, v in data.items()}
    pert[coin].loc[D, "open"] = np.nan
    tr2, _, _, d2 = engine(pert)
    if d2.get("missing_open_fill_skips", 0) <= 0 or d2.get("pending_exit_days", 0) <= 0:
        raise AssertionError("T3 注入缺开盘未触发退出挂起诊断")

    def same_entry(p):
        return (p["pair"] == row["pair"]
                and pd.Timestamp(p["open_date"]) == pd.Timestamp(row["open_date"])
                and bool(p["is_short"]) == bool(row["is_short"]))

    # 对目标仓位核验，而非只看本来就可能为正的全局计数。
    closed = [p for p in pd.DataFrame(tr2).to_dict("records") if same_entry(p)]
    opened = [p for p in d2.get("open_positions", []) if same_entry(p)]
    later = data[coin].loc[data[coin].index > D, "open"]
    valid = later[np.isfinite(later) & (later > 0)]
    if len(valid):
        expected = pd.Timestamp(valid.index[0])
        if len(closed) != 1 or opened:
            raise AssertionError("T3 挂起退出必须仍对应同一笔仓位并最终成交")
        target = closed[0]
        if not target.get("was_pending", False):
            raise AssertionError("T3 目标仓位退出未保留 pending_exit 标记")
        if pd.Timestamp(target["close_date"]) != expected:
            raise AssertionError("T3 挂起退出未在首个有效开盘成交")
        if float(target["close_rate"]) != float(valid.iloc[0]):
            raise AssertionError("T3 挂起退出成交价不等于首个有效开盘价")
        resolution = expected.strftime("%Y-%m-%d")
    else:
        if closed or len(opened) != 1 or not opened[0].get("pending_exit", False):
            raise AssertionError("T3 期末仍缺价时必须保留同一笔挂起仓位")
        target = opened[0]
        resolution = "pending_at_end"
    if float(target["stake"]) != float(row["stake"]):
        raise AssertionError("T3 挂起退出改变了原仓位金额")

    ev["injected_missing_open"] = {
        "coin": row["pair"], "data_key": coin, "day": D.strftime("%Y-%m-%d"),
        "entry_day": pd.Timestamp(row["open_date"]).strftime("%Y-%m-%d"),
        "missing_open_fill_skips": d2["missing_open_fill_skips"],
        "pending_exit_days": d2["pending_exit_days"], "resolution": resolution,
    }
    ev["passed"] = True
    log(f"    注入缺开盘({row['pair']} {D.date()}) → 同一仓位挂起；退出={resolution}")
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    import event_backtest as EB
    engine = EB.run_v2
    data = load()
    t0 = time.time()

    # 抽样：覆盖全部年份。按季度均匀取点。
    all_days = sorted(set().union(*[set(d.index) for d in data.values()]))
    if args.quick:
        sample = [all_days[i] for i in np.linspace(20, len(all_days) - 3, 8).astype(int)]
        cuts = [all_days[i] for i in np.linspace(200, len(all_days) - 3, 3).astype(int)]
    else:
        sample = [all_days[i] for i in np.linspace(20, len(all_days) - 3, 40).astype(int)]
        cuts = [all_days[i] for i in np.linspace(200, len(all_days) - 3, 8).astype(int)]

    out = {"engine": "run_v2", "factor": FACTOR, "coverage": "sampled_dates"}

    print(f"  ══ T1 锐利单日扰动（{len(sample)} 天，按时段抽样）══")
    out["T1"] = T1_sharp(engine, data, sample)
    bad = [r for r in out["T1"] if r["changed"]]
    t1_covered = sum(r["n_base"] > 0 for r in out["T1"])
    out["T1_coverage"] = {
        "sampled_dates": len(out["T1"]),
        "dates_with_base_decisions": int(t1_covered),
        "base_decisions": int(sum(r["n_base"] for r in out["T1"])),
        "has_decision_coverage": t1_covered > 0,
    }
    print(f"  → {len(bad)}/{len(out['T1'])} 天受未来收盘价影响")
    print(f"    覆盖：{t1_covered}/{len(out['T1'])} 个样本日有基线决策")
    # 按年汇总
    by_year = {}
    for r in out["T1"]:
        y = r["day"][:4]
        by_year.setdefault(y, [0, 0])
        by_year[y][0] += 1
        by_year[y][1] += 1 if r["changed"] else 0
    print("  ── 按年分段 ──")
    for y in sorted(by_year):
        n, b = by_year[y]
        print(f"     {y}: {b}/{n} 天受影响")
    out["T1_by_year"] = by_year

    print(f"\n  ══ T2 未来段扰动（{len(cuts)} 个切点）══")
    out["T2"] = T2_future(engine, data, cuts)
    bad2 = [r for r in out["T2"] if r["changed"]]
    t2_covered = sum(r["has_history_coverage"] for r in out["T2"])
    out["T2_coverage"] = {
        "cuts": len(out["T2"]),
        "cuts_with_base_history": int(t2_covered),
        "all_cuts_covered": bool(out["T2"] and t2_covered == len(out["T2"])),
    }
    print(f"  → {len(bad2)}/{len(out['T2'])} 个切点存在跨段泄漏")
    print(f"    覆盖：{t2_covered}/{len(out['T2'])} 个切点有基线历史决策")

    print("\n  ══ T3 边界证据 ══")
    try:
        out["T3"] = T3_boundaries(engine, data)
    except AssertionError as exc:
        out["T3"] = {"passed": False, "error": str(exc)}
        print(f"    ❌ T3 失败：{exc}")

    print(f"\n  用时 {time.time()-t0:.0f}s")
    out["verdict"] = {
        "T1_pass": len(bad) == 0 and t1_covered > 0,
        "T2_pass": len(bad2) == 0 and bool(out["T2"]) and t2_covered == len(out["T2"]),
        "T3_pass": out["T3"]["passed"],
        "note": "T1 至少一个样本日有基线决策，T2 每个切点均有基线历史决策；跨年份抽样不构成全日期或所有配置的证明",
    }
    print("  ══ 判定 ══")
    print(f"    T1 锐利单日扰动: {'✅ 通过' if out['verdict']['T1_pass'] else '❌ 失败'}")
    print(f"    T2 未来段扰动:   {'✅ 通过' if out['verdict']['T2_pass'] else '❌ 失败'}")
    print(f"    T3 缺开盘边界:   {'✅ 通过' if out['T3']['passed'] else '❌ 失败'}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"  已写 {args.json}")
    return 0 if all(out["verdict"][k] for k in ("T1_pass", "T2_pass", "T3_pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
