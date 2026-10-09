#!/usr/bin/env python3
"""
部署总体检验 —— 配置在【真正的开仓点】上还有没有预测力？

════════════════════════════════════════════════════════════════════
为什么要做这个
════════════════════════════════════════════════════════════════════
自动迭代的判据测的是【密集采样总体】的 IC（6 万+ 事件，每根趋势中的 K 线）。
但第 22 轮实测过：

    密集采样总体   IC +0.0286  (t=3.30)   ✅
    部署总体（604 个开仓点）  IC +0.0045  (t=0.11)   ❌

**同一个模型，换个总体，信号就没了。**

所以"通过判据"不等于"可用"。
本脚本把【迭代的候选】放到【部署总体】上检验，回答那个真正重要的问题：
**这个配置拿到实盘开仓点上，还灵吗？**

════════════════════════════════════════════════════════════════════
做法
════════════════════════════════════════════════════════════════════
① event_backtest.run_v2() 取组合策略的每笔成交，分数对齐到前一根信号 K 线
② 复刻 auto_research.evaluate 的扩展窗口走查，训练配置并得到【每个事件的分】
③ 只在开仓点上算：
     · 逐笔 IC（分数 vs 实际盈亏）
     · 分位数分组（5 组）的平均盈亏
     · 与密集总体 IC 的对照

此处直接检验已生成的交易分数相关性，不重新构造权重或净值。

用法:
    python deploy_check.py --key 8b319d56
    python deploy_check.py --spec lstm:45:64:2:0.2:0.001
    python deploy_check.py --top 3            # 账本里最差种子最高的 3 个
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

TRIALS_PATH = os.path.join(_HERE, "user_data", "research_trials.jsonl")


# ══════════════════════════════════════════════════════════════════
#  ① 部署点：生产策略真正开仓的那些时点
# ══════════════════════════════════════════════════════════════════
def deployment_points(log=print, entry=20, exit_=20, entries=None):
    """返回组合回测成交点，或显式标记为代理的原始入场事件。

    entries: 多个 entry 周期的单币原始事件代理。该路径不模拟组合排名、top_n、
        持仓槽位、共享资金或拒单规则；只能做探索性事件关联，不能代表完整部署行为。
    """
    import ml_lab as ml
    import event_backtest as E

    data = ml.load_ohlcv()

    if entries:
        # 这是未模拟组合约束的单币原始事件代理，不代表生产策略实际成交。
        import ml_entry_trained as MET
        frames = []
        for p in entries:
            try:
                ev = MET.entry_events(data, p)
            except Exception as e:
                log(f"  ⚠ entry={p} 取开仓点失败：{type(e).__name__}: {e}")
                continue
            if ev is None or len(ev) == 0:
                log(f"  ⚠ entry={p} 没有开仓点")
                continue
            t = ev.rename(columns={"ret": "profit_pct"}).copy()
            t["profit_pct"] = t["profit_pct"] * 100.0     # 小数 → 百分数
            t["profit_abs"] = np.nan
            t["is_short"] = t["side"] < 0
            t["date"] = pd.to_datetime(t["date"])
            t["decision_date"] = t["date"]
            t["fill_date"] = pd.NaT
            t["entry_period"] = p
            t["sample_kind"] = "raw_entry_event_proxy"
            frames.append(t[["date", "coin", "profit_pct", "profit_abs", "is_short",
                             "decision_date", "fill_date", "entry_period", "sample_kind"]])
            log(f"  entry={p:<3} → {len(t):>5} 笔开仓")
        if not frames:
            return pd.DataFrame(columns=["date", "coin", "profit_pct"])
        out = pd.concat(frames, ignore_index=True)
        log(f"  原始入场事件代理：{len(out)} 个事件（{len(entries)} 个 entry 周期；不含组合约束）"
            f" · 去重 (date,coin) {out[['date','coin']].drop_duplicates().shape[0]}")
        return out

    # 旧 run() 含已确认的入场时序前视，不能作为新的部署检验点来源。
    trades, eq, ret, _diag = E.run_v2(data)
    if trades is None or len(trades) == 0:
        return pd.DataFrame(columns=["date", "coin", "profit_pct"])
    t = trades[["open_date", "pair", "profit_pct", "profit_abs", "is_short"]].copy()
    t = t.rename(columns={"open_date": "date", "pair": "coin"})
    t["coin"] = t["coin"].astype(str).str.split("/").str[0]
    t["fill_date"] = pd.to_datetime(t["date"], utc=True).dt.tz_localize(None)
    t["date"] = t["fill_date"]
    t["decision_date"] = prior_candle_dates(data, t["fill_date"])
    t["entry_period"] = entry
    t["sample_kind"] = "portfolio_run_v2"
    log(f"  run_v2 组合成交：{len(t)} 笔 · 信号日 {t['decision_date'].min().date()}"
        f" → {t['decision_date'].max().date()}")
    return t


def prior_candle_dates(data, fill_dates):
    """将 run_v2 的开盘成交日映射回前一根决策收盘 K 线。"""
    if not data:
        return pd.to_datetime([pd.NaT] * len(fill_dates))
    raw_index = sorted(set().union(*(set(frame.index) for frame in data.values())))
    candles = pd.DatetimeIndex(pd.to_datetime(raw_index, utc=True)).tz_localize(None)
    fills = pd.DatetimeIndex(pd.to_datetime(fill_dates, utc=True)).tz_localize(None)
    positions = candles.get_indexer(fills)
    return pd.to_datetime([
        candles[pos - 1] if pos > 0 else pd.NaT for pos in positions
    ])


# ══════════════════════════════════════════════════════════════════
#  ② 打分：复刻 evaluate 的切分，但返回分数向量
# ══════════════════════════════════════════════════════════════════
def score_panel(cfg, seq, meta, device="mps", step=6, seeds=3, bs=512,
                epochs=None, seed_offset=0, log=print,
                purge_by_t1=True, same_day_rank=True):
    """扩展窗口走查 → 每个事件一个分数。

    ══════════════════════════════════════════════════════════════════
    P1 的两处因果性修正（Codex 评审要求）
    ══════════════════════════════════════════════════════════════════
    ① purge_by_t1（保守口径）：训练样本必须满足【t1 < 训练截止 a】。
       为什么不能只写 t1 不落在测试窗：退出发生在【测试窗之后】的训练样本
       同样泄漏 —— 它的标签在训练时还没成熟，用未来信息标了它。
       实测 meta 的标签持有期：中位 20 天、**最大 185 天**
       → 原实现 (dates < a) 的泄漏窗口极大。
       ⚠ embargo 不能替代 t1 修正，两者解决的是不同问题。

    ② same_day_rank：排名改为【同日横截面】百分位。
       原实现对整个 6 个月测试段做池化秩 → 一个样本的分数取决于其它日期的
       预测分布，生产时拿不到。同日横截面才是部署时可得的量
       （Freqtrade 的 _current_strengths 遍历当日全白名单，语义就是同日横截面）。
       ⚠ 这仍【不等价于】部署：候选全集/槽位/拒单/不补位规则还需另行对齐。
    """
    import ml_seq

    F = seq.shape[2]
    dates = pd.to_datetime(meta["date"])
    y = meta["label"].values
    t1s = pd.to_datetime(meta["t1"]).values if "t1" in meta.columns else None
    if purge_by_t1 and t1s is None:
        raise ValueError("purge_by_t1=True 但 meta 里没有 t1 列 —— 缓存可能是 v1 的旧格式")
    purged_total = 0

    start = pd.Timestamp("2021-07-01")
    cuts, t0 = [], start
    end = dates.max()
    while t0 < end:
        cuts.append((t0, min(t0 + pd.DateOffset(months=step), end + pd.Timedelta(days=1))))
        t0 = t0 + pd.DateOffset(months=step)

    ep = int(epochs if epochs is not None else cfg.get("epochs", 8))
    n_seeds = max(1, int(seeds))
    per_seed = [np.full(len(meta), np.nan) for _ in range(n_seeds)]
    scores = np.full(len(meta), np.nan)

    for ci, (a, b) in enumerate(cuts, 1):
        tr_raw = (dates < a).values
        te = ((dates >= a) & (dates < b)).values
        # ① 按 t1 做 purge：训练样本的标签必须在训练截止【之前】已成熟
        purged = 0
        if purge_by_t1:
            keep = (t1s < np.datetime64(a))
            purged = int((tr_raw & ~keep).sum())
            tr = tr_raw & keep
        else:
            tr = tr_raw
        purged_total += purged
        if tr.sum() < 2000 or te.sum() < 100:
            continue
        mu = seq[tr].reshape(-1, F).mean(axis=0)
        sd = seq[tr].reshape(-1, F).std(axis=0) + 1e-6
        Xtr = seq[tr].astype(np.float32)
        Xtr -= mu
        Xtr /= sd
        np.nan_to_num(Xtr, copy=False)
        Xte = seq[te].astype(np.float32)
        Xte -= mu
        Xte /= sd
        np.nan_to_num(Xte, copy=False)

        preds = []
        for si in range(n_seeds):
            m = ml_seq.train_seq(Xtr, y[tr], F, cfg["kind"], device, epochs=ep, bs=bs,
                                 lr=float(cfg["lr"]), hidden=int(cfg["hidden"]),
                                 layers=int(cfg["layers"]), dropout=float(cfg["dropout"]),
                                 seed=42 + (si + seed_offset) * 37)
            preds.append(ml_seq.predict_seq(m, Xte, device))
            del m
        # 段内秩平均（不能在全局平均 —— 分数尺度跨段漂移会造假，第 13 轮教训）
        # ⚠ 必须【单种子也做段内秩归一化】。
        #   否则单种子保存的是原始预测值，跨段尺度不同，
        #   而下游 IC 是【全局】秩相关 —— 跨段尺度漂移会污染排名，
        #   让"部署总体 IC"测的其实是段间效应而不是段内信号。
        #   （这是我自己踩的坑：1 种子给 −0.006，3 种子平均给 +0.030，
        #     差的不是噪声，是这个口径不一致。）
        if same_day_rank:
            # ② 同日横截面百分位 —— 部署时可得的量
            day_key = dates[te].values
            R = np.vstack([
                pd.Series(p).groupby(day_key).rank(pct=True).values for p in preds])
        else:
            # 旧口径：整段池化秩（保留以便对照，勿用于新结论）
            R = np.vstack([pd.Series(p).rank().values / len(p) for p in preds])
        scores[te] = R.mean(axis=0)
        for si, p in enumerate(preds):
            per_seed[si][te] = p
        log(f"    段 {ci}/{len(cuts)} · 训练 {tr.sum()}（purge 掉 {purged}）"
            f" / 测试 {te.sum()}")

    if purged_total:
        log(f"    purge 合计 {purged_total} 个训练样本（t1 >= 训练截止）")
    return scores, per_seed


# ══════════════════════════════════════════════════════════════════
#  ③ 检验
# ══════════════════════════════════════════════════════════════════
def _ic_t(x, y):
    """IC 与其 t 值（逐笔口径）。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 30:
        return np.nan, np.nan, n
    xr = pd.Series(x).rank().values
    yr = pd.Series(y).rank().values
    xr = (xr - xr.mean()) / (xr.std() + 1e-12)
    yr = (yr - yr.mean()) / (yr.std() + 1e-12)
    ic = float((xr * yr).mean())
    t = ic * np.sqrt((n - 2) / max(1e-12, 1 - ic ** 2))
    return ic, float(t), n


