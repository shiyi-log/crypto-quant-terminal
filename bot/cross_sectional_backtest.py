#!/usr/bin/env python3
"""
横截面市场中性回测器

思路：FreqAI 是「每个币独立建模」，但模型的预测力体现在**币种之间的相对强弱排序**，
而不是单个币的涨跌方向。因此正确用法是：

    每个调仓时点，按预测值给全池币种排序 → 做多前 K 名 / 做空后 K 名，等名义本金

这样剥离了市场 beta，只赚排序的钱。

关键成本处理：
1. 手续费：每腿每次调仓双边 0.05% × 2
2. 资金费率：多空等名义本金时**大部分天然对冲**（多头付、空头收），
   但两篮子平均资金费率不同，残差必须计入 —— 这是市场中性策略相对方向性策略的一大优势

用法：
    python cross_sectional_backtest.py --identifier btc-eth-funding-1h-7d \
        --label-hours 168 --hold-hours 168 --topk 3
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

FEES = 0.0005          # 单边 taker
DATA_DIR = "user_data/data/binance/futures"
MODELS_DIR = "user_data/models"


def load_predictions(identifier: str, signal: str = "rollz", rollz_window: int = 720) -> pd.DataFrame:
    """
    汇总全池预测：列为币种，索引为时间。

    ⚠️ 标准化方式决定信号正负，必须只用「当时可知」的信息：

    signal='rollz'（默认，推荐）：对每个币，用**滚动过去 rollz_window 小时**
      的预测均值/标准差做标准化。只用历史，无前视偏差，且自适应模型尺度漂移。

    signal='windowz'：用预测文件自带的 &-s_close_mean / &-s_close_std（训练期
      目标统计量）标准化。合法（建模时已知），但不随模型漂移自适应。

    signal='raw'：原始预测值。各币模型尺度差异可达百倍，跨币种排序无效。

    ⚠️ 已知陷阱：若改用「该回测窗口内预测的经验均值/标准差」标准化，会得到
      显著为正的结果（14币 IC +0.19、价差 +9.2%），但那是**前视偏差**——
      交易时该窗口的预测均值尚不可知。窗口内 IC 对标准化方式本应完全不变
      （仿射不变性），两种方式窗口内 IC 实测均为 +0.126/+0.129/+0.168/+0.228，
      差异全部来自跨窗口合并，正负由窗口间偏置决定。
    """
    files = sorted(glob.glob(f"{MODELS_DIR}/{identifier}/backtesting_predictions/*_prediction.feather"))
    if not files:
        raise SystemExit(f"未找到预测文件：{MODELS_DIR}/{identifier}/backtesting_predictions/")

    frames = []
    for f in files:
        base = os.path.basename(f)
        parts = base.split("_")
        if len(parts) < 3:
            continue
        sym = parts[1].upper()
        raw = pd.read_feather(f)
        d = pd.DataFrame(
            {
                "date": raw["date"],
                "pair": sym,
                "do_predict": raw["do_predict"],
                "raw": raw["&-s_close"],
            }
        )
        if signal == "windowz":
            sd = raw["&-s_close_std"].abs().to_numpy()
            d["sig"] = (raw["&-s_close"].to_numpy() - raw["&-s_close_mean"].to_numpy()) / (sd + 1e-12)
        else:
            d["sig"] = raw["&-s_close"].to_numpy()
        frames.append(d)

    allp = pd.concat(frames, ignore_index=True)
    pred = allp.pivot_table(index="date", columns="pair", values="sig", aggfunc="last")
    mask = allp.pivot_table(index="date", columns="pair", values="do_predict", aggfunc="last")
    pred = pred.where(mask == 1)

    if signal == "rollz":
        # 补齐到完整小时网格，保证滚动窗口是真实时间长度
        full = pd.date_range(pred.index.min(), pred.index.max(), freq="h")
        pred = pred.reindex(full)
        minp = max(48, rollz_window // 8)
        mu = pred.rolling(rollz_window, min_periods=minp).mean()
        sd = pred.rolling(rollz_window, min_periods=minp).std()
        pred = (pred - mu) / (sd + 1e-12)

    return pred


def load_prices(pairs) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (收盘价, 资金费率) 两张宽表。"""
    close, funding = {}, {}
    for p in pairs:
        f = f"{DATA_DIR}/{p}_USDT_USDT-1h-futures.feather"
        if not os.path.exists(f):
            continue
        d = pd.read_feather(f).sort_values("date")
        close[p] = d.set_index("date")["close"]
        ff = f"{DATA_DIR}/{p}_USDT_USDT-1h-funding_rate.feather"
        if os.path.exists(ff):
            fr = pd.read_feather(ff).sort_values("date")
            funding[p] = fr.set_index("date")["funding_rate"]
    return pd.DataFrame(close), pd.DataFrame(funding)


