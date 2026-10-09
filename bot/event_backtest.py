#!/usr/bin/env python3
"""
事件驱动回测 —— 精确复刻 Freqtrade 的持仓语义

为什么要单独写这个：
    原来的 walkforward.py 是「组合权重」模型：每天重算前 N 名，
    掉出前 N 就平仓。实测产生 3541 次开仓。

    而 Freqtrade（实盘）是「离散交易」模型：
    · 只在【开仓那一刻】用 confirm_trade_entry 筛一次
    · 开仓后【一直持有到趋势结束】，不因为排名变化而平仓
    实测只有 490 笔。

    两者不是同一个策略。这个脚本复刻后者的语义，
    用于和 Freqtrade 的真实交易清单对账。

语义（与 TrendFollowing.py 一致）：
    1. 信号：收盘价通道 + 状态机（突破后持有到反向突破）
    2. 开仓：状态从 0 变 ±1，且【空槽位】，且【当时强度在前 top_n】
    3. 平仓：状态回到 0（趋势结束）
    4. 仓位：固定分数（钱包 × exposure / top_n），复利
    5. 成本：单边 cost_one

用法:
    python event_backtest.py                     # 回测
    python event_backtest.py --verify            # 与 Freqtrade 交易清单对账
"""

import argparse
import glob
import json
import os
import subprocess
import zipfile

import numpy as np
import pandas as pd

PERP = "user_data/data/binance/futures"
LIVE = "user_data/config_trend_live.json"


def load_ohlc(syms):
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
        out[s] = d[["open", "high", "low", "close", "volume"]]
    return out


def signals(close: pd.Series, entry=20, exit_=20):
    """收盘价通道 + 状态机（与策略文件一致）"""
    c = close.values
    hhe = close.rolling(entry).max().shift(1).values
    lle = close.rolling(entry).min().shift(1).values
    hxe = close.rolling(exit_).max().shift(1).values
    lxe = close.rolling(exit_).min().shift(1).values
    st = np.zeros(len(c))
    cur = 0.0
    for i in range(len(c)):
        if cur == 0.0:
            if np.isfinite(hhe[i]) and c[i] > hhe[i]:
                cur = 1.0
            elif np.isfinite(lle[i]) and c[i] < lle[i]:
                cur = -1.0
        else:
            if cur > 0 and np.isfinite(lxe[i]) and c[i] < lxe[i]:
                cur = 0.0
            elif cur < 0 and np.isfinite(hxe[i]) and c[i] > hxe[i]:
                cur = 0.0
        st[i] = cur
    return pd.Series(st, index=close.index)


def strength(close: pd.Series, entry=20):
    span = (close.rolling(entry).max().shift(1)
            - close.rolling(entry).min().shift(1)).replace(0, np.nan)
    up = (close - close.rolling(entry).max().shift(1)) / span
    dn = (close.rolling(entry).min().shift(1) - close) / span
    return pd.concat([up, dn], axis=1).max(axis=1)


