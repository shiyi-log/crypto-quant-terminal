#!/usr/bin/env python3
"""
状态：已禁用，仅保留历史源码；下述研究预期和可用性判据已经作废。

干跑绩效追踪 —— 累积平仓样本，对照研究预期，判断模型是否可用。

════════════════════════════════════════════════════════════════════
「可用」怎么判定（先定标准，避免事后主观解释）
════════════════════════════════════════════════════════════════════
研究侧（739 笔实盘规则回放 + 剥离幸存者偏差后的诚实区间）：

    胜率        41.9%
    平均盈利    +44.61%
    平均亏损    -14.28%
    盈亏比       3.12
    期望/笔     +10.43%
    诚实年化     13% ~ 26%
    最大回撤     -19% ~ -32%
    Calmar      0.6 ~ 0.85

本脚本做三件事：

  ① 事实：累积样本的胜率 / 盈亏比 / 期望 / 极值
  ② 机制：确认「少数大赢单覆盖多数小亏单」是否在运行
  ③ 功效：当前样本量能检出多大的偏差 —— 样本不足时【明确说不足】，
          绝不用十几个样本宣布「可用」或「不可用」

════════════════════════════════════════════════════════════════════
统计功效说明（为什么样本量这么关键）
════════════════════════════════════════════════════════════════════
要把「胜率 41.9%」与「50% 掷硬币」区分开，需要：

    SE = sqrt(p(1-p)/n)
    n=30  → SE=9.0%  → 41.9% 与 50% 差 0.9 个 SE，检不出
    n=100 → SE=4.9%  → 差 1.6 个 SE，仍检不出
    n=400 → SE=2.5%  → 差 3.3 个 SE，能检出

研究回测约 490 笔 / 5.8 年 ≈ 84 笔/年 ≈ 7 笔/月。
→ 结论：**判「可用」需要按月计的积累，不是几天的事。**

用法:
    python dryrun_stats.py
    python dryrun_stats.py --json out.json
"""
# Refuse before imports, database reads, or output writes, including when
# another module attempts to reuse the superseded usability calculation.
raise SystemExit(
    "LEGACY_DISABLED: dryrun_stats.py 已禁用。旧 EXP 收益预期已作废，"
    "胜率功效不能作为 usable 判据；历史源码与研究记录保留。"
    "当前订单、收益状态和限制请查看研究账本及 docs/DEVELOPMENT_LEDGER.md。"
)

import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_HERE, "tradesv3.dryrun.sqlite")

# 研究预期
EXP = {
    "win_rate": 41.9,          # %
    "avg_win": 44.61,          # %
    "avg_loss": -14.28,        # %
    "profit_factor": 3.12,
    "expectancy": 10.43,       # % / 笔
    "annual_low": 13.0,        # %
    "annual_high": 26.0,       # %
    "dd_low": -32.0,           # %
    "dd_high": -19.0,          # %
    "trades_per_year": 84,
}


# ══════════════════════════════════════════════════════════════════
#  统计功效 —— 模块级唯一实现，供多处复用
# ══════════════════════════════════════════════════════════════════
#  为什么抽出来：原先这个公式在【两处】各写了一遍（主路径 + 零平仓分支），
#  我改了主路径后另一处仍在输出已被推翻的 149 笔 —— 重复逻辑必然漂移。
Z_ALPHA_2 = 1.959964      # 双侧 α=0.05
Z_BETA = 0.8416212        # 80% 功效


def need_for_power(p1, p0=0.50, power=0.80):
    """达到指定功效所需的【独立】样本量（正态近似）。

    ⚠ 不能用 (δ/2)² —— 那是 z=2、功效只有约 50% 的点。我据此报过
      "149 笔可达统计功效"，是错的：精确二项在 n=149 时功效仅 50.6%。
    """
    delta = abs(p0 - p1)
    if delta <= 0:
        return float("inf")
    p = p1
    return p * (1 - p) / ((delta / (Z_ALPHA_2 + Z_BETA)) ** 2)