def evaluate_deployment(scores, meta, pts, log=print):
    """把分数接到部署点上，做逐笔 IC 与分位数分组。"""
    m = meta.copy()
    m["score"] = scores
    m["date"] = pd.to_datetime(m["date"])
    score_date = "decision_date" if "decision_date" in pts.columns else "date"
    j = pts.merge(m[["date", "coin", "score"]], left_on=[score_date, "coin"],
                  right_on=["date", "coin"], how="left")
    hit = j["score"].notna()
    log(f"  部署点匹配到分数：{hit.sum()}/{len(j)}"
        + ("" if hit.sum() == len(j) else "  ← 未匹配的做不了检验"))

    ic, t, n = _ic_t(j.loc[hit, "score"].values, j.loc[hit, "profit_pct"].values)
    out = {"n": int(n), "ic": ic, "t": t}

    # 分位数分组
    q = j.loc[hit].copy()
    if len(q) >= 50:
        try:
            q["grp"] = pd.qcut(q["score"], 5, labels=False, duplicates="drop")
            g = q.groupby("grp")["profit_pct"].agg(["mean", "count"])
            out["quintiles"] = {int(k): (float(v["mean"]), int(v["count"]))
                                for k, v in g.iterrows()}
            hi, lo = int(g.index.max()), int(g.index.min())
            out["hi_minus_lo"] = float(g.loc[hi, "mean"] - g.loc[lo, "mean"])
            # 分块自助法 CI（按时间分块，避免逐笔相关性低估方差）
            rng = np.random.default_rng(0)
            blocks = np.array_split(np.arange(len(q)), 12)
            diffs = []
            for _ in range(400):
                idx = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
                s = q.iloc[idx]
                h = s.loc[s["score"] >= s["score"].quantile(0.8), "profit_pct"]
                low = s.loc[s["score"] <= s["score"].quantile(0.2), "profit_pct"]
                if len(h) > 2 and len(low) > 2:
                    diffs.append(h.mean() - low.mean())
            if diffs:
                out["hi_lo_ci"] = (float(np.percentile(diffs, 2.5)),
                                   float(np.percentile(diffs, 97.5)))
        except Exception as e:
            out["quintile_error"] = f"{type(e).__name__}: {e}"
    return out


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════
def load_ledger():
    if not os.path.exists(TRIALS_PATH):
        return {}
    cur = {}
    with open(TRIALS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("key"):
                cur[r["key"]] = r
    return cur


def parse_spec(s):
    k, L, h, ly, dp, lr = s.split(":")
    return {"kind": k, "seq_len": int(L), "hidden": int(h), "layers": int(ly),
            "dropout": float(dp), "lr": float(lr), "epochs": 8}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=None, help="账本里的配置 key")
    ap.add_argument("--spec", default=None, help="kind:seq_len:hidden:layers:dropout:lr")
    ap.add_argument("--top", type=int, default=0, help="账本里最差种子最高的 N 个")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="种子偏移：便于单独测某个种子（1 种子时用它扫不同种子）")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--entries", default=None,
                    help="逗号分隔的多个 entry 周期，用于提高功效。例 --entries 20,30,40,55")
    args = ap.parse_args()

    print("=" * 100)
    print("  部署总体检验 —— 配置在【真正的开仓点】上还有没有预测力？")
    print("=" * 100)

    ledger = load_ledger()
    targets = []
    if args.key:
        if args.key not in ledger:
            print(f"  ❌ 账本里没有 key={args.key}")
            sys.exit(1)
        targets.append((args.key, ledger[args.key].get("config")))
    if args.spec:
        targets.append(("spec", parse_spec(args.spec)))
    if args.top:
        cand = [v for v in ledger.values() if v.get("config") and v.get("seed_t_min") is not None]
        cand.sort(key=lambda v: -(v.get("seed_t_min") or 0))
        for v in cand[:args.top]:
            targets.append((v["key"], v["config"]))
    if not targets:
        print("  请给 --key / --spec / --top")
        sys.exit(1)

    # 部署点（只算一次）
    _entries = ([int(x) for x in args.entries.split(",")] if args.entries else None)
    pts = deployment_points(entries=_entries)
    if len(pts) == 0:
        print("  ❌ 取不到部署点")
        sys.exit(1)

    import panel_cache as pc
    results = []
    for key, cfg in targets:
        print(f"\n{'─' * 100}")
        print(f"  【{key}】 {cfg.get('kind')} L={cfg.get('seq_len')} h={cfg.get('hidden')} "
              f"ly={cfg.get('layers')} dp={cfg.get('dropout')} lr={cfg.get('lr')}")
        print(f"{'─' * 100}")
        t0 = time.time()
        seq, meta, feats = pc.load_or_build(int(cfg["seq_len"]), dense=True, log=print)
        scores, per_seed = score_panel(cfg, seq, meta, device=args.device,
                                       step=args.step, seeds=args.seeds,
                                       epochs=args.epochs,
                                       seed_offset=args.seed_offset, log=print)
        # 对照：密集总体 IC
        dens_ic, dens_t, dens_n = _ic_t(scores, meta["ret"].values)
        # 部署总体
        dep = evaluate_deployment(scores, meta, pts, log=print)
        print("\n  ┌─ 结果 ─────────────────────────────────────────────")
        print(f"  │ 密集总体   n={dens_n:<6} IC={dens_ic:+.4f}  t={dens_t:+.2f}")
        if dep["n"]:
            print(f"  │ 部署总体   n={dep['n']:<6} IC={dep['ic']:+.4f}  t={dep['t']:+.2f}")
            if dep.get("hi_minus_lo") is not None:
                ci = dep.get("hi_lo_ci")
                print(f"  │ 高20% − 低20% 平均盈亏差 {dep['hi_minus_lo']:+.3f}%"
                      + (f"   95%CI [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else ""))
                if dep.get("quintiles"):
                    qs = "  ".join(f"Q{k}={v[0]:+.2f}%" for k, v in sorted(dep["quintiles"].items()))
                    print(f"  │ 分位: {qs}")
        else:
            print("  │ 部署总体   匹配不到足够样本，无法检验")
        print(f"  └─ 耗时 {time.time() - t0:.0f}s")
        # 保存分数：供后续做「多配置平均分数」的集成检验（不必重训）
        try:
            import hashlib as _h
            tag = _h.md5(f"{key}|{json.dumps(cfg, sort_keys=True)}|s{args.seeds}|o{args.seed_offset}".encode()).hexdigest()[:10]
            sp = os.path.join(_HERE, "user_data", "deploy_scores", f"{tag}.npz")
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            np.savez_compressed(sp, scores=scores,
                                date=meta["date"].astype(str).values,
                                coin=meta["coin"].astype(str).values,
                                ret=meta["ret"].values)
            print(f"  │ 分数已存 {os.path.basename(sp)}")
        except Exception as e:
            print(f"  │ ⚠ 分数保存失败：{type(e).__name__}: {e}")
        results.append({"key": key, "config": cfg, "dense": {"ic": dens_ic, "t": dens_t, "n": dens_n}, "deploy": dep})

    out = os.path.join(_HERE, "user_data", "deploy_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n  结果已存 {out}")


if __name__ == "__main__":
    main()
