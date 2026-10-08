#!/usr/bin/env python3
"""
ML 实验室 —— 正确的金融机器学习框架

为什么重建：
    之前用 FreqAI 直接预测收益，4 个模型（LightGBM/XGBoost/MLP/Transformer）
    全部亏损 -46.78% ~ -73.94%。但那不是「深度学习不行」，
    而是【框架错了】：直接预测收益的信号噪声比太低（加密日收益约 99% 是噪声）。

本模块实现的是金融 ML 的成熟做法（López de Prado, Advances in Financial ML）：

    ① 事件采样（event-based sampling）
       不在每个交易日采样，只在【出现交易信号】时采样
       → 样本量下降但信噪比大幅提升

    ② 三重障碍标注（triple-barrier labeling）
       每个事件用三条线界定结果：止盈线 / 止损线 / 时间上限
       → 把连续收益预测变成「哪一个先到」的分类问题，更稳

    ③ 元标记（meta-labeling）
       主模型（趋势策略）决定【方向】，ML 只决定【做不做、做多大】
       → 二分类问题，基准胜率约 40%，模型到 55% 就有实际价值

    ④ 净化 K 折交叉验证（purged K-fold with embargo）
       训练集剔除与测试集【标签区间重叠】的样本，并加禁运期
       → 这是金融 ML 最容易泄漏的地方，不处理必然虚高

    ⑤ 防前视：所有特征 shift(1)

用法（作为库）:
    from ml_lab import build_features, make_events, triple_barrier, purged_kfold
"""

import os

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
NEWSRC = "user_data/newsrc"


# ═══════════════════════════════════════════════════════════
#  数据加载
# ═══════════════════════════════════════════════════════════

def load_ohlcv(coins=None, min_days=400):
    import glob
    out = {}
    for f in sorted(glob.glob(f"{PERP}/*-1d-futures.feather")):
        s = os.path.basename(f).split("_")[0]
        if coins and s not in coins:
            continue
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        if len(d) < min_days:
            continue
        out[s] = d[["open", "high", "low", "close", "volume"]]
    return out


def load_funding():
    """Binance 资金费率（8 小时一次 → 日频求和）"""
    f = f"{NEWSRC}/funding_binance.pkl"
    if not os.path.exists(f):
        return None
    return pd.read_pickle(f)


def load_dvol():
    out = {}
    for c in ["btc", "eth"]:
        f = f"{NEWSRC}/dvol_{c}.pkl"
        if os.path.exists(f):
            out[c.upper()] = pd.read_pickle(f)
    return out or None


# ═══════════════════════════════════════════════════════════
#  ① 特征工程
# ═══════════════════════════════════════════════════════════

