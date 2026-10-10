#!/usr/bin/env python3
"""
模型版本注册表 —— 「正在使用的版本」与「最新迭代的版本」分开管理

设计原则（与 docs「自动迭代系统.md」一致）：
    · 自动迭代只【生产】新版本（challenger），绝不自动上线
    · 上线（promote）必须人工执行，且留痕（--note 必填）
    · 每个版本都记录：规格 / 指标 / 是否达标 / 部署位置 / 历史

两层版本：
    live_strategy  实盘策略版本（正在真实或干跑运行的）
    ml_model       研究模型版本（ML 序列模型等，可尚未上线）

存储：user_data/model_versions.json

用法：
    python model_registry.py seed                  # 用现有证据初始化（幂等）
    python model_registry.py list
    python model_registry.py champion
    python model_registry.py show v8-seq-lstm
    python model_registry.py promote auto-r9-1a2b3c --note "样本外达标，人工确认上线"
    python model_registry.py archive v8-seq-gru

被 auto_research.py 调用（只写 challenger，不 promote）：
    from model_registry import load, save, upsert, champion
"""

import argparse
from copy import deepcopy
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
REG_PATH = os.path.join(BASE, "user_data", "model_versions.json")
TZ = "Asia/Shanghai"

LAYERS = {
    "live_strategy": "实盘策略",
    "ml_model": "研究模型",
}
STATUS = ("champion", "challenger", "archived")
LEGACY_LIVE_VERSION = "live-trend-20-20"
LEGACY_METRIC_REASON = (
    "旧研究回测口径已作废；注册表中的实盘年化、夏普与实现偏差没有"
    "实际已平仓交易证据，不能作为测量结果或通过判据。"
)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════ 读写 ══════════════════

def load(path: str = REG_PATH) -> dict:
    """读取注册表；文件不存在时返回空骨架（不落盘）。"""
    if not os.path.exists(path):
        return {"updated": None, "champions": {}, "versions": []}
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    reg.setdefault("updated", None)
    reg.setdefault("champions", {})
    reg.setdefault("versions", [])
    return reg


def save(reg: dict, path: str = REG_PATH) -> None:
    """原子写：先写临时文件再替换，避免中断导致注册表损坏。"""
    reg["updated"] = now()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get(reg: dict, vid: str):
    for v in reg["versions"]:
        if v["id"] == vid:
            return v
    return None


def upsert(reg: dict, entry: dict) -> dict:
    """按 id 覆盖写入；保留一条 history 便于追溯。"""
    old = get(reg, entry["id"])
    if old:
        hist = old.get("history", [])[:]
        hist.append({
            "t": now(),
            "event": "updated",
            "from_status": old.get("status"),
            "metrics_before": old.get("metrics"),
        })
        entry["history"] = hist
        entry.setdefault("created", old.get("created", now()))
        idx = reg["versions"].index(old)
        reg["versions"][idx] = entry
    else:
        entry.setdefault("created", now())
        entry.setdefault("history", [])
        reg["versions"].append(entry)
    return entry


def champion(reg: dict, layer: str = "ml_model"):
    vid = reg["champions"].get(layer)
    return get(reg, vid) if vid else None


def challengers(reg: dict, layer: str = None):
    out = [v for v in reg["versions"] if v.get("status") == "challenger"]
    if layer:
        out = [v for v in out if v.get("layer") == layer]
    return out


def public_entry(entry: dict) -> dict:
    """返回可展示/判断的视图，保留已作废的原始证据，不改写注册表。"""
    view = deepcopy(entry)
    if view.get("id") != LEGACY_LIVE_VERSION:
        return view
    metrics = view.get("metrics") or {}
    # 使用独立留档字段使投影幂等，也让旧文件/数据库再次同步时不会恢复有效性。
    view.setdefault("legacy_metrics", deepcopy(metrics))
    view.setdefault("legacy_gate", deepcopy(view.get("gate")))
    view["metrics"] = deepcopy(metrics)
    for key in ("research_annual", "live_annual", "impl_gap_pct", "calmar_live", "sharpe_live"):
        view["metrics"][key] = None
    view["metrics_validity"] = "invalidated"
    view["metrics_reason"] = LEGACY_METRIC_REASON
    view["gate"] = {
        "criteria": "已作废的 C1 实现一致性判据",
        "passed": False,
        "status": "invalidated",
        "reasons": [LEGACY_METRIC_REASON],
    }
    return view


