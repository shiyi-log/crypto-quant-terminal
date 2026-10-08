#!/usr/bin/env python3
"""
「反着做」可行性检验

用户的假设：既然亏钱说明方向买错了，那反过来做（预测涨就做空）是不是就赚了？

要分清两种情况：
    A. 真·反向 alpha —— 模型系统性判反，反过来是稳定规律
    B. 多头偏置假象 —— 模型只会看涨（多148 vs 空78），而市场在跌，
       反过来做 = 做空 = 赚的是市场下跌的钱，换个行情就失效

判据：
    看【信号本身】的未来收益（不含止损路径依赖），并按年份拆开。
    如果 pred>+2% 的样本在未来确实下跌，且这个关系在涨跌不同的年份都成立 → A
    如果只在下跌年份成立 → B

用法: python inversion_test.py [--model mlp|xgb|lgb]
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

MODELS = "user_data/models"
PERP = "user_data/data/binance/futures"
LABEL = 168   # 7 天 × 24 小时
ENTRY_TH = 0.02

MODEL_DIR = {
    "mlp": "universe15-mlp-1h-7d",
    "xgb": "universe15-xgb-1h-7d",
    "lgb": "btc-eth-funding-1h-7d",
    "trf": "universe15-trf-1h-7d",
}


def load_prices(sym):
    p = f"{PERP}/{sym}_USDT_USDT-1h-futures.feather"
    if not os.path.exists(p):
        return None
    d = pd.read_feather(p).sort_values("date")
    d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
    return d.set_index("date")["close"].astype(float)


def load_predictions(ident, sym):
    out = []
    for f in sorted(glob.glob(f"{MODELS}/{ident}/backtesting_predictions/cb_{sym.lower()}_*_prediction.feather")):
        try:
            d = pd.read_feather(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        # 记录每个预测文件 = 一个训练窗口，用于分窗口计算 IC
        d["_window"] = os.path.basename(f)
        out.append(d[["date", "&-s_close", "do_predict", "_window"]])
    return pd.concat(out).drop_duplicates("date") if out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlp", choices=list(MODEL_DIR))
    args = ap.parse_args()
    ident = MODEL_DIR[args.model]

    syms = [os.path.basename(f).split("_")[0]
            for f in sorted(glob.glob(f"{PERP}/*-1h-futures.feather"))]
    rows = []
    for s in syms:
        px = load_prices(s)
        pr = load_predictions(ident, s)
        if px is None or pr is None or pr.empty:
            continue
        d = pr.set_index("date").join(px.rename("close"), how="inner")
        if d.empty:
            continue
        # 与策略标签一致的前瞻收益：未来 168 根 K 线的均价 / 当前价 − 1
        c = d["close"]
        d["fwd"] = c.shift(-LABEL).rolling(LABEL).mean().shift(0) / c - 1
        d["sym"] = s
        d["year"] = d.index.year
        rows.append(d[["&-s_close", "do_predict", "fwd", "sym", "year", "_window"]])

    df = pd.concat(rows).dropna(subset=["&-s_close", "fwd"])
    df = df[df["do_predict"] == 1]
    print(f"  模型 {args.model}（{ident}）")
    print(f"  有效样本 {len(df)} 条（do_predict==1 且有价格）")
    print(f"  预测值范围 [{df['&-s_close'].min():.4f}, {df['&-s_close'].max():.4f}]  "
          f"均值 {df['&-s_close'].mean():+.4f}")

    print()
    print("=" * 96)
    print("检验 1：全样本 —— 不同预测区间的【实际未来收益】")
    print("=" * 96)
    df["bucket"] = pd.cut(df["&-s_close"],
                          [-np.inf, -0.05, -0.02, 0.02, 0.05, np.inf],
                          labels=["<-5%", "-5%~-2%", "±2%内", "+2%~+5%", ">+5%"])
    print(f"  {'预测区间':<12}{'样本数':>8}{'实际未来收益(均值)':>20}{'中位数':>12}{'胜率':>10}")
    print("  " + "-" * 62)
    for b, g in df.groupby("bucket", observed=True):
        print(f"  {str(b):<12}{len(g):>8}{g['fwd'].mean()*100:>19.3f}%"
              f"{g['fwd'].median()*100:>11.3f}%{(g['fwd']>0).mean()*100:>9.1f}%")
    print("  " + "-" * 62)

    long_side = df[df["&-s_close"] > ENTRY_TH]["fwd"]
    short_side = df[df["&-s_close"] < -ENTRY_TH]["fwd"]
    print(f"  策略做多的样本(pred>+2%): {len(long_side)} 条, 实际未来收益 {long_side.mean()*100:+.3f}%")
    print(f"  策略做空的样本(pred<-2%): {len(short_side)} 条, 实际未来收益 {short_side.mean()*100:+.3f}%")
    inv = -long_side.mean() + short_side.mean()
    print()
    print(f"  → 原策略（多高预测/空低预测）单边期望: {(long_side.mean()-short_side.mean())/2*100:+.3f}%")
    print(f"  → 【反着做】单边期望:                  {(short_side.mean()-long_side.mean())/2*100:+.3f}%")
    print(f"     扣双边手续费 0.10% 后:              {((short_side.mean()-long_side.mean())/2-0.001)*100:+.3f}%")

    print()
    print("=" * 96)
    print("检验 2：分窗口 IC（模型每个训练窗口重训，跨窗口不可比，必须分窗口算）")
    print("=" * 96)
    ics, neg = [], 0
    for w, g in df.groupby("_window"):
        if len(g) > 50 and g["&-s_close"].std() > 0:
            ic = spearmanr(g["&-s_close"], g["fwd"]).statistic
            if not np.isnan(ic):
                ics.append(ic)
                neg += ic < 0
    ics = np.array(ics)
    print(f"  窗口数 {len(ics)}")
    print(f"  IC 均值   : {ics.mean():+.4f}   （正=模型方向对，负=系统性判反）")
    print(f"  IC 中位数 : {np.median(ics):+.4f}")
    print(f"  IC<0 占比 : {neg/len(ics)*100:.1f}%")
    print(f"  IC>0.05 占比: {(ics>0.05).mean()*100:.1f}%   IC<-0.05 占比: {(ics<-0.05).mean()*100:.1f}%")

    print()
    print("=" * 96)
    print("检验 3【决定性】：按年份拆开 —— 反向是「真规律」还是「赌市场下跌」？")
    print("=" * 96)
    print(f"  {'年份':<8}{'样本':>8}{'pred>+2%的实际收益':>22}{'pred<-2%的实际收益':>22}{'反向单边期望':>16}")
    print("  " + "-" * 76)
    for y, g in df.groupby("year"):
        hi = g[g["&-s_close"] > ENTRY_TH]["fwd"]
        lo = g[g["&-s_close"] < -ENTRY_TH]["fwd"]
        if len(hi) < 10 or len(lo) < 10:
            print(f"  {y:<8}{len(g):>8}{'样本不足':>22}{'':>22}{'':>16}")
            continue
        invy = (lo.mean() - hi.mean()) / 2
        print(f"  {y:<8}{len(g):>8}{hi.mean()*100:>21.3f}%{lo.mean()*100:>21.3f}%{invy*100:>15.3f}%")
    print("  " + "-" * 76)

    print()
    print("=" * 96)
    print("检验 4：与「无条件做空」对比 —— 如果反向收益 ≈ 无条件做空，说明只是方向性押注")
    print("=" * 96)
    base = df["fwd"].mean()
    print(f"  全样本无条件未来收益（= 长期持有）: {base*100:+.3f}%")
    print(f"  无条件做空的单边期望            : {-base/2*100:+.3f}%")
    print(f"  反向策略的单边期望              : {(short_side.mean()-long_side.mean())/2*100:+.3f}%")
    diff = (short_side.mean() - long_side.mean()) / 2 + base / 2
    print(f"  反向 vs 无条件做空 的差异       : {diff*100:+.3f}%")
    print()
    if abs(diff) < 0.001:
        print("  → ⚠ 反向策略几乎等同于「无条件做空」：这不是规律，是方向性押注")
    elif diff > 0:
        print("  → ✅ 反向策略比无条件做空更好：存在与市场方向无关的增量信息")
    else:
        print("  → ❌ 反向策略不如无条件做空")


if __name__ == "__main__":
    main()