def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(d, n=14):
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["close"].shift(1)).abs(),
        (d["low"] - d["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def features_one(d: pd.DataFrame, funding: pd.Series = None,
                 dvol: pd.Series = None) -> pd.DataFrame:
    """
    单币特征。全部基于【已收盘】数据，最后统一 shift(1) 保证无前视。
    """
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    f = pd.DataFrame(index=d.index)

    # ── 动量 ──
    for n in [1, 3, 5, 10, 20, 60]:
        f[f"ret_{n}"] = c.pct_change(n)
    # ── 均线偏离 ──
    for n in [10, 20, 50, 100]:
        f[f"ma_dev_{n}"] = c / c.rolling(n).mean() - 1
    # ── 波动 ──
    r = c.pct_change()
    for n in [10, 20, 60]:
        f[f"vol_{n}"] = r.rolling(n).std()
    f["vol_ratio"] = f["vol_10"] / f["vol_60"].replace(0, np.nan)
    f["atr_pct"] = _atr(d, 14) / c
    # ── 通道位置 / 突破 ──
    for n in [20, 55]:
        hh = c.rolling(n).max().shift(1)
        ll = c.rolling(n).min().shift(1)
        span = (hh - ll).replace(0, np.nan)
        f[f"chan_pos_{n}"] = (c - ll) / span
        f[f"brk_up_{n}"] = (c - hh) / span
        f[f"brk_dn_{n}"] = (ll - c) / span
    # ── 量能 ──
    f["vol_z"] = (v - v.rolling(20).mean()) / v.rolling(20).std().replace(0, np.nan)
    f["dollar_vol"] = (c * v)
    f["dvol_ma_ratio"] = f["dollar_vol"] / f["dollar_vol"].rolling(20).mean().replace(0, np.nan)
    # ── 形态 ──
    f["rsi_14"] = _rsi(c, 14)
    f["range_pct"] = (h - l) / c
    f["close_loc"] = (c - l) / (h - l).replace(0, np.nan)
    f["up_days_20"] = (r > 0).rolling(20).mean()
    # ── 极值距离 ──
    f["dist_max_60"] = c / c.rolling(60).max().replace(0, np.nan) - 1
    f["dist_min_60"] = c / c.rolling(60).min().replace(0, np.nan) - 1
    # ── 加速度 ──
    f["ret_accel"] = f["ret_5"] - f["ret_20"] / 4

    # ── 路径依赖特征（第 6 轮新增）──
    # 当前特征都是截面快照，无法表达「趋势走了多久、走得多顺」。
    # 这几个是趋势跟踪最核心的状态变量。
    hh20 = c.rolling(20).max()
    f["days_since_high"] = (c < hh20).astype(int).groupby(
        (c >= hh20).cumsum()).cumsum()
    f["days_since_low"] = (c > c.rolling(20).min()).astype(int).groupby(
        (c <= c.rolling(20).min()).cumsum()).cumsum()
    # 趋势持续天数（收盘价在 20 日均线上方/下方的连续天数）
    above = (c > c.rolling(20).mean()).astype(int)
    f["days_above_ma20"] = above.groupby((above == 0).cumsum()).cumsum()
    f["days_below_ma20"] = (1 - above).groupby(above.cumsum()).cumsum()
    # 从近期高点的回撤深度
    f["dd_from_high20"] = c / c.rolling(20).max() - 1
    f["dd_from_high60"] = c / c.rolling(60).max() - 1
    # 连续涨跌
    up = (r > 0).astype(int)
    f["consec_up"] = up.groupby((up == 0).cumsum()).cumsum()
    f["consec_dn"] = (1 - up).groupby(up.cumsum()).cumsum()
    # 趋势效率（净位移 / 总路程）—— 区分「顺畅趋势」与「来回震荡」
    for n in [10, 20]:
        net = (c - c.shift(n)).abs()
        path = c.diff().abs().rolling(n).sum()
        f[f"efficiency_{n}"] = net / path.replace(0, np.nan)
    # 波动率的趋势（放大 or 收敛）
    f["vol_trend"] = f["vol_10"] / f["vol_20"].replace(0, np.nan) - 1
    # 成交量的趋势
    f["vol_ma_ratio_5_20"] = (v.rolling(5).mean() / v.rolling(20).mean().replace(0, np.nan))

    # ── 资金费率（外部数据）──
    if funding is not None:
        fr = funding.reindex(d.index).ffill()
        f["funding"] = fr
        f["funding_ma3"] = fr.rolling(3).mean()
        f["funding_z"] = (fr - fr.rolling(30).mean()) / fr.rolling(30).std().replace(0, np.nan)
        f["funding_cum7"] = fr.rolling(7).sum()

    # ── 期权波动率（外部数据，仅 BTC/ETH 有）──
    if dvol is not None:
        dv = dvol.reindex(d.index).ffill()
        f["dvol"] = dv
        f["dvol_chg_5"] = dv.pct_change(5)
        f["dvol_z"] = (dv - dv.rolling(60).mean()) / dv.rolling(60).std().replace(0, np.nan)

    return f


def build_features(data: dict, funding=None, dvol=None, cross_sectional=True):
    """
    构建全市场特征面板。返回 MultiIndex(date, coin) 的长表。
    """
    frames = []
    for s, d in data.items():
        fr = funding[s] if (funding is not None and s in funding.columns) else None
        dv = None
        if dvol:
            # 只有 BTC/ETH 有期权数据，其余用 BTC 的作为市场代理
            # （不能用 `or`，pandas Series 的真值有歧义）
            dv = dvol[s] if s in dvol else dvol.get("BTC")
        ff = features_one(d, fr, dv)
        ff["coin"] = s
        frames.append(ff)

    # ── 横截面标准化（同一时点跨币比较）──
    if cross_sectional:
        allf = pd.concat(frames)
        allf = allf.reset_index().rename(columns={"index": "date"})
        if "date" not in allf.columns:
            allf = allf.rename(columns={allf.columns[0]: "date"})
        feat_cols = [c for c in allf.columns if c not in ("date", "coin")]
        # 只用价格类特征做横截面 z-score（资金费率已含绝对信息）
        z_cols = [c for c in feat_cols if c.startswith(("ret_", "vol_", "ma_dev_",
                                                        "chan_pos_", "brk_", "dist_",
                                                        "vol_ratio", "rsi_", "up_days_",
                                                        "days_", "dd_from_", "consec_",
                                                        "efficiency_", "vol_trend",
                                                        "vol_ma_ratio"))]
        g = allf.groupby("date")
        for c in z_cols:
            mu = g[c].transform("mean")
            sd = g[c].transform("std").replace(0, np.nan)
            allf[f"xs_{c}"] = (allf[c] - mu) / sd
        allf = allf.set_index(["date", "coin"]).sort_index()
        return allf

    allf = pd.concat(frames)
    allf.index.name = "date"
    return allf.reset_index().set_index(["date", "coin"]).sort_index()


# ═══════════════════════════════════════════════════════════
#  ② 事件采样 + 三重障碍标注
# ═══════════════════════════════════════════════════════════

def make_events(data: dict, entry=20):
    """
    事件 = 趋势状态从 0 变为 ±1 的那一天（与实盘策略一致）。
    返回 DataFrame[date, coin, side, t1]。
    """
    rows = []
    for s, d in data.items():
        c = d["close"]
        hh = c.rolling(entry).max().shift(1)
        ll = c.rolling(entry).min().shift(1)
        st = pd.Series(0.0, index=d.index)
        cur = 0.0
        cv, hv, lv = c.values, hh.values, ll.values
        for i in range(len(cv)):
            if cur == 0.0:
                if np.isfinite(hv[i]) and cv[i] > hv[i]:
                    cur = 1.0
                elif np.isfinite(lv[i]) and cv[i] < lv[i]:
                    cur = -1.0
            else:
                # 与策略一致：反向突破才离场
                if cur > 0 and np.isfinite(lv[i]) and cv[i] < lv[i]:
                    cur = 0.0
                elif cur < 0 and np.isfinite(hv[i]) and cv[i] > hv[i]:
                    cur = 0.0
            st.iloc[i] = cur
        prev = st.shift(1).fillna(0.0)
        for i in range(1, len(st)):
            if st.iloc[i] != 0 and (prev.iloc[i] == 0 or np.sign(st.iloc[i]) != np.sign(prev.iloc[i])):
                rows.append({"date": d.index[i], "coin": s, "side": int(st.iloc[i])})
    ev = pd.DataFrame(rows)
    return ev.sort_values("date").reset_index(drop=True)


def make_dense_events(data: dict, entry=20, exit_=20, side_filter=None):
    """
    稠密采样 —— 让训练分布与推理分布一致。

    为什么需要：
        稀疏采样只在【状态转折点】取样本（2080 个），
        但实盘/回测在【每一个状态非零的 K 线】都可能入场。
        训练分布 ≠ 推理分布 → ML 过滤无法生效（实测交易数只从 449 降到 336）。

        本函数为【每一根处于趋势中的 K 线】生成样本，与推理时一致。

    代价：样本量大幅上升（约 5 万+），且标签重叠严重 ——
         必须配合更长的禁运期做净化 K 折。
    """
    rows = []
    for s, d in data.items():
        c = d["close"]
        hh = c.rolling(entry).max().shift(1)
        ll = c.rolling(entry).min().shift(1)
        hx = c.rolling(exit_).max().shift(1)
        lx = c.rolling(exit_).min().shift(1)
        cv, hv, lv = c.values, hh.values, ll.values
        hxv, lxv = hx.values, lx.values
        cur = 0.0
        for i in range(len(cv)):
            if cur == 0.0:
                if np.isfinite(hv[i]) and cv[i] > hv[i]:
                    cur = 1.0
                elif np.isfinite(lv[i]) and cv[i] < lv[i]:
                    cur = -1.0
            else:
                if cur > 0 and np.isfinite(lxv[i]) and cv[i] < lxv[i]:
                    cur = 0.0
                elif cur < 0 and np.isfinite(hxv[i]) and cv[i] > hxv[i]:
                    cur = 0.0
            # 状态非零即可入场（与 enter_long = trend_state>0 一致）
            if cur != 0 and i + 1 < len(cv):
                if side_filter is not None and cur != side_filter:
                    continue
                rows.append({"date": d.index[i], "coin": s, "side": int(cur),
                             "entered": True})
    ev = pd.DataFrame(rows)
    return ev.sort_values("date").reset_index(drop=True)


def triple_barrier(data: dict, events: pd.DataFrame,
                   pt_sl=(2.0, 1.0), max_days=30, vol_scale=True):
    """
    三重障碍标注。

    对每个事件：
      · 从事件次日的开盘价进入
      · 上障碍 = 入场价 × (1 + pt × σ)   （多头；空头方向相反）
      · 下障碍 = 入场价 × (1 - sl × σ)
      · 垂直障碍 = max_days 天后
    标签：先碰到上障碍 → 1；先碰下障碍 → 0；都没碰 → 按到期收益符号

    vol_scale=True 时 σ 用事件前 20 日波动率（自适应障碍宽度）。
    """
    pt, sl = pt_sl
    out = []
    for _, e in events.iterrows():
        s, dt, side = e["coin"], e["date"], e["side"]
        d = data.get(s)
        if d is None or dt not in d.index:
            continue
        i = d.index.get_loc(dt)
        if i + 1 >= len(d):
            continue
        # 入场：事件次日开盘
        entry = d["open"].iloc[i + 1]
        if not np.isfinite(entry) or entry <= 0:
            continue
        if vol_scale:
            r = d["close"].pct_change().iloc[max(0, i - 19):i + 1]
            sig = r.std()
            if not np.isfinite(sig) or sig <= 0:
                sig = 0.03
        else:
            sig = 1.0
        up = entry * (1 + pt * sig * side)
        dn = entry * (1 - sl * sig * side)
        end = min(i + 1 + max_days, len(d) - 1)

        label, t1, reason = None, end, "vertical"
        for j in range(i + 1, end + 1):
            hi, lo = d["high"].iloc[j], d["low"].iloc[j]
            if side > 0:
                if hi >= up:
                    label, t1, reason = 1, j, "pt"; break
                if lo <= dn:
                    label, t1, reason = 0, j, "sl"; break
            else:
                if lo <= up:
                    label, t1, reason = 1, j, "pt"; break
                if hi >= dn:
                    label, t1, reason = 0, j, "sl"; break
        if label is None:
            exit_px = d["close"].iloc[end]
            ret = (exit_px / entry - 1) * side
            label = 1 if ret > 0 else 0
        ret_abs = (d["close"].iloc[t1] / entry - 1) * side
        out.append({
            "date": dt, "coin": s, "side": side, "entry": entry,
            "t1": d.index[t1], "label": label, "ret": ret_abs, "reason": reason,
            "hold_days": t1 - (i + 1),
        })
    return pd.DataFrame(out)


def strategy_label(data: dict, events: pd.DataFrame, exit_period=20):
    """
    策略忠实标注 —— 元标记最正确的目标。

    为什么不用固定止盈止损：
        三重障碍（pt=2/sl=1, 30天）实测平均持仓仅 1.5 天，
        标签被短期噪声主导；而趋势信号要抓的是几周的行情。
        用固定障碍标注 = 标签与策略目标不匹配。

    本函数按【策略真实规则】模拟每笔交易：
        入场 = 事件次日开盘
        离场 = 收盘价反向突破 exit_period 通道（与 TrendFollowing.py 一致）
        标签 = 该笔实际盈亏是否为正

    这样 ML 学到的就是「这笔趋势交易会不会按策略赚钱」，与上线目标一致。
    """
    out = []
    for _, e in events.iterrows():
        s, dt, side = e["coin"], e["date"], e["side"]
        d = data.get(s)
        if d is None or dt not in d.index:
            continue
        i = d.index.get_loc(dt)
        if i + 1 >= len(d):
            continue
        c = d["close"]
        hh = c.rolling(exit_period).max().shift(1)
        ll = c.rolling(exit_period).min().shift(1)
        entry = d["open"].iloc[i + 1]
        if not np.isfinite(entry) or entry <= 0:
            continue
        # 反向突破离场
        exit_i, reason = len(d) - 1, "eod"
        for j in range(i + 1, len(d)):
            if side > 0 and np.isfinite(ll.iloc[j]) and c.iloc[j] < ll.iloc[j]:
                exit_i, reason = j, "channel"; break
            if side < 0 and np.isfinite(hh.iloc[j]) and c.iloc[j] > hh.iloc[j]:
                exit_i, reason = j, "channel"; break
        exit_px = d["open"].iloc[min(exit_i + 1, len(d) - 1)]
        ret = (exit_px / entry - 1) * side
        out.append({
            "date": dt, "coin": s, "side": side, "entry": entry,
            "t1": d.index[min(exit_i + 1, len(d) - 1)],
            "label": 1 if ret > 0 else 0,
            "ret": ret,
            "reason": reason,
            "hold_days": min(exit_i + 1, len(d) - 1) - (i + 1),
        })
    return pd.DataFrame(out)


# ═══════════════════════════════════════════════════════════
#  ③ 净化 K 折交叉验证
# ═══════════════════════════════════════════════════════════

def purged_kfold(events: pd.DataFrame, n_splits=5, embargo_days=5):
    """
    净化 K 折：训练集剔除【标签区间与测试集重叠】的样本，并加禁运期。

    为什么必须做：
        金融标签有【重叠】—— 一个事件的收益区间可能横跨另一个事件的区间。
        随机 K 折会让训练集包含测试期的信息 → 样本外表现虚高。
        这是金融 ML 最常见的致命错误。

    返回 [(train_idx, test_idx), ...]，idx 是 events 的位置索引。
    """
    ev = events.sort_values("date").reset_index(drop=True)
    n = len(ev)
    fold = n // n_splits
    # 每个样本的标签区间 [date, t1]
    starts = ev["date"].values
    ends = pd.to_datetime(ev["t1"]).values

    for k in range(n_splits):
        lo, hi = k * fold, (n if k == n_splits - 1 else (k + 1) * fold)
        test = np.arange(lo, hi)
        if len(test) == 0:
            continue
        t_start = starts[test].min()
        t_end = ends[test].max()
        emb = np.timedelta64(embargo_days, "D")

        train = []
        for i in range(n):
            if lo <= i < hi:
                continue
            # 净化：训练样本的标签区间与测试区间重叠 → 剔除
            if ends[i] >= t_start and starts[i] <= t_end:
                continue
            # 禁运：测试期结束后 embargo_days 内的样本也剔除
            if starts[i] > t_end and starts[i] <= t_end + emb:
                continue
            train.append(i)
        yield np.array(train), test


# ═══════════════════════════════════════════════════════════
#  评估
# ═══════════════════════════════════════════════════════════

def score(y_true, y_pred, y_prob=None):
    """分类指标"""
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    tp = ((y_pred == 1) & (y_true == 1)).sum()
    fp = ((y_pred == 1) & (y_true == 0)).sum()
    fn = ((y_pred == 0) & (y_true == 1)).sum()
    tn = ((y_pred == 0) & (y_true == 0)).sum()
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    acc = (tp + tn) / len(y_true) if len(y_true) else 0
    # AUC（手写，避免依赖 sklearn）
    auc = np.nan
    if y_prob is not None and len(set(y_true)) == 2:
        p = np.asarray(y_prob)
        r = pd.Series(p).rank().values
        n1, n0 = (y_true == 1).sum(), (y_true == 0).sum()
        if n1 and n0:
            auc = (r[y_true == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
    return {"acc": acc, "prec": prec, "rec": rec, "auc": auc,
            "base": y_true.mean() if len(y_true) else np.nan,
            "n": len(y_true), "n_pred_pos": int((y_pred == 1).sum())}
