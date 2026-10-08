#!/usr/bin/env python3
"""
高频元标记实验 —— BTC/ETH · 5m（第 16 轮）

目标（对应 objective ①②④⑤）：
    ① 元标记：Donchian 突破定方向，ML 判断【该不该做】
    ② 用【实测成本】而非假设成本（第 15 轮实测 6.8bp，回测一直用 4bp）
    ④ 系统性迭代：逻辑回归基线 → LightGBM → LSTM → 集成
    ⑤ 样本外验证 + 防四项假象

为什么高频在统计上可行（第 14 轮实测）：
    日线 BTC/ETH 20日通道   独立趋势 129    最小可检测 IC 0.2465  ← 不可行
    5m  BTC/ETH 8h 通道     独立趋势 3,633  最小可检测 IC 0.0465  ← 可行

成本（第 15 轮实测，不是假设）：
    maker 往返        4.00 bp
    平均半价差        0.66 bp
    337U 下单冲击     2.14 bp
    ────────────────────────────
    合计              6.80 bp    ← 本实验用这个

纪律：
    · 特征全部 shift(1) 或更早，无前视
    · 走查（扩展窗口）+ 净化（持有期内样本从训练集剔除，避免标签重叠）
    · 报【多窗口宽度 t 值】与【多种子均值】，不报单次运行
    · 不用「正窗口占比」（第 11/12 轮已证伪：15 个种子最高 76%）
    · 成本必须扣

用法:
    python hft_meta.py --tf 5m --entry 96 --model lgbm
    python hft_meta.py --tf 5m --entry 96 --model lstm --seeds 3
    python hft_meta.py --tf 5m --entry 96 --all
"""

import argparse
import json
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import hft_lab as H

from ml_regime import ic_t

# ── 实测成本（第 15 轮 live_exec_monitor 实盘盘口） ──
COST_MEASURED = 6.8 / 10_000      # 6.8 bp
COST_ASSUMED = 4.0 / 10_000       # 旧的假设值（对照用）

WALK_START = "2024-07-01"
STEP_MONTHS = 3


# ══════════════════ 数据 ══════════════════

def build(tf="5m", entry=96, coins=("BTC", "ETH")):
    """
    构造 (特征, 标签, 元数据)。
    标签：按 Donchian 规则交易，扣【实测成本】后是否盈利。
    特征：价格量能 + 订单流（taker 买卖量）+ 微结构。
    """
    # 价格数据（带订单流字段）
    dfs = {}
    for c in coins:
        f = f"user_data/data/orderflow/{c}_{tf}_orderflow.feather"
        if not os.path.exists(f):
            raise FileNotFoundError(f"缺少 {f}，先跑 fetch_orderflow.py")
        d = pd.read_feather(f).set_index("date").sort_index()
        dfs[c] = d

    ev = H.hf_events({c: d[["open", "high", "low", "close", "volume"]]
                      for c, d in dfs.items()}, entry, entry)
    print(f"  事件 {len(ev):,}")

    rows = []
    for c, d in dfs.items():
        cv = d["close"].values
        opx = d["open"].values
        lx = d["close"].rolling(entry).min().shift(1).values
        hx = d["close"].rolling(entry).max().shift(1).values
        idx = {t: i for i, t in enumerate(d.index)}
        for r in ev[ev["coin"] == c].itertuples():
            i = idx.get(r.date)
            if i is None or i + 1 >= len(cv):
                continue
            e = opx[i + 1]
            if not np.isfinite(e) or e <= 0:
                continue
            j = i + 1
            while j < len(cv):
                if r.side > 0 and np.isfinite(lx[j]) and cv[j] < lx[j]:
                    break
                if r.side < 0 and np.isfinite(hx[j]) and cv[j] > hx[j]:
                    break
                j += 1
            j = min(j + 1, len(cv) - 1)
            ret = (opx[j] / e - 1) * r.side
            rows.append({"date": r.date, "coin": c, "side": r.side,
                         "ret": ret, "hold": j - (i + 1), "entry_i": i})
    y = pd.DataFrame(rows)
    print(f"  样本 {len(y):,} · 平均持仓 {y['hold'].mean():.0f} 根")
    return dfs, y


