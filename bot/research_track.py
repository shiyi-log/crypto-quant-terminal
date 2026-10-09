#!/usr/bin/env python3
"""
状态：历史扫描 CLI 已禁用；工具函数仅供离线实现检验，不作生产可用性判定。

研究轨道：策略变体评估器（不碰实盘 dry_run）

════════════════════════════════════════════════════════════════════
定位
════════════════════════════════════════════════════════════════════
用户要求「持续修改策略」但又要「验证模型可用」——两者在同一实例里矛盾。
所以分轨：

    A 实盘模拟 dry_run   ← 冻结参数，累积干净记录，用于验证「当前模型是否可用」
    B 研究轨道（本脚本）  ← 随便改，用已验证引擎评估变体
    C 并行 dry_run       ← 可选，让候选变体也积累真实时序记录

只有 B 证明某变体稳健更好、且经用户同意，才换进 A（A 的验证计时重新开始）。

════════════════════════════════════════════════════════════════════
为什么必须用 event_backtest.py
════════════════════════════════════════════════════════════════════
研究文档记录：早期「多策略并行」的正面结论因【临时引擎在高换手下有跨时点
重复计价缺陷】而被撤回（V 的真实 Calmar 是 0.14 而非 1.46）。

所以本脚本只调用 `event_backtest.run_v2()` —— 这是当前唯一保留的时序正确参考引擎：
  ① 离场信号在收盘成立、成交在【次根开盘】（早一根会系统性高估 4.79% vs 3.29%）
  ② 排名在【全部有信号的币】里算，而不是只在不持仓的候选里（否则暴露 61% vs 30%）
  ③ 用 close 通道而非 high/low（Calmar 0.95 vs 0.67）

════════════════════════════════════════════════════════════════════
防过拟合的硬性要求
════════════════════════════════════════════════════════════════════
注册表明确写着 `C2 参数稳定性未过`（当前参数近 12 个月排 5/6）——
说明参数面本身就是脆的。在这种情况做参数搜索极易找到过拟合点。

所以本脚本强制报告：
  ① 全期指标
  ② 【前后半分】指标 —— 后段必须也不差，否则是过拟合
  ③ 【逐年】年化与回撤 —— 不能只靠某一年
  ④ 与基线（当前 20/20/top_n=8/30%）的对照

任何一个变体只有在【前段与后段都不差于基线】时才值得进一步考虑。

用法:
    python research_track.py --baseline                 # 只跑基线
    python research_track.py --sweep top_n              # 扫描 top_n
    python research_track.py --sweep universe           # 扫描币池大小
    python research_track.py --sweep exposure           # 扫描敞口
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 当前实盘 dry_run 的冻结参数（= 基线）
BASE = dict(top_n=8, max_open=10, exposure=0.30, cost_one=0.0005,
            start=None, universe=None,
            chan_entry=20, chan_exit=20)

# 策略参数（Donchian 通道周期）。
CHANNEL = 20


def metrics(trades, eq, ret, label):
    """⚠ 口径说明：
       eq  是净值曲线、ret 是【日】收益序列 —— 两者都用于年化/回撤/夏普。
       【逐笔】统计必须从 trades 的 profit_pct 算，不能用 ret！
       （我曾经混用过：把日胜利当逐笔胜率，得出 51.1% vs 研究 41.9% 的矛盾。）
    """
    eq = eq.dropna()
    if len(eq) < 2:
        return None
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    dr = ret.dropna()

    t = pd.DataFrame(trades)
    n = len(t)
    if n and "profit_pct" in t:
        tr_ret = t["profit_pct"].astype(float) / 100.0          # 逐笔收益（分数）
        wr = float((tr_ret > 0).mean() * 100)
        avg_tr = float(tr_ret.mean() * 100)
        wins = tr_ret[tr_ret > 0]; losses = tr_ret[tr_ret <= 0]
        pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else np.nan
        aw = float(wins.mean() * 100) if len(wins) else np.nan
        al = float(losses.mean() * 100) if len(losses) else np.nan
    else:
        wr = avg_tr = pf = aw = al = np.nan

    return {
        "label": label, "trades": int(n), "years": round(yrs, 2),
        "ann": float(ann) * 100, "dd": float(dd) * 100,
        "calmar": float(ann / abs(dd)) if dd else np.nan,
        "win_rate": wr,               # 逐笔胜率
        "profit_factor": pf,          # 逐笔盈亏比
        "avg_win": aw, "avg_loss": al,
        "avg_trade": avg_tr,          # 逐笔期望（%）
        "sharpe": float(dr.mean() / dr.std() * np.sqrt(252)) if len(dr) and dr.std() else np.nan,
    }


def show(m):
    if not m:
        print("    （样本不足）"); return
    print(f"    {m['label']:<20}{m['trades']:>6}笔{m['ann']:>9.2f}%{m['dd']:>9.2f}%"
          f"{m['calmar']:>8.2f}{m['win_rate']:>8.1f}%{m['profit_factor']:>8.2f}{m['avg_trade']:>9.2f}%")


def run_variant(data, params, label, splits=True):
    """跑一个变体，返回 (全期, 前段, 后段, 逐年)。"""
    import event_backtest as EB
    kw = dict(top_n=params["top_n"], max_open=params["max_open"],
              exposure=params["exposure"], cost_one=params["cost_one"],
              chan_entry=params.get("chan_entry", 20),
              chan_exit=params.get("chan_exit", 20))
    if params.get("start"):
        kw["start"] = params["start"]
    try:
        # 旧 run() 含已确认的入场时序前视，不能用于新的研究结论。
        tr, eq, ret, _diag = EB.run_v2(data, **kw)
    except Exception as e:
        print(f"    ❌ {label}: {type(e).__name__}: {e}")
        return None, None, None, None

    full = metrics(tr, eq, ret, label)

    if not splits:
        return full, None, None, None

    # 前后半分（用净值曲线对半切，分别算 —— 各自重新基准化）
    eq = eq.dropna()
    mid = len(eq) // 2
    seg = {}
    for name, sl in (("前段", slice(0, mid)), ("后段", slice(mid, None))):
        e = eq.iloc[sl]
        if len(e) < 30:
            seg[name] = None; continue
        e = e / e.iloc[0]
        yrs = (e.index[-1] - e.index[0]).days / 365.25
        ann = (e.iloc[-1]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
        dd = (e / e.cummax() - 1).min()
        seg[name] = {"ann": float(ann) * 100, "dd": float(dd) * 100,
                     "calmar": float(ann / abs(dd)) if dd else np.nan}

    # 逐年
    tdf = pd.DataFrame(tr)
    yearly = {}
    if len(tdf) and "close_date" in tdf:
        tdf["close_date"] = pd.to_datetime(tdf["close_date"])
        for y, g in tdf.groupby(tdf["close_date"].dt.year):
            if len(g) < 10:
                continue
            p = g["profit_abs"].sum()
            yearly[int(y)] = {"trades": int(len(g)), "pnl": float(p)}
    return full, seg.get("前段"), seg.get("后段"), yearly


def main():
    raise SystemExit(
        "LEGACY_DISABLED: research_track.py 历史扫描入口已禁用。run_v2 工具函数可导入，"
        "但幸存币池未校正，滑点/资金费未知，候选排名不是生产可用性或完整净收益。"
        "停止新增历史搜索；当前证据口径见 docs/DEVELOPMENT_LEDGER.md。"
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="只跑基线")
    ap.add_argument("--sweep", default=None,
                    choices=["top_n", "universe", "exposure", "max_open", "channel"])
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("=" * 100)
    print("  研究轨道 —— 策略变体评估（不碰实盘 dry_run）")
    print("=" * 100)
    print(f"  引擎：event_backtest.run_v2()（时序正确参考；旧 run() 仅留档）")
    print(f"  基线：top_n={BASE['top_n']} · max_open={BASE['max_open']} · "
          f"敞口={BASE['exposure']:.0%} · 通道 {CHANNEL}/{CHANNEL}")

    import ml_lab as ml
    t0 = time.time()
    data_all = ml.load_ohlcv()
    print(f"  行情：{len(data_all)} 个币 · 加载 {time.time()-t0:.0f}s")

    # 币池：按交易数/历史长度排序，取前 N（避免用「今天还活着」的后见之明）
    if args.sweep == "universe":
        order = sorted(data_all, key=lambda c: -len(data_all[c]))
    else:
        order = None

    results = []

    def do(params, label):
        d = data_all
        if params.get("universe") and order:
            d = {c: data_all[c] for c in order[:params["universe"]]}
        full, a, b, y = run_variant(d, params, label)
        if full:
            show(full)
            if a and b:
                print(f"      └ 前段 年化{a['ann']:>7.2f}% 回撤{a['dd']:>8.2f}% Calmar{a['calmar']:>6.2f}"
                      f"   |   后段 年化{b['ann']:>7.2f}% 回撤{b['dd']:>8.2f}% Calmar{b['calmar']:>6.2f}")
            full["front"], full["back"] = a, b
            full["yearly"] = y
            full["params"] = params
            results.append(full)
        return full

    if args.sweep is None or args.baseline:
        print("\n  ── 基线 ──")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        do(dict(BASE), "基线 20/20 tn8")

    if args.sweep == "top_n":
        print("\n  ── 扫描 top_n（当前 8，走查最优区间 5~12）──")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        do(dict(BASE), "【基线】当前实盘设置")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        for n in (5, 6, 7, 8, 10, 12):
            p = dict(BASE); p["top_n"] = n
            do(p, f"top_n={n}")

    if args.sweep == "universe":
        print("\n  ── 扫描币池大小（当前实盘 20）──")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        p0 = dict(BASE); p0["universe"] = 20
        do(p0, "【基线】当前实盘 20 币")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        for u in (10, 30, 40, 56):
            p = dict(BASE); p["universe"] = u
            do(p, f"币池={u}")

    if args.sweep == "exposure":
        print("\n  ── 扫描敞口（当前 30%；注意这是线性换回撤的旋钮）──")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        do(dict(BASE), "【基线】当前实盘设置")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        for e in (0.20, 0.30, 0.40, 0.50, 0.60):
            p = dict(BASE); p["exposure"] = e
            do(p, f"敞口={e:.0%}")

    if args.sweep == "channel":
        print("\n  ── 扫描 Donchian 通道周期（当前 20/20）──")
        print("     实盘策略用 enter_period=exit_period=20；")
        print("     短周期=更敏感更多信号，长周期=更少更强趋势")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        do(dict(BASE), "【基线】20/20")
        for e, x in ((10,10),(15,15),(20,20),(30,30),(40,40),(60,60),(20,10),(10,20),(40,20),(20,40)):
            if e==20 and x==20: continue
            p2=dict(BASE); p2["chan_entry"]=e; p2["chan_exit"]=x
            do(p2, f"通道 {e}/{x}")

    if args.sweep == "max_open":
        print("\n  ── 扫描 max_open（当前 10；top_n=8 是实际约束）──")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        do(dict(BASE), "【基线】当前实盘设置")
        print(f"    {'变体':<20}{'笔数':>6}{'年化':>10}{'回撤':>9}{'Calmar':>8}{'逐笔胜率':>9}{'盈亏比':>8}{'均笔':>10}")
        for m in (4, 6, 8, 10, 15):
            p = dict(BASE); p["max_open"] = m
            do(p, f"max_open={m}")

    # ── 汇总与判读 ──
    print("\n" + "=" * 100)
    print("  判读（防过拟合）")
    print("=" * 100)
    if len(results) >= 2:
        # ⚠ 必须与【当前实盘设置】对照，不能拿 results[0]——sweep 模式下第一个变体不是基线
        base = next((r for r in results if r["label"].startswith("【基线】")
                     or r["label"].startswith("基线")), None)
        if base is None:
            print("  ⚠ 本轮没有与当前实盘设置一致的变体，无法判读"); return
        print(f"  对照基准（当前实盘设置）：{base['label']} · 年化 {base['ann']:.2f}% · "
              f"回撤 {base['dd']:.2f}% · Calmar {base['calmar']:.2f}")
        print(f"\n  {'变体':<22}{'年化差':>9}{'Calmar差':>10}{'前段Calmar':>11}{'后段Calmar':>11}{'判定':>12}")
        print("  " + "-" * 76)
        for r in results:
            if r is base:
                continue
            da = r["ann"] - base["ann"]
            dc = (r["calmar"] or 0) - (base["calmar"] or 0)
            fc = (r.get("front") or {}).get("calmar", float("nan"))
            bc = (r.get("back") or {}).get("calmar", float("nan"))
            ok = (da > 0) and (dc > 0) and (fc >= (base.get("front") or {}).get("calmar", -9)) \
                 and (bc >= (base.get("back") or {}).get("calmar", -9))
            print(f"  {r['label']:<22}{da:>+8.2f}%{dc:>+10.2f}{fc:>11.2f}{bc:>11.2f}"
                  f"{'✅ 候选' if ok else '❌':>12}")
        print("\n  ⚠ 判据：年化与 Calmar 都要涨，且【前段与后段都不差于基线】。")
        print("     只满足全期变好 = 过拟合迹象，不得采纳。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2, default=float)
        print(f"\n  已存 {args.json}")


if __name__ == "__main__":
    main()