def run(pred: pd.DataFrame, close: pd.DataFrame, funding: pd.DataFrame,
        label_hours: int, hold_hours: int, topk: int, use_zscore: bool,
        cost_mult: float = 1.0):
    """主回测循环，返回逐期明细 DataFrame。"""
    times = sorted(pred.index)
    records = []
    # 调仓时点：从预测起点开始，每 hold_hours 一次
    t0 = times[label_hours]
    rebal = [t for t in times if t >= t0 and (t - t0).total_seconds() % (hold_hours * 3600) == 0]

    for t in rebal:
        t_end = t + pd.Timedelta(hours=hold_hours)
        if t_end > close.index[-1]:
            break
        p = pred.loc[t].dropna()
        # 只保留在 t 与 t_end 都有价格的币
        valid = [c for c in p.index if c in close.columns
                 and not np.isnan(close.at[t, c]) and not np.isnan(close.at[t_end, c])]
        if len(valid) < max(4, topk * 2):
            continue
        s = p[valid].sort_values(ascending=False)
        longs, shorts = list(s.index[:topk]), list(s.index[-topk:])

        # 持有期收益
        def leg_ret(pl):
            return np.mean([close.at[t_end, c] / close.at[t, c] - 1 for c in pl])
        rl, rs = leg_ret(longs), leg_ret(shorts)
        gross = rl - rs

        # 手续费：两腿各开+平，换手按 100% 计（保守）
        fee = 4 * FEES * cost_mult

        # 资金费率：持有期内做多要付、做空要收
        fund_cost = 0.0
        if len(funding) and t in funding.index:
            for c in longs:
                if c in funding.columns:
                    seg = funding.loc[(funding.index > t) & (funding.index <= t_end), c].dropna()
                    fund_cost += seg.sum()          # 多头付出正费率
            for c in shorts:
                if c in funding.columns:
                    seg = funding.loc[(funding.index > t) & (funding.index <= t_end), c].dropna()
                    fund_cost -= seg.sum()          # 空头收取正费率
            fund_cost /= topk

        records.append(dict(time=t, gross=gross, fee=fee, funding=fund_cost,
                            net=gross - fee - fund_cost,
                            long_ret=rl, short_ret=rs,
                            longs=",".join(longs), shorts=",".join(shorts)))
    return pd.DataFrame(records)