def features(d):
    """价格量能 + 订单流 + 微结构，全部无前视"""
    c, h, l, o, v = d["close"], d["high"], d["low"], d["open"], d["volume"]
    f = pd.DataFrame(index=d.index)
    rng = (h - l).replace(0, np.nan)
    f["body"] = (c - o).abs() / rng
    f["wick_asym"] = ((h - np.maximum(c, o)) - (np.minimum(c, o) - l)) / rng
    for n in [1, 3, 6, 12, 24, 48, 96, 288]:
        f[f"ret{n}"] = c.pct_change(n)
        f[f"vol{n}"] = c.pct_change().rolling(n).std()
    for n in [48, 96, 288]:
        hh = c.rolling(n).max().shift(1)
        ll = c.rolling(n).min().shift(1)
        span = (hh - ll).replace(0, np.nan)
        f[f"chan{n}"] = (c - ll) / span
        f[f"brkU{n}"] = (c - hh) / span
        f[f"brkD{n}"] = (ll - c) / span
    f["eff12"] = (c - c.shift(12)).abs() / c.diff().abs().rolling(12).sum().replace(0, np.nan)
    f["eff48"] = (c - c.shift(48)).abs() / c.diff().abs().rolling(48).sum().replace(0, np.nan)
    f["volz"] = (v - v.rolling(96).mean()) / v.rolling(96).std().replace(0, np.nan)
    f["range_pct"] = rng / c
    f["hour"] = d.index.hour
    f["dow"] = d.index.dayofweek

    # ── 订单流（用户要的"买卖盘订单量"） ──
    if "flow_imbal" in d.columns:
        fi = d["flow_imbal"]
        f["flow_now"] = fi
        for n in [6, 12, 48, 96]:
            f[f"flow_ma{n}"] = fi.rolling(n).mean()
            f[f"flow_sum{n}"] = fi.rolling(n).sum()
        f["flow_z"] = (fi - fi.rolling(96).mean()) / fi.rolling(96).std().replace(0, np.nan)
        f["flow_chg"] = fi.diff()
        f["flow_rev"] = -fi                       # 反转方向（实测相关性为负）
        br = d["buy_ratio"]
        f["buy_ratio_ma48"] = br.rolling(48).mean()
        f["flow_vs_ma"] = fi - fi.rolling(48).mean()
        f["nt"] = d["num_trades"]
        f["nt_z"] = (d["num_trades"] - d["num_trades"].rolling(96).mean()) / \
            d["num_trades"].rolling(96).std().replace(0, np.nan)
        f["ats"] = d["avg_trade_sz"]
        f["ats_z"] = (d["avg_trade_sz"] - d["avg_trade_sz"].rolling(96).mean()) / \
            d["avg_trade_sz"].rolling(96).std().replace(0, np.nan)
        f["qv_z"] = (d["quote_volume"] - d["quote_volume"].rolling(96).mean()) / \
            d["quote_volume"].rolling(96).std().replace(0, np.nan)
        # 价格与订单流的背离（价涨但买盘减弱 → 反转信号）
        f["px_flow_div"] = c.pct_change(12) - fi.rolling(12).mean()
    return f


# ══════════════════ 模型 ══════════════════

def fit_logit(Xtr, ytr, Xte):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(Xtr)
    m = LogisticRegression(max_iter=500, C=0.1).fit(sc.transform(Xtr), ytr)
    return m.predict_proba(sc.transform(Xte))[:, 1]


def fit_lgbm(Xtr, ytr, Xte, seed=42):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=150, learning_rate=0.05, num_leaves=15,
                           min_child_samples=100, subsample=0.8,
                           colsample_bytree=0.8, random_state=seed,
                           n_jobs=1, verbose=-1)
    m.fit(Xtr, ytr)
    return m.predict_proba(Xte)[:, 1]


def fit_lstm(Xtr, ytr, Xte, seed=42, epochs=6):
    import torch
    import torch.nn as nn
    torch.set_num_threads(1)
    dev = "mps" if torch.backends.mps.is_available() else "cpu"

    class Net(nn.Module):
        def __init__(self, nf):
            super().__init__()
            self.l = nn.LSTM(nf, 48, batch_first=True)
            self.d = nn.Dropout(0.3)
            self.o = nn.Linear(48, 1)

        def forward(self, x):
            h, _ = self.l(x)
            return self.o(self.d(h[:, -1])).squeeze(-1)

    torch.manual_seed(seed)
    net = Net(Xtr.shape[2]).to(dev)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    lossf = nn.BCEWithLogitsLoss()
    Xt = torch.tensor(Xtr, dtype=torch.float32).to(dev)
    yt = torch.tensor(ytr, dtype=torch.float32).to(dev)
    n = len(Xt)
    bs = 512
    net.train()
    for ep in range(epochs):
        perm = torch.randperm(n)
        for i in range(0, n, bs):
            b = perm[i:i + bs]
            opt.zero_grad()
            loss = lossf(net(Xt[b]), yt[b])
            loss.backward()
            opt.step()
    net.eval()
    with torch.no_grad():
        out = torch.sigmoid(net(torch.tensor(Xte, dtype=torch.float32).to(dev)))
    return out.cpu().numpy()