def public_view(reg: dict) -> dict:
    """原始历史仍由 load/save 管理；API 和 CLI 使用这一非变更有效视图。"""
    view = deepcopy(reg)
    view["versions"] = [public_entry(entry) for entry in view.get("versions", [])]
    return view


# ══════════════════ 状态变更（人工） ══════════════════

def set_champion(reg: dict, vid: str, note: str) -> dict:
    """
    人工上线：把 vid 设为该层的 champion，原 champion 降为 archived。
    注意：本函数只改注册表，不会去改实盘配置 —— 实际部署仍需人工操作 config。
    """
    entry = get(reg, vid)
    if entry is None:
        raise SystemExit(f"❌ 版本不存在: {vid}")
    layer = entry.get("layer", "ml_model")
    prev = champion(reg, layer)
    if prev and prev["id"] != vid:
        prev["status"] = "archived"
        prev.setdefault("history", []).append(
            {"t": now(), "event": "demoted", "by": vid, "note": note})
    entry["status"] = "champion"
    entry.setdefault("history", []).append(
        {"t": now(), "event": "promoted", "note": note})
    entry.setdefault("notes", "")
    reg["champions"][layer] = vid
    return entry


def archive(reg: dict, vid: str, note: str = "手工归档") -> dict:
    entry = get(reg, vid)
    if entry is None:
        raise SystemExit(f"❌ 版本不存在: {vid}")
    if reg["champions"].get(entry.get("layer")) == vid:
        raise SystemExit(f"❌ {vid} 是当前 champion，不能归档；请先 promote 新版本")
    entry["status"] = "archived"
    entry.setdefault("history", []).append({"t": now(), "event": "archived", "note": note})
    return entry


# ══════════════════ 初始化（用现有证据 seed） ══════════════════