def summarize(df: pd.DataFrame, label: str, hold_hours: int, periods_per_year: float):
    if df.empty:
        print(f"  {label}: 无有效调仓记录"); return
    net = df["net"].values
    cum = np.cumprod(1 + net)
    peak = np.maximum.accumulate(cum)
    dd = (cum / peak - 1).min()
    ann = (1 + net.mean()) ** periods_per_year - 1
    sharpe = net.mean() / (net.std() + 1e-12) * np.sqrt(periods_per_year)
    tstat = net.mean() / (net.std() / np.sqrt(len(net)) + 1e-12)
    # bootstrap CI
    rng = np.random.default_rng(3)
    bs = np.array([rng.choice(net, size=len(net), replace=True).mean() for _ in range(5000)])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    winr = (net > 0).mean() * 100
    print(f"  ── {label}")
    print(f"     调仓次数   : {len(df)}")
    print(f"     毛收益/期  : {df['gross'].mean()*100:+.3f}%")
    print(f"     手续费/期  : {df['fee'].mean()*100:.3f}%   资金费率/期: {df['funding'].mean()*100:+.3f}%")
    print(f"     净收益/期  : {net.mean()*100:+.3f}%   (中位 {np.median(net)*100:+.3f}%)")
    print(f"     95% CI     : [{lo*100:+.3f}%, {hi*100:+.3f}%]  {'✅ 不含0' if lo>0 else '❌ 含0'}")
    print(f"     t 统计量   : {tstat:+.2f}")
    print(f"     胜率       : {winr:.1f}%")
    print(f"     最大回撤   : {dd*100:.2f}%")
    print(f"     年化(折算) : {ann*100:+.1f}%    Sharpe {sharpe:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--identifier", required=True)
    ap.add_argument("--label-hours", type=int, default=168)
    ap.add_argument("--hold-hours", type=int, default=168)
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="手续费倍数，用于压力测试")
    ap.add_argument(
        "--signal",
        choices=["rollz", "windowz", "raw"],
        default="rollz",
        help="rollz=滚动历史标准化(推荐,无前视)；windowz=训练期统计量；raw=原始值",
    )
    ap.add_argument("--rollz-window", type=int, default=720, help="rollz 滚动窗口(小时), 默认 720=30天")
    args = ap.parse_args()

    pred = load_predictions(args.identifier, args.signal, args.rollz_window)
    close, funding = load_prices(list(pred.columns))
    common = [c for c in pred.columns if c in close.columns]
    pred = pred[common]
    sig_label = {
        "rollz": f"滚动历史 z-score（过去 {args.rollz_window} 小时，无前视偏差）",
        "windowz": "训练期统计量 z-score（&-s_close_mean/std）",
        "raw": "原始预测值（有严重币种偏置，仅对照）",
    }[args.signal]
    print("=" * 84)
    print(f"横截面市场中性回测  |  币种池 {len(common)} 个  |  调仓每 {args.hold_hours}h  |  多/空各 {args.topk} 个")
    print(f"  币种: {', '.join(common)}")
    print(f"  排序信号: {sig_label}")
    print("=" * 84)

    df = run(pred, close, funding, args.label_hours, args.hold_hours,
             args.topk, use_zscore=False, cost_mult=args.cost_mult)
    ppy = 365 * 24 / args.hold_hours
    summarize(df, "全区间", args.hold_hours, ppy)

    if not df.empty:
        print("\n  ── 分段稳健性（按季度）")
        d = df.copy()
        d["q"] = pd.PeriodIndex(pd.to_datetime(d["time"]), freq="Q").astype(str)
        for q, g in d.groupby("q"):
            net = g["net"].values
            rng = np.random.default_rng(5)
            bs = np.array([rng.choice(net, size=len(net), replace=True).mean() for _ in range(3000)]) if len(net) > 2 else np.array([net.mean()])
            lo, hi = np.percentile(bs, [2.5, 97.5])
            print(f"     {q}  n={len(g):3d}  毛 {g['gross'].mean()*100:+6.3f}%  "
                  f"净 {net.mean()*100:+6.3f}%  CI[{lo*100:+.2f}%,{hi*100:+.2f}%] "
                  f"{'✅' if lo>0 else ('❌' if hi<0 else '⚠')}")

        print("\n  ── 手续费压力测试（净收益/期）")
        for m, tag in [(1.0, "taker×1"), (1.5, "taker×1.5"), (2.0, "taker×2"), (0.4, "maker(0.02%)")]:
            d2 = run(pred, close, funding, args.label_hours, args.hold_hours,
                     args.topk, use_zscore=False, cost_mult=m)
            if not d2.empty:
                print(f"     {tag:14s} 净 {d2['net'].mean()*100:+.3f}%/期")

        out = f"user_data/cross_sectional_{args.identifier}_k{args.topk}_h{args.hold_hours}.csv"
        df.to_csv(out, index=False)
        print(f"\n  明细已保存: {out}")


if __name__ == "__main__":
    main()
