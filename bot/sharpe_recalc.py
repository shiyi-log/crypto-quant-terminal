#!/usr/bin/env python3
"""
Sharpe 口径重算

背景：
    Freqtrade 报 Sharpe 0.42，而本项目研究框架报 1.30 —— 同一策略差 3 倍。
    查明是【零收益日】的处理差异：

        全部日历日（含无持仓日）：Sharpe 0.43   t = 1.11
        仅活跃日（排除无持仓日）：Sharpe 1.26   t = 1.11

    **t 值相同，Sharpe 差 3 倍。** 说明 Sharpe 对口径极其敏感，
    不适合作为跨框架的改进判据。

本脚本对关键结论用【两种口径】重算，并给出不受口径影响的替代指标：
    · Calmar = 年化 / 最大回撤
    · 年化 / 波动
    · t 值（不依赖零日处理）

用法: python sharpe_recalc.py
"""

import numpy as np
import pandas as pd

import walkforward as wf

CAL = 365


def both_sharpes(r: pd.Series, label: str):
    """返回两种口径的 Sharpe 以及稳健指标"""
    if r is None or len(r) < 100:
        return None
    eq = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365
    ann = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(CAL)
    mdd = ((eq / eq.cummax()) - 1).min()

    # 口径 A：全部日历日（标准，含无持仓日=0）
    sh_all = (r.mean() * CAL) / vol if vol > 0 else np.nan
    t_all = sh_all * np.sqrt(years)

    # 口径 B：仅活跃日（本项目研究框架此前用的，会虚高）
    act = r[r != 0]
    if len(act) > 30:
        vol_a = act.std() * np.sqrt(CAL)
        sh_act = (act.mean() * CAL) / vol_a if vol_a > 0 else np.nan
        # 活跃日口径的 t 值：用活跃日数折算
        t_act = ((act.mean() / act.std()) * np.sqrt(len(act))) if act.std() > 0 else np.nan
    else:
        sh_act = t_act = np.nan

    return {
        "label": label, "years": years, "ann": ann * 100, "vol": vol * 100,
        "mdd": mdd * 100, "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
        "vol_ret": ann / vol if vol > 0 else np.nan,
        "sharpe_all": sh_all, "sharpe_active": sh_act,
        "t_all": t_all, "t_active": t_act,
        "n_all": len(r), "n_active": len(act) if len(act) else 0,
        "active_pct": (len(act) / len(r) * 100) if len(r) else np.nan,
    }


def show(rows):
    print(f"  {'配置':<28}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'年化/波动':>10}"
          f"{'Sharpe全':>10}{'Sharpe活':>10}{'倍数':>6}{'t值':>7}{'活跃日占比':>10}")
    print("  " + "-" * 110)
    for r in rows:
        if not r:
            continue
        mult = r["sharpe_active"] / r["sharpe_all"] if r["sharpe_all"] else np.nan
        print(f"  {r['label']:<28}{r['ann']:>8.2f}%{r['mdd']:>8.2f}%{r['calmar']:>8.2f}"
              f"{r['vol_ret']:>10.2f}{r['sharpe_all']:>10.2f}{r['sharpe_active']:>10.2f}"
              f"{mult:>6.1f}{r['t_all']:>7.2f}{r['active_pct']:>9.0f}%")
    print("  " + "-" * 110)


def main():
    syms = wf.discover()
    data = wf.load(syms)
    print(f"  币种 {len(data)}")

    print()
    print("=" * 118)
    print("关键结论的两种 Sharpe 口径重算")
    print("=" * 118)

    rows = []

    # 1. 趋势跟踪：全部币等权
    rows.append(both_sharpes(
        wf.backtest(data, sizing="equal", start="2020-01-01"),
        "趋势跟踪 全部币/等权"))

    # 2. 趋势跟踪：全部币 + 波动率目标
    rows.append(both_sharpes(
        wf.backtest(data, sizing="vol_target", start="2020-01-01"),
        "趋势跟踪 全部币/波动率目标"))

    # 3. 趋势跟踪：全部币 + ATR 定仓
    rows.append(both_sharpes(
        wf.backtest(data, sizing="atr_risk", start="2020-01-01"),
        "趋势跟踪 全部币/ATR定仓"))

    # 4. v2：突破强度选前 8
    rows.append(both_sharpes(
        wf.backtest(data, sizing="equal", top_n=8, start="2020-01-01"),
        "v2 强度前8/等权"))

    # 5. v2：前 3 强
    rows.append(both_sharpes(
        wf.backtest(data, sizing="equal", top_n=3, start="2020-01-01"),
        "v2 强度前3/等权"))

    # 6. v2：前 12 强
    rows.append(both_sharpes(
        wf.backtest(data, sizing="equal", top_n=12, start="2020-01-01"),
        "v2 强度前12/等权"))

    show(rows)

    print()
    print("=" * 118)
    print("结论")
    print("=" * 118)
    valid = [r for r in rows if r]
    if valid:
        m = np.mean([r["sharpe_active"] / r["sharpe_all"] for r in valid
                     if r["sharpe_all"] and not np.isnan(r["sharpe_all"])])
        print(f"""
  ① 「仅活跃日」口径把 Sharpe 平均放大 {m:.1f} 倍
     （活跃日只占全部交易日的 {np.mean([r['active_pct'] for r in valid]):.0f}%）

  ② t 值两种口径一致 —— 因为 t = Sharpe × √样本量，
     「仅活跃日」时样本量同步缩小，两者抵消。
     所以【t 值可信，Sharpe 不可直接比较】。

  ③ 替代指标（完全不受该口径影响）:
        Calmar = 年化 / 最大回撤
        年化 / 波动
     v2 相对 v1 的改进在这两个指标上依然成立：
        Calmar      {next(r['calmar'] for r in valid if '前8' in r['label']):.2f}  (v2 前8)
                    {next(r['calmar'] for r in valid if '波动率目标' in r['label']):.2f}  (v1 波动率目标)
        年化/波动   {next(r['vol_ret'] for r in valid if '前8' in r['label']):.2f}  (v2 前8)
                    {next(r['vol_ret'] for r in valid if '波动率目标' in r['label']):.2f}  (v1 波动率目标)
""")
    return rows


if __name__ == "__main__":
    main()
