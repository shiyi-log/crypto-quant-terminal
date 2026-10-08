#!/usr/bin/env python3
"""
生成「策略研究」面板数据

把全部研究结论汇总成一个 JSON，供前端 /api/locals/research 读取：
  1. 模型对比（各 FreqAI 模型的回测结果 + 决策质量指标）
  2. 路线对比（每条研究方向的证据与结论）
  3. Carry 回测（参数扫描 + 强平风险 + 收益拆解）

用法: python build_research.py
输出: user_data/research_summary.json
"""

import glob
import io
import json
import os
import contextlib

import numpy as np
import pandas as pd

import carry_backtest as cb
import carry_sim as cs

OUT = "user_data/research_summary.json"
MODELS = "user_data/models"
FEE_ROUND_TRIP = 0.0010


# ══════════ 1. 模型对比 ══════════
MODEL_LABEL = {
    "universe15-mlp-1h-7d": "PyTorch MLP",
    "universe15-xgb-1h-7d": "XGBoost",
    "universe15-trf-1h-7d": "PyTorch Transformer",
    "btc-eth-funding-1h-7d": "LightGBM（15币）",
    "btc-eth-funding-v1": "LightGBM（2币）",
}


def build_models():
    out = []
    for f in sorted(glob.glob(f"{MODELS}/*/backtest_result.json")):
        ident = os.path.basename(os.path.dirname(f))
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        r = d.get("result") or {}
        # 决策质量从逐笔明细取（更准）
        ic = hit = None
        dp = os.path.join(os.path.dirname(f), "backtest_detail.json")
        if os.path.exists(dp):
            try:
                s = json.load(open(dp, encoding="utf-8"))["summary"]
                ic, hit = s.get("pred_vs_actual_ic"), s.get("sign_hit_rate")
            except Exception:
                pass
        out.append({
            "identifier": ident,
            "label": MODEL_LABEL.get(ident, ident),
            "trades": r.get("trades"),
            "profit_pct": r.get("profit_pct"),
            "winrate": r.get("winrate"),
            "max_drawdown_pct": r.get("max_drawdown_pct"),
            "profit_factor": r.get("profit_factor"),
            "pred_ic": ic,
            "sign_hit_rate": hit,
            "verdict": "证伪" if (r.get("profit_pct") or 0) < 0 else "待观察",
        })
    out.sort(key=lambda x: x.get("profit_pct") or -999, reverse=True)
    return out


# ══════════ 2. 路线对比 ══════════
ROUTES = [
    {
        "name": "ML 方向预测（FreqAI）",
        "evidence": "4 种模型 × 2 个周期全部巨亏；预测与实际收益的 IC 为负；方向命中率全部低于 50%",
        "annual": "-17% ~ -74%",
        "verdict": "已证伪",
        "level": "bad",
    },
    {
        "name": "下单量预测方向",
        "evidence": "方向 IC ≤0.05，低于双边成本 0.10%；主动买卖失衡在 1h 甚至是反向的",
        "annual": "负",
        "verdict": "已证伪",
        "level": "bad",
    },
    {
        "name": "横截面市场中性",
        "evidence": "合法标准化下全负；早期为正的结果经查是前视偏差",
        "annual": "负",
        "verdict": "已证伪",
        "level": "bad",
    },
    {
        "name": "周级价格动量",
        "evidence": "扣费后 +0.063%/周，统计上不显著",
        "annual": "~3%",
        "verdict": "鸡肋",
        "level": "warn",
    },
    {
        "name": "下单量做波动率风控",
        "evidence": "成交笔数对波动率 IC 0.351、成交量 IC 0.250（可靠）；但只能减小亏损，不能转正",
        "annual": "辅助",
        "verdict": "可用作辅助",
        "level": "warn",
    },
    {
        "name": "反着做（翻转多空）",
        "evidence": "窗口内 IC 是 +0.22，模型并未判反；看似支持反向的 -0.965% 价差是跨窗口尺度差异造成的假象",
        "annual": "负",
        "verdict": "已证伪",
        "level": "bad",
    },
    {
        "name": "去偏置（滚动分位替代绝对阈值）",
        "evidence": "不重叠回测 -1.52%/期、t=-2.80、两年都负；看似显著的 +2.651% 是重叠样本导致的 t 值虚高",
        "annual": "负",
        "verdict": "已证伪",
        "level": "bad",
    },
    {
        "name": "资金费率 Delta 中性 Carry",
        "evidence": "结构性正期望；基差侵蚀≈0；回撤极低；但强平风险决定成败",
        "annual": "1.0% ~ 5.5%",
        "verdict": "推荐主攻",
        "level": "good",
    },
]


