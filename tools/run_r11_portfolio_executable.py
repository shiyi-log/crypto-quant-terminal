#!/usr/bin/env python3
"""T026 historical portfolio rerun at executable 0.1 underlying units per leg."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_r11_portfolio import DVOL_START_MS, DAY_MS, INITIAL_EQUITY, SOURCE_POSITIONS, load_candidates

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "research_multi_strategy/R11_EXECUTABLE_SCALE_T026_20261025.md"
T024_REPORT = ROOT / "artifacts/r11_vol_risk_premium_portfolio_20261025/REPORT.json"
EXECUTABLE_UNITS_PER_LEG = 0.1
BASE_NOTIONAL_PER_LEG = 2500.0
MAX_ACTIVE_CAP = 10
INITIAL_MARGIN_RATE = 0.30
MAINTENANCE_MARGIN_RATE = 0.15
HOLDING_DAYS = 30
OPTION_SPREADS = (0.002, 0.01, 0.0225)
HEDGE_FEES_BPS = (8.0, 16.0)
BOOTSTRAP_ITERATIONS = 1000
BOOTSTRAP_SEED = 20261026
RUN_ID = "r11-executable-portfolio-20261025-v1"
EVIDENCE = "forward_paper"
UTC = timezone.utc


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def asset_spot(row: dict[str, Any]) -> float:
    for key in ("strike", "spot", "entry_spot"):
        if row.get(key) not in (None, 0):
            return float(row[key])
    return BASE_NOTIONAL_PER_LEG / max(float(row.get("quantity", 1.0)), 1e-12)


def executable_sizing(row: dict[str, Any], sizing: str, equity: float) -> dict[str, float]:
    spot = asset_spot(row)
    leg_notional = EXECUTABLE_UNITS_PER_LEG * spot
    if sizing == "risk_budget_2pct":
        # Keep the executable unit as a floor: scale the 0.1-unit position down
        # only when the 2% equity budget is smaller than one executable straddle.
        budget_total = equity * 0.02
        total_notional = min(2.0 * leg_notional, budget_total / INITIAL_MARGIN_RATE) if budget_total > 0 else 0.0
        leg_notional = total_notional / 2.0
    elif sizing != "fixed_executable_0.1":
        raise ValueError(sizing)
    return {"spot": spot, "leg_notional": leg_notional, "total_notional": 2.0 * leg_notional,
            "scale_from_t023": leg_notional / BASE_NOTIONAL_PER_LEG,
            "initial_margin": 2.0 * leg_notional * INITIAL_MARGIN_RATE,
            "maintenance_margin": 2.0 * leg_notional * MAINTENANCE_MARGIN_RATE}


def scenario_pnl(row: dict[str, Any], option_spread: float, hedge_fee_bps: float, *, leg_notional: float) -> float:
    scale = leg_notional / BASE_NOTIONAL_PER_LEG
    return scale * (float(row["gross_pnl"]) - abs(float(row["premium_received"])) * option_spread - float(row.get("hedge_turnover", 0.0)) * hedge_fee_bps / 10_000.0)


def by_day(candidates: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[int(row["entry_ts"])].append(row)
    for ts in grouped:
        grouped[ts].sort(key=lambda row: (row["asset"] != "BTC", row["asset"]))
    return grouped


def bootstrap_bankruptcy(curve: list[dict[str, Any]]) -> float:
    values = [float(row["daily_pnl"]) for row in curve]
    if not values:
        return 0.0
    blocks = [values[i:i + 30] for i in range(0, len(values), 30)]
    rng = random.Random(BOOTSTRAP_SEED)
    count = 0
    for _ in range(BOOTSTRAP_ITERATIONS):
        eq = INITIAL_EQUITY
        for _ in blocks:
            for value in blocks[rng.randrange(len(blocks))]:
                eq += value
                if eq <= 0:
                    count += 1
                    break
            if eq <= 0:
                break
    return count / BOOTSTRAP_ITERATIONS


def metrics(curve: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    if not curve:
        return {"status": "not_estimable"}
    eqs = [float(x["equity"]) for x in curve]
    rets = [float(x["daily_return"]) for x in curve]
    peak = eqs[0]
    max_dd = 0.0
    for eq in eqs:
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1.0)
    years = max((curve[-1]["timestamp"] - curve[0]["timestamp"]) / DAY_MS / 365.0, 1 / 365.0)
    ann = (eqs[-1] / INITIAL_EQUITY) ** (1.0 / years) - 1.0 if eqs[-1] > 0 else -1.0
    sd = stdev(rets) if len(rets) > 1 else 0.0
    sharpe = mean(rets) / sd * math.sqrt(365.0) if sd else 0.0
    months: dict[str, float] = defaultdict(float)
    for row in curve:
        months[datetime.fromtimestamp(row["timestamp"] / 1000, tz=UTC).strftime("%Y-%m")] += row["daily_pnl"]
    vals = sorted(months.values())
    q95 = vals[max(0, int(math.floor((len(vals) - 1) * 0.05)))] if vals else 0.0
    q99 = vals[max(0, int(math.floor((len(vals) - 1) * 0.01)))] if vals else 0.0
    cvar95 = mean([v for v in vals if v <= q95]) if vals else 0.0
    cvar99 = mean([v for v in vals if v <= q99]) if vals else 0.0
    margin_calls = sum(e["event"] == "margin_call" for e in events)
    liquidations = sum(e["event"] == "forced_liquidation" for e in events)
    numeric_pass = ann > 0.10 and max_dd > -0.30
    return {"status": "estimable", "terminal_equity": eqs[-1], "annualized_return": ann, "max_drawdown_pct": max_dd, "sharpe_daily_annualized": sharpe, "monthly_cvar95_usdt": cvar95, "monthly_cvar99_usdt": cvar99, "worst_month_usdt": min(vals) if vals else 0.0, "margin_calls": margin_calls, "forced_liquidations": liquidations, "bankrupt_historical_path": any(e <= 0 for e in eqs), "bankruptcy_probability_bootstrap": bootstrap_bankruptcy(curve), "max_active_positions": max(row["active_positions"] for row in curve), "numeric_forward_gate": numeric_pass, "forward_paper_gate": "blocked_execution_evidence_forward_paper" if numeric_pass else "not_met_numeric_gate", "monthly_count": len(vals)}


def simulate(candidates: list[dict[str, Any]], *, sizing: str, option_spread: float, hedge_fee_bps: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    grouped = by_day(candidates)
    if not grouped:
        return [], [], metrics([], [])
    first = max(DVOL_START_MS, min(grouped))
    last = max(int(x["expiry_ts"]) for x in candidates)
    active: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    equity = INITIAL_EQUITY
    for ts in range(first, last + DAY_MS, DAY_MS):
        start_eq = equity
        remaining = []
        for pos in active:
            if pos["expiry_ts"] <= ts:
                equity += pos["net_pnl"]
                events.append({"event": "exit", "timestamp": ts, "asset": pos["asset"], "net_pnl": pos["net_pnl"], "total_notional": pos["total_notional"]})
            else:
                remaining.append(pos)
        active = remaining
        maintenance = sum(pos["maintenance_margin"] for pos in active)
        margin_call = equity < maintenance and bool(active)
        if margin_call:
            events.append({"event": "margin_call", "timestamp": ts, "equity": equity, "maintenance": maintenance, "active": len(active)})
            for pos in active:
                elapsed = max(0.0, min(1.0, (ts - pos["entry_ts"]) / (HOLDING_DAYS * DAY_MS)))
                forced = pos["net_pnl"] * elapsed
                equity += forced
                events.append({"event": "forced_liquidation", "timestamp": ts, "asset": pos["asset"], "net_pnl": forced})
            active = []
        todays = grouped.get(ts, [])
        if not margin_call and equity > 0 and todays and len(active) < MAX_ACTIVE_CAP:
            counts = {asset: sum(p["asset"] == asset for p in active) for asset in ("BTC", "ETH")}
            chosen = sorted(todays, key=lambda r: (counts.get(r["asset"], 0), r["asset"] != "BTC"))[0]
            size = executable_sizing(chosen, sizing, equity)
            if size["initial_margin"] > 0 and sum(p["initial_margin"] for p in active) + size["initial_margin"] <= equity:
                pnl = scenario_pnl(chosen, option_spread, hedge_fee_bps, leg_notional=size["leg_notional"])
                active.append({"asset": chosen["asset"], "entry_ts": int(chosen["entry_ts"]), "expiry_ts": int(chosen["expiry_ts"]), "net_pnl": pnl, **size})
                events.append({"event": "entry", "timestamp": ts, "asset": chosen["asset"], "total_notional": size["total_notional"], "leg_notional": size["leg_notional"], "initial_margin": size["initial_margin"], "scale_from_t023": size["scale_from_t023"], "net_pnl_at_expiry": pnl})
        reserved = sum(p["initial_margin"] for p in active)
        maintenance = sum(p["maintenance_margin"] for p in active)
        daily_pnl = equity - start_eq
        curve.append({"timestamp": ts, "date": datetime.fromtimestamp(ts / 1000, tz=UTC).date().isoformat(), "equity": equity, "daily_pnl": daily_pnl, "daily_return": daily_pnl / start_eq if start_eq else -1.0, "active_positions": len(active), "reserved_margin": reserved, "maintenance_margin": maintenance, "margin_call": margin_call, "sizing": sizing, "option_spread": option_spread, "hedge_fee_bps": hedge_fee_bps})
    return curve, events, metrics(curve, events)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows))


def run(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    forward_report: dict[str, Any] = {}
    report_path = output / "REPORT.json"
    if report_path.exists():
        try:
            candidate_report = json.loads(report_path.read_text())
            if candidate_report.get("strategy_id") == "r11_forward_paper_executable_v1":
                forward_report = candidate_report
                (output / "forward_REPORT.json").write_text(json.dumps(candidate_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except (OSError, json.JSONDecodeError):
            forward_report = {}
    candidates = load_candidates()
    results: dict[str, Any] = {}
    curves: list[dict[str, Any]] = []
    events_all: list[dict[str, Any]] = []
    for sizing in ("fixed_executable_0.1", "risk_budget_2pct"):
        for spread in OPTION_SPREADS:
            for fee in HEDGE_FEES_BPS:
                key = f"{sizing}:spread_{spread:.4f}:fee_{int(fee)}bp"
                curve, events, result = simulate(candidates, sizing=sizing, option_spread=spread, hedge_fee_bps=fee)
                results[key] = result | {"option_spread": spread, "hedge_fee_bps": fee, "candidate_count": len(candidates)}
                curves.extend({"run_key": key, **x} for x in curve)
                events_all.extend({"run_key": key, **x} for x in events)
    baseline = {}
    if T024_REPORT.exists():
        try:
            old = json.loads(T024_REPORT.read_text()).get("results", {})
            for key in ("fixed_2500:spread_0.002:fee_8bp", "fixed_2500:spread_0.020:fee_16bp"):
                if key in old:
                    baseline[key] = {name: old[key].get(name) for name in ("annualized_return", "max_drawdown_pct", "sharpe_daily_annualized", "monthly_cvar95_usdt", "monthly_cvar99_usdt", "numeric_forward_gate")}
        except (OSError, json.JSONDecodeError):
            baseline = {}
    report = {"task": "T026", "run_id": RUN_ID, "strategy_id": "r11_executable_portfolio_v1", "execution_evidence": EVIDENCE, "spec_sha256": sha(SPEC_PATH), "t024_report_sha256": sha(T024_REPORT) if T024_REPORT.exists() else "unknown", "candidate_count": len(candidates), "config": {"initial_equity": INITIAL_EQUITY, "executable_units_per_leg": EXECUTABLE_UNITS_PER_LEG, "max_active_cap": MAX_ACTIVE_CAP, "initial_margin_rate": INITIAL_MARGIN_RATE, "maintenance_margin_rate": MAINTENANCE_MARGIN_RATE, "option_spreads": OPTION_SPREADS, "hedge_fee_bps": HEDGE_FEES_BPS}, "forward_paper_report": forward_report, "comparison_to_t024": baseline, "results": results, "conclusion": "Executable-size rerun is historical synthetic evidence; forward paper remains paper-only and no deployment is authorized."}
    write_jsonl(output / "equity_curves.jsonl", curves)
    write_jsonl(output / "portfolio_events.jsonl", events_all)
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T026 executable-scale R11 portfolio", "", f"- execution_evidence: `{EVIDENCE}`", f"- spec_sha256: `{report['spec_sha256']}`", f"- candidate positions: `{len(candidates)}`", "", "Each position is 0.1 underlying units per call and put, with total notional approximately 2 × 0.1 × spot. Initial margin is 30% of total straddle notional; active positions are capped at 10 and also constrained by available equity. The prior 2,500 USDT/leg fixed run is the comparison baseline in T024.", "", "| sizing | spread | fee | ann. return | max DD | Sharpe | CVaR95 | CVaR99 | worst month | max active | margin calls | liquidations | bankruptcy | numeric gate |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for key, r in results.items():
        md.append(f"| {key.split(':')[0]} | {r['option_spread']:.2%} | {r['hedge_fee_bps']:.0f}bp | {r['annualized_return']:.2%} | {r['max_drawdown_pct']:.2%} | {r['sharpe_daily_annualized']:.2f} | {r['monthly_cvar95_usdt']:.2f} | {r['monthly_cvar99_usdt']:.2f} | {r['worst_month_usdt']:.2f} | {r['max_active_positions']} | {r['margin_calls']} | {r['forced_liquidations']} | {r['bankruptcy_probability_bootstrap']:.3%} | {r['numeric_forward_gate']} |")
    md.extend(["", "The 2.25% row is the measured forward spread reference. A numeric pass remains blocked from deployment because the portfolio history is synthetic; the live evidence level is forward_paper.", "", "## Forward paper", ""])
    if forward_report:
        fs = forward_report.get("snapshot", {})
        md.append(f"- executable observation: `{forward_report.get('observations')}`; spot `{fs.get('spot_index')}`, bid premium `{fs.get('paper_premium_bid_usd')}`, straddle notional `{fs.get('paper_unit', {}).get('actual_straddle_notional_usdt')}`; measured width `{fs.get('straddle_bid_ask_width_pct_of_bid')}%`; lifecycle evidence `{forward_report.get('execution_evidence')}`")
    md.extend(["", "## T024 comparison", ""])
    for key, value in baseline.items():
        md.append(f"- `{key}`: ann. return `{value['annualized_return']:.2%}`, max DD `{value['max_drawdown_pct']:.2%}`, Sharpe `{value['sharpe_daily_annualized']:.2f}`, numeric gate `{value['numeric_forward_gate']}`")
    md.append("")
    (output / "REPORT.md").write_text("\n".join(md))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_portfolio_executable.py --output artifacts/r11_executable_scale_20261025\n")
    reproduce.chmod(0o755)
    files = {}
    for p in sorted(output.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            try:
                name = str(p.resolve().relative_to(ROOT.resolve()))
            except ValueError:
                name = str(p.resolve())
            files[name] = {"sha256": sha(p), "bytes": p.stat().st_size}
    manifest = {"task": "T026", "run_id": RUN_ID, "execution_evidence": EVIDENCE, "spec_sha256": report["spec_sha256"], "code_sha256": sha(Path(__file__)), "forward_code_sha256": sha(ROOT / "tools/run_r11_forward_paper_executable.py"), "source_positions_sha256": sha(SOURCE_POSITIONS), "generated_at_utc": datetime.now(UTC).isoformat(), "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/r11_executable_scale_20261025")
    args = parser.parse_args()
    report = run(args.output)
    baseline = report["results"].get("fixed_executable_0.1:spread_0.0020:fee_8bp", {})
    print(json.dumps({"annualized_return": baseline.get("annualized_return"), "max_drawdown_pct": baseline.get("max_drawdown_pct"), "max_active_positions": baseline.get("max_active_positions")}, sort_keys=True))


if __name__ == "__main__":
    main()