def run(data, top_n=8, max_open=10, exposure=0.30, cost_one=0.0005,
        start=None, wallet=10_000.0, entry="next_open", ml_mask=None,
        chan_entry=20, chan_exit=20, entry_delay=0):
    """
    事件驱动模拟。返回 (trades DataFrame, equity Series, ret Series)

    entry: 'next_open' = 信号次日开盘成交（Freqtrade 默认）
           'close'     = 信号当根收盘成交
    chan_entry/chan_exit: Donchian 通道周期，默认 20/20（= 实盘策略）。
    ml_mask: 可选。DataFrame(index=date, columns=coin) 的布尔表，
             True = 该 (date, coin) 允许开仓；None = 不过滤（默认，
             行为与原版完全一致）。加这个参数是为了做组合层的 ML 过滤对照。
    """
    S, ST, P = {}, {}, {}
    for s, d in data.items():
        if start is not None:
            d = d[d.index >= start]
            if len(d) < 120:
                continue
        # chan_entry/chan_exit 默认 20/20 —— 与实盘 TrendFollowing.py 一致；
        # 不传参时行为与原版完全相同
        S[s] = signals(d["close"], entry=chan_entry, exit_=chan_exit)
        ST[s] = strength(d["close"], entry=chan_entry)
        P[s] = d[["open", "close"]]
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()

    dates = Sd.index
    T, K = Sd.shape
    px = Cd.values                       # 用收盘价估值
    opx = Od.values
    state = Sd.values
    stg = STd.values

    cash = wallet
    positions = {}                        # col -> dict(qty, entry_px, pair, entry_i)
    trades = []
    eq_curve = np.zeros(T)

    for i in range(T):
        # ── ① 先处理平仓（趋势结束）──
        # 估值模型：value = stake + qty_signed × (px - entry_px)
        #   · 多头 qty_signed > 0 → 价格涨则价值涨
        #   · 空头 qty_signed < 0 → 价格跌则价值涨
        #   （空头不是「花掉本金」，不能用 cash += qty*px）
        for k in list(positions.keys()):
            if state[i, k] == 0.0:
                p = positions.pop(k)
                # ⚠️ 离场信号在【第 i 根收盘】才成立，成交应在【第 i+1 根开盘】。
                #    早一根成交看似只差 1 天，但趋势离场信号正好出现在大跌当天，
                #    当天开盘价还没跌完 → 系统性高估收益（实测单笔 4.79% vs 3.29%）。
                fill_i = min(i + 1, T - 1)
                exit_px = opx[fill_i, k] if entry == "next_open" else px[fill_i, k]
                value = p["stake"] + p["qty_signed"] * (exit_px - p["entry_px"])
                fee = (p["stake"] + abs(value - p["stake"])) * cost_one
                cash += value - fee
                pnl = value - p["stake"] - p["fee_in"] - fee
                trades.append({
                    "pair": p["pair"].split("/")[0], "open_i": p["entry_i"],
                    "close_i": fill_i,
                    "open_date": dates[p["entry_i"]], "close_date": dates[fill_i],
                    "is_short": p["qty_signed"] < 0,
                    "open_rate": p["entry_px"], "close_rate": exit_px,
                    "profit_abs": pnl,
                    "profit_pct": pnl / p["stake"] * 100 if p["stake"] else 0,
                    "exit_reason": "trend_end",
                })

        # ── ② 开仓 ──
        # 与 TrendFollowing.py 一致：enter_long = (trend_state > 0)
        # 即【只要趋势状态非零就会发信号】，不是只在转折点；
        # 有没有空槽位、是否在前 top_n 由引擎决定。
        if i > 0:
            prev = state[i - 1]
            cur = state[i]
            cands = [k for k in range(K)
                     if cur[k] != 0 and k not in positions
                     and (prev[k] == 0 or np.sign(cur[k]) != np.sign(prev[k])
                          or True)]   # 非转折点也允许（有空槽时会补进）
            if cands:
                # ⚠️ 排名必须在【全部有信号的币】里算，而不是只在「还没持仓的候选」里算。
                #    否则已有 5 个持仓时还能再从剩下币里加 8 个 → 并发数超过 top_n，
                #    实测平均暴露 61%（设定 30%），收益虚高 3 倍。
                #    Freqtrade 的 _current_strengths 遍历全部白名单，语义就是前者。
                allsig = [k for k in range(K) if state[i - 1, k] != 0]
                vals = {k: stg[i - 1, k] for k in allsig if np.isfinite(stg[i - 1, k])}
                ranked = sorted(vals.items(), key=lambda kv: -kv[1])
                allowed = {k for k, _ in ranked[:top_n]}
                for k in cands:
                    if len(positions) >= max_open:
                        break
                    if k not in allowed:
                        continue
                    # ── 可选的 ML 过滤器（默认 None → 与原版行为完全一致）──
                    if ml_mask is not None:
                        _c = Sd.columns[k]
                        _d = dates[i - 1]
                        try:
                            _ok = bool(ml_mask.at[_d, _c])
                        except KeyError:
                            _ok = True
                        if not _ok:
                            continue
                    # 按当前总权益算每笔金额（复利）
                    equity = cash + sum(
                        pp["stake"] + pp["qty_signed"] * (px[i, kk] - pp["entry_px"])
                        for kk, pp in positions.items())
                    stake = equity * exposure / top_n
                    # ⚠ entry_delay=0 是【原行为】：用第 i 天收盘的 state[i] 决策，
                    #   却在第 i 天开盘成交 —— 这是前视（Codex 评审指出，已证实）。
                    #   entry_delay=1 才是时序正确的：第 i 天收盘决策 → 第 i+1 天开盘成交。
                    fi = min(i + entry_delay, T - 1)
                    entry_px = opx[fi, k] if entry == "next_open" else px[fi, k]
                    if not np.isfinite(entry_px) or entry_px <= 0:
                        continue
                    qty_signed = stake / entry_px * np.sign(cur[k])
                    fee = stake * cost_one
                    if cash < stake + fee:
                        continue
                    cash -= stake + fee
                    positions[k] = {
                        "pair": Cd.columns[k], "stake": stake,
                        "qty_signed": qty_signed, "entry_px": entry_px,
                        "entry_i": fi, "fee_in": fee,
                    }

        # ── ③ 记录权益 ──
        eq_curve[i] = cash + sum(
            pp["stake"] + pp["qty_signed"] * (px[i, kk] - pp["entry_px"])
            for kk, pp in positions.items())

    eq = pd.Series(eq_curve, index=dates)
    ret = eq.pct_change().fillna(0.0)
    return pd.DataFrame(trades), eq, ret


def stats(eq, ret, label=""):
    years = (eq.index[-1] - eq.index[0]).days / 365
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(365)
    mdd = ((eq / eq.cummax()) - 1).min()
    return {"label": label, "years": years, "ann": ann * 100, "vol": vol * 100,
            "mdd": mdd * 100, "calmar": ann / abs(mdd) if mdd < 0 else np.nan,
            "total": (eq.iloc[-1] / eq.iloc[0] - 1) * 100}


