#!/usr/bin/env python3
"""
去偏置信号回测（修正「绝对阈值」这个病根）

发现：
    模型在每个训练窗口【内部】的排名是有效的（IC +0.22，
    窗口内分5层的多空价差 +2.651%，t=8.25）。
    但策略用的是绝对阈值 ±2%，而各窗口预测均值从 -24% 到 +42%
    （窗口间标准差 0.0976 ≫ 窗口内标准差 0.0253），
    导致「>+2%」在不同窗口里含义完全不同 →
    策略实际是在赌「模型当期整体看多/看空」，而不是利用排名能力。

修正：
    不再用绝对阈值，改用【滚动分位数】——预测值在过去 N 根 K 线中的百分位。
    这样天然消除了窗口间的均值/尺度差异。

回测设计（严格）：
    - 每 168 小时（7天）调仓一次，【不重叠】
    - 高百分位做多、低百分位做空
    - 计入双边手续费 0.10%
    - 分别统计多头腿、空头腿、多空合计

用法: python debiased_backtest.py --model mlp --pct 0.8 --lookback 720
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
MODELS = "user_data/models"
LABEL = 168

MODEL_DIR = {
    "mlp": "universe15-mlp-1h-7d",
    "xgb": "universe15-xgb-1h-7d",
    "lgb": "btc-eth-funding-1h-7d",
    "trf": "universe15-trf-1h-7d",
}


def build_panel(ident):
    syms = [os.path.basename(f).split("_")[0]
            for f in sorted(glob.glob(f"{PERP}/*-1h-futures.feather"))]
    cols_pred, cols_px = {}, {}
    for s in syms:
        px = pd.read_feather(f"{PERP}/{s}_USDT_USDT-1h-futures.feather").sort_values("date")
        px["date"] = pd.to_datetime(px["date"], utc=True).dt.tz_localize(None)
        px = px.set_index("date")["close"].astype(float)
        prs = []
        for f in sorted(glob.glob(f"{MODELS}/{ident}/backtesting_predictions/cb_{s.lower()}_*_prediction.feather")):
            d = pd.read_feather(f)
            d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
            prs.append(d[["date", "&-s_close", "do_predict"]])
        if not prs:
            continue
        pr = pd.concat(prs).drop_duplicates("date").set_index("date")
        j = pr.join(px.rename("px"), how="inner")
        cols_pred[s] = j["&-s_close"]
        cols_px[s] = j["px"]
    P = pd.DataFrame(cols_pred).sort_index()
    X = pd.DataFrame(cols_px).sort_index()
    idx = P.index.intersection(X.index)
    return P.loc[idx], X.loc[idx]


def run(P, X, pct, lookback, cost=0.0010, period=LABEL):
    # 滚动百分位（只用历史，无前视）
    rank = P.rolling(lookback, min_periods=lookback // 4).rank(pct=True)
    hi = rank >= pct
    lo = rank <= (1 - pct)

    # 调仓点：每 period 根 K 线，取整点避免重叠
    ts = list(X.index[lookback::period])
    recs = []
    for t in ts:
        if t not in X.index:
            continue
        nxt = t + pd.Timedelta(hours=period)
        fwd = X.index[X.index.searchsorted(nxt)] if X.index.searchsorted(nxt) < len(X.index) else None
        if fwd is None:
            continue
        r = X.loc[fwd] / X.loc[t] - 1          # 各币未来 7 天收益
        h = hi.loc[t]; l = lo.loc[t]
        if h.sum() == 0 or l.sum() == 0:
            continue
        long_ret = r[h].mean()
        short_ret = -r[l].mean()
        recs.append({
            "t": t, "n_long": int(h.sum()), "n_short": int(l.sum()),
            "long_gross": long_ret, "short_gross": short_ret,
            "ls_gross": (long_ret + short_ret) / 2,
            "long_net": long_ret - cost, "short_net": short_ret - cost,
            "ls_net": (long_ret + short_ret) / 2 - cost,
        })
    return pd.DataFrame(recs)


def stats(s, n_per_year):
    s = s.dropna()
    if len(s) < 5:
        return dict(n=len(s), mean=np.nan, t=np.nan, ann=np.nan, win=np.nan, sharpe=np.nan, mdd=np.nan)
    mean = s.mean()
    t = mean / (s.std() / np.sqrt(len(s))) if s.std() > 0 else np.nan
    eq = (1 + s).cumprod()
    mdd = ((eq / eq.cummax()) - 1).min()
    return dict(n=len(s), mean=mean, t=t, ann=(1 + mean) ** n_per_year - 1,
                win=(s > 0).mean(), sharpe=mean / s.std() * np.sqrt(n_per_year) if s.std() > 0 else np.nan,
                mdd=mdd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlp", choices=list(MODEL_DIR))
    ap.add_argument("--pct", type=float, default=0.8, help="分位阈值：≥该值为多，≤(1-pct)为空")
    ap.add_argument("--lookback", type=int, default=720, help="滚动分位回看根数")
    args = ap.parse_args()

    ident = MODEL_DIR[args.model]
    P, X = build_panel(ident)
    print(f"  模型 {args.model}（{ident}）")
    print(f"  面板: {P.shape[1]} 币 × {P.shape[0]} 根 K 线")
    print(f"  区间: {X.index[0]} → {X.index[-1]}")

    df = run(P, X, args.pct, args.lookback)
    if df.empty:
        print("  无有效调仓点")
        return
    NY = 365 * 24 / LABEL  # 每年调仓次数

    print()
    print("=" * 96)
    print(f"去偏置策略回测（滚动分位 {args.pct:.0%} / 回看 {args.lookback}h / 每 7 天调仓，不重叠）")
    print("=" * 96)
    print(f"  调仓次数 {len(df)}   平均持多 {df['n_long'].mean():.1f} 币 / 持空 {df['n_short'].mean():.1f} 币")
    print()
    print(f"  {'':<16}{'每期均值':>12}{'t值':>9}{'年化':>12}{'胜率':>9}{'Sharpe':>9}{'最大回撤':>11}")
    print("  " + "-" * 78)
    for key, name in [("long_gross", "多头腿(毛)"), ("short_gross", "空头腿(毛)"),
                      ("long_net", "多头腿(净)"), ("short_net", "空头腿(净)"),
                      ("ls_net", "多空合计(净)")]:
        st = stats(df[key], NY)
        print(f"  {name:<16}{st['mean']*100:>11.3f}%{st['t']:>9.2f}{st['ann']*100:>11.1f}%"
              f"{st['win']*100:>8.1f}%{st['sharpe']:>9.2f}{st['mdd']*100:>10.1f}%")

    print()
    print("=" * 96)
    print("分年拆解（判断是否只是「赌某个方向」）")
    print("=" * 96)
    df["year"] = df["t"].dt.year
    print(f"  {'年份':<8}{'次数':>6}{'多头(净)':>14}{'空头(净)':>14}{'多空合计(净)':>16}{'合计年化':>12}")
    print("  " + "-" * 70)
    for y, g in df.groupby("year"):
        l, s2, ls = g["long_net"].mean(), g["short_net"].mean(), g["ls_net"].mean()
        ny = len(g) / ((g["t"].iloc[-1] - g["t"].iloc[0]).days / 365) if len(g) > 1 else NY
        print(f"  {y:<8}{len(g):>6}{l*100:>13.3f}%{s2*100:>13.3f}%{ls*100:>15.3f}%{(1+ls)**ny*100-100:>11.1f}%")

    print()
    print("=" * 96)
    print("对照：市场基准（同期各币等权买入持有）")
    print("=" * 96)
    bench = (X.iloc[-1] / X.iloc[0] - 1).mean()
    days = (X.index[-1] - X.index[0]).days
    print(f"  等权买入持有: 累计 {bench*100:+.2f}%   年化 {((1+bench)**(365/days)-1)*100:+.2f}%")
    print(f"  → 多空策略若在【涨跌两个年份都为正】，才说明赚的是信号而不是方向。")


if __name__ == "__main__":
    main()