# ══════════════════ 评估 ══════════════════

def robust_t(dates, ret, score, widths=(1, 2, 3, 7, 14, 30)):
    """多种窗口宽度的 t 值（天为单位）"""
    dates = pd.DatetimeIndex(pd.to_datetime(np.asarray(dates)))
    out = {}
    for wd in widths:
        step = pd.Timedelta(days=wd)
        ics = []
        a = dates.min()
        while a < dates.max() - step:
            b = a + step
            m = np.asarray((dates >= a) & (dates < b)) & np.isfinite(score)
            if m.sum() >= 100:
                v, _ = ic_t(score[m], ret[m])
                if np.isfinite(v):
                    ics.append(v)
            a = a + step
        if len(ics) < 5:
            continue
        arr = np.array(ics)
        t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else np.nan
        out[wd] = {"t": float(t), "ic": float(arr.mean()), "n": len(arr)}
    return out


def walk_predict(df, feats, model, seed=42, seq_len=30, purge=True):
    """
    走查预测 + 净化（purge）：训练集剔除与测试段标签区间重叠的样本。
    """
    dates = pd.to_datetime(df["date"])
    y = df["label"].values
    hold = df["hold"].values
    sc = np.full(len(df), np.nan)

    cuts, t = [], pd.Timestamp(WALK_START)
    end = dates.max()
    while t < end:
        cuts.append((t, min(t + pd.DateOffset(months=STEP_MONTHS),
                            end + pd.Timedelta(days=1))))
        t = t + pd.DateOffset(months=STEP_MONTHS)

    for a, b in cuts:
        tem = ((dates >= a) & (dates < b)).values
        if purge:
            # 训练集：标签结束时间早于测试段开始
            trm = ((dates + pd.to_timedelta(hold, unit="m")) < a).values
        else:
            trm = (dates < a).values
        if trm.sum() < 500 or tem.sum() < 100:
            continue
        if model == "lstm":
            # 需要序列：用特征矩阵按 coin 分组的滑窗
            pass
        if model in ("logit", "lgbm"):
            Xtr = np.nan_to_num(df.loc[trm, feats].values)
            Xte = np.nan_to_num(df.loc[tem, feats].values)
            fn = fit_logit if model == "logit" else fit_lgbm
            sc[tem] = fn(Xtr, y[trm], Xte, seed) if model == "lgbm" else fn(Xtr, y[trm], Xte)
        elif model == "lstm":
            Xtr = np.nan_to_num(df.loc[trm, feats].values)
            Xte = np.nan_to_num(df.loc[tem, feats].values)
            # 用滑窗构造序列（步长 1，宽度 seq_len）
            def mk(X, L):
                if len(X) <= L:
                    return np.zeros((0, L, X.shape[1]), np.float32)
                idx = np.arange(L - 1, len(X))
                return np.stack([X[i - L + 1:i + 1] for i in idx]).astype(np.float32)
            Xtr_s = mk(Xtr, seq_len)
            Xte_s = mk(Xte, seq_len)
            if len(Xtr_s) < 200 or len(Xte_s) < 10:
                continue
            p = fit_lstm(Xtr_s, y[trm][seq_len - 1:], Xte_s, seed)
            tgt = np.where(tem)[0][seq_len - 1:]
            sc[tgt] = p
    return sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--entry", type=int, default=96)
    ap.add_argument("--model", default="lgbm",
                    choices=["logit", "lgbm", "lstm"])
    ap.add_argument("--all", action="store_true", help="跑全部模型")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seq-len", type=int, default=30)
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 100)
    print(f"高频元标记 · {args.tf} · entry={args.entry} · 成本 {COST_MEASURED*1e4:.1f}bp（实测）")
    print("=" * 100)
    dfs, y = build(args.tf, args.entry)

    # 合并特征
    Xs = []
    for c, d in dfs.items():
        f = features(d)
        f["coin"] = c
        Xs.append(f)
    X = pd.concat(Xs)
    X = X[~X.index.duplicated(keep="first")]

    y = y.set_index(["date", "coin"])
    X = X[~X.index.duplicated(keep="first")]
    # X 的 coin 列与 y 的 coin 索引层级重名 → 先丢掉列，靠索引 join
    Xc = X.drop(columns=["coin"], errors="ignore")
    df = Xc.join(y[["ret", "hold", "side"]], how="inner").dropna(subset=["ret"])
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"])

    feats = list(Xc.columns)
    feats = [c for c in feats if df[c].notna().mean() > 0.6]
    print(f"  对齐后 {len(df):,} · 特征 {len(feats)}")

    # 标签：扣【实测成本】后是否盈利
    df["label"] = ((df["ret"] - COST_MEASURED) > 0).astype(int)
    print(f"  正样本率 {df['label'].mean():.3f}（扣 {COST_MEASURED*1e4:.1f}bp 后）")
    print(f"  毛收益均值 {df['ret'].mean()*1e4:+.2f}bp · "
          f"净收益均值 {(df['ret'].mean()-COST_MEASURED)*1e4:+.2f}bp")
    print()

    # 基线
    print("=" * 100)
    print("基线（不过滤，全做）")
    print("=" * 100)
    net = df["ret"] - COST_MEASURED
    print(f"  交易 {len(df):,} 笔 · 毛 {df['ret'].mean()*1e4:+.2f}bp · "
          f"净 {net.mean()*1e4:+.2f}bp · 累计净收益 {net.sum()*100:+.1f}%")
    print()

    models = ["logit", "lgbm", "lstm"] if args.all else [args.model]
    seeds = list(range(42, 42 + args.seeds))
    all_res = {}

    for model in models:
        print("=" * 100)
        print(f"模型 {model}（走查 + 净化 · {len(seeds)} 个种子）")
        print("=" * 100)
        per_seed = []
        for sd in seeds:
            sc = walk_predict(df, feats, model, seed=sd, seq_len=args.seq_len)
            ok = np.isfinite(sc)
            if ok.sum() < 500:
                print(f"  种子 {sd}: 有效预测不足"); continue
            ic, t = ic_t(sc[ok], df["ret"].values[ok])
            # 过滤后净收益（保留预测分 > 中位）
            med = np.nanmedian(sc[ok])
            keep = ok & (sc > med)
            net_keep = (df["ret"].values[keep] - COST_MEASURED).mean()
            per_seed.append({"seed": sd, "ic": ic, "t": t,
                             "n_keep": int(keep.sum()),
                             "net_keep_bp": net_keep * 1e4,
                             "rt": robust_t(df["date"].values[ok], df["ret"].values[ok], sc[ok])})
            print(f"  种子 {sd}: IC {ic:+.4f} (t={t:.2f}) · "
                  f"保留 {keep.sum():,} 笔 · 净 {net_keep*1e4:+.3f}bp")
        if not per_seed:
            continue
        ts = [x["t"] for x in per_seed]
        nets = [x["net_keep_bp"] for x in per_seed]
        print()
        print(f"  多种子均值: IC t={np.mean(ts):.2f} · 过滤后净 {np.mean(nets):+.3f}bp "
              f"(基线 {net.mean()*1e4:+.3f}bp)")
        # 稳健性
        rts = per_seed[0]["rt"]
        print(f"  多窗口宽度 t 值: ", end="")
        for w, r in sorted(rts.items()):
            print(f"{w}d:{r['t']:+.2f} ", end="")
        print()
        npass = sum(1 for r in rts.values() if r["t"] > 2)
        print(f"  稳健通过 {npass}/{len(rts)}")
        all_res[model] = {"t_mean": float(np.mean(ts)),
                          "net_keep_bp": float(np.mean(nets)),
                          "base_net_bp": float(net.mean() * 1e4),
                          "robust_pass": npass, "robust_n": len(rts),
                          "per_seed": [{k: v for k, v in x.items() if k != "rt"}
                                       for x in per_seed]}
        print()

    print("=" * 100)
    print("总结")
    print("=" * 100)
    print(f"  {'模型':<10}{'IC t均值':>10}{'过滤后净bp':>13}{'基线净bp':>12}{'稳健':>8}{'增量':>10}")
    print("  " + "-" * 66)
    base = net.mean() * 1e4
    for m, r in all_res.items():
        inc = r["net_keep_bp"] - base
        mark = "✅" if inc > 0.5 else ("🟡" if inc > 0 else "❌")
        print(f"  {m:<10}{r['t_mean']:>10.2f}{r['net_keep_bp']:>13.3f}{base:>12.3f}"
              f"{str(r['robust_pass'])+'/'+str(r['robust_n']):>8}{inc:>+9.3f} {mark}")
    print("  " + "-" * 66)
    print()
    print(f"  ⚠️ 判读要点：")
    print(f"     · 基线净收益 {base:+.3f}bp/笔 —— 若本来就为负，问题在策略而非 ML")
    print(f"     · ML 过滤只有在【净收益由负转正】时才有意义")
    print(f"     · 成本用实测 {COST_MEASURED*1e4:.1f}bp（旧假设 4.0bp 会高估结果）")

    with open("user_data/hft_meta_results.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                   "tf": args.tf, "entry": args.entry, "n": len(df),
                   "cost_bp": COST_MEASURED * 1e4,
                   "baseline_net_bp": float(net.mean() * 1e4),
                   "results": all_res}, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ user_data/hft_meta_results.json  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