def ft_trades(timerange="20230101-20261008"):
    """跑 Freqtrade 并取交易清单"""
    subprocess.run([".venv/bin/python", "-m", "freqtrade", "backtesting",
                    "--config", LIVE, "--strategy", "TrendFollowing",
                    "--timerange", timerange, "--export", "trades"],
                   capture_output=True, text=True)
    f = sorted(glob.glob("user_data/backtest_results/*.zip"),
               key=os.path.getmtime)[-1]
    z = zipfile.ZipFile(f)
    inner = [n for n in z.namelist() if n.endswith(".json") and "meta" not in n][0]
    d = json.loads(z.read(inner))
    st = list(d["strategy"].values())[0]
    ft = pd.DataFrame(st["trades"])
    ft["open_d"] = pd.to_datetime(ft["open_date"], utc=True).dt.tz_localize(None).dt.normalize()
    ft["close_d"] = pd.to_datetime(ft["close_date"], utc=True).dt.tz_localize(None).dt.normalize()
    ft["pair"] = (ft["pair"].str.split("/").str[0])   # AAVE/USDT:USDT → AAVE
    return ft


def main():
    raise SystemExit(
        "LEGACY_DISABLED: event_backtest.py 旧 CLI 调用含前视的 run()，已禁用。"
        "历史源码保留；请使用修正后的 run_v2 或 paper_dryrun.py 离线检验。"
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--start", default="2023-01-01")
    args = ap.parse_args()

    cfg = json.load(open(LIVE))
    coins = [p.split("/")[0] for p in cfg["exchange"]["pair_whitelist"]]
    data = load_ohlc(coins)
    print(f"  币种 {len(data)} · 起点 {args.start}")

    tr, eq, ret = run(data, start=args.start)
    s = stats(eq, ret, "事件驱动模型")
    print()
    print("=" * 84)
    print("事件驱动回测（复刻 Freqtrade 语义）")
    print("=" * 84)
    print(f"  交易 {len(tr)} 笔 · 年化 {s['ann']:.2f}% · 波动 {s['vol']:.1f}% · "
          f"回撤 {s['mdd']:.2f}% · Calmar {s['calmar']:.2f}")

    if not args.verify:
        return

    ft = ft_trades()
    print(f"  Freqtrade {len(ft)} 笔")
    print()
    print("=" * 84)
    print("交易清单对账（同一币对 + 同一开仓日 + 同一方向）")
    print("=" * 84)
    # ⚠️ 日期口径差 1 天：
    #    本研究用「信号当天」，Freqtrade 的 open_date 是「次日成交」。
    #    实测偏移 +1 天时匹配率从 1.5% 跳到 43.8%，确认是这个原因。
    a = {(r["pair"], r["open_date"].date() + pd.Timedelta(days=1),
          "空" if r["is_short"] else "多") for _, r in tr.iterrows()}
    b = {(r["pair"], r["open_d"].date(), "空" if r["is_short"] else "多")
         for _, r in ft.iterrows()}
    inter = a & b
    print(f"  研究(事件驱动) 开仓 {len(a)}")
    print(f"  Freqtrade      开仓 {len(b)}")
    print(f"  完全匹配        {len(inter)}")
    print(f"  匹配率(对研究)  {len(inter)/max(len(a),1)*100:.1f}%")
    print(f"  匹配率(对FT)    {len(inter)/max(len(b),1)*100:.1f}%")
    print()
    print(f"  仅研究有(前5): {sorted(a-b)[:5]}")
    print(f"  仅FT有(前5):   {sorted(b-a)[:5]}")
    # 槽位竞争导致顺序不唯一，匹配率 40%+ 属正常（交易数已基本一致）
    ok = len(inter) / max(len(b), 1) > 0.35
    print()
    print("  " + ("✅ 交易清单基本一致 —— 两套实现是同一个策略"
                  if ok else
                  "❌ 交易清单仍不一致 —— 继续排查"))


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════
#  时序正确的事件驱动引擎 run_v2()
# ══════════════════════════════════════════════════════════════════════
#  为什么要有 v2：旧 run() 有三处时序错误（Codex 评审指出，已由我逐条复核证实）
#    ① 用 state[i]（需第 i 天收盘）决策，却在 opx[i]（第 i 天开盘）成交 → 前视
#    ② 定仓用第 i 天收盘权益 px[i]
#    ③ 平仓块的成交在 opx[i+1]，但 cash += value 在第 i 轮就生效
#       → 第 i 轮开仓能用上还没到手的钱
#
#  v2 的事件顺序（严格按信息可得时点）：
#    第 i-1 日【收盘】→ 生成信号 state[i-1]
#    第 i  日【开盘】→ 依次做四件事，同一次遍历内完成：
#        (1) 用 opx[i] 重估仍持有的仓位
#        (2) 结算【因 state[i-1]==0 而到期】的退出（费用 + 现金释放）
#        (3) 冻结扣费后的组合权益快照 E = cash + Σ持仓价值(按 opx[i])
#        (4) 用 E 为这一批新入场【统一定仓】，受现金与槽位约束后执行
#    未实现/需报告的假设：同一开盘既释放退出资金又成交新单 —— 这是回测执行
#    假设，真实部署能否立即再用资金取决于订单成交与资金可用状态。
# ══════════════════════════════════════════════════════════════════════
def run_v2(data, top_n=8, max_open=10, exposure=0.30, cost_one=0.0005,
           start=None, wallet=10_000.0, chan_entry=20, chan_exit=20,
           ml_mask=None, allow_stale_entry=False, event_sink=None):
    """时序正确的版本（第 2 版 —— 修掉 Codex 复核指出的三处 bug）。

    返回 (trades, equity, ret, diag)。

    ══════════════════════════════════════════════════════════════════
    本版修掉的三处（Codex 独立复核指出，我逐条复现确认）
    ══════════════════════════════════════════════════════════════════
    ① 缺开盘价时用【当日收盘】估值 → 仍是前视
       复现：A 已持仓、D 日 open 缺失；只改 A 的 D 日 close，B 在 D 日的仓位规模就变。
       修法：估值一律用【截至此刻已知的最后有效价】（前一交易日收盘），
             并计入 diag["stale_valuation"]；【成交】则绝不退化 —— 缺价即跳过/挂起。
    ② 缺开盘价的退出没有持久化 → 后来的信号可以取消已触发的退出
       修法：pending_exit 标记；退出信号一旦触发就不可撤销，
             在第一个有有效价的开盘成交。
    ③ 出口手续费公式错：应为 |qty_signed| × exit_px × cost_one，
       而不是 (stake + |PnL|) × cost_one。
       例：多头 stake300/entry10/exit8/1% → 应 −65.4（旧式 −66.6）
           空头 同参数              → 应 +54.6（旧式 +53.4）

    另外：
    · 指标在【完整序列】上算，再限制模拟窗口 —— 不先按 start 截断（保住热身与既存趋势态）
    · 缺价币占着 top_n 槽位时【不自动补位】（不拿下一名顶替），只计 diag
    · 净值曲线含未实现盈亏；已平仓笔数单独披露（diag["closed_trades"]）
    · 支持 ml_mask（Freqtrade-like 的 (date, coin) 允许开仓掩码），供 ML 组合层检验；
      掩码缺少信号日/币列或值无效时 fail-closed（拒绝开仓）
    ══════════════════════════════════════════════════════════════════
    """
    S, ST, P = {}, {}, {}
    for s_, d in data.items():
        # ✅ 指标体系在完整序列上计算（不先按 start 截断），
        #    start 只用来限制【模拟窗口】—— 否则开头几十根没有通道值，会改变信号
        full = d
        S[s_] = signals(full["close"], entry=chan_entry, exit_=chan_exit)
        ST[s_] = strength(full["close"], entry=chan_entry)
        P[s_] = full[["open", "close"]]

    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()

    if start is not None:
        keep = Sd.index >= pd.Timestamp(start)
        Sd, STd, Od, Cd = Sd[keep], STd[keep], Od[keep], Cd[keep]

    dates = Sd.index
    T, K = Sd.shape
    px = Cd.values
    opx = Od.values
    state = Sd.values
    stg = STd.values
    cols = list(Sd.columns)

    def utc_iso(value):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return stamp.isoformat()

    cadence = None
    if len(dates) > 1:
        deltas = pd.Series(dates[1:] - dates[:-1])
        if not deltas.empty:
            cadence = deltas.median()

    cash = wallet
    positions = {}
    trades = []
    eq_curve = np.full(T, np.nan)
    last_px = np.full(K, np.nan)      # 每币【最后已知有效价】
    diag = {"missing_open_fill_skips": 0, "stale_valuation": 0,
            "rejected_no_cash": 0, "frozen_equity_used": 0,
            "pending_exit_days": 0, "topn_slot_blocked_by_missing": 0,
            "ml_mask_blocked": 0}

    def mask_decision(signal_date, coin):
        """Resolve one explicit mask cell; missing or ambiguous cells deny."""
        if not hasattr(ml_mask, "at"):
            return False
        try:
            value = ml_mask.at[signal_date, coin]
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        # Duplicate row/column labels return a Series/DataFrame instead of one
        # scalar. Treat that malformed input as unavailable rather than allow.
        return bool(value) if isinstance(value, (bool, np.bool_)) else False

    def pos_value(k, price):
        p = positions[k]
        return p["stake"] + p["qty_signed"] * (price - p["entry_px"])

    def known_price(k, i):
        """截至第 i 日开盘【已知】的价格：优先当日开盘；缺失则退回最后已知有效价。
        ⚠ 绝不使用 px[i]（当日收盘）—— 那才是前视。"""
        pr = opx[i, k]
        if np.isfinite(pr) and pr > 0:
            return pr, False
        lp = last_px[k]
        if np.isfinite(lp) and lp > 0:
            return lp, True          # True = 陈旧估值
        return np.nan, True

    for i in range(T):
        held_before = [
            {"coin": positions[k]["pair"].split("/")[0],
             "side": "short" if positions[k]["qty_signed"] < 0 else "long",
             "open_rate": float(positions[k]["entry_px"]),
             "stake": float(positions[k]["stake"]),
             "open_candle_utc": utc_iso(dates[positions[k]["entry_i"]])}
            for k in sorted(positions, key=lambda key: cols[key])
        ] if event_sink is not None else None
        # Entry decisions are made from the previous candle's close.  Readiness
        # must therefore use that signal candle, rather than the execution
        # candle whose close is still in the future at order time.
        signal_missing = []
        if i > 0:
            signal_missing = [cols[k].split("/")[0] for k in range(K)
                              if not (np.isfinite(px[i - 1, k]) and px[i - 1, k] > 0)]
        data_ready = i > 0 and not signal_missing
        candle_event = None
        if event_sink is not None:
            candle_event = {
                "event_type": "decision",
                "candle_utc": utc_iso(dates[i - 1]) if i > 0 else utc_iso(dates[i]),
                "decided_at_utc": utc_iso(dates[i - 1] + cadence) if i > 0 and cadence is not None else None,
                "execution_at_utc": utc_iso(dates[i]),
                "data_ready": data_ready,
                "missing": signal_missing,
                "held_before": held_before,
                "slots_before": max(0, max_open - len(positions)),
                "candidates": [],
                "exits": [],
                "decision_reason": ("engine_warmup" if i == 0
                                    else "data_not_ready" if signal_missing else None),
            }
        # 先更新【最后已知有效价】：第 i-1 日收盘在第 i 日开盘时已知
        if i > 0:
            for k in range(K):
                c_prev = px[i - 1, k]
                if np.isfinite(c_prev) and c_prev > 0:
                    last_px[k] = c_prev

        if i == 0:
            eq_curve[i] = cash
            if candle_event is not None:
                candle_event["held_after"] = []
                event_sink.append(candle_event)
            continue

        prev_state = state[i - 1]

        # ── (1) 结算退出：pending_exit 优先，且退出信号【不可撤销】──
        for k in list(positions.keys()):
            p = positions[k]
            want_exit = p.get("pending_exit", False) or (prev_state[k] == 0.0)
            if not want_exit:
                continue
            exit_px = opx[i, k]
            if not np.isfinite(exit_px) or exit_px <= 0:
                # ② 缺价 → 挂起，等第一个有效价；【不能】让后续信号把它取消
                p["pending_exit"] = True
                diag["missing_open_fill_skips"] += 1
                diag["pending_exit_days"] += 1
                if candle_event is not None:
                    candle_event["exits"].append({
                        "coin": p["pair"].split("/")[0],
                        "reason": "trend_end_pending_missing_open",
                        "status": "pending",
                        "actual_fill": None,
                    })
                continue
            positions.pop(k)
            value = p["stake"] + p["qty_signed"] * (exit_px - p["entry_px"])
            # ③ 正确的手续费：按成交名义额 |qty| × price × rate
            fee = abs(p["qty_signed"]) * exit_px * cost_one
            cash += value - fee
            pnl = value - p["stake"] - p["fee_in"] - fee
            trades.append({
                "pair": p["pair"].split("/")[0], "open_i": p["entry_i"], "close_i": i,
                "open_date": dates[p["entry_i"]], "close_date": dates[i],
                "is_short": p["qty_signed"] < 0,
                "open_rate": p["entry_px"], "close_rate": exit_px,
                "profit_abs": pnl,
                "profit_pct": pnl / p["stake"] * 100 if p["stake"] else 0,
                "stake": p["stake"],            # 供外部核验手续费公式
                "exit_reason": "trend_end",
                "was_pending": bool(p.get("pending_exit", False)),
            })
            if event_sink is not None:
                event_sink.append({
                    "event_type": "fill",
                    "action": "exit",
                    "coin": p["pair"].split("/")[0],
                    "side": "short" if p["qty_signed"] < 0 else "long",
                    "candle_utc": utc_iso(dates[i - 1]),
                    "filled_at_utc": utc_iso(dates[i]),
                    "price": float(exit_px),
                    "quantity": float(abs(p["qty_signed"])),
                    "fee": float(fee),
                    "profit_abs": float(pnl),
                    "was_pending": bool(p.get("pending_exit", False)),
                })
            if candle_event is not None:
                candle_event["exits"].append({
                    "coin": p["pair"].split("/")[0],
                    "reason": "trend_end" if not p.get("pending_exit", False) else "pending_exit_filled",
                    "status": "filled",
                    "actual_fill": {"price": float(exit_px), "filled_at_utc": utc_iso(dates[i])},
                })

        # ── (2)(3) 用【已知价】重估持仓，冻结扣费后权益快照 E ──
        held_val = 0.0
        for k in positions:
            pr, stale = known_price(k, i)
            if not np.isfinite(pr) or pr <= 0:
                pr = positions[k]["entry_px"]     # 完全无价：退到成本，最保守
                stale = True
            if stale:
                diag["stale_valuation"] += 1
            held_val += pos_value(k, pr)
        E = cash + held_val
        if positions:
            diag["frozen_equity_used"] += 1

        # ── (4) 用冻结的 E 为这批新入场统一定仓 ──
        # Fail closed for a partial whitelist: a missing signal candle for any
        # symbol blocks the whole new-entry batch. Existing positions still
        # follow their exit/pending-exit path above.
        allowed = [k for k in range(K) if prev_state[k] != 0] if data_ready else []
        if ml_mask is not None:
            keep = []
            for k in allowed:
                coin = cols[k].split("/")[0]
                if mask_decision(dates[i - 1], coin):
                    keep.append(k)
                else:
                    diag["ml_mask_blocked"] += 1
            allowed = keep
        if allowed:
            vals = {k: stg[i - 1, k] for k in allowed if np.isfinite(stg[i - 1, k])}
            ranked = sorted(vals.items(), key=lambda kv: (-kv[1], cols[kv[0]]))
            chosen = [k for k, _ in ranked[:top_n]]
            rank_by_coin = {k: rank for rank, (k, _) in enumerate(ranked, start=1)}
            candidate_rows = {}
            if candle_event is not None:
                for k, score in ranked:
                    candidate_rows[k] = {
                        "coin": cols[k].split("/")[0],
                        "side": "short" if prev_state[k] < 0 else "long",
                        "strength": float(score),
                        "rank": rank_by_coin[k],
                        "decision": None,
                        "reason": None,
                        "expected_fee": None,
                        "actual_fill": None,
                    }
            stake = E * exposure / max(top_n, 1)
            fee_one = stake * cost_one
            for k, _ in ranked:
                row = candidate_rows.get(k) if candidate_rows is not None else None
                if k not in chosen:
                    if row is not None:
                        row.update(decision="deny", reason="outside_top_n")
                    continue
                if k in positions:
                    if row is not None:
                        row.update(decision="deny", reason="already_held")
                    continue
                if len(positions) >= max_open:
                    if row is not None:
                        row.update(decision="deny", reason="max_open")
                    continue
                ep = opx[i, k]
                if not np.isfinite(ep) or ep <= 0:
                    # 缺价币占着槽位：计数告警，【不自动补位】（不拿下一名顶替）
                    diag["topn_slot_blocked_by_missing"] += 1
                    diag["missing_open_fill_skips"] += 1
                    if row is not None:
                        row.update(decision="deny", reason="missing_open_price")
                    continue
                if cash < stake + fee_one:
                    diag["rejected_no_cash"] += 1
                    if row is not None:
                        row.update(decision="deny", reason="insufficient_cash")
                    continue
                cash -= stake + fee_one
                positions[k] = {
                    "pair": cols[k], "stake": stake,
                    "qty_signed": stake / ep * np.sign(prev_state[k]),
                    "entry_px": ep, "entry_i": i, "fee_in": fee_one,
                    "pending_exit": False,
                }
                if row is not None:
                    row.update(decision="allow", reason="top_n_and_slot_available",
                               expected_fee=float(fee_one),
                               actual_fill={"status": "filled", "price": float(ep),
                                            "filled_at_utc": utc_iso(dates[i])})
                if event_sink is not None:
                    event_sink.append({
                        "event_type": "fill",
                        "action": "entry",
                        "coin": cols[k].split("/")[0],
                        "side": "short" if prev_state[k] < 0 else "long",
                        "candle_utc": utc_iso(dates[i - 1]),
                        "filled_at_utc": utc_iso(dates[i]),
                        "price": float(ep),
                        "quantity": float(abs(positions[k]["qty_signed"])),
                        "stake": float(stake),
                        "fee": float(fee_one),
                    })
            if candle_event is not None:
                candle_event["candidates"] = [candidate_rows[k] for k, _ in ranked]
                if not ranked:
                    candle_event["decision_reason"] = "no_rankable_candidates"
        elif candle_event is not None and candle_event["decision_reason"] is None:
            candle_event["decision_reason"] = "no_signal_candidates"

        # ── 收盘净值（含未实现盈亏）──
        close_val = 0.0
        for k in positions:
            pr = px[i, k]
            if not (np.isfinite(pr) and pr > 0):
                pr = last_px[k]
            if not (np.isfinite(pr) and pr > 0):
                pr = positions[k]["entry_px"]
            close_val += pos_value(k, pr)
        eq_curve[i] = cash + close_val
        if candle_event is not None:
            candle_event["held_after"] = [
                {"coin": positions[k]["pair"].split("/")[0],
                 "side": "short" if positions[k]["qty_signed"] < 0 else "long",
                 "open_rate": float(positions[k]["entry_px"]),
                 "stake": float(positions[k]["stake"]),
                 "open_candle_utc": utc_iso(dates[positions[k]["entry_i"]])}
                for k in sorted(positions, key=lambda key: cols[key])
            ]
            event_sink.append(candle_event)

    eq = pd.Series(eq_curve, index=dates)
    ret = eq.pct_change()
    diag["closed_trades"] = len(trades)
    diag["open_positions_at_end"] = len(positions)
    # ✅ 期末未平仓【明细】—— Codex 要求保留的边界证据。
    #   若只给计数，外部就无法观察"这些仓位的进场事件"，
    #   会让扰动测试把"留到期末因而未进 trades"的进场误判为缺失
    #   （我第一版 T2 就栽在这里）。
    diag["open_positions"] = [
        {"pair": positions[k]["pair"].split("/")[0],
         "entry_i": int(positions[k]["entry_i"]),
         "open_date": dates[positions[k]["entry_i"]].strftime("%Y-%m-%d"),
         "open_rate": float(positions[k]["entry_px"]),
         "stake": float(positions[k]["stake"]),
         "is_short": bool(positions[k]["qty_signed"] < 0),
         "pending_exit": bool(positions[k].get("pending_exit", False))}
        for k in positions]
    return pd.DataFrame(trades), eq, ret, diag


# ══════════════════════════════════════════════════════════════════════
#  分项消融引擎 —— 用同一个引擎，每次只回退【一项】修正
# ══════════════════════════════════════════════════════════════════════
#  为什么需要它：Codex 指出 34.19→20.15 的归因不能由总收益差定性 ——
#  因为 run_v2 相对 run() 同时改了四项（信号/强度对齐、按强度成交顺序、
#  权益冻结、现金时序），而且 entry_delay=1 那一版还混用了 state[i] 与
#  strength[i-1]、提前记录未来成交头寸，不是干净的单项对照。
#
#  本函数把这四项各自做成开关，默认【全部修正】（= 正确行为），
#  回退任一项即可测出该项的单独贡献。这样归因是引擎内的对照，不是跨引擎比较。
# ══════════════════════════════════════════════════════════════════════
def run_ablate(data, top_n=8, max_open=10, exposure=0.30, cost_one=0.0005,
               start=None, wallet=10_000.0, chan_entry=20, chan_exit=20,
               # ── 四项修正的开关（True = 采用修正）──
               fix_align=True,        # 入场信号对齐：用 state[i-1] 决策、opx[i] 成交
               legacy_rank_basis=False,  # True = 旧 run() 的排序基准：在 state[i-1]!=0 的全部币里排名
               legacy_iter_order=False,  # True = 旧 run() 的遍历顺序：按列下标（而非强度降序）
               fix_exit_align=True,   # 平仓信号对齐：用 state[i-1] 决策、opx[i] 成交
                                      #   False = 旧 run() 的行为：state[i] 决策 → opx[i+1] 成交
                                      #   （注意 False 不是"更保守"，而是"滞后一天"，两者都无前视；
                                      #     真正的差别在于用哪根 K 线的状态做决策）
               fix_cash_timing=True,  # 现金时序：平仓现金在【成交那一轮】才可用
               fix_equity=True,       # 权益口径：用开盘价重估、且不用当日收盘
               fix_fee=True):         # 手续费：|qty|×price×rate
    """分项消融。返回 (trades, equity, ret, diag)。"""
    S, ST, P = {}, {}, {}
    for s_, d in data.items():
        S[s_] = signals(d["close"], entry=chan_entry, exit_=chan_exit)
        ST[s_] = strength(d["close"], entry=chan_entry)
        P[s_] = d[["open", "close"]]
    Sd = pd.DataFrame(S).sort_index().fillna(0.0)
    STd = pd.DataFrame(ST).sort_index()
    Od = pd.DataFrame({k: v["open"] for k, v in P.items()}).sort_index()
    Cd = pd.DataFrame({k: v["close"] for k, v in P.items()}).sort_index()
    if start is not None:
        keep = Sd.index >= pd.Timestamp(start)
        Sd, STd, Od, Cd = Sd[keep], STd[keep], Od[keep], Cd[keep]

    dates = Sd.index
    T, K = Sd.shape
    px, opx = Cd.values, Od.values
    state, stg = Sd.values, STd.values
    cols = list(Sd.columns)

    cash = wallet
    positions = {}
    trades = []
    pending_cash = 0.0            # 用于模拟「现金提前一轮可用」
    eq_curve = np.full(T, np.nan)
    last_px = np.full(K, np.nan)
    diag = {"closed_trades": 0}

    def pos_value(k, price):
        p = positions[k]
        return p["stake"] + p["qty_signed"] * (price - p["entry_px"])

    for i in range(T):
        if i > 0:
            for k in range(K):
                c = px[i - 1, k]
                if np.isfinite(c) and c > 0:
                    last_px[k] = c
        if i == 0:
            eq_curve[i] = cash
            continue

        # fix_align=False → 回到旧行为：用 state[i]（需当日收盘）决策
        dec_state = state[i - 1] if fix_align else state[i]
        # fix_equity=False → 估值/定仓回退到当日收盘
        val_px = 0 if fix_equity else 1

        # 若禁用现金时序修正：把上一轮挂账的现金先放回（= 提前一轮可用）
        if not fix_cash_timing and pending_cash:
            cash += pending_cash
            pending_cash = 0.0
        # ⚠ Codex 复核指出：原先 fix_cash_timing=False 时，平仓款进了 pending_cash
        #   而 E = cash + held 里【既不含 cash 也不含 held】（仓位已 pop）
        #   → 那笔应收款在权益里凭空消失一天。
        #   结果：−5.72pp 的归因混了【现金何时可用于新单】与【应收款是否计入权益】
        #   两个效应。按它要求先修权益计量，再重新归因。
        #   修法：应收款【计入权益 E】，但【不参与下单能力】—— 两者分离。

        # ── 退出 ──
        # 平仓侧的信号-成交对齐：fix_exit_align=True 用 state[i-1] 决策、opx[i] 成交；
        # False 复刻旧 run() 的 state[i] 决策 + opx[i+1] 成交（滞后一天，无前视）。
        exit_state = state[i - 1] if fix_exit_align else state[i]
        exit_fill_i = i if fix_exit_align else min(i + 1, T - 1)
        for k in list(positions.keys()):
            p = positions[k]
            if not (p.get("pending_exit", False) or exit_state[k] == 0.0):
                continue
            exit_px = opx[exit_fill_i, k]
            if not (np.isfinite(exit_px) and exit_px > 0):
                if val_px:
                    exit_px = px[i, k]          # 旧行为：退到当日收盘
                else:
                    p["pending_exit"] = True
                    continue
            positions.pop(k)
            value = p["stake"] + p["qty_signed"] * (exit_px - p["entry_px"])
            if fix_fee:
                fee = abs(p["qty_signed"]) * exit_px * cost_one
            else:
                fee = (p["stake"] + abs(value - p["stake"])) * cost_one
            if fix_cash_timing:
                cash += value - fee
            else:
                pending_cash += value - fee     # 挂到下一轮才可用（= 旧行为）
            pnl = value - p["stake"] - p["fee_in"] - fee
            trades.append({
                "pair": p["pair"].split("/")[0], "open_i": p["entry_i"],
                "close_i": exit_fill_i,
                "open_date": dates[p["entry_i"]], "close_date": dates[exit_fill_i],
                "is_short": p["qty_signed"] < 0,
                "open_rate": p["entry_px"], "close_rate": exit_px,
                "profit_abs": pnl,
                "profit_pct": pnl / p["stake"] * 100 if p["stake"] else 0,
                "stake": p["stake"],
                "exit_reason": "trend_end",
            })

        # ── 权益快照 ──
        held = 0.0
        for k in positions:
            if val_px:
                pr = px[i, k]
            else:
                pr, _ = (opx[i, k], False) if (np.isfinite(opx[i, k]) and opx[i, k] > 0) \
                    else (last_px[k], True)
            if not (np.isfinite(pr) and pr > 0):
                pr = positions[k]["entry_px"]
            held += pos_value(k, pr)
        # ✅ 权益 = 已结算现金 + 应收款 + 持仓市值（应收款不再凭空消失）
        E = cash + held + (0.0 if fix_cash_timing else pending_cash)
        # ✅ 但【下单能力】只用已结算现金（这才是 fix_cash_timing 要测的效应）
        usable_cash = cash if fix_cash_timing else cash

        # ── 入场 ──
        allowed = [k for k in range(K) if dec_state[k] != 0]
        if allowed:
            # ① 排序基准：旧 run() 在 state[i-1]!=0 的【全部】币里排名，
            #    而不是只在当日候选里 —— 这会改变 top_n 的边界成员。
            basis = [k for k in range(K) if state[i - 1, k] != 0] if legacy_rank_basis else allowed
            vals = {k: stg[i - 1, k] for k in basis if np.isfinite(stg[i - 1, k])}
            ranked = sorted(vals.items(), key=lambda kv: (-kv[1], cols[kv[0]]))
            stake = E * exposure / max(top_n, 1)
            fee_one = stake * cost_one
            # ② 遍历顺序：旧 run() 按列下标遍历 cands，而非按强度降序 ——
            #    max_open 绑定时决定谁被填满。
            if legacy_iter_order:
                chosen = [k for k in range(K) if k in dict(ranked[:top_n])]
            else:
                chosen = [k for k, _ in ranked[:top_n]]
            for k in chosen:
                if len(positions) >= max_open:
                    break
                if k in positions:
                    continue
                ep = opx[i, k]
                if not (np.isfinite(ep) and ep > 0):
                    continue
                if usable_cash < stake + fee_one:
                    continue
                cash -= stake + fee_one
                usable_cash -= stake + fee_one
                positions[k] = {"pair": cols[k], "stake": stake,
                                "qty_signed": stake / ep * np.sign(dec_state[k]),
                                "entry_px": ep, "entry_i": i, "fee_in": fee_one,
                                "pending_exit": False}

        close_val = 0.0
        for k in positions:
            pr = px[i, k]
            if not (np.isfinite(pr) and pr > 0):
                pr = last_px[k]
            if not (np.isfinite(pr) and pr > 0):
                pr = positions[k]["entry_px"]
            close_val += pos_value(k, pr)
        # ✅ 净值也必须含应收款 —— 与上面的权益 E 同一口径，
        #    否则平仓当天净值会凭空下跳一天（同一 bug 的第二个表现）。
        eq_curve[i] = cash + close_val + (0.0 if fix_cash_timing else pending_cash)

    eq = pd.Series(eq_curve, index=dates)
    diag["closed_trades"] = len(trades)
    diag["open_positions_at_end"] = len(positions)
    return pd.DataFrame(trades), eq, eq.pct_change(), diag
