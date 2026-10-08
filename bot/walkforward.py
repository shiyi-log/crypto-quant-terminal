#!/usr/bin/env python3
"""
Walk-forward 评估框架 + 策略变量拆解

要回答的问题：
    同一个趋势跟踪策略，15 币 Sharpe 1.14，55 币只有 0.63 —— 为什么加币变差？
    是【股票池规模】、【信号选择】、【仓位分配】还是【参数】的问题？

四个待测变量：
    A. 股票池规模   10 / 20 / 30 / 55 币
    B. 信号选择     全部突破 vs 按强度取前 N 个
    C. 仓位分配     等权 / 波动率目标 / ATR 风险预算
    D. 参数         entry × exit 网格

评估纪律（本项目教训固化）：
    1. 所有指标 shift(1)，绝无前视
    2. 报告 Sharpe 的 t 值 = Sharpe × √年数（这是唯一正确的检验）
    3. 分年符号一致性检查
    4. walk-forward：前段定参数，后段验证，只认样本外表现
    5. 变量拆解时其他条件必须固定（避免混淆）

用法:
    python walkforward.py --universe       # A. 池规模
    python walkforward.py --select         # B. 信号选择
    python walkforward.py --sizing         # C. 仓位分配
    python walkforward.py --params         # D. 参数 + 走查
    python walkforward.py --all
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
COST_ONE = 0.0003      # maker 0.02% + 滑点 0.01%，单边


# ══════════════════ 数据 ══════════════════

def discover():
    """按【历史长度 × 成交额代理】排序，返回可用币列表"""
    rows = []
    for f in sorted(glob.glob(f"{PERP}/*-1d-futures.feather")):
        sym = os.path.basename(f).split("_")[0]
        try:
            d = pd.read_feather(f)
        except Exception:
            continue
        if len(d) < 400:
            continue
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date").sort_index()
        # 用近期成交额做流动性代理
        liq = (d["close"] * d["volume"]).tail(90).median()
        rows.append({"sym": sym, "rows": len(d), "liq": liq,
                     "start": d.index[0]})
    df = pd.DataFrame(rows)
    # 只保留有足够历史 + 流动性靠前的
    df = df[df["rows"] >= 400].sort_values("liq", ascending=False)
    return list(df["sym"])


def load(syms):
    out = {}
    for s in syms:
        f = f"{PERP}/{s}_USDT_USDT-1d-futures.feather"
        if not os.path.exists(f):
            continue
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) < 200:
            continue
        out[s] = d[["close", "high", "low", "volume"]]
    return out


def liquidity_panel(data, win=90):
    """各币滚动成交额（美元）—— 只用历史"""
    return pd.DataFrame({s: (d["close"] * d["volume"]).rolling(win).median()
                         for s, d in data.items()}).sort_index()


def top_n_at(liq: pd.DataFrame, n: int, shift=1):
    """
    每个时点按【滚动成交额】取前 n 个币，返回布尔面板。
    shift=1 保证用的是昨天及以前的排名（无前视）。
    """
    ranked = liq.shift(shift).rank(axis=1, ascending=False, method="first")
    return ranked <= n


# ══════════════════ 信号与仓位 ══════════════════

def signals(close: pd.Series, entry=20, exit_=20):
    hh = close.rolling(entry).max().shift(1)
    ll = close.rolling(entry).min().shift(1)
    ex_h = close.rolling(exit_).max().shift(1)
    ex_l = close.rolling(exit_).min().shift(1)

    pos = np.zeros(len(close))
    cur = 0.0
    c = close.values
    h_, l_, eh, el = hh.values, ll.values, ex_h.values, ex_l.values
    for i in range(len(c)):
        if np.isfinite(h_[i]) and np.isfinite(l_[i]):
            if cur == 0:
                if c[i] > h_[i]:
                    cur = 1.0
                elif c[i] < l_[i]:
                    cur = -1.0
        if cur != 0:
            if cur > 0 and np.isfinite(el[i]) and c[i] < el[i]:
                cur = 0.0
            elif cur < 0 and np.isfinite(eh[i]) and c[i] > eh[i]:
                cur = 0.0
        pos[i] = cur
    return pd.Series(pos, index=close.index)


def signal_strength(close: pd.Series, entry=20):
    """突破强度：收盘价超出通道边界的幅度（越大越强）"""
    hh = close.rolling(entry).max().shift(1)
    ll = close.rolling(entry).min().shift(1)
    span = (hh - ll).replace(0, np.nan)
    up = (close - hh) / span
    dn = (ll - close) / span
    return up.fillna(-np.inf).combine(dn.fillna(-np.inf), max)


def realized_vol(close: pd.Series, win=30):
    return close.pct_change().rolling(win).std().shift(1) * np.sqrt(365)


# ══════════════════ 回测核心 ══════════════════

def backtest(data, entry=20, exit_=20, sizing="vol_target", target_vol=0.15,
             top_n=None, start=None, max_weight=0.25,
             universe_n=None, universe_win=90):
    """
    sizing: 'equal' | 'vol_target' | 'atr_risk'
    top_n : 只做信号强度最高的 N 个币（None = 全部）
    """
    S, C, V = {}, {}, {}
    for s, d in data.items():
        if start is not None:
            d = d[d.index >= start]
            if len(d) < 120:
                continue
        S[s] = signals(d["close"], entry, exit_)
        C[s] = d["close"]
        V[s] = realized_vol(d["close"])
    if not S:
        return None

    Sd = pd.DataFrame(S).sort_index()
    Cd = pd.DataFrame(C).sort_index()
    Vd = pd.DataFrame(V).sort_index()

    # ── 仓位分配 ──
    # ⚠️ 关键：vol_target 不能简单把权重归一化到 1（那是满仓），
    #    否则信号少的时候会高度集中，波动率飙到 70%+。
    #    标准做法是：先算原始权重 → 估计组合波动率 → 缩放到目标波动率。
    if sizing == "equal":
        W = Sd / Sd.shape[1]
    elif sizing == "vol_target":
        inv = 1.0 / Vd.replace(0, np.nan)
        W = inv.mul(Sd != 0).fillna(0.0)        # 逆波动率 × 有无信号
        W = W.clip(upper=max_weight * Sd.shape[1])   # 单币上限（相对）
    elif sizing == "atr_risk":
        atr = Cd.pct_change().abs().rolling(20).mean().shift(1)
        unit = (0.01 / atr.replace(0, np.nan)).clip(upper=1.0)
        W = (unit * (Sd != 0)).div(Sd.shape[1]).fillna(0.0)
    else:
        raise ValueError(sizing)

    # ── 信号选择：只保留强度最高的 top_n 个 ──
    if top_n:
        ST = {}
        for s, d in data.items():
            if s in Sd.columns:
                ST[s] = signal_strength(d["close"], entry)
        STd = pd.DataFrame(ST).sort_index()
        active = (Sd != 0)
        # 每个时点按强度排名，非前 N 的置 0
        rank = STd.where(active).rank(axis=1, ascending=False, method="first")
        keep = rank <= top_n
        W = W.where(keep, 0.0)

    # ── 动态选池：每个时点只做滚动成交额前 universe_n 的币（无前视）──
    if universe_n:
        liq = liquidity_panel(data, universe_win)
        keep_u = top_n_at(liq, universe_n).reindex_like(W).fillna(False)
        W = W.where(keep_u, 0.0)

    # ── 组合波动率缩放（vol_target 模式）──
    if sizing == "vol_target":
        ret_all = Cd.pct_change()
        raw = (W * ret_all).sum(axis=1)
        vol_est = raw.rolling(60, min_periods=20).std().shift(1) * np.sqrt(365)
        scale = (target_vol / vol_est.replace(0, np.nan)).clip(upper=3.0).fillna(0.0)
        W = W.mul(scale, axis=0)

    W = W.shift(1).fillna(0.0)                 # 信号执行滞后一期

    ret = Cd.pct_change()
    gross = (W * ret).sum(axis=1)
    turn = W.diff().abs().sum(axis=1).fillna(0)
    net = (gross - turn * COST_ONE).dropna()
    return net


def evaluate(r, label=""):
    if r is None or len(r) < 100:
        return None
    eq = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365
    ann = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = r.std() * np.sqrt(365)
    sharpe = (r.mean() * 365) / vol if vol > 0 else np.nan
    mdd = ((eq / eq.cummax()) - 1).min()
    t = sharpe * np.sqrt(years) if years > 0 else np.nan     # 正确的显著性检验
    yearly = {int(y): ((1 + g).prod() - 1) * 100
              for y, g in r.groupby(r.index.year)}
    pos_years = sum(1 for v in yearly.values() if v > 0)
    return {"label": label, "years": years, "ann": ann * 100, "vol": vol * 100,
            "sharpe": sharpe, "t": t, "mdd": mdd * 100,
            "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "yearly": yearly, "pos_years": pos_years, "n_years": len(yearly),
            "win": (r > 0).mean() * 100}


def show(e):
    if not e:
        print("  （样本不足）")
        return
    sig = "✅" if abs(e["t"]) > 2 else "  "
    print(f"  {e['label']:<34}{e['ann']:>9.2f}%{e['vol']:>8.1f}%{e['sharpe']:>9.2f}"
          f"{e['t']:>8.2f}{sig}{e['mdd']:>10.2f}%{e['calmar']:>9.2f}"
          f"{e['pos_years']:>5}/{e['n_years']:<3}")


def header(title):
    print()
    print("=" * 112)
    print(title)
    print("=" * 112)
    print(f"  {'配置':<34}{'年化':>10}{'波动':>9}{'Sharpe':>9}{'t值':>8}"
          f"  {'回撤':>10}{'Calmar':>9}{'正收益年':>8}")
    print("  " + "-" * 100)


# ══════════════════ 四个变量 ══════════════════

def test_universe(all_syms):
    header("变量 A：股票池规模（单笔等权、20/20、固定 2020 起）")
    res = []
    for n in [10, 20, 30, 40, 55]:
        syms = all_syms[:n]
        data = load(syms)
        r = backtest(data, sizing="equal", start="2020-01-01")
        e = evaluate(r, f"{n} 币（等权）")
        show(e)
        if e:
            res.append((n, e))
    return res


def test_dynamic_universe(all_syms):
    header("变量 A-2：动态选池（每时点按滚动 90 日成交额取前 N，无前视）")
    data = load(all_syms)
    res = []
    for n in [5, 10, 15, 20, 30, None]:
        r = backtest(data, sizing="vol_target", universe_n=n, start="2020-01-01")
        e = evaluate(r, f"动态前 {n} 强" if n else "全部（等权归一）")
        show(e)
        if e:
            res.append((n, e))
    return res


def test_select(all_syms):
    header("变量 B：信号选择（55 币池，只做强度最高的 N 个）")
    data = load(all_syms[:55])
    res = []
    for n in [None, 3, 5, 8, 12]:
        for sizing in ["equal", "vol_target"]:
            r = backtest(data, sizing=sizing, top_n=n, start="2020-01-01")
            e = evaluate(r, f"{'全部' if n is None else f'前{n}强'}/{sizing}")
            show(e)
            if e:
                res.append((n, sizing, e))
    return res


def test_sizing(all_syms):
    header("变量 C：仓位分配（55 币，20/20）")
    data = load(all_syms[:55])
    res = []
    for sizing in ["equal", "vol_target", "atr_risk"]:
        r = backtest(data, sizing=sizing, start="2020-01-01")
        e = evaluate(r, sizing)
        show(e)
        if e:
            res.append((sizing, e))
    return res


def test_params(all_syms, oos_year=2023):
    """
    变量 D：参数 + walk-forward
    样本内 2020-2022 选参数 → 样本外 2023-2026 验证
    """
    data = load(all_syms[:30])
    grid = [(10, 5), (20, 10), (20, 20), (30, 15), (55, 20), (55, 10), (100, 50)]

    header(f"变量 D-1：样本内（2020-2022）选参数")
    ins = []
    for en, ex in grid:
        r = backtest(data, entry=en, exit_=ex, sizing="vol_target",
                     start="2020-01-01")
        if r is None:
            continue
        r = r[r.index < f"{oos_year}-01-01"]
        e = evaluate(r, f"entry={en} exit={ex}")
        show(e)
        if e:
            ins.append((en, ex, e))

    if not ins:
        return None
    best = max(ins, key=lambda x: x[2]["sharpe"])
    print(f"\n  样本内最优: entry={best[0]} exit={best[1]} "
          f"Sharpe {best[2]['sharpe']:.2f}")

    header(f"变量 D-2：样本外验证（{oos_year} 起，用样本内选出的参数）")
    oos = []
    for en, ex in grid:
        r = backtest(data, entry=en, exit_=ex, sizing="vol_target",
                     start=f"{oos_year}-01-01")
        e = evaluate(r, f"entry={en} exit={ex}")
        show(e)
        if e:
            oos.append((en, ex, e))
    if oos:
        b = max(oos, key=lambda x: x[2]["sharpe"])
        print(f"\n  样本外最优: entry={b[0]} exit={b[1]} Sharpe {b[2]['sharpe']:.2f}")
        print(f"  样本内选的参数在样本外的排名: "
              f"{[i for i,(a,bb,_) in enumerate(sorted(oos, key=lambda x:-x[2]['sharpe'])) if (a,bb)==(best[0],best[1])][0]+1}"
              f" / {len(oos)}")
        print(f"  → {'参数稳定 ✅' if (best[0],best[1])==(b[0],b[1]) else '参数不稳定 ⚠（样本内最优 ≠ 样本外最优）'}")
    return ins, oos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--dynamic", action="store_true")
    ap.add_argument("--select", action="store_true")
    ap.add_argument("--sizing", action="store_true")
    ap.add_argument("--params", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    syms = discover()
    print(f"  可用币种 {len(syms)} 个（按流动性排序）")
    print(f"  前 12 个: {', '.join(syms[:12])}")

    if args.all or args.universe:
        test_universe(syms)
    if args.all or args.dynamic:
        test_dynamic_universe(syms)
    if args.all or args.select:
        test_select(syms)
    if args.all or args.sizing:
        test_sizing(syms)
    if args.all or args.params:
        test_params(syms)

    if not any([args.all, args.universe, args.select, args.sizing, args.params]):
        print("\n  请指定 --universe / --select / --sizing / --params / --all")


if __name__ == "__main__":
    main()
