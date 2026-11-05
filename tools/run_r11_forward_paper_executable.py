#!/usr/bin/env python3
"""T026 executable-size forward paper (0.1 BTC/underlying unit per leg)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_r11_forward_paper import (
    DAILY_WINDOW_MS,
    DAY_MS,
    EVIDENCE,
    HEDGE_FEE_BPS,
    MIN_OBSERVATIONS,
    ROOT,
    SPREAD_SCENARIOS_PCT,
    SPEC_PATH as T025_SPEC_PATH,
    T023_SPEC_PATH,
    _download_json,
    fetch_snapshot,
    iso,
    public_url,
    sha256_file,
)

SPEC_PATH = ROOT / "research_multi_strategy/R11_EXECUTABLE_SCALE_T026_20261025.md"
T028_SPEC_PATH = ROOT / "research_multi_strategy/R11_FORWARD_PAPER_EXECUTABLE_T028_20261025.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_forward_paper_t027_20261025"
EXECUTABLE_TARGET_UNITS = 0.1
RUN_ID = "r11-executable-scale-20261025-v1"
T028_RUN_ID = "r11-forward-paper-t028-20261025-v1"
LAUNCHD_LABEL = "com.quant.r11forward"
UTC = timezone.utc


def h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def quantity_meta(snapshot: dict[str, Any]) -> dict[str, Any]:
    first = snapshot["legs"]["call"]
    min_trade = float(first.get("min_trade_amount", 0.1))
    contract_size = float(first["contract_size"])
    executable = (EXECUTABLE_TARGET_UNITS // min_trade) * min_trade
    # Floating point floor can underflow exactly 0.1; snap to the instrument step.
    if executable < min_trade and EXECUTABLE_TARGET_UNITS + 1e-12 >= min_trade:
        executable = min_trade
    actual = executable * snapshot["spot_index"]
    return {"target_quantity_per_leg": EXECUTABLE_TARGET_UNITS, "min_trade_amount_per_leg": min_trade,
            "contract_size_btc": contract_size, "executable_quantity_per_leg": executable,
            "actual_notional_usdt_per_leg": actual, "actual_straddle_notional_usdt": 2.0 * actual,
            "notional_residual_usdt_per_leg": (EXECUTABLE_TARGET_UNITS - executable) * snapshot["spot_index"]}


def transform_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    q = quantity_meta(snapshot)
    legs = {}
    for label, leg in snapshot["legs"].items():
        scale = q["executable_quantity_per_leg"] / float(leg["contract_size"])
        legs[label] = dict(leg)
        legs[label]["executable_quantity"] = q["executable_quantity_per_leg"]
        legs[label]["paper_bid_usd"] = float(leg["best_bid_usd"]) * scale
        legs[label]["paper_ask_usd"] = float(leg["best_ask_usd"]) * scale
        legs[label]["paper_mark_usd"] = float(leg["mark_usd"]) * scale
    snapshot = dict(snapshot)
    snapshot["legs"] = legs
    snapshot["paper_unit"] = q
    snapshot["paper_premium_bid_usd"] = sum(x["paper_bid_usd"] for x in legs.values())
    snapshot["paper_premium_ask_usd"] = sum(x["paper_ask_usd"] for x in legs.values())
    snapshot["paper_premium_mark_usd"] = sum(x["paper_mark_usd"] for x in legs.values())
    snapshot["target_hedge_btc"] = sum(float(x["delta"]) * q["executable_quantity_per_leg"] for x in legs.values())
    snapshot["execution_evidence"] = EVIDENCE
    snapshot["scale_basis"] = "executable_0.1_underlying_units_per_leg"
    return snapshot


def base(snapshot: dict[str, Any], rule_hash: str, event: str) -> dict[str, Any]:
    return {"event": event, "timestamp": snapshot["timestamp"], "timestamp_iso": snapshot["timestamp_iso"], "rule_hash": rule_hash, "read_no": snapshot["read_no"], "burn": True, "paper_only": True, "execution_evidence": EVIDENCE}


def lifecycle(snapshot: dict[str, Any], rule_hash: str, *, record_entry: bool, dry_run: bool) -> list[dict[str, Any]]:
    if not (record_entry or dry_run):
        return []
    pid = f"{snapshot['timestamp']}-{snapshot['selected_expiry_ts']}-{snapshot['strike']}"
    events = [base(snapshot, rule_hash, "risk_gate") | {"position_id": pid, "status": "open", "preflight": "paper_only_no_order", "target_quantity_per_leg": EXECUTABLE_TARGET_UNITS}]
    events.append(base(snapshot, rule_hash, "fill") | {"position_id": pid, "side": "sell", "instrument": "BTC-30d-ATM-straddle", "call_instrument": snapshot["legs"]["call"]["instrument_name"], "put_instrument": snapshot["legs"]["put"]["instrument_name"], "expiry_ts": snapshot["selected_expiry_ts"], "strike": snapshot["strike"], "premium_usd_per_contract": snapshot["straddle_premium_bid_usd"], "paper_premium_bid_usd": snapshot["paper_premium_bid_usd"], "quantity_per_leg": snapshot["paper_unit"]["executable_quantity_per_leg"], "actual_notional_usdt_per_leg": snapshot["paper_unit"]["actual_notional_usdt_per_leg"], "actual_straddle_notional_usdt": snapshot["paper_unit"]["actual_straddle_notional_usdt"], "notional_residual_usdt_per_leg": snapshot["paper_unit"]["notional_residual_usdt_per_leg"], "fill_policy": "sell_at_observed_bid_at_executable_min_trade_amount", "dry_run_lifecycle": dry_run})
    if dry_run:
        events.append(base(snapshot, rule_hash, "mark") | {"position_id": pid, "mark_premium_usd": snapshot["paper_premium_mark_usd"], "entry_premium_usd": snapshot["paper_premium_bid_usd"], "unrealized_pnl_usd": snapshot["paper_premium_bid_usd"] - snapshot["paper_premium_mark_usd"], "mark_proxy": "observed_option_mark_at_0.1_units_per_leg", "dry_run_lifecycle": True})
        hedge_notional = abs(snapshot["target_hedge_btc"] * snapshot["perp"]["mark_price"])
        events.append(base(snapshot, rule_hash, "hedge") | {"position_id": pid, "side": "buy_to_offset_short_delta", "quantity_btc": snapshot["target_hedge_btc"], "price_usd": snapshot["perp"]["mark_price"], "hedge_notional_usd": hedge_notional, "fee_bps": HEDGE_FEE_BPS, "fee_usd": hedge_notional * HEDGE_FEE_BPS / 10_000.0, "dry_run_lifecycle": True})
        exit_ask = snapshot.get("paper_premium_ask_usd")
        if exit_ask is None:
            raise RuntimeError("missing executable ask premium for short-straddle exit; fail closed")
        events.append(base(snapshot, rule_hash, "exit") | {"position_id": pid, "exit_reason": "same_snapshot_dry_run_lifecycle", "exit_premium_usd": exit_ask, "mark_premium_usd": snapshot["paper_premium_mark_usd"], "spread_cost_usd": exit_ask - snapshot["paper_premium_mark_usd"], "exit_price_policy": "buy_back_at_observed_ask", "realized_pnl_usd": snapshot["paper_premium_bid_usd"] - exit_ask, "dry_run_lifecycle": True})
    return events


def run_t026_legacy(output: Path = ROOT / "artifacts/r11_executable_scale_20261025", *, now_ms: int | None = None, record_entry: bool = False, daily: bool = False, dry_run_lifecycle: bool = False) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    snapshot, requests = fetch_snapshot(now_ms, output=output)
    snapshot = transform_snapshot(snapshot)
    snapshot["read_no"] = len(read_jsonl(output / "forward_snapshot.jsonl")) + 1
    spec_hash = sha256_file(SPEC_PATH)
    rule_hash = h({"unit": EXECUTABLE_TARGET_UNITS, "hedge_fee_bps": HEDGE_FEE_BPS, "spreads": SPREAD_SCENARIOS_PCT, "entry": "sell_at_bid", "daily_window_ms": DAILY_WINDOW_MS})
    snapshot["rule_hash"] = rule_hash
    if daily and snapshot["schedule_status"] != "on_boundary":
        snapshot["schedule_status"] = "daily_skipped_outside_boundary"
        events: list[dict[str, Any]] = []
    else:
        events = [base(snapshot, rule_hash, "snapshot") | snapshot]
        events.extend(lifecycle(snapshot, rule_hash, record_entry=record_entry, dry_run=dry_run_lifecycle))
    old_ledger = read_jsonl(output / "forward_ledger_events.jsonl")
    old_snapshots = read_jsonl(output / "forward_snapshot.jsonl")
    old_requests = read_jsonl(output / "api_requests.jsonl")
    (output / "forward_ledger_events.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in old_ledger + events))
    (output / "forward_snapshot.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in old_snapshots + [snapshot]))
    (output / "api_requests.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in old_requests + requests))
    observed = snapshot["straddle_bid_ask_width_pct_of_bid"]
    spreads = [0.2, 1.0, observed if observed is not None else 2.25]
    spread_table = [{"option_spread_pct_of_premium": x, "paper_premium_usd": snapshot["paper_premium_bid_usd"], "spread_cost_usd": snapshot["paper_premium_bid_usd"] * x / 100.0, "hedge_fee_bps": HEDGE_FEE_BPS, "observed": x == observed} for x in spreads]
    report = {"task": "T026", "run_id": RUN_ID, "strategy_id": "r11_forward_paper_executable_v1", "execution_evidence": EVIDENCE, "status": "collecting" if len(old_snapshots) + 1 < MIN_OBSERVATIONS else "comparison_ready", "observations": len(old_snapshots) + 1, "spec_sha256": spec_hash, "t025_spec_sha256": sha256_file(T025_SPEC_PATH), "t023_spec_sha256": sha256_file(T023_SPEC_PATH), "rule_hash": rule_hash, "schedule_status": snapshot["schedule_status"], "snapshot": snapshot, "spread_scenarios": spread_table, "lifecycle_event_counts": {name: sum(1 for x in events if x.get("event") == name) for name in ("snapshot", "risk_gate", "fill", "mark", "hedge", "exit")}, "api_request_count": len(old_requests) + len(requests), "conclusion": "Executable-size observation only; no trading-day PnL conclusion until 20-30 daily observations."}
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T026 executable-scale R11 forward paper", "", f"- execution_evidence: `{EVIDENCE}`", f"- status: `{report['status']}`", f"- observations: `{report['observations']}` / `{MIN_OBSERVATIONS}` minimum", f"- spec_sha256: `{spec_hash}`", f"- schedule_status: `{snapshot['schedule_status']}`", "", "This run records real public Deribit quotes and a paper-only lifecycle at the executable minimum quantity. No order or hedge endpoint is called.", "", "## Executable quantity", "", f"- target and executable quantity: `{snapshot['paper_unit']['executable_quantity_per_leg']:.8f}` underlying units per leg", f"- Deribit min trade amount: `{snapshot['paper_unit']['min_trade_amount_per_leg']:.8f}`", f"- actual notional: `{snapshot['paper_unit']['actual_notional_usdt_per_leg']:.4f} USDT` per leg / `{snapshot['paper_unit']['actual_straddle_notional_usdt']:.4f} USDT` straddle", f"- residual: `{snapshot['paper_unit']['notional_residual_usdt_per_leg']:.4f} USDT` per leg", "", "## Observation", "", f"- expiry: `{snapshot['selected_expiry_iso']}`", f"- strike: `{snapshot['strike']}`", f"- spot: `{snapshot['spot_index']:.2f}`", f"- executable bid premium: `{snapshot['paper_premium_bid_usd']:.4f} USD`", f"- executable mark premium: `{snapshot['paper_premium_mark_usd']:.4f} USD`", f"- observed width: `{snapshot['straddle_bid_ask_width_pct_of_bid']:.4f}%` of bid", f"- scaled target hedge: `{snapshot['target_hedge_btc']:.8f} BTC`", "", "## Spread scenarios", "", "| spread | premium | spread cost | hedge fee |", "|---:|---:|---:|---:|"]
    md.extend(f"| {x['option_spread_pct_of_premium']:.4f}% | {x['paper_premium_usd']:.4f} | {x['spread_cost_usd']:.4f} | {x['hedge_fee_bps']:.0f} bp |" for x in spread_table)
    md.append("")
    (output / "REPORT.md").write_text("\n".join(md))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_forward_paper_executable.py --output artifacts/r11_executable_scale_20261025 --once --dry-run-lifecycle\n# 00:00 UTC cron: python3 tools/run_r11_forward_paper_executable.py --output artifacts/r11_executable_scale_20261025 --daily --record-entry\n")
    reproduce.chmod(0o755)
    files = {}
    for p in sorted(output.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[str(p.resolve().relative_to(ROOT.resolve()))] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
    manifest = {"task": "T026", "run_id": RUN_ID, "execution_evidence": EVIDENCE, "spec_sha256": spec_hash, "t025_spec_sha256": sha256_file(T025_SPEC_PATH), "t023_spec_sha256": sha256_file(T023_SPEC_PATH), "code_sha256": sha256_file(Path(__file__)), "generated_at_utc": datetime.now(UTC).isoformat(), "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def run(output: Path = OUTPUT_DEFAULT, *, now_ms: int | None = None, record_entry: bool = False, daily: bool = False,
        dry_run_lifecycle: bool = False, conditional_entry: bool = False) -> dict[str, Any]:
    """Launch the canonical T028 stateful runner.

    Keeping this entry point in the executable-size runner prevents launchd or
    cron from accidentally invoking the old snapshot-only implementation.
    """
    from tools.run_r11_forward_paper_t027 import process

    report = process(output, now_ms=now_ms, record_entry=record_entry, daily=daily, dry_run_lifecycle=dry_run_lifecycle, conditional_entry=conditional_entry)
    report["task"] = "T028"
    report["run_id"] = T028_RUN_ID
    report["strategy_id"] = "r11_forward_paper_t028_v1"
    report["spec_sha256"] = sha256_file(T028_SPEC_PATH)
    report["launchd_label"] = LAUNCHD_LABEL
    report["runner_code_sha256"] = sha256_file(Path(__file__))
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    report_md = output / "REPORT.md"
    if report_md.exists():
        text = report_md.read_text()
        text = text.replace("# T027 executable-size daily forward paper", "# T028 executable-size daily forward paper")
        import re
        text = re.sub(r"- spec_sha256: `[^`]+`", f"- spec_sha256: `{report['spec_sha256']}`", text, count=1)
        text = text.replace("tools/run_r11_forward_paper_t027.py", "tools/run_r11_forward_paper_executable.py")
        text += f"\n- launchd label: `{LAUNCHD_LABEL}`\n- canonical runner: `tools/run_r11_forward_paper_executable.py`\n"
        report_md.write_text(text)
    launchd_plist = output / "com.quant.r11forward.plist"
    launchd_plist.write_text(f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
<key>Label</key><string>{LAUNCHD_LABEL}</string>
<key>ProgramArguments</key><array>
<string>/usr/bin/python3</string>
<string>{ROOT / 'tools/run_r11_forward_paper_executable.py'}</string>
<string>--output</string><string>{output}</string>
<string>--daily</string><string>--record-entry</string>
</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>{output / 'launchd.log'}</string>
<key>StandardErrorPath</key><string>{output / 'launchd.error.log'}</string>
</dict></plist>
""")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.update({"task": "T028", "run_id": T028_RUN_ID, "execution_evidence": EVIDENCE, "spec_sha256": sha256_file(T028_SPEC_PATH), "code_sha256": sha256_file(Path(__file__)), "state_runner_code_sha256": sha256_file(ROOT / "tools/run_r11_forward_paper_t027.py"), "launchd_label": LAUNCHD_LABEL})
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            try:
                file_name = str(path.resolve().relative_to(ROOT.resolve()))
            except ValueError:
                file_name = str(path.resolve())
            files[file_name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--record-entry", action="store_true")
    parser.add_argument("--dry-run-lifecycle", action="store_true")
    parser.add_argument("--conditional-entry", action="store_true", help="gate new entries on trailing 252-observation DVOL median")
    parser.add_argument("--timestamp-ms", type=int, default=None, help="override observation timestamp; useful for a UTC-boundary dry-run")
    args = parser.parse_args()
    report = run(args.output, now_ms=args.timestamp_ms, record_entry=args.record_entry, daily=args.daily, dry_run_lifecycle=args.dry_run_lifecycle, conditional_entry=args.conditional_entry)
    print(report["status"], report["observations"], report["latest_daily"]["equity"])


if __name__ == "__main__":
    main()
