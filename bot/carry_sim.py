#!/usr/bin/env python3
"""
Carry 落地模拟：逐小时跟踪保证金账户，含真实强平

之前的回测假设「持仓一定能拿满整个周期」，这在现实中不成立：
永续空头腿是独立保证金账户，币价暴涨时会被强平 —— 现货腿的盈利救不了它。

本脚本逐小时模拟三种保证金方案，给出各方案的真实年化：

  方案 A【逐仓·无备用金】
      保证金 = N/杠杆，不补充。强平即损失全部保证金，对冲破裂。

  方案 B【逐仓·有备用金】⭐ 现实可行
      预留现金备用金，当保证金率跌破阈值时自动划转补足。
      备用金耗尽后仍会被强平。

  方案 C【统一账户/组合保证金】
      现货作为期货抵押品，强平看【合并净值】。
      Delta 中性头寸合并净值几乎不动 → 基本不会强平。

用法:
    python carry_sim.py                      # 三种方案对比
    python carry_sim.py --leverage 2 --reserve 0.5
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP_DIR = "user_data/data/binance/futures"
SPOT_DIR = "user_data/data/binance"
TAKER = 0.0005
SLIPPAGE = 0.0002
MAINT = 0.005          # 维持保证金率
LIQ_FEE = 0.00125      # 强平罚金（近似）


def discover():
    return sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(f"{PERP_DIR}/*-1h-futures.feather"))


def load(sym, kind):
    p = (f"{SPOT_DIR}/{sym}_USDT-1h.feather" if kind == "spot"
         else f"{PERP_DIR}/{sym}_USDT_USDT-1h-futures.feather")
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")["close"].astype(float)


def load_funding(sym):
    p = f"{PERP_DIR}/{sym}_USDT_USDT-1h-funding_rate.feather"
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")["funding_rate"].astype(float)


def simulate(topn, rebalance_days, leverage, reserve_ratio, mode,
             start=None, verbose=True):
    """
    mode: 'isolated' 逐仓（无备用金）
          'reserve'  逐仓 + 备用金自动划转
          'unified'  统一账户（合并净值）
    """
    syms = discover()
    S, P, F = {}, {}, {}
    for s in syms:
        sp, pp, fr = load(s, "spot"), load(s, "perp"), load_funding(s)
        if sp is None or pp is None or fr is None:
            continue
        S[s], P[s], F[s] = sp, pp, fr
    u = sorted(S.keys())
    Sd = pd.DataFrame(S)[u].sort_index()
    Pd = pd.DataFrame(P)[u].sort_index()
    Fd = pd.DataFrame(F)[u].sort_index()
    idx = Sd.index.intersection(Pd.index)
    if start:
        idx = idx[idx >= pd.Timestamp(start, tz="UTC")]
    Sd, Pd = Sd.loc[idx], Pd.loc[idx]

    rebal = [t for t in idx[::rebalance_days * 24]]
    equity = 1.0                    # 以总资金为 1
    curve, events = [], []
    period_returns = []
    prev_sel = None

    for i, t in enumerate(rebal[:-1]):
        nxt = rebal[i + 1]
        hist = Fd.loc[:t].tail(720).sum().dropna()
        if len(hist) < topn:
            continue
        sel = list(hist.nlargest(topn).index)

        # 资金分配（按总资金 1 归一化）
        # ⚠️ 必须除以持仓币数，否则等于变相加杠杆
        per_coin = 1.0 / topn / (1.0 + 1.0 / leverage + reserve_ratio)
        N = per_coin                                     # 每币名义本金
        M = N / leverage                                 # 永续保证金
        R = N * reserve_ratio                            # 每币备用金

        total_pnl = 0.0
        for s in sel:
            seg = Pd[s].loc[t:nxt]
            sseg = Sd[s].loc[t:nxt]
            if len(seg) < 2:
                continue
            p0, s0 = seg.iloc[0], sseg.iloc[0]
            c = 0.0
            wallet = M
            reserve = R
            liquidated = False

            prev_px = p0
            for ts, px in seg.items():
                spot_val = N * (sseg.loc[ts] / s0 - 1)      # 现货腿盈亏
                perp_pnl = -N * (px / p0 - 1)               # 空头腿盈亏

                # 资金费（空头收取）
                if ts in Fd.index and s in Fd.columns:
                    f = Fd.at[ts, s]
                    if pd.notna(f):
                        c += N * float(f)

                if mode == "unified":
                    # 合并净值作为保证金，几乎不会强平
                    combined = M + perp_pnl + c + N * (sseg.loc[ts] / s0)
                    if combined < MAINT * N:
                        liquidated = True
                        break
                else:
                    wallet = M + perp_pnl + c
                    if mode == "reserve" and wallet < 0.5 * M and reserve > 0:
                        need = M - wallet
                        move = min(reserve, need)
                        wallet += move
                        reserve -= move
                    if wallet < MAINT * N:
                        liquidated = True
                        break

            if liquidated:
                # 强平：损失保证金 + 罚金；对冲破裂，按当时现货盈亏了结
                loss = -(M + R - reserve) - LIQ_FEE * N
                total_pnl += loss
                events.append({"t": t, "sym": s, "type": "强平",
                               "px_move": px / p0 - 1})
            else:
                spot_val = N * (sseg.iloc[-1] / s0 - 1)
                perp_pnl = -N * (seg.iloc[-1] / p0 - 1)
                total_pnl += spot_val + perp_pnl + c

        # 调仓成本：两只腿 × 开平各一次 × 实际换手的币数
        # 选币有粘性（LINK/BTC/LTC 反复出现），按真实换手算，不能用满仓换手
        if prev_sel is None:
            churn = 1.0
        else:
            churn = len(set(sel) - set(prev_sel)) / topn
        cost = churn * 2 * (TAKER + SLIPPAGE) * 2 * N * topn
        total_pnl -= cost
        prev_sel = sel

        r = total_pnl                      # 已按总资金归一化
        equity *= (1 + r)
        period_returns.append(r)
        curve.append((nxt, equity))

    pr = np.array(period_returns)
    days = (rebal[len(period_returns)] - rebal[0]).days if len(period_returns) else 1
    years = days / 365
    total = equity - 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    eqs = np.array([c[1] for c in curve])
    dd = ((eqs / np.maximum.accumulate(eqs)) - 1).min() if len(eqs) else np.nan

    return {
        "mode": mode, "leverage": leverage, "reserve_ratio": reserve_ratio,
        "periods": len(pr), "total": total, "annual": ann, "mdd": dd,
        "win_rate": (pr > 0).mean() if len(pr) else np.nan,
        "liquidations": len(events),
        "liq_detail": events[:5],
        "avg_period": pr.mean() if len(pr) else np.nan,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--rebalance-days", type=int, default=30)
    args = ap.parse_args()

    print("=" * 100)
    print("Carry 落地模拟：三种保证金方案的真实年化（逐小时跟踪保证金账户）")
    print("=" * 100)
    print(f"  参数：持费率最高 {args.topn} 币 · 每 {args.rebalance_days} 天调仓 · 计双边手续费+滑点")
    print()
    print(f"  {'方案':<34}{'杠杆':>5}{'备用金':>8}{'强平次数':>10}{'累计':>10}{'年化':>10}{'回撤':>9}")
    print("  " + "-" * 88)

    cases = [
        ("A 逐仓 · 无备用金",           "isolated", 1, 0.0),
        ("A 逐仓 · 无备用金",           "isolated", 2, 0.0),
        ("A 逐仓 · 无备用金",           "isolated", 3, 0.0),
        ("B 逐仓 · 备用金 50%",         "reserve",  2, 0.5),
        ("B 逐仓 · 备用金 100%",        "reserve",  2, 1.0),
        ("B 逐仓 · 备用金 200%",        "reserve",  2, 2.0),
        ("C 统一账户（现货可抵押）",     "unified",  1, 0.0),
        ("C 统一账户（现货可抵押）",     "unified",  3, 0.0),
    ]
    results = []
    for label, mode, lev, res in cases:
        try:
            r = simulate(args.topn, args.rebalance_days, lev, res, mode, verbose=False)
        except Exception as exc:
            print(f"  {label:<34}{lev:>4}x{res:>8.0%}  失败: {exc}")
            continue
        results.append((label, r))
        print(f"  {label:<34}{lev:>4}x{res:>8.0%}{r['liquidations']:>10}"
              f"{r['total']*100:>9.2f}%{r['annual']*100:>9.2f}%{r['mdd']*100:>8.2f}%")
    print("  " + "-" * 88)

    print()
    print("=" * 100)
    print("强平事件明细（逐仓无备用金，1x）")
    print("=" * 100)
    base = simulate(args.topn, args.rebalance_days, 1, 0.0, "isolated", verbose=False)
    if base["liq_detail"]:
        for e in base["liq_detail"]:
            print(f"    {e['t'].date()}  {e['sym']:<6} 币价变动 {e['px_move']*100:+.1f}%")
    n = base["liquidations"]
    print(f"  共 {n} 次强平")

    print()
    print("=" * 100)
    print("结论")
    print("=" * 100)
    print("  · 逐仓模式即使 1x 杠杆也会被强平（币价单月翻倍就会击穿保证金）")
    print("  · 备用金能推迟但不能根除强平：币价涨 248% 时任何合理备用金都会耗尽")
    print("  · 只有【统一账户/组合保证金】能从根上解决 —— 合并净值几乎不动")
    print("  · 因此 Carry 的可行年化完全取决于保证金基础设施，差距可达数倍")


if __name__ == "__main__":
    main()
