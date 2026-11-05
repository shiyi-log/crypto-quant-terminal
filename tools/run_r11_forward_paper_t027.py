#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T027 executable-size daily forward paper with persistent equity ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_r11_forward_paper import (  # noqa: E402
    DAILY_WINDOW_MS,
    DAY_MS,
    EVIDENCE,
    HEDGE_FEE_BPS,
    MIN_OBSERVATIONS,
    ROOT,
    fetch_snapshot,
    iso,
    sha256_file,
)
from tools.run_r11_forward_paper_executable import transform_snapshot  # noqa: E402

SPEC_PATH = ROOT / "research_multi_strategy/R11_FORWARD_PAPER_EXECUTABLE_T027_20261025.md"
T026_SPEC_PATH = ROOT / "research_multi_strategy/R11_EXECUTABLE_SCALE_T026_20261025.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_forward_paper_t027_20261025"
INITIAL_EQUITY = 50_000.0
MAX_ACTIVE_POSITIONS = 10
# T026 sizing convention: approximately 4,800 USDT initial margin for a
# 1.0 BTC straddle, scaled to the executable 0.1 BTC paper unit.
MARGIN_PER_BTC_STRADDLE_USD = 4_800.0
POSITION_SIZE_BTC = 0.1
MARGIN_PER_POSITION_USD = MARGIN_PER_BTC_STRADDLE_USD * POSITION_SIZE_BTC
SYNTHETIC_EXPECTATION_MONTHLY_BP = 163.45
REJECT_ANNUALIZED_RETURN = 0.057
RUN_ID = "r11-forward-paper-t027-20261025-v1"
UTC = timezone.utc


def position_capacity(initial_equity: float, margin_per_position_usd: float = MARGIN_PER_POSITION_USD) -> int:
    """Return the independent active-position cap for one forward track."""
    if margin_per_position_usd <= 0:
        raise ValueError("margin_per_position_usd must be positive")
    affordable = math.floor(max(0.0, float(initial_equity)) / margin_per_position_usd)
    return min(MAX_ACTIVE_POSITIONS, affordable)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows))


def base(snapshot: dict[str, Any], rule_hash: str, event: str) -> dict[str, Any]:
    return {"event": event, "timestamp": snapshot["timestamp"], "timestamp_iso": snapshot["timestamp_iso"], "rule_hash": rule_hash, "read_no": snapshot["read_no"], "burn": True, "paper_only": True, "execution_evidence": EVIDENCE}


def rebuild_state_from_ledger(path: Path) -> dict[str, Any]:
    state = {"initial_equity": INITIAL_EQUITY, "cash_equity": INITIAL_EQUITY, "positions": {}, "last_equity": INITIAL_EQUITY, "last_timestamp": None}
    for event in read_jsonl(path):
        pid = event.get("position_id")
        if event.get("event") == "fill" and pid:
            state["positions"][pid] = {"position_id": pid, "entry_ts": event.get("timestamp"), "expiry_ts": event.get("expiry_ts"), "entry_premium_usd": event.get("paper_premium_bid_usd", 0.0), "last_mark_premium_usd": event.get("paper_premium_bid_usd", 0.0), "quantity_per_leg": event.get("quantity_per_leg", 0.1)}
        elif event.get("event") == "hedge":
            state["cash_equity"] -= float(event.get("fee_usd", 0.0))
        elif event.get("event") == "exit" and pid:
            state["cash_equity"] += float(event.get("realized_pnl_usd", 0.0))
            state["positions"].pop(pid, None)
    return state


def load_state(path: Path, ledger_path: Path) -> dict[str, Any]:
    if not path.exists():
        return rebuild_state_from_ledger(ledger_path) if ledger_path.exists() else {"initial_equity": INITIAL_EQUITY, "cash_equity": INITIAL_EQUITY, "positions": {}, "last_equity": INITIAL_EQUITY, "last_timestamp": None}
    return json.loads(path.read_text())


