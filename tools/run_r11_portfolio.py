#!/usr/bin/env python3
"""T024 portfolio simulation for the T023 synthetic short-straddle ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "research_multi_strategy/R11_PORTFOLIO_T024_20261025.md"
T023_SPEC_PATH = ROOT / "research_multi_strategy/R11_VOL_RISK_PREMIUM_T023_20261024.md"
SOURCE_POSITIONS = ROOT / "artifacts/r11_vol_risk_premium_20261024/positions.jsonl"
START_MS = 1_609_459_200_000
END_MS = 1_790_812_800_000
DVOL_START_MS = 1_640_995_200_000
DAY_MS = 86_400_000
HOLDING_DAYS = 30
BASE_NOTIONAL = 2500.0
INITIAL_EQUITY = 50_000.0
MAX_ACTIVE = 30
MAX_NEW_PER_DAY = 1
INITIAL_MARGIN_RATE = 0.30
MAINTENANCE_MARGIN_RATE = 0.15
RISK_BUDGET = 0.02
OPTION_SPREADS = (0.002, 0.005, 0.01, 0.02)
HEDGE_FEES_BPS = (8.0, 16.0)
BOOTSTRAP_ITERATIONS = 1000
BOOTSTRAP_SEED = 20261025
RUN_ID = "r11-portfolio-20261025-v1"
UTC = timezone.utc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_candidates(path: Path = SOURCE_POSITIONS) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"T023 positions not found: {path}")
    rows = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("method") != "short_atm_straddle" or row.get("frequency") != "daily":
            continue
        required = ("entry_ts", "expiry_ts", "asset", "gross_pnl", "premium_received", "hedge_turnover")
        if any(row.get(key) is None for key in required):
            continue
        rows.append(row)
    rows.sort(key=lambda row: (int(row["entry_ts"]), 0 if row["asset"] == "BTC" else 1))
    return rows


def scenario_pnl(row: dict[str, Any], option_spread: float, hedge_fee_bps: float, notional: float) -> float:
    scale = notional / BASE_NOTIONAL
    return scale * (float(row["gross_pnl"]) - abs(float(row["premium_received"])) * option_spread
                   - float(row.get("hedge_turnover", 0.0)) * hedge_fee_bps / 10_000.0)


def _candidate_by_day(candidates: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[int(row["entry_ts"])].append(row)
    for ts in grouped:
        grouped[ts].sort(key=lambda row: (row["asset"] != "BTC", row["asset"]))
    return grouped


def bootstrap_bankruptcy(curve: list[dict[str, Any]], initial_equity: float, *, sizing: str) -> float:
    pnl_or_returns = [float(row["daily_return"] if sizing == "risk_budget_2pct" else row["daily_pnl"]) for row in curve]
    if not pnl_or_returns:
        return 0.0
    block_len = max(1, 30)
    blocks = [pnl_or_returns[i:i + block_len] for i in range(0, len(pnl_or_returns), block_len)]
    rng = random.Random(BOOTSTRAP_SEED + (1 if sizing == "risk_budget_2pct" else 0))
    bankrupt = 0
    for _ in range(BOOTSTRAP_ITERATIONS):
        equity = initial_equity
        for _block in blocks:
            block = blocks[rng.randrange(len(blocks))]
            for value in block:
                equity = equity * (1.0 + value) if sizing == "risk_budget_2pct" else equity + value
                if equity <= 0:
                    bankrupt += 1
                    break
            if equity <= 0:
                break
    return bankrupt / BOOTSTRAP_ITERATIONS


def metrics(curve: list[dict[str, Any]], events: list[dict[str, Any]], *, sizing: str) -> dict[str, Any]:
    if not curve:
        return {"status": "not_estimable", "reason": "no_curve"}
    equities = [float(row["equity"]) for row in curve]
    daily_returns = [float(row["daily_return"]) for row in curve]
    peak = equities[0]
    max_dd = 0.0
    for equity in equities:
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    years = max((int(curve[-1]["timestamp"]) - int(curve[0]["timestamp"])) / DAY_MS / 365.0, 1 / 365.0)
    annualized_return = (equities[-1] / INITIAL_EQUITY) ** (1.0 / years) - 1.0 if equities[-1] > 0 else -1.0
    sharpe = mean(daily_returns) / stdev(daily_returns) * math.sqrt(365.0) if len(daily_returns) > 1 and stdev(daily_returns) else 0.0
    monthly: dict[str, float] = defaultdict(float)
    for row in curve:
        month = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=UTC).strftime("%Y-%m")
        monthly[month] += float(row["daily_pnl"])
    monthly_values = list(monthly.values())
    monthly_sorted = sorted(monthly_values)
    def q(qv: float) -> float:
        if not monthly_sorted:
            return 0.0
        idx = min(len(monthly_sorted) - 1, int(math.floor((len(monthly_sorted) - 1) * qv)))
        return monthly_sorted[idx]
    q95, q99 = q(0.05), q(0.01)
    cvar95 = mean([v for v in monthly_values if v <= q95]) if monthly_values else None
    cvar99 = mean([v for v in monthly_values if v <= q99]) if monthly_values else None
    margin_calls = [event for event in events if event["event"] == "margin_call"]
    liquidations = [event for event in events if event["event"] == "forced_liquidation"]
    bankruptcy = any(equity <= 0 for equity in equities)
    numeric_pass = annualized_return > 0.10 and max_dd > -0.30
    return {
        "status": "estimable", "sizing": sizing, "start_equity": INITIAL_EQUITY,
        "terminal_equity": equities[-1], "annualized_return": annualized_return,
        "max_drawdown_pct": max_dd, "sharpe_daily_annualized": sharpe,
        "monthly_cvar95_usdt": cvar95, "monthly_cvar99_usdt": cvar99,
        "worst_month_usdt": min(monthly_values) if monthly_values else None,
        "margin_calls": len(margin_calls), "forced_liquidations": len(liquidations),
        "bankrupt_historical_path": bankruptcy,
        "bankruptcy_probability_bootstrap": bootstrap_bankruptcy(curve, INITIAL_EQUITY, sizing=sizing),
        "monthly_count": len(monthly_values), "numeric_forward_gate": numeric_pass,
        "forward_paper_gate": "blocked_execution_evidence_unknown" if numeric_pass else "not_met_numeric_gate",
    }


def simulate_portfolio(candidates: list[dict[str, Any]], *, sizing: str, option_spread: float,
                       hedge_fee_bps: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if sizing not in {"fixed_2500", "risk_budget_2pct"}:
        raise ValueError("unknown sizing")
    by_day = _candidate_by_day(candidates)
    if not by_day:
        return [], [], metrics([], [], sizing=sizing)
    first_day = max(DVOL_START_MS, min(by_day))
    last_expiry = max(int(row["expiry_ts"]) for row in candidates)
    equity = INITIAL_EQUITY
    active: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for ts in range(first_day, last_expiry + DAY_MS, DAY_MS):
        daily_start_equity = equity
        # Realize positions at expiry before checking capacity or opening a new trade.
        remaining: list[dict[str, Any]] = []
        for pos in active:
            if int(pos["expiry_ts"]) <= ts:
                equity += pos["net_pnl"]
                events.append({"event": "exit", "timestamp": ts, "asset": pos["asset"], "entry_ts": pos["entry_ts"], "net_pnl": pos["net_pnl"]})
            else:
                remaining.append(pos)
        active = remaining
        maintenance = sum(float(pos["notional"]) * MAINTENANCE_MARGIN_RATE for pos in active)
        margin_call = False
        if active and equity < maintenance:
            margin_call = True
            events.append({"event": "margin_call", "timestamp": ts, "equity": equity, "maintenance": maintenance, "active": len(active)})
            for pos in active:
                elapsed = max(0.0, min(1.0, (ts - int(pos["entry_ts"])) / (HOLDING_DAYS * DAY_MS)))
                forced_pnl = pos["net_pnl"] * elapsed
                equity += forced_pnl
                events.append({"event": "forced_liquidation", "timestamp": ts, "asset": pos["asset"], "entry_ts": pos["entry_ts"], "net_pnl": forced_pnl, "mark_policy": "linear_terminal_pnl_proxy"})
            active = []
            maintenance = 0.0
        # At most one new position per day, and choose the least represented asset.
        candidates_today = by_day.get(ts, [])
        if not margin_call and len(active) < MAX_ACTIVE and equity > 0 and candidates_today:
            counts = {asset: sum(1 for pos in active if pos["asset"] == asset) for asset in ("BTC", "ETH")}
            chosen = sorted(candidates_today, key=lambda row: (counts.get(row["asset"], 0), row["asset"] != "BTC"))[0]
            notional = BASE_NOTIONAL if sizing == "fixed_2500" else equity * RISK_BUDGET
            if notional > 0:
                net_pnl = scenario_pnl(chosen, option_spread, hedge_fee_bps, notional)
                position = {"asset": chosen["asset"], "entry_ts": int(chosen["entry_ts"]), "expiry_ts": int(chosen["expiry_ts"]),
                            "notional": notional, "net_pnl": net_pnl, "margin": notional * INITIAL_MARGIN_RATE,
                            "maintenance": notional * MAINTENANCE_MARGIN_RATE}
                active.append(position)
                events.append({"event": "entry", "timestamp": ts, "asset": chosen["asset"], "entry_ts": chosen["entry_ts"], "notional": notional, "net_pnl_at_expiry": net_pnl})
        reserved = sum(float(pos["margin"]) for pos in active)
        maintenance = sum(float(pos["maintenance"]) for pos in active)
        daily_pnl = equity - daily_start_equity
        daily_return = daily_pnl / daily_start_equity if daily_start_equity else -1.0
        curve.append({"timestamp": ts, "date": datetime.fromtimestamp(ts / 1000, tz=UTC).date().isoformat(),
                      "equity": equity, "daily_pnl": daily_pnl, "daily_return": daily_return,
                      "active_positions": len(active), "reserved_margin": reserved,
                      "maintenance_margin": maintenance, "margin_call": margin_call,
                      "sizing": sizing, "option_spread": option_spread, "hedge_fee_bps": hedge_fee_bps})
    return curve, events, metrics(curve, events, sizing=sizing)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def run(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    t023_spec_hash = sha256_file(T023_SPEC_PATH)
    source_hash = sha256_file(SOURCE_POSITIONS)
    results: dict[str, Any] = {}
    all_curves: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    for sizing in ("fixed_2500", "risk_budget_2pct"):
        for spread in OPTION_SPREADS:
            for fee_bps in HEDGE_FEES_BPS:
                key = f"{sizing}:spread_{spread:.3f}:fee_{int(fee_bps)}bp"
                curve, events, result = simulate_portfolio(candidates, sizing=sizing, option_spread=spread, hedge_fee_bps=fee_bps)
                results[key] = result | {"option_spread": spread, "hedge_fee_bps": fee_bps, "candidate_count": len(candidates)}
                all_curves.extend({"run_key": key, **row} for row in curve)
                all_events.extend({"run_key": key, **event} for event in events)
    spec_hash = sha256_file(SPEC_PATH)
    code_hash = sha256_file(Path(__file__))
    report = {
        "task": "T024", "run_id": RUN_ID, "strategy_id": "r11_portfolio_short_straddle_v1",
        "execution_evidence": "unknown", "conclusion": "portfolio_synthetic_only",
        "data_quality": {
            "lookahead_fix_applied": True,
            "entry_iv_source": "deribit_dvol_1h_close_at_entry",
            "pre_fix_t023_t024_results": "invalidated",
        },
        "requested_window": {"start": "2021-01-01T00:00:00Z", "end_exclusive": "2026-10-01T00:00:00Z"},
        "effective_position_window": {"start": "2022-01-01T00:00:00Z", "reason": "pre_DVOL_rows_unknown_excluded"},
        "spec_sha256": spec_hash, "t023_spec_sha256": t023_spec_hash,
        "source_positions": str(SOURCE_POSITIONS.relative_to(ROOT)), "source_positions_sha256": source_hash,
        "candidate_count": len(candidates),
        "config": {"initial_equity": INITIAL_EQUITY, "max_active": MAX_ACTIVE, "max_new_per_day": MAX_NEW_PER_DAY,
                   "initial_margin_rate": INITIAL_MARGIN_RATE, "maintenance_margin_rate": MAINTENANCE_MARGIN_RATE,
                   "risk_budget": RISK_BUDGET, "option_spreads": OPTION_SPREADS, "hedge_fee_bps": HEDGE_FEES_BPS,
                   "bankruptcy_bootstrap_iterations": BOOTSTRAP_ITERATIONS, "margin_mark_policy": "linear_terminal_pnl_proxy"},
        "results": results, "code_sha256": code_hash,
    }
    write_jsonl(output / "equity_curves.jsonl", all_curves)
    write_jsonl(output / "portfolio_events.jsonl", all_events)
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T024 R11 portfolio simulation", "", f"- execution_evidence: `{report['execution_evidence']}`", f"- spec_sha256: `{spec_hash}`", f"- T023 spec_sha256: `{t023_spec_hash}`", f"- candidate positions: `{len(candidates)}`", "", "Entry IV uses the Deribit DVOL 1h close at the exact UTC entry timestamp. The daily DVOL close is not used for entry; all pre-fix T023/T024 results are invalidated. The simulation opens at most one synthetic short straddle per UTC day, caps active positions at 30, and uses 30% initial / 15% maintenance margin. Results are historical synthetic evidence; `execution_evidence=unknown` blocks deployment or forward-paper approval.", "", "| sizing | spread | hedge fee | terminal equity | ann. return | max DD | Sharpe | monthly CVaR95 | monthly CVaR99 | worst month | margin calls | liquidations | bankrupt bootstrap | gate |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for key, result in results.items():
        md.append(f"| {key.split(':')[0]} | {result['option_spread']:.3%} | {result['hedge_fee_bps']:.0f} | {result['terminal_equity']:.2f} | {result['annualized_return']:.2%} | {result['max_drawdown_pct']:.2%} | {result['sharpe_daily_annualized']:.2f} | {result['monthly_cvar95_usdt']:.2f} | {result['monthly_cvar99_usdt']:.2f} | {result['worst_month_usdt']:.2f} | {result['margin_calls']} | {result['forced_liquidations']} | {result['bankruptcy_probability_bootstrap']:.3%} | {result['forward_paper_gate']} |")
    md += ["", "Forward-paper numeric gate: annualized return >10% and max drawdown >-30%. Any numeric pass remains blocked because `execution_evidence=unknown`. The margin-call mark is a linear terminal-PnL proxy, not an observed option mark surface.", ""]
    (output / "REPORT.md").write_text("\n".join(md))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_portfolio.py --output artifacts/r11_vol_risk_premium_portfolio_20261025\n")
    reproduce.chmod(0o755)
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.resolve().relative_to(ROOT.resolve()))] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {"task": "T024", "run_id": RUN_ID, "execution_evidence": "unknown", "spec_sha256": spec_hash,
                "t023_spec_sha256": t023_spec_hash, "source_positions_sha256": source_hash,
                "code_sha256": code_hash, "git_commit": git_value(["git", "rev-parse", "HEAD"]),
                "git_dirty": bool(git_value(["git", "status", "--porcelain"])),
                "generated_at_utc": datetime.now(UTC).isoformat(), "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/r11_vol_risk_premium_portfolio_20261025")
    args = parser.parse_args()
    report = run(args.output)
    for key, result in report["results"].items():
        print(key, f"ann={result['annualized_return']:.2%}", f"max_dd={result['max_drawdown_pct']:.2%}", result["forward_paper_gate"])


if __name__ == "__main__":
    main()
