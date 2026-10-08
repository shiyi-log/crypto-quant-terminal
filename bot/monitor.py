#!/usr/bin/env python3
"""
实盘运维与持续迭代（运维层）

回答四个问题：
    1. 策略现在在干什么？          → 持仓、信号距离、下一根 K 线什么时候来
    2. 策略的健康度如何？          → 回撤状态、波动率中枢漂移
    3. 因子还灵吗？                → 滚动 IC 监控 + 衰减预警
    4. 模型该重估了吗？            → 重估触发条件

设计原则（来自本项目的教训）：
    · 任何指标都用【扩张窗口】或【滚动窗口】，绝不使用全样本统计
    · 因子 IC 用自相关修正后的 t 值，避免"看起来显著"
    · 监控的是【漂移】，不是【水平】—— 水平会随市场变化，漂移才是信号

输出：
    user_data/ops_status.json —— 供面板读取

用法:
    python monitor.py                # 生成一次快照
    python monitor.py --watch 3600   # 每小时刷新（常驻）
"""

import argparse
import glob
import json
import os
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

PERP = "user_data/data/binance/futures"


def to_py(o):
    """递归把 numpy 类型转成 Python 原生类型（json 不支持 numpy.bool_/float64）"""
    if isinstance(o, dict):
        return {k: to_py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_py(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.ndarray):
        return to_py(o.tolist())
    return o
OUT = "user_data/ops_status.json"
HIST = "user_data/ops_history.jsonl"      # 历史快照（追加），用于看漂移趋势
HIST_MAX = 2000                            # 保留条数上限
FT_API = "http://127.0.0.1:8889"


# ══════════════════ 策略状态 ══════════════════

def load_pairs():
    p = "user_data/config_trend_live.json"
    if not os.path.exists(p):
        return [], {}
    c = json.load(open(p))
    return c["exchange"]["pair_whitelist"], c


def signal_distance(df: pd.DataFrame, entry=20, exit_=20):
    """距突破还有多远（0%=在通道底部，100%=在顶部；突破则超出区间）"""
    d = df.copy()
    d["hh"] = d["high"].rolling(entry).max().shift(1)
    d["ll"] = d["low"].rolling(entry).min().shift(1)
    last = d.iloc[-1]
    if not np.isfinite(last["hh"]) or not np.isfinite(last["ll"]):
        return None
    if last["close"] > last["hh"]:
        return {"pos": 100.0, "signal": "long",
                "need_pct": (last["hh"] / last["close"] - 1) * 100}
    if last["close"] < last["ll"]:
        return {"pos": 0.0, "signal": "short",
                "need_pct": (last["ll"] / last["close"] - 1) * 100}
    span = last["hh"] - last["ll"]
    pos = (last["close"] - last["ll"]) / span * 100 if span > 0 else 50
    return {"pos": round(pos, 1), "signal": None,
            "need_up": round((last["hh"] / last["close"] - 1) * 100, 2),
            "need_dn": round((1 - last["ll"] / last["close"]) * 100, 2)}


def strategy_state():
    pairs, cfg = load_pairs()
    rows = []
    for pair in pairs:
        sym = pair.split("/")[0]
        f = f"{PERP}/{sym}_USDT_USDT-1d-futures.feather"
        if not os.path.exists(f):
            continue
        d = pd.read_feather(f).sort_values("date")
        d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_localize(None)
        d = d.set_index("date")
        sig = signal_distance(d)
        if sig is None:
            continue
        ret = d["close"].pct_change()
        vol30 = ret.tail(30).std() * np.sqrt(365) * 100
        atr20 = d["close"].pct_change().abs().tail(20).mean() * 100
        rows.append({
            "pair": pair, "price": float(d["close"].iloc[-1]),
            "date": str(d.index[-1].date()),
            "vol30_pct": round(float(vol30), 1),
            "atr_pct": round(float(atr20), 2),
            "suggested_scale": round(min(0.01 / (atr20 / 100), 5.0), 2) if atr20 > 0 else None,
            **sig,
        })
    rows.sort(key=lambda r: -(r.get("pos") or 0))
    return rows


# ══════════════════ 因子健康度（滚动 IC） ══════════════════

def rolling_factor_health(window=500, horizon=7):
    """
    对每个因子，用最近 window 期计算滚动 IC，
    并与全历史基线对比 —— 看的是【漂移】，不是绝对水平。
    """
    import factor_lib as fl
    panels, C = fl.build_panels("1d")
    F = fl.forward_returns(C, horizon)
    out = []
    for grp, names in fl.FACTOR_GROUPS.items():
        for n in names:
            if n not in panels:
                continue
            ic = fl.cross_sectional_ic(panels[n], F).dropna()
            if len(ic) < window + 100:
                continue
            recent = ic.tail(window)
            base = ic.iloc[:-window]
            if len(base) < 100:
                continue
            r_ic, b_ic = float(recent.mean()), float(base.mean())
            r_ir = r_ic / recent.std() if recent.std() > 0 else np.nan
            b_ir = b_ic / base.std() if base.std() > 0 else np.nan
            ac = max(min(recent.autocorr(1), 0.98), -0.98)
            n_eff = len(recent) * (1 - ac) / (1 + ac)
            t = r_ir * np.sqrt(max(n_eff, 1))
            # 漂移判定：必须【基线本身有意义】才谈得上反转/衰减
            # 否则 rev_7d 这种 IC 只有 ±0.02 的因子上会满屏假阳性
            meaningful = abs(b_ic) >= 0.03          # 基线 IC 至少 0.03 才算有信号
            flip = bool(meaningful and np.sign(r_ic) != np.sign(b_ic) and abs(r_ic) > 0.02)
            decay = bool(meaningful and abs(r_ic) < abs(b_ic) * 0.5)
            if not meaningful:
                status = "弱信号(基线IC<0.03)"
            else:
                status = "⚠ 符号反转" if flip else ("⚠ 衰减过半" if decay else "正常")
            out.append({
                "factor": n, "group": grp,
                "ic_base": round(b_ic, 4), "ic_recent": round(r_ic, 4),
                "t_recent": round(t, 2),
                "drift": round(r_ic - b_ic, 4),
                "status": status,
                # 结构化标志：前端按标志过滤/判定，不再依赖 status 的中文文案
                "weak": bool(not meaningful),
                "flip": flip,
                "decay": decay,
                "alert": bool(flip or (abs(t) > 2 and abs(r_ic) > 0.02)),
            })
    out.sort(key=lambda x: x["t_recent"])
    return out


# ══════════════════ 波动率中枢漂移 ══════════════════

def vol_regime():
    """市场波动率中枢是否漂移 —— 它变了，仓位规模就要重算"""
    files = glob.glob(f"{PERP}/*-1d-futures.feather")
    vols = {}
    for f in files[:60]:
        sym = os.path.basename(f).split("_")[0]
        try:
            d = pd.read_feather(f).sort_values("date")
            d["date"] = pd.to_datetime(d["date"], utc=True)
            r = d.set_index("date")["close"].astype(float).pct_change()
            vols[sym] = r
        except Exception:
            continue
    idx = pd.DataFrame(vols).sort_index()
    mkt = idx.mean(axis=1)
    v20 = mkt.rolling(20).std() * np.sqrt(365) * 100
    v90 = mkt.rolling(90).std() * np.sqrt(365) * 100
    cur20 = float(v20.iloc[-1])
    cur90 = float(v90.iloc[-1])
    hist = v90.dropna()
    pct = float((hist < cur90).mean() * 100) if len(hist) else np.nan
    return {
        "vol_20d_pct": round(cur20, 1),
        "vol_90d_pct": round(cur90, 1),
        "vol_90d_percentile": round(pct, 1),
        "regime": "高波动" if pct > 75 else ("低波动" if pct < 25 else "正常"),
        "scale_hint": round(15.0 / cur90, 2) if cur90 > 0 else None,
    }


# ══════════════════ 重估触发 ══════════════════

def retrain_signal(factors, vol):
    """判断是否需要重估/重训，以及原因"""
    reasons = []
    if factors:
        flipped = [f for f in factors if f["status"] == "⚠ 符号反转"]
        if flipped:
            reasons.append(f"{len(flipped)} 个因子符号反转: " +
                           ", ".join(f["factor"] for f in flipped[:4]))
        decayed = [f for f in factors if f["status"] == "⚠ 衰减过半"]
        if len(decayed) >= 3:
            reasons.append(f"{len(decayed)} 个因子 IC 衰减过半")
    if vol and vol.get("vol_90d_percentile") is not None:
        if vol["vol_90d_percentile"] > 90:
            reasons.append(f"波动率处于历史 {vol['vol_90d_percentile']:.0f}% 分位（偏高，应降低仓位）")
        elif vol["vol_90d_percentile"] < 10:
            reasons.append(f"波动率处于历史 {vol['vol_90d_percentile']:.0f}% 分位（偏低）")
    return {"need_retrain": bool(len(reasons) > 0), "reasons": reasons}


# ══════════════════ 输出 ══════════════════

def snapshot(full=True):
    t0 = time.time()
    st = strategy_state()
    _, cfg = load_pairs()
    vol = vol_regime()
    factors = rolling_factor_health() if full else []
    write_hist = full        # 快速模式缺因子数据，不写历史（否则污染趋势序列）
    rs = retrain_signal(factors, vol)

    # 信号接近突破的币（距离 < 3%）
    near = [r for r in st if r.get("signal") or
            (r.get("need_up") is not None and r["need_up"] < 3) or
            (r.get("need_dn") is not None and r["need_dn"] < 3)]

    data = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "strategy": {
            # 全部来自实盘 config，避免面板显示与真实配置脱节
            "name": cfg.get("strategy", "—"),
            "timeframe": cfg.get("timeframe", "—"),
            "dry_run": bool(cfg.get("dry_run", False)),
            "max_open_trades": cfg.get("max_open_trades"),
            "pairs": len(st),
            "near_breakout": near,
            "all": st,
        },
        "vol_regime": vol,
        "factors": factors,
        "retrain": rs,
    }
    payload = to_py(data)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # ── 追加历史（只存关键指标，避免文件膨胀）──
    try:
        if not write_hist:
            raise RuntimeError("skip")
        rec = {
            "t": payload["generated_at"],
            "vol_90d_pct": payload["vol_regime"]["vol_90d_pct"],
            "vol_pctile": payload["vol_regime"]["vol_90d_percentile"],
            "alerts": sum(1 for f in payload["factors"] if f["alert"]),
            "need_retrain": payload["retrain"]["need_retrain"],
            "near": len(payload["strategy"]["near_breakout"]),
            # 核心因子的近期 IC，用来看衰减
            "ic": {f["factor"]: f["ic_recent"] for f in payload["factors"][:8]},
        }
        lines = []
        if os.path.exists(HIST):
            with open(HIST, encoding="utf-8") as fh:
                lines = fh.readlines()
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
        if len(lines) > HIST_MAX:
            lines = lines[-HIST_MAX:]
        with open(HIST, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except Exception as exc:
        print(f"  ⚠ 历史写入失败: {exc}")

    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="常驻刷新间隔秒数")
    ap.add_argument("--daemon", action="store_true",
                    help="常驻模式：默认每 1800 秒刷新一次（供 start.sh 调用）")
    ap.add_argument("--quick", action="store_true", help="跳过因子健康度（快）")
    args = ap.parse_args()

    if args.daemon and not args.watch:
        args.watch = 1800

    while True:
        d = snapshot(full=not args.quick)
        print(f"\n{'='*96}")
        print(f"实盘运维快照  {d['generated_at']}  ({d['elapsed_s']}s)")
        print("=" * 96)
        s = d["strategy"]
        print(f"  策略 {s['name']} · {s['timeframe']} · {s['pairs']} 币")
        print(f"  接近突破的币: {len(s['near_breakout'])} 个")
        for r in s["near_breakout"][:6]:
            if r.get("signal"):
                print(f"    ▲ {r['pair']:<16} 已突破 → {r['signal']}  波动 {r['vol30_pct']:.0f}%")
            else:
                print(f"    · {r['pair']:<16} 距上破 {r.get('need_up')}%  距下破 {r.get('need_dn')}%"
                      f"  通道位置 {r['pos']}%")
        v = d["vol_regime"]
        print(f"\n  波动率中枢: 20日 {v['vol_20d_pct']}%  90日 {v['vol_90d_pct']}%"
              f"  历史分位 {v['vol_90d_percentile']}%  → {v['regime']}"
              f"  建议仓位系数 {v['scale_hint']}")
        if d["factors"]:
            print(f"\n  因子健康度（最近 500 期滚动 IC vs 全历史基线）:")
            print(f"    {'因子':<18}{'基线IC':>10}{'近期IC':>10}{'漂移':>10}{'修正t':>9}  状态")
            print("    " + "-" * 68)
            for f in d["factors"][:12]:
                print(f"    {f['factor']:<18}{f['ic_base']:>10.4f}{f['ic_recent']:>10.4f}"
                      f"{f['drift']:>10.4f}{f['t_recent']:>9.2f}  {f['status']}")
            alerts = [f for f in d["factors"] if f["alert"]]
            print(f"    {'':<18}告警 {len(alerts)} 个")
        r = d["retrain"]
        print(f"\n  模型重估: {'⚠ 需要' if r['need_retrain'] else '✅ 不需要'}")
        for x in r["reasons"]:
            print(f"    · {x}")
        print(f"\n  已写入 {OUT}")
        try:
            if os.path.exists(HIST):
                with open(HIST, encoding="utf-8") as fh:
                    hist = [json.loads(x) for x in fh if x.strip()]
                if len(hist) > 1:
                    print(f"\n  历史趋势（最近 {min(len(hist),10)} 次快照）:")
                    print(f"    {'时间':<21}{'90日波动':>10}{'分位':>8}{'告警':>7}{'重估':>8}")
                    for h in hist[-10:]:
                        print(f"    {h['t']:<21}{h['vol_90d_pct']:>9.1f}%{h['vol_pctile']:>7.1f}%"
                              f"{h['alerts']:>7}{'是' if h['need_retrain'] else '否':>8}")
        except Exception:
            pass

        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