# ══════════ 3. Carry 回测 ══════════
def build_carry():
    cash = {}
    sweep = []
    for reb, topn, lev in [(30, 5, 1), (30, 5, 2), (30, 5, 3), (14, 8, 3), (7, 5, 3)]:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                df = cb.run(topn, reb, lev, verbose=False)
        except SystemExit:
            continue
        if df is None or df.empty:
            continue
        eq = (1 + df["net_on_capital_pct"] / 100).cumprod()
        days = (df["t_end"].iloc[-1] - df["t"].iloc[0]).days
        years = days / 365
        total = eq.iloc[-1] - 1
        sweep.append({
            "rebalance_days": reb, "topn": topn, "leverage": lev,
            "annual_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2),
            "total_pct": round(total * 100, 2),
            "max_drawdown_pct": round(((eq / eq.cummax()) - 1).min() * 100, 2),
            "win_rate": round((df["net_pct"] > 0).mean() * 100, 1),
        })
        if (reb, topn, lev) == (30, 5, 1):
            cash = {
                "periods": len(df),
                "funding_pct": round(df["funding_pct"].mean(), 4),
                "basis_pct": round(df["price_pnl_pct"].mean(), 4),
                "cost_pct": round(df["cost_pct"].mean(), 4),
                "net_pct": round(df["net_pct"].mean(), 4),
                "basis_stats": {
                    "mean": round(float((p := _basis())["mean"]), 4),
                    "std": round(float(p["std"]), 4),
                    "p95_low": round(float(p["p95_low"]), 3),
                    "p95_high": round(float(p["p95_high"]), 3),
                },
                "yearly": [
                    {"year": int(y), "periods": len(g),
                     "net_pct": round(((1 + g["net_on_capital_pct"] / 100).prod() - 1) * 100, 2),
                     "funding_pct": round(g["funding_pct"].sum(), 2),
                     # 折算年化：区间收益按实际期数换算，才看得出衰减
                     "annualized_pct": round(
                         ((1 + ((1 + g["net_on_capital_pct"] / 100).prod() - 1))
                          ** (12 / len(g)) - 1) * 100, 2)}
                    for y, g in df.groupby(df["t"].dt.year)
                ],
                # 前瞻预期：按最近一年的实际运行速率，而不是回测均值
                "forward_annual_1x": 1.14,
                "forward_annual_3x": 1.70,
                "backtest_avg_1x": 3.64,
                "backtest_avg_3x": 5.50,
            }
    # 强平表
    #
    # 阈值可由「维持保证金率」直接推出：价格涨幅超过 1/杠杆 − 维持保证金率 即被强平。
    # 原先这里是写死的 (1,99.5) (2,49.5) (3,32.8) (5,19.5)，与公式结果一致，
    # 但看不出出处；改成计算式后改杠杆/改维持保证金率会自动跟着变。
    #
    # ⚠ liquidated_periods 仍是情景输入（历史上被击穿的期数），不是本脚本算出来的；
    #   它是研究结论的一部分，重跑不会变 —— 数值变化需要重做那次分析。
    liq = []
    for lev, periods in [(1, 4), (2, 9), (3, 16), (5, 20)]:
        thr = round((1 / lev - cb.MAINT_MARGIN) * 100, 1)
        liq.append({"leverage": lev, "threshold_pct": thr,
                    "liquidated_periods": periods,
                    "violation_rate": round(periods / 33 * 100)})
    # 缓冲情景
    #
    # capital_multiple = 1 + 缓冲比例（全额现货 + 缓冲保证金 → 占用资金倍数），
    # 与写死值逐一核对过：30.8→1.31、98.8→1.99、248.6→3.49、20.7→1.21、72.2→1.72。
    # annual_pct 是情景假设（保证金占用不同 → 摊薄后的年化），保留为研究输入。
    buffers = []
    for label, buf, ann in [
        ("忽略强平（不可行）", None, 5.50),
        ("中位数缓冲（全部15币）", 30.8, 2.74),
        ("90%分位缓冲（全部15币）", 98.8, 1.80),
        ("最坏情况缓冲", 248.6, 1.02),
        ("中位数缓冲（仅大盘8币）", 20.7, 2.98),
        ("90%分位缓冲（仅大盘8币）", 72.2, 2.08),
    ]:
        multiple = round(1 + buf / 100, 2) if buf is not None else 1.33
        buffers.append({"label": label, "buffer_pct": buf,
                        "capital_multiple": multiple, "annual_pct": ann})
    # 保证金方案对比（逐小时模拟，含真实强平）
    schemes = []
    for label, mode, lev, res in [
        ("逐仓 · 无备用金", "isolated", 1, 0.0),
        ("逐仓 · 无备用金", "isolated", 2, 0.0),
        ("逐仓 · 无备用金", "isolated", 3, 0.0),
        ("逐仓 + 备用金 100%", "reserve", 2, 1.0),
        ("统一账户（现货可抵押）", "unified", 1, 0.0),
        ("统一账户（现货可抵押）", "unified", 3, 0.0),
    ]:
        try:
            r = cs.simulate(5, 30, lev, res, mode, verbose=False)
        except Exception:
            continue
        schemes.append({
            "label": label, "mode": mode, "leverage": lev,
            "liquidations": r["liquidations"],
            "total_pct": round(r["total"] * 100, 2),
            "annual_pct": round(r["annual"] * 100, 2) if np.isfinite(r["annual"]) else None,
            "max_drawdown_pct": round(r["mdd"] * 100, 2),
        })
    return {"cash": cash, "sweep": sweep, "liquidation": liq,
            "buffers": buffers, "schemes": schemes}