def seed(reg: dict) -> dict:
    """把工作区里已有的证据登记成初始版本。幂等：已存在的 id 不重复登记。"""
    entries = [
        {
            "id": "live-trend-20-20",
            "layer": "live_strategy",
            "label": "趋势跟踪 20/20（实盘干跑）",
            "status": "champion",
            "source": "manual",
            "round": None,
            "spec": {
                "strategy": "TrendFollowing",
                "enter_period": 20,
                "exit_period": 20,
                "timeframe": "1d",
                "max_open_trades": 10,
            },
            "metrics": {
                "research_annual": None,
                "live_annual": None,
                "impl_gap_pct": None,
                "calmar_live": None,
                "sharpe_live": None,
            },
            "metrics_validity": "invalidated",
            "metrics_reason": LEGACY_METRIC_REASON,
            "gate": {
                "criteria": "已作废的 C1 实现一致性判据",
                "passed": False,
                "status": "invalidated",
                "reasons": [LEGACY_METRIC_REASON],
            },
            "deployed": {
                "mode": "dry_run",
                "config": "bot/user_data/config_trend_live.json",
                "since": "2026-10-07",
            },
            "artifacts": [],
            "notes": "C2 参数稳定性未过（当前参数近 12 个月排 5/6），但整体环境变难，"
                     "不因样本内挑最优而改参数（见 自动迭代系统.md）。",
        },
        {
            "id": "v8-seq-lstm",
            "layer": "ml_model",
            "label": "第 8 轮 序列 LSTM",
            "status": "champion",
            "source": "manual_round",
            "round": 8,
            "spec": {"kind": "lstm", "seq_len": 30, "hidden": 64, "layers": 1,
                     "dropout": 0.3, "lr": 0.001, "epochs": 8,
                     "label": "策略忠实标注（三重障碍）"},
            "metrics": {"ic_period": 0.0757, "t_period": 3.02, "pos_windows": 16,
                        "n_windows": 22, "n": 69301, "ic_pool": 0.0147, "t_pool": 3.66,
                        "q_value": None},
            "gate": {
                "criteria": "逐窗口 IC 的 t > 2 且 正窗口占比 >= 80%",
                "passed": False,
                "reasons": ["逐窗口 t=3.02 ✅", "正窗口 16/22 = 73% ❌（差 2 个窗口）"],
            },
            "deployed": None,
            "artifacts": ["bot/user_data/ml_seq_results.jsonl"],
            "notes": "七轮以来第一个 t>2 的结果；未达标在正窗口占比，尚未上线。",
        },
        {
            "id": "v8-seq-gru",
            "layer": "ml_model",
            "label": "第 8 轮 序列 GRU",
            "status": "archived",
            "source": "manual_round",
            "round": 8,
            "spec": {"kind": "gru", "seq_len": 30, "hidden": 64, "layers": 1, "dropout": 0.3},
            "metrics": {"ic_period": 0.0575, "t_period": 2.23, "pos_windows": 14,
                        "n_windows": 22, "n": 69301, "ic_pool": -0.0247, "t_pool": -6.13},
            "gate": {"criteria": "逐窗口 IC 的 t > 2 且 正窗口占比 >= 80%",
                     "passed": False, "reasons": ["正窗口 14/22 = 64% ❌"]},
            "deployed": None,
            "artifacts": [],
            "notes": "池化 IC 与逐窗口 IC 符号相反（辛普森悖论），只认逐窗口口径。",
        },
        {
            "id": "v8-seq-transformer",
            "layer": "ml_model",
            "label": "第 8 轮 序列 Transformer",
            "status": "archived",
            "source": "manual_round",
            "round": 8,
            "spec": {"kind": "transformer", "seq_len": 30, "hidden": 64, "layers": 2,
                     "dropout": 0.3},
            "metrics": {"ic_period": 0.058, "t_period": 2.00, "pos_windows": 15,
                        "n_windows": 22, "n": 69301, "ic_pool": 0.0211, "t_pool": 5.24},
            "gate": {"criteria": "逐窗口 IC 的 t > 2 且 正窗口占比 >= 80%",
                     "passed": False, "reasons": ["正窗口 15/22 = 68% ❌"]},
            "deployed": None, "artifacts": [],
            "notes": "t 值刚好踩线 2.00，未达标。",
        },
        {
            "id": "v8-seq-cnn",
            "layer": "ml_model",
            "label": "第 8 轮 序列 1D-CNN",
            "status": "archived",
            "source": "manual_round",
            "round": 8,
            "spec": {"kind": "cnn", "seq_len": 30, "hidden": 64, "layers": 1, "dropout": 0.3},
            "metrics": {"ic_period": 0.0404, "t_period": 2.01, "pos_windows": 16,
                        "n_windows": 22, "n": 69301, "ic_pool": -0.0255, "t_pool": -6.34},
            "gate": {"criteria": "逐窗口 IC 的 t > 2 且 正窗口占比 >= 80%",
                     "passed": False, "reasons": ["正窗口 16/22 = 73% ❌"]},
            "deployed": None, "artifacts": [],
            "notes": "池化为负，逐窗口为正 —— 再次验证不能池化。",
        },
    ]
    for e in entries:
        if get(reg, e["id"]) is None:
            reg["versions"].append(e)
    for e in entries:
        if e["layer"] not in reg["champions"] and e["status"] == "champion":
            reg["champions"][e["layer"]] = e["id"]
    # 已登记的 champion 保持现状；空位补齐
    for layer, vid in (("live_strategy", "live-trend-20-20"), ("ml_model", "v8-seq-lstm")):
        reg["champions"].setdefault(layer, vid)
    return reg


# ══════════════════ 展示 ══════════════════

def fmt_metrics(v: dict) -> str:
    v = public_entry(v)
    m = v.get("metrics") or {}
    if v.get("metrics_validity") == "invalidated":
        return "研究口径已作废 · 实际年化 未知（旧值仅留档）"
    if m.get("ic_period") is not None:
        return (f"IC {m['ic_period']:+.4f} · t={m.get('t_period')} · "
                f"正窗口 {m.get('pos_windows')}/{m.get('n_windows')}")
    if m.get("research_annual") is not None:
        live = "未知" if m.get("live_annual") is None else f"{m['live_annual']:.1%}"
        return f"研究年化 {m['research_annual']:.1%} · 实际年化 {live}"
    return "—"