def mark_equity(state: dict[str, Any]) -> float:
    return float(state["cash_equity"]) + sum(float(pos["entry_premium_usd"]) - float(pos["last_mark_premium_usd"]) for pos in state["positions"].values())


def require_exit_ask(snapshot: dict[str, Any]) -> float:
    ask = snapshot.get("paper_premium_ask_usd")
    if ask is None:
        raise RuntimeError("missing executable ask premium for short-straddle exit; fail closed")
    try:
        ask_value = float(ask)
    except (TypeError, ValueError):
        raise RuntimeError("invalid executable ask premium for short-straddle exit; fail closed")
    if ask_value != ask_value or ask_value in (float("inf"), float("-inf")):
        raise RuntimeError("non-finite executable ask premium for short-straddle exit; fail closed")
    return ask_value


def process(output: Path = OUTPUT_DEFAULT, *, now_ms: int | None = None, record_entry: bool = False, daily: bool = False,
            dry_run_lifecycle: bool = False, conditional_entry: bool = False, file_prefix: str = "") -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    is_dry_run = bool(dry_run_lifecycle)
    ledger_path = output / (f"{file_prefix}dryrun_ledger_events.jsonl" if is_dry_run else f"{file_prefix}ledger_events.jsonl")
    snapshots_path = output / (f"{file_prefix}dryrun_snapshots.jsonl" if is_dry_run else f"{file_prefix}snapshots.jsonl")
    requests_path = output / (f"{file_prefix}dryrun_api_requests.jsonl" if is_dry_run else f"{file_prefix}api_requests.jsonl")
    equity_daily_path = output / (f"{file_prefix}dryrun_equity_daily.jsonl" if is_dry_run else f"{file_prefix}equity_daily.jsonl")
    equity_state_path = output / (f"{file_prefix}dryrun_equity_state.json" if is_dry_run else f"{file_prefix}equity_state.json")
    report_path = output / (f"{file_prefix}dryrun_REPORT.json" if is_dry_run else f"{file_prefix}REPORT.json")
    report_md_path = output / (f"{file_prefix}dryrun_REPORT.md" if is_dry_run else f"{file_prefix}REPORT.md")
    snapshot, requests = fetch_snapshot(now_ms, output=output)
    snapshot = transform_snapshot(snapshot)
    conditional_info: dict[str, Any] = {"enabled": conditional_entry, "decision": "unconditional"}
    if conditional_entry:
        from tools.run_r11_conditional import live_entry_gate
        # The entry rule is defined at the UTC 00:00 boundary.  A cron process
        # can start a few seconds into the five-minute capture window, so use
        # the snapshot's normalized scheduled_decision_ts for the DVOL 1h
        # lookup rather than the wall-clock quote timestamp.
        entry_gate_ts = int(snapshot.get("scheduled_decision_ts", snapshot["timestamp"]))
        conditional_info = {"enabled": True, "entry_timestamp_ms": entry_gate_ts,
                            **live_entry_gate(entry_gate_ts, output)}
        snapshot["conditional_entry"] = conditional_info
    snapshots = read_jsonl(snapshots_path)
    snapshot["read_no"] = len(snapshots) + 1
    rule_hash = digest({"unit": 0.1, "hedge_fee_bps": HEDGE_FEE_BPS, "daily_window_ms": DAILY_WINDOW_MS, "entry": "sell_at_bid"})
    snapshot["rule_hash"] = rule_hash
    old_ledger = read_jsonl(ledger_path)
    # A retry at the same timestamp must be a no-op. This protects cron/launchd
    # retries from duplicating fills, hedges, exits, or equity observations.
    if any(int(row.get("timestamp", -1)) == int(snapshot["timestamp"]) for row in snapshots):
        existing_report = report_path
        if existing_report.exists():
            return json.loads(existing_report.read_text())
    state = load_state(equity_state_path, ledger_path)
    prior_equity = mark_equity(state)
    events: list[dict[str, Any]] = []
    if daily and snapshot["schedule_status"] != "on_boundary":
        snapshot["schedule_status"] = "daily_skipped_outside_boundary"
    else:
        events.append(base(snapshot, rule_hash, "snapshot") | snapshot)
        # Mark and hedge all existing positions, then exit any that have expired.
        for pid, position in list(state["positions"].items()):
            current_mark = snapshot["paper_premium_mark_usd"]
            mark_pnl = float(position["entry_premium_usd"]) - current_mark
            events.append(base(snapshot, rule_hash, "mark") | {"position_id": pid, "entry_premium_usd": position["entry_premium_usd"], "mark_premium_usd": current_mark, "unrealized_pnl_usd": mark_pnl, "daily_mark_pnl_usd": float(position["last_mark_premium_usd"]) - current_mark, "mark_proxy": "observed_option_mark_at_0.1_units_per_leg"})
            hedge_qty = snapshot["target_hedge_btc"]
            hedge_notional = abs(hedge_qty * snapshot["perp"]["mark_price"])
            hedge_fee = hedge_notional * HEDGE_FEE_BPS / 10_000.0
            state["cash_equity"] -= hedge_fee
            events.append(base(snapshot, rule_hash, "hedge") | {"position_id": pid, "quantity_btc": hedge_qty, "price_usd": snapshot["perp"]["mark_price"], "hedge_notional_usd": hedge_notional, "fee_bps": HEDGE_FEE_BPS, "fee_usd": hedge_fee})
            position["last_mark_premium_usd"] = current_mark
            if int(position["expiry_ts"]) <= snapshot["timestamp"]:
                exit_ask = require_exit_ask(snapshot)
                realized = float(position["entry_premium_usd"]) - exit_ask
                state["cash_equity"] += realized
                events.append(base(snapshot, rule_hash, "exit") | {"position_id": pid, "exit_reason": "expiry_ask_buyback", "exit_premium_usd": exit_ask, "mark_premium_usd": current_mark, "spread_cost_usd": exit_ask - current_mark, "exit_price_policy": "buy_back_at_observed_ask", "realized_pnl_usd": realized, "dry_run_lifecycle": False})
                del state["positions"][pid]
        # Open one new position. For audit mode, complete its lifecycle on this snapshot.
        entry_permitted = (not conditional_entry) or conditional_info.get("decision") == "enter"
        if conditional_entry and not entry_permitted:
            skip_event = "skipped_low_iv" if conditional_info.get("decision") == "skipped_low_iv" else "conditional_unknown"
            events.append(base(snapshot, rule_hash, skip_event) | {"decision": conditional_info.get("decision"), "entry_dvol_1h": conditional_info.get("entry_dvol_1h"), "trailing_252d_median": conditional_info.get("trailing_252d_median"), "conditional_entry": True})
        position_cap = position_capacity(state.get("initial_equity", INITIAL_EQUITY))
        margin_used = len(state["positions"]) * MARGIN_PER_POSITION_USD
        if (record_entry or dry_run_lifecycle) and entry_permitted:
            active_positions = len(state["positions"])
            if active_positions >= position_cap:
                events.append(base(snapshot, rule_hash, "capacity_blocked") | {
                    "position_cap": position_cap,
                    "cap": position_cap,
                    "active_positions": active_positions,
                    "active": active_positions,
                    "margin_per_position_usd": MARGIN_PER_POSITION_USD,
                    "margin_used_usd": margin_used,
                    "initial_equity": float(state.get("initial_equity", INITIAL_EQUITY)),
                    "reason": "active_position_cap_reached",
                })
            else:
                pid = f"{snapshot['timestamp']}-{snapshot['selected_expiry_ts']}-{snapshot['strike']}"
                events.append(base(snapshot, rule_hash, "risk_gate") | {"position_id": pid, "status": "open", "preflight": "paper_only_no_order", "target_quantity_per_leg": 0.1})
                fill = base(snapshot, rule_hash, "fill") | {"position_id": pid, "side": "sell", "instrument": "BTC-30d-ATM-straddle", "expiry_ts": snapshot["selected_expiry_ts"], "strike": snapshot["strike"], "paper_premium_bid_usd": snapshot["paper_premium_bid_usd"], "quantity_per_leg": snapshot["paper_unit"]["executable_quantity_per_leg"], "actual_notional_usdt_per_leg": snapshot["paper_unit"]["actual_notional_usdt_per_leg"], "actual_straddle_notional_usdt": snapshot["paper_unit"]["actual_straddle_notional_usdt"], "notional_residual_usdt_per_leg": snapshot["paper_unit"]["notional_residual_usdt_per_leg"], "fill_policy": "sell_at_observed_bid_at_executable_min_trade_amount", "dry_run_lifecycle": dry_run_lifecycle}
                events.append(fill)
                state["positions"][pid] = {"position_id": pid, "entry_ts": snapshot["timestamp"], "expiry_ts": snapshot["selected_expiry_ts"], "entry_premium_usd": snapshot["paper_premium_bid_usd"], "last_mark_premium_usd": snapshot["paper_premium_bid_usd"], "quantity_per_leg": snapshot["paper_unit"]["executable_quantity_per_leg"]}
                if dry_run_lifecycle:
                    current_mark = snapshot["paper_premium_mark_usd"]
                    events.append(base(snapshot, rule_hash, "mark") | {"position_id": pid, "entry_premium_usd": snapshot["paper_premium_bid_usd"], "mark_premium_usd": current_mark, "unrealized_pnl_usd": snapshot["paper_premium_bid_usd"] - current_mark, "daily_mark_pnl_usd": snapshot["paper_premium_bid_usd"] - current_mark, "mark_proxy": "observed_option_mark_at_0.1_units_per_leg", "dry_run_lifecycle": True})
                    hedge_qty = snapshot["target_hedge_btc"]
                    hedge_notional = abs(hedge_qty * snapshot["perp"]["mark_price"])
                    hedge_fee = hedge_notional * HEDGE_FEE_BPS / 10_000.0
                    state["cash_equity"] -= hedge_fee
                    events.append(base(snapshot, rule_hash, "hedge") | {"position_id": pid, "quantity_btc": hedge_qty, "price_usd": snapshot["perp"]["mark_price"], "hedge_notional_usd": hedge_notional, "fee_bps": HEDGE_FEE_BPS, "fee_usd": hedge_fee, "dry_run_lifecycle": True})
                    exit_ask = require_exit_ask(snapshot)
                    realized = snapshot["paper_premium_bid_usd"] - exit_ask
                    state["cash_equity"] += realized
                    events.append(base(snapshot, rule_hash, "exit") | {"position_id": pid, "exit_reason": "same_snapshot_dry_run_lifecycle", "exit_premium_usd": exit_ask, "mark_premium_usd": current_mark, "spread_cost_usd": exit_ask - current_mark, "exit_price_policy": "buy_back_at_observed_ask", "realized_pnl_usd": realized, "dry_run_lifecycle": True})
                    del state["positions"][pid]
    equity = mark_equity(state)
    daily_pnl = equity - prior_equity
    notional = float(snapshot["paper_unit"]["actual_straddle_notional_usdt"])
    monthlyized_bp = daily_pnl / notional * 10_000.0 * 30.0 if notional else None
    deviation = monthlyized_bp - SYNTHETIC_EXPECTATION_MONTHLY_BP if monthlyized_bp is not None else None
    live_daily_rows = read_jsonl(output / f"{file_prefix}equity_daily.jsonl")
    live_observations = sum(row.get("run_mode") == "real_boundary" for row in live_daily_rows)
    is_real_boundary = not is_dry_run and daily and snapshot["schedule_status"] == "on_boundary"
    live_observations += int(is_real_boundary)
    observations = live_observations
    boundary_rows = [row for row in live_daily_rows if row.get("run_mode") == "real_boundary"]
    first_boundary_ts = int(boundary_rows[0]["timestamp"]) if boundary_rows else int(snapshot["timestamp"])
    elapsed_days = max(1.0, (int(snapshot["timestamp"]) - first_boundary_ts) / DAY_MS + 1.0)
    annualized = None if is_dry_run else ((equity / INITIAL_EQUITY) ** (365.0 / elapsed_days) - 1.0 if equity > 0 else -1.0)
    status = "dry_run" if is_dry_run else ("collecting" if observations < MIN_OBSERVATIONS else ("rejected" if annualized < REJECT_ANNUALIZED_RETURN else "comparison_ready"))
    position_cap = position_capacity(state.get("initial_equity", INITIAL_EQUITY))
    margin_used = len(state["positions"]) * MARGIN_PER_POSITION_USD
    daily_row = {"timestamp": snapshot["timestamp"], "timestamp_iso": snapshot["timestamp_iso"], "equity": equity, "cash_equity": state["cash_equity"], "daily_pnl_usd": daily_pnl, "monthlyized_pnl_bp": monthlyized_bp, "synthetic_expectation_monthly_bp": SYNTHETIC_EXPECTATION_MONTHLY_BP, "deviation_bp": deviation, "observed_spread_pct": snapshot["straddle_bid_ask_width_pct_of_bid"], "active_positions": len(state["positions"]), "position_cap": position_cap, "margin_per_position_usd": MARGIN_PER_POSITION_USD, "margin_used_usd": margin_used, "initial_equity": float(state.get("initial_equity", INITIAL_EQUITY)), "observations": observations, "live_observations": live_observations, "run_mode": "dry_run" if is_dry_run else ("real_boundary" if daily and snapshot["schedule_status"] == "on_boundary" else "capture"), "status": status, "execution_evidence": EVIDENCE}
    write_jsonl(ledger_path, old_ledger + events)
    write_jsonl(snapshots_path, snapshots + [snapshot])
    write_jsonl(requests_path, read_jsonl(requests_path) + requests)
    write_jsonl(equity_daily_path, read_jsonl(equity_daily_path) + [daily_row])
    state["last_equity"] = equity
    state["last_timestamp"] = snapshot["timestamp"]
    state["last_equity"] = equity
    state["last_timestamp"] = snapshot["timestamp"]
    (equity_state_path).write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    report = {"task": "T027", "run_id": RUN_ID, "strategy_id": "r11_forward_paper_t027_v1", "execution_evidence": EVIDENCE, "run_mode": "dry_run" if is_dry_run else daily and snapshot["schedule_status"] == "on_boundary" and "real_boundary" or "capture", "status": status, "observations": observations, "live_observations": live_observations, "minimum_observations_for_comparison": MIN_OBSERVATIONS, "spec_sha256": sha256_file(SPEC_PATH), "t026_spec_sha256": sha256_file(T026_SPEC_PATH), "synthetic_expectation_monthly_bp": SYNTHETIC_EXPECTATION_MONTHLY_BP, "reject_annualized_return_threshold": REJECT_ANNUALIZED_RETURN, "position_capacity": {"max_active_positions": MAX_ACTIVE_POSITIONS, "margin_per_btc_straddle_usd": MARGIN_PER_BTC_STRADDLE_USD, "position_size_btc": POSITION_SIZE_BTC, "margin_per_position_usd": MARGIN_PER_POSITION_USD, "formula": "min(max_active_positions, floor(initial_equity / margin_per_position_usd))", "source": "T026"}, "conditional_entry": conditional_info, "latest_daily": daily_row, "snapshot": snapshot, "lifecycle_event_counts": {name: sum(1 for event in events if event.get("event") == name) for name in ("snapshot", "risk_gate", "fill", "capacity_blocked", "mark", "hedge", "exit")}, "conditional_event_counts": {name: sum(1 for event in events if event.get("event") == name) for name in ("skipped_low_iv", "conditional_unknown")}, "live_state_path": str(equity_state_path.relative_to(output.parent) if not is_dry_run else "artifacts/r11_forward_paper_t027_20261025/equity_state.json"), "conclusion": "Dry-run is isolated from live state; collect at least 20-30 real daily observations; no real orders or hedges are connected."}
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T027 executable-size daily forward paper", "", f"- execution_evidence: `{EVIDENCE}`", f"- run_mode: `{report['run_mode']}`", f"- status: `{status}`", f"- observations: `{observations}` (live observations: `{live_observations}`) / `{MIN_OBSERVATIONS}` minimum", f"- spec_sha256: `{report['spec_sha256']}`", f"- active-position cap: `min({MAX_ACTIVE_POSITIONS}, floor(initial_equity / {MARGIN_PER_POSITION_USD:.0f}))`; margin per position: `{MARGIN_PER_POSITION_USD:.0f} USDT` (`{MARGIN_PER_BTC_STRADDLE_USD:.0f} USDT/BTC straddle × {POSITION_SIZE_BTC:.1f} BTC`)", "", "Real public Deribit quotes are recorded at 0.1 BTC/leg. Fills, hedges and exits are paper-only; no order endpoint is called.", "", "Dry-run lifecycle records are isolated under `dryrun_*` files and do not advance live equity or observations.", "", "## Latest daily report", "", f"- observed bid/ask spread: `{snapshot['straddle_bid_ask_width_pct_of_bid']:.4f}%` of bid", f"- executable premium at bid: `{snapshot['paper_premium_bid_usd']:.4f} USDT`", f"- today paper PnL: `{daily_pnl:.4f} USDT`", f"- cumulative equity: `{equity:.4f} USDT`", f"- monthlyized paper PnL: `{monthlyized_bp:.4f} bp`", f"- deviation from synthetic +{SYNTHETIC_EXPECTATION_MONTHLY_BP:.2f} bp/month: `{deviation:.4f} bp`", f"- active positions: `{len(state['positions'])}` / `{position_cap}`; margin used: `{margin_used:.2f} USDT`", "", "The same-snapshot dry-run is explicitly excluded from trading-day performance conclusions.", "", "## Schedule", "", "Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && /usr/bin/python3 tools/run_r11_forward_paper_executable.py --output artifacts/r11_forward_paper_t027_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t027_20261025/cron.log 2>&1`.", "Launchd on an Asia/Shanghai machine: run at local 08:00 (00:00 UTC) with `StartCalendarInterval` Hour=8, Minute=0 and the same canonical `--daily --record-entry` command.", "", "After 20-30 trading days, mark `rejected` if annualized paper return is below 5.7% or slippage is persistently large; otherwise continue the execution review.", ""]
    report_md_path.write_text("\n".join(md))
    if not is_dry_run:
        reproduce = output / "reproduce.sh"
        reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_forward_paper_t027.py --output artifacts/r11_forward_paper_t027_20261025 --once --record-entry --dry-run-lifecycle\n# Daily cron at 00:00 UTC: use --daily --record-entry without --dry-run-lifecycle.\n")
        reproduce.chmod(0o755)
        files = {}
        for p in sorted(output.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                try:
                    name = str(p.resolve().relative_to(ROOT.resolve()))
                except ValueError:
                    name = str(p.resolve())
                files[name] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
        manifest = {"task": "T027", "run_id": RUN_ID, "execution_evidence": EVIDENCE, "spec_sha256": report["spec_sha256"], "t026_spec_sha256": report["t026_spec_sha256"], "code_sha256": sha256_file(Path(__file__)), "generated_at_utc": datetime.now(UTC).isoformat(), "files": files}
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--record-entry", action="store_true")
    parser.add_argument("--dry-run-lifecycle", action="store_true")
    parser.add_argument("--conditional-entry", action="store_true", help="gate new entries on trailing 252-observation DVOL median")
    args = parser.parse_args()
    report = process(args.output, record_entry=args.record_entry, daily=args.daily, dry_run_lifecycle=args.dry_run_lifecycle, conditional_entry=args.conditional_entry)
    print(report["status"], report["observations"], report["latest_daily"]["equity"])


if __name__ == "__main__":
    main()