def _basis():
    """基差统计（缓存）"""
    global _BASIS_CACHE
    if _BASIS_CACHE is not None:
        return _BASIS_CACHE
    syms = cb.discover_symbols()
    S, P = {}, {}
    for s in syms:
        sp = cb.load_series(s, "1h", "spot")
        pp = cb.load_series(s, "1h", "perp")
        if sp is None or pp is None:
            continue
        S[s], P[s] = sp, pp
    u = sorted(S.keys())
    Sd = pd.DataFrame(S)[u].sort_index()
    Pd = pd.DataFrame(P)[u].sort_index()
    idx = Sd.index.intersection(Pd.index)
    b = (Pd.loc[idx] / Sd.loc[idx] - 1).stack()
    _BASIS_CACHE = {
        "mean": b.mean() * 100, "std": b.std() * 100,
        "p95_low": b.quantile(0.025) * 100, "p95_high": b.quantile(0.975) * 100,
    }
    return _BASIS_CACHE


_BASIS_CACHE = None


def main():
    print("  汇总模型对比…")
    models = build_models()
    print("  运行 Carry 回测…")
    carry = build_carry()
    data = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
        "models": models,
        "routes": ROUTES,
        "carry": carry,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"\n  ✅ {OUT}  ({os.path.getsize(OUT)//1024} KB)")
    print(f"     模型 {len(models)} 个 · 路线 {len(ROUTES)} 条 · Carry 配置 {len(carry['sweep'])} 组")
    for m in models:
        print(f"       {m['label']:<22} {m['profit_pct']:>8.2f}%  IC {m['pred_ic']}  命中 {m['sign_hit_rate']}%")


if __name__ == "__main__":
    main()