def exact_power(n, p1, p0=0.50, alpha=0.05):
    """精确二项检验功效（双侧），用于核验正态近似。"""
    from math import comb

    def cdf(k, nn, pp):
        return sum(comb(nn, i) * pp ** i * (1 - pp) ** (nn - i) for i in range(0, k + 1))

    if n < 10:
        return float("nan")
    lo = hi = None
    for k in range(int(n * p0), -1, -1):
        if 2 * cdf(k, n, p0) <= alpha:
            lo = k
            break
    for k in range(int(n * p0), n + 1):
        if 2 * (1 - cdf(k - 1, n, p0)) <= alpha:
            hi = k
            break
    if lo is None or hi is None:
        return float("nan")
    return cdf(lo, n, p1) + (1 - cdf(hi - 1, n, p1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("=" * 96)
    print("  干跑绩效追踪 —— 对照研究预期")
    print("=" * 96)

    if not os.path.exists(args.db):
        print(f"  ❌ 找不到 {args.db}"); sys.exit(1)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    tr = pd.read_sql_query("select * from trades", con)
    con.close()

    if tr.empty:
        print("  交易库为空"); sys.exit(0)
    tr["is_open"] = tr["is_open"].astype(bool)
    for c in ("open_date", "close_date"):
        if c in tr:
            tr[c] = pd.to_datetime(tr[c], errors="coerce", utc=True).dt.tz_localize(None)

    op = tr[tr["is_open"]]
    cl = tr[~tr["is_open"]].copy()

    # ── 概览 ──
    print(f"\n  总交易 {len(tr)} · 持仓 {len(op)} · 已平仓 {len(cl)}")
    if len(tr):
        print(f"  时间跨度 {tr['open_date'].min()} → {tr['open_date'].max()}")
    days = (tr["open_date"].max() - tr["open_date"].min()).days if len(tr) > 1 else 0
    # 用【首笔开仓到现在】的天数，且下限 1 天 —— 否则同一天批量开仓会算出荒谬的月率
    span = max(((pd.Timestamp.now() - tr["open_date"].min()).days if len(tr) else 0), 1)
    rate_m = len(tr) / span * 30
    print(f"  累计 {span} 天（首笔至今）· 平均 {rate_m:.1f} 笔/月"
          f"  （研究侧约 {EXP['trades_per_year']/12:.1f} 笔/月）")
    if span < 30:
        print("  ⚠ 观察期不足 30 天，月率还不能当参考")

    # ── 持仓 ──
    if len(op):
        tot = op["stake_amount"].sum()
        print(f"\n  持仓 {len(op)} 笔 · 多 {int((~op['is_short'].astype(bool)).sum())}"
              f" · 空 {int(op['is_short'].astype(bool).sum())}")
        print(f"  敞口 {tot:,.0f} / 9000 = {tot/9000*100:.2f}%")

    # ── 平仓统计 ──
    if cl.empty:
        print("\n" + "=" * 96)
        print("  ⚠ 尚无平仓交易 —— 【无法判断模型是否可用】")
        print("=" * 96)
        print("  现在只能说：开仓实现正确（见 dryrun_verify.py）。")
        print("  离场行为与盈亏分布【一次都还没被检验过】。")
        _p1 = EXP["win_rate"] / 100
        _need = int(np.ceil(need_for_power(_p1)))
        print(f"\n  参照：研究侧约 {EXP['trades_per_year']/12:.1f} 笔/月")
        print(f"  区分「胜率 {EXP['win_rate']}%」与「50% 掷硬币」：")
        print(f"    · n=149 时精确二项功效仅 {exact_power(149, _p1):.1%} —— 约等于掷硬币，不足以判定")
        print(f"    · 达 80% 功效需约 {_need} 笔【独立】交易 ≈ {_need/(EXP['trades_per_year']/12):.0f} 个月")
        print(f"  ⚠ 且「胜率是否低于 50%」本身不是趋势策略的检验对象 ——")
        print(f"    盈利来源是盈亏比（研究侧预期 3.12），应以【扣费后净增量】检验；")
        print(f"    交易相关性还会进一步推高所需样本量。")
        if args.json:
            json.dump({"n_open": len(op), "n_closed": 0, "verdict": "insufficient"},
                      open(args.json, "w"), ensure_ascii=False, indent=2)
        return

    pnl = cl["close_profit_abs"].astype(float)
    wins = pnl[pnl > 0]; losses = pnl[pnl <= 0]
    n = len(cl)
    wr = (pnl > 0).mean() * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    exp = pnl.mean()
    stake = cl["stake_amount"].astype(float).replace(0, np.nan)
    exp_pct = (pnl / stake).mean() * 100

    print("\n" + "=" * 96)
    print("  平仓统计 vs 研究预期")
    print("=" * 96)
    print(f"  {'指标':<16}{'实测':>14}{'研究预期':>16}{'':>6}")
    print("  " + "-" * 56)
    print(f"  {'平仓笔数':<16}{n:>14}{'≈84/年':>16}")
    print(f"  {'胜率':<16}{wr:>13.1f}%{EXP['win_rate']:>15.1f}%")
    aw = (wins/stake[pnl > 0]).mean()*100 if len(wins) else np.nan
    al = (losses/stake[pnl <= 0]).mean()*100 if len(losses) else np.nan
    print(f"  {'平均盈利':<16}{aw:>13.2f}%{EXP['avg_win']:>15.2f}%")
    print(f"  {'平均亏损':<16}{al:>13.2f}%{EXP['avg_loss']:>15.2f}%")
    print(f"  {'盈亏比':<16}{pf:>14.2f}{EXP['profit_factor']:>16.2f}")
    # ⚠ 单位必须对齐：exp 是绝对 USDT，exp_pct 才是可与研究预期比较的百分比
    print(f"  {'期望/笔(绝对)':<16}{exp:>13.2f} {'USDT':>16}")
    print(f"  {'期望/笔(占本金)':<16}{exp_pct:>13.2f}%{EXP['expectancy']:>15.2f}%")

    # ── 机制核验 ──
    print("\n  ── 机制核验：少数大赢单覆盖多数小亏单？ ──")
    if len(wins) == 0:
        print(f"  ❌ 样本内【一笔盈利都没有】（{len(losses)} 笔全亏）")
        print("     正偏机制的核心是「少数大赢单覆盖多数小亏单」——")
        print("     一笔盈利都没有时，该机制【未体现】。但样本极少时这可能是偶然，")
        print("     下一行给出统计判断，不要据此立刻下结论。")
    if len(wins):
        top_gain = wins.max()
        share = top_gain / pnl.sum() * 100 if pnl.sum() != 0 else np.nan
        print(f"  最大单笔盈利 {top_gain:+,.2f} · 占净利润 {share:.1f}%")
        print(f"  盈利笔数 {len(wins)} · 亏损笔数 {len(losses)}")
        med_w = wins.median(); med_l = abs(losses.median()) if len(losses) else np.nan
        print(f"  中位盈利 {med_w:+,.2f} · 中位亏损 {med_l:,.2f}"
              f"  → 中位盈亏比 {med_w/med_l if med_l else np.nan:.2f}")
        if pf > 1.5 and wr < 50:
            print("  ✅ 正偏机制在运行（低胜率 + 高盈亏比）")
        elif pf <= 1:
            print("  ❌ 盈亏比 ≤ 1 —— 正偏机制【未】体现，需要警惕")
        else:
            print("  🟡 偏弱，样本还少，暂不下结论")

    # ── 统计功效 ──
    # ⚠ 这里原先是 (δ/2)²，即 z=2 的 2σ 点 —— 那是【功效只有 50%】的位置，
    #    不是可判定的样本量。我据此报过"149 笔可达统计功效"，是错的：
    #    精确二项检验在 n=149 时功效仅 50.6%（约等于掷硬币）。
    #    正确用 80% 功效：SE = δ/(z_{α/2} + z_β)，α=0.05 双侧、β=0.20。
    p_exp = EXP["win_rate"] / 100
    need = int(np.ceil(need_for_power(p_exp)))
    # 精确二项功效（用于核验，避免再犯近似错的错）
    from math import comb as _comb
    def _binom_cdf(k, nn, pp):
        return sum(_comb(nn, i) * pp**i * (1 - pp)**(nn - i) for i in range(0, k + 1))
    def _exact_power(nn):
        lo = hi = None
        for k in range(int(nn * 0.5), -1, -1):
            if 2 * _binom_cdf(k, nn, 0.5) <= 0.05:
                lo = k; break
        for k in range(int(nn * 0.5), nn + 1):
            if 2 * (1 - _binom_cdf(k - 1, nn, 0.5)) <= 0.05:
                hi = k; break
        if lo is None or hi is None:
            return float("nan")
        return _binom_cdf(lo, nn, p_exp) + (1 - _binom_cdf(hi - 1, nn, p_exp))
    # ⚠ 胜率 0% 或 100% 时 p(1-p)=0，正态近似失效 —— 用 Wilson 区间
    z = 1.96
    den = 1 + z**2/n
    ctr = (wr/100 + z**2/(2*n)) / den
    half = z*np.sqrt(wr/100*(1-wr/100)/n + z**2/(4*n**2)) / den
    lo_w, hi_w = (ctr-half)*100, (ctr+half)*100
    print("\n  ── 统计功效（能否区分「41.9%」与「50% 掷硬币」）──")
    print(f"  当前 n={n} · 胜率 {wr:.1f}% 的 95% Wilson 区间 [{lo_w:.1f}%, {hi_w:.1f}%]")
    if lo_w <= EXP["win_rate"] <= hi_w:
        print(f"  → 预期胜率 {EXP['win_rate']}% 落在区间内，【尚无矛盾证据】")
    else:
        print(f"  → 预期胜率 {EXP['win_rate']}% 落在区间外，与实测有偏离")
    if n < need:
        print(f"  → n 不足（达 80% 功效需约 {need} 笔），当前无法判定")
    print(f"  区分 41.9% 与 50%：")
    print(f"    · 约 149 笔 → 精确二项功效仅 {_exact_power(min(149, 400)):.1%}（≈掷硬币，不足以判定）")
    print(f"    · 达 80% 功效需约 {need} 笔【独立】交易（当前 n={n}，功效 {_exact_power(min(max(n,10),400)):.1%}）")
    print(f"    按 {EXP['trades_per_year']/12:.1f} 笔/月 → 约 {need/(EXP['trades_per_year']/12):.0f} 个月")
    print(f"  ⚠ 且「胜率是否低于 50%」本身【不是】趋势策略的检验对象 ——")
    print(f"    它的盈利来源是盈亏比（研究侧预期 3.12），应以【扣费后净增量】检验，")
    print(f"    并注意交易之间的相关性会进一步推高所需样本量。")

    # ── 结论 ──
    print("\n" + "=" * 96)
    print("  结论")
    print("=" * 96)
    if n < 30:
        print(f"  ⚠ n={n} 样本不足 —— 【不能】判断可用性，也不该据此报警或下结论。")
        print("  继续积累。当前只报告事实。")
        verdict = "insufficient"
    elif n < need:
        print(f"  🟡 n={n} 有初步迹象，但未达统计功效（需约 {need} 笔）。")
        print(f"  胜率 {wr:.1f}% · 盈亏比 {pf:.2f} · 期望/笔 {exp:+.2f}")
        print("  可以说「尚无矛盾证据」，不能说「已验证可用」。")
        verdict = "preliminary"
    else:
        print(f"  ✅ n={n} 达到统计功效，可下判断：")
        ok = (pf > 2.0) and (exp > 0)
        print(f"  胜率 {wr:.1f}%（预期 {EXP['win_rate']}%）· 盈亏比 {pf:.2f}（预期 {EXP['profit_factor']}）"
              f"· 期望/笔 {exp:+.2f}")
        print(f"  → {'与预期一致，机制可视为可用' if ok else '与预期不符，需要复核'}")
        verdict = "usable" if ok else "mismatch"

    if args.json:
        json.dump({"n_open": len(op), "n_closed": n, "win_rate": float(wr),
                   "profit_factor": float(pf), "expectancy_abs": float(exp),
                   "expectancy_pct": float(exp_pct), "se": float(se),
                   "needed": int(need), "verdict": verdict},
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n  已存 {args.json}")


if __name__ == "__main__":
    main()