def cmd_list(reg: dict, args) -> None:
    reg = public_view(reg)
    print(f"\n  模型版本注册表 · 更新于 {reg.get('updated') or '—'}")
    print("  " + "═" * 92)
    for layer, cname in LAYERS.items():
        vs = [v for v in reg["versions"] if v.get("layer") == layer]
        if not vs:
            continue
        print(f"\n  【{cname} / {layer}】")
        order = {"champion": 0, "challenger": 1, "archived": 2}
        for v in sorted(vs, key=lambda x: (order.get(x["status"], 9), x["id"])):
            if not args.all and v["status"] == "archived" and not args.archived:
                continue
            icon = {"champion": "🏆", "challenger": "🧪", "archived": "·"}[v["status"]]
            gate_data = v.get("gate") or {}
            gate = ("口径作废" if gate_data.get("status") == "invalidated" else
                    "✅达标" if gate_data.get("passed") else "❌未达标")
            dep = "已部署" if v.get("deployed") else "未部署"
            print(f"    {icon} {v['id']:<28} {v['status']:<10} {gate:<8} {dep:<6} "
                  f"{fmt_metrics(v)}")
    print("\n  " + "═" * 92)
    print("  🏆 champion＝优选登记（不代表已部署）   🧪 challenger＝最新迭代候选   "
          "· archived＝历史版本")
    print("  上线命令：python model_registry.py promote <id> --note \"人工确认理由\"\n")


def cmd_show(reg: dict, args) -> None:
    v = get(reg, args.id)
    if v is None:
        raise SystemExit(f"❌ 版本不存在: {args.id}")
    print(json.dumps(public_entry(v), ensure_ascii=False, indent=2))


def cmd_champion(reg: dict, args) -> None:
    for layer in LAYERS:
        v = champion(reg, layer)
        if v is None:
            print(f"  {layer}: （未设置）")
            continue
        dep = (v.get("deployed") or {}).get("config", "未部署")
        print(f"  🏆 {layer:<14} {v['id']:<24} {v.get('label','')}")
        print(f"      {fmt_metrics(v)}   → {dep}")


def main():
    ap = argparse.ArgumentParser(description="模型版本注册表")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有版本")
    p.add_argument("--all", action="store_true", help="包含历史版本")
    p.add_argument("--archived", action="store_true", help="包含 archived")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看某个版本详情")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("champion", help="当前正在使用的版本")
    p.set_defaults(func=cmd_champion)

    p = sub.add_parser("seed", help="用工作区现有证据初始化（幂等）")
    p.set_defaults(func=lambda reg, a: seed(reg))

    p = sub.add_parser("promote", help="人工上线：设为 champion")
    p.add_argument("id")
    p.add_argument("--note", required=True, help="人工确认理由（必填，留痕）")
    p.set_defaults(func=lambda reg, a: set_champion(reg, a.id, a.note))

    p = sub.add_parser("archive", help="归档一个非 champion 版本")
    p.add_argument("id")
    p.add_argument("--note", default="手工归档")
    p.set_defaults(func=lambda reg, a: archive(reg, a.id, a.note))

    p = sub.add_parser("register", help="从 JSON 文件登记/更新版本")
    p.add_argument("file")
    p.set_defaults(func=lambda reg, a: upsert(reg, json.load(open(a.file, encoding="utf-8"))))

    args = ap.parse_args()
    reg = load()
    if args.cmd == "seed":
        seed(reg)
        save(reg)
        cmd_list(reg, argparse.Namespace(all=False, archived=False))
        return
    args.func(reg, args)
    if args.cmd in ("promote", "archive", "register"):
        save(reg)
        print(f"  ✅ 已更新 {REG_PATH}")
        cmd_list(reg, argparse.Namespace(all=False, archived=False))


if __name__ == "__main__":
    sys.exit(main())
