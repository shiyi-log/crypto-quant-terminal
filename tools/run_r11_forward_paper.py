#!/usr/bin/env python3
"""T025 read-only forward paper for a BTC Deribit ATM straddle.

The market data are real public Deribit observations. Every fill and hedge is
paper-only. Quotes are retained at their native one-BTC contract size and are
also scaled to the T023 paper unit of 2,500 USDT notional per option leg.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "research_multi_strategy/R11_FORWARD_PAPER_T025_20261025.md"
T023_SPEC_PATH = ROOT / "research_multi_strategy/R11_VOL_RISK_PREMIUM_T023_20261024.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_forward_paper_20261025"
DAY_MS = 86_400_000
TARGET_DAYS = 30
OPTION_NOTIONAL = 2500.0
HEDGE_FEE_BPS = 8.0
MIN_OBSERVATIONS = 20
DAILY_WINDOW_MS = 5 * 60 * 1000
SPREAD_SCENARIOS_PCT = (0.2, 1.0, 2.25)
RUN_ID = "r11-forward-paper-20261025-v2"
EVIDENCE = "forward_paper"
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


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _download_json(url: str, path: Path, requests: list[dict[str, Any]], fetcher: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        payload = path.read_bytes()
        cached = True
    else:
        if fetcher is None:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "r11-forward-paper/2.0"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    payload = response.read()
            except Exception:
                payload = subprocess.run(["curl", "-fsSL", "--max-time", "30", url], check=True, stdout=subprocess.PIPE).stdout
        else:
            payload = fetcher(url)
        path.write_bytes(payload)
        cached = False
    try:
        cache_name = str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        cache_name = str(path)
    requests.append({"url": url, "cache": cache_name, "sha256": sha256_bytes(payload), "bytes": len(payload), "cached": cached})
    decoded = json.loads(payload)
    if "error" in decoded:
        raise RuntimeError(f"Deribit public API error: {decoded['error']}")
    return decoded


def public_url(method: str, params: dict[str, Any]) -> str:
    return "https://www.deribit.com/api/v2/public/" + method + "?" + urllib.parse.urlencode(params)


def select_atm_pair(instruments: list[dict[str, Any]], spot: float, now_ms: int, target_days: int = TARGET_DAYS) -> dict[str, Any]:
    target_expiry = now_ms + target_days * DAY_MS
    grouped: dict[int, dict[float, dict[str, dict[str, Any]]]] = {}
    for item in instruments:
        if item.get("state") != "open" or not item.get("is_active", True):
            continue
        if item.get("option_type") not in {"call", "put"}:
            continue
        expiry = int(item["expiration_timestamp"])
        if expiry <= now_ms:
            continue
        strike = float(item["strike"])
        grouped.setdefault(expiry, {}).setdefault(strike, {})[item["option_type"]] = item
    candidates = []
    for expiry, strikes in grouped.items():
        for strike, legs in strikes.items():
            if "call" in legs and "put" in legs:
                candidates.append((abs(expiry - target_expiry), abs(strike - spot), expiry, strike, legs))
    if not candidates:
        raise RuntimeError("no active call/put pair available near 30-day expiry")
    _, _, expiry, strike, legs = min(candidates, key=lambda x: (x[0], x[1], x[2], x[3]))
    return {"expiry_ts": expiry, "strike": strike, "call": legs["call"], "put": legs["put"], "target_expiry_ts": target_expiry, "spot": spot}


def quote_usd(price_native: float | None, contract_size: float, underlying_price: float) -> float | None:
    if price_native is None:
        return None
    value = float(price_native) * float(contract_size) * float(underlying_price)
    return value if math.isfinite(value) else None


def paper_quantity(spot: float, contract_size: float) -> dict[str, float]:
    if spot <= 0 or contract_size <= 0:
        raise RuntimeError("invalid spot or contract_size")
    target = OPTION_NOTIONAL / spot
    executable = math.floor(target / contract_size) * contract_size
    actual = executable * spot
    return {"target_quantity_btc": target, "executable_contract_quantity": executable / contract_size,
            "executable_quantity_btc": executable, "actual_notional_usdt": actual,
            "notional_residual_usdt": OPTION_NOTIONAL - actual, "contract_step_btc": contract_size}


def _ticker_payload(decoded: dict[str, Any]) -> dict[str, Any]:
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("ticker result is not an object")
    return result


def fetch_snapshot(now_ms: int | None = None, *, output: Path = OUTPUT_DEFAULT,
                   fetcher: Callable[[str], bytes] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw_cache = output / "raw_cache"
    requests: list[dict[str, Any]] = []
    index = _download_json(public_url("get_index_price", {"index_name": "btc_usd"}), raw_cache / "index_price.json", requests, fetcher)
    spot = float(index["result"]["index_price"])
    instruments = _download_json(public_url("get_instruments", {"currency": "BTC", "kind": "option", "expired": "false"}), raw_cache / "instruments_btc_option.json", requests, fetcher)["result"]
    selected = select_atm_pair(instruments, spot, now_ms)
    tickers: dict[str, dict[str, Any]] = {}
    for label in ("call", "put"):
        instrument = selected[label]
        name = instrument["instrument_name"]
        tickers[label] = {"instrument": instrument, "ticker": _ticker_payload(_download_json(public_url("ticker", {"instrument_name": name}), raw_cache / f"ticker_{name}.json", requests, fetcher))}
    perp_name = "BTC-PERPETUAL"
    perp = _ticker_payload(_download_json(public_url("ticker", {"instrument_name": perp_name}), raw_cache / "ticker_BTC-PERPETUAL.json", requests, fetcher))
    legs: dict[str, Any] = {}
    premium_bid = premium_ask = premium_mark = 0.0
    deltas = 0.0
    quantity_meta: dict[str, float] | None = None
    for label in ("call", "put"):
        instrument = tickers[label]["instrument"]
        ticker = tickers[label]["ticker"]
        contract_size = float(instrument.get("contract_size", 1.0))
        underlying = float(ticker.get("underlying_price") or spot)
        greeks = ticker.get("greeks") or {}
        bid, ask, mark, delta = ticker.get("best_bid_price"), ticker.get("best_ask_price"), ticker.get("mark_price"), greeks.get("delta")
        if bid is None or ask is None or mark is None or delta is None or ticker.get("mark_iv") is None:
            raise RuntimeError(f"missing bid/ask/mark/IV/delta for {label}")
        if quantity_meta is None:
            quantity_meta = paper_quantity(spot, contract_size)
        elif abs(quantity_meta["contract_step_btc"] - contract_size) > 1e-12:
            raise RuntimeError("call/put contract sizes differ")
        leg = {"instrument_name": instrument["instrument_name"], "option_type": instrument["option_type"], "strike": float(instrument["strike"]), "expiry_ts": int(instrument["expiration_timestamp"]), "contract_size": contract_size, "min_trade_amount": float(instrument.get("min_trade_amount", contract_size)), "underlying_price": underlying,
               "best_bid_native": float(bid), "best_ask_native": float(ask), "mark_native": float(mark), "best_bid_usd": quote_usd(float(bid), contract_size, underlying), "best_ask_usd": quote_usd(float(ask), contract_size, underlying), "mark_usd": quote_usd(float(mark), contract_size, underlying), "bid_iv": ticker.get("bid_iv"), "ask_iv": ticker.get("ask_iv"), "mark_iv": ticker.get("mark_iv"), "delta": float(delta), "best_bid_amount": ticker.get("best_bid_amount"), "best_ask_amount": ticker.get("best_ask_amount"), "ticker_timestamp": ticker.get("timestamp")}
        leg["paper_bid_usd"] = leg["best_bid_usd"] * quantity_meta["target_quantity_btc"] / contract_size
        leg["paper_ask_usd"] = leg["best_ask_usd"] * quantity_meta["target_quantity_btc"] / contract_size
        leg["paper_mark_usd"] = leg["mark_usd"] * quantity_meta["target_quantity_btc"] / contract_size
        legs[label] = leg
        premium_bid += leg["best_bid_usd"]
        premium_ask += leg["best_ask_usd"]
        premium_mark += leg["mark_usd"]
        deltas += float(delta) * contract_size
    assert quantity_meta is not None
    target_hedge = deltas * quantity_meta["target_quantity_btc"] / quantity_meta["contract_step_btc"]
    perp_price = float(perp.get("mark_price") or perp.get("underlying_price") or spot)
    schedule_status = "on_boundary" if now_ms % DAY_MS <= DAILY_WINDOW_MS else "outside_boundary"
    snapshot = {"event": "snapshot", "timestamp": now_ms, "timestamp_iso": iso(now_ms), "scheduled_decision_ts": (now_ms // DAY_MS) * DAY_MS, "schedule_status": schedule_status, "asset": "BTC", "spot_index": spot, "selected_expiry_ts": selected["expiry_ts"], "selected_expiry_iso": iso(selected["expiry_ts"]), "strike": selected["strike"], "target_expiry_ts": selected["target_expiry_ts"], "legs": legs,
                "straddle_premium_bid_usd": premium_bid, "straddle_premium_ask_usd": premium_ask, "straddle_premium_mark_usd": premium_mark, "straddle_bid_ask_width_usd": premium_ask - premium_bid, "straddle_bid_ask_width_pct_of_bid": (premium_ask - premium_bid) / premium_bid * 100.0 if premium_bid else None, "straddle_mark_iv": mean([float(legs["call"]["mark_iv"]), float(legs["put"]["mark_iv"])]), "short_straddle_delta_btc_per_contract": -deltas, "target_hedge_btc_per_contract": deltas, "target_hedge_btc": target_hedge, "paper_premium_bid_usd": sum(leg["paper_bid_usd"] for leg in legs.values()), "paper_premium_ask_usd": sum(leg["paper_ask_usd"] for leg in legs.values()), "paper_premium_mark_usd": sum(leg["paper_mark_usd"] for leg in legs.values()), "paper_unit": quantity_meta, "perp": {"instrument_name": perp_name, "mark_price": perp_price, "timestamp": perp.get("timestamp"), "underlying_price": perp.get("underlying_price"), "best_bid_price": perp.get("best_bid_price"), "best_ask_price": perp.get("best_ask_price")}, "execution_evidence": EVIDENCE}
    return snapshot, requests


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _open_positions(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for event in events:
        pid = event.get("position_id")
        if not pid:
            continue
        if event.get("event") == "fill":
            positions[pid] = dict(event)
        elif event.get("event") == "exit":
            positions.pop(pid, None)
    return positions


def _event_base(snapshot: dict[str, Any], rule_hash: str, event: str) -> dict[str, Any]:
    return {"event": event, "timestamp": snapshot["timestamp"], "timestamp_iso": snapshot["timestamp_iso"], "rule_hash": rule_hash, "read_no": snapshot["read_no"], "burn": True, "execution_evidence": EVIDENCE, "paper_only": True}


def lifecycle_events(snapshot: dict[str, Any], existing_events: list[dict[str, Any]], rule_hash: str, *, record_entry: bool, dry_run_lifecycle: bool) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    positions = _open_positions(existing_events)
    for pid, position in list(positions.items()):
        current_mark = snapshot["paper_premium_mark_usd"]
        entry_premium = float(position.get("paper_premium_bid_usd", position.get("premium_usd", 0.0)))
        events.append(_event_base(snapshot, rule_hash, "mark") | {"position_id": pid, "mark_premium_usd": current_mark, "entry_premium_usd": entry_premium, "unrealized_pnl_usd": entry_premium - current_mark, "mark_proxy": "observed_option_mark_scaled_to_2500"})
        hedge_qty = snapshot["target_hedge_btc"]
        hedge_notional = abs(hedge_qty * snapshot["perp"]["mark_price"])
        events.append(_event_base(snapshot, rule_hash, "hedge") | {"position_id": pid, "side": "buy_to_offset_short_delta", "quantity_btc": hedge_qty, "target_delta_btc": hedge_qty, "price_usd": snapshot["perp"]["mark_price"], "fee_bps": HEDGE_FEE_BPS, "fee_usd": hedge_notional * HEDGE_FEE_BPS / 10_000.0, "hedge_notional_usd": hedge_notional})
        if int(position.get("expiry_ts", snapshot["selected_expiry_ts"])) <= snapshot["timestamp"]:
            events.append(_event_base(snapshot, rule_hash, "exit") | {"position_id": pid, "exit_reason": "expiry_mark_or_settlement_proxy", "exit_premium_usd": current_mark, "realized_pnl_usd": entry_premium - current_mark, "dry_run_lifecycle": False})
    if record_entry or dry_run_lifecycle:
        pid = f"{snapshot['timestamp']}-{snapshot['selected_expiry_ts']}-{snapshot['strike']}"
        events.append(_event_base(snapshot, rule_hash, "risk_gate") | {"position_id": pid, "status": "open", "preflight": "paper_only_no_order", "target_notional_usdt_per_leg": OPTION_NOTIONAL})
        fill = _event_base(snapshot, rule_hash, "fill") | {"position_id": pid, "side": "sell", "instrument": "BTC-30d-ATM-straddle", "call_instrument": snapshot["legs"]["call"]["instrument_name"], "put_instrument": snapshot["legs"]["put"]["instrument_name"], "expiry_ts": snapshot["selected_expiry_ts"], "strike": snapshot["strike"], "premium_usd_per_contract": snapshot["straddle_premium_bid_usd"], "paper_premium_bid_usd": snapshot["paper_premium_bid_usd"], "quantity_underlying_btc": snapshot["paper_unit"]["target_quantity_btc"], "target_notional_usdt_per_leg": OPTION_NOTIONAL, "executable_contract_quantity": snapshot["paper_unit"]["executable_contract_quantity"], "actual_notional_usdt": snapshot["paper_unit"]["actual_notional_usdt"], "notional_residual_usdt": snapshot["paper_unit"]["notional_residual_usdt"], "fill_policy": "sell_at_observed_bid_scaled_to_2500_usdt_per_leg", "dry_run_lifecycle": bool(dry_run_lifecycle)}
        events.append(fill)
        if dry_run_lifecycle:
            pid = fill["position_id"]
            events.append(_event_base(snapshot, rule_hash, "mark") | {"position_id": pid, "mark_premium_usd": snapshot["paper_premium_mark_usd"], "entry_premium_usd": snapshot["paper_premium_bid_usd"], "unrealized_pnl_usd": snapshot["paper_premium_bid_usd"] - snapshot["paper_premium_mark_usd"], "mark_proxy": "observed_option_mark_scaled_to_2500", "dry_run_lifecycle": True})
            hedge_qty = snapshot["target_hedge_btc"]
            hedge_notional = abs(hedge_qty * snapshot["perp"]["mark_price"])
            events.append(_event_base(snapshot, rule_hash, "hedge") | {"position_id": pid, "side": "buy_to_offset_short_delta", "quantity_btc": hedge_qty, "target_delta_btc": hedge_qty, "price_usd": snapshot["perp"]["mark_price"], "fee_bps": HEDGE_FEE_BPS, "fee_usd": hedge_notional * HEDGE_FEE_BPS / 10_000.0, "hedge_notional_usd": hedge_notional, "dry_run_lifecycle": True})
            events.append(_event_base(snapshot, rule_hash, "exit") | {"position_id": pid, "exit_reason": "same_snapshot_dry_run_lifecycle", "exit_premium_usd": snapshot["paper_premium_mark_usd"], "realized_pnl_usd": snapshot["paper_premium_bid_usd"] - snapshot["paper_premium_mark_usd"], "dry_run_lifecycle": True})
    return events


def run(output: Path = OUTPUT_DEFAULT, *, now_ms: int | None = None, record_entry: bool = False, daily: bool = False, dry_run_lifecycle: bool = False) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    snapshot, requests = fetch_snapshot(now_ms, output=output)
    spec_hash = sha256_file(SPEC_PATH)
    t023_hash = sha256_file(T023_SPEC_PATH)
    rule_hash = sha256_bytes(canonical_json({"target_days": TARGET_DAYS, "option_notional": OPTION_NOTIONAL, "hedge_fee_bps": HEDGE_FEE_BPS, "entry": "sell_at_bid", "spread_scenarios_pct": SPREAD_SCENARIOS_PCT, "daily_window_ms": DAILY_WINDOW_MS, "quantity": "target_2500_scaled_with_contract_residual"}).encode())
    snapshot["rule_hash"] = rule_hash
    snapshot["read_no"] = len(_read_jsonl(output / "snapshots.jsonl")) + 1
    snapshot["burn"] = True
    existing_ledger = _read_jsonl(output / "ledger_events.jsonl")
    existing_snapshots = _read_jsonl(output / "snapshots.jsonl")
    existing_requests = _read_jsonl(output / "api_requests.jsonl")
    if daily and snapshot["schedule_status"] != "on_boundary":
        snapshot["schedule_status"] = "daily_skipped_outside_boundary"
        new_events: list[dict[str, Any]] = []
    else:
        new_events = [_event_base(snapshot, rule_hash, "snapshot") | snapshot]
        new_events.extend(lifecycle_events(snapshot, existing_ledger, rule_hash, record_entry=record_entry, dry_run_lifecycle=dry_run_lifecycle))
    ledger = existing_ledger + new_events
    all_snapshots = existing_snapshots + [snapshot]
    all_requests = existing_requests + requests
    (output / "ledger_events.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger))
    (output / "snapshots.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in all_snapshots))
    (output / "api_requests.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in all_requests))
    observed_spread = snapshot["straddle_bid_ask_width_pct_of_bid"]
    spread_table = [{"option_spread_pct_of_premium": pct, "paper_entry_premium_usd": snapshot["paper_premium_bid_usd"], "synthetic_spread_cost_usd": snapshot["paper_premium_bid_usd"] * pct / 100.0, "hedge_fee_bps": HEDGE_FEE_BPS, "observed": observed_spread is not None and abs(observed_spread - pct) < 0.25} for pct in SPREAD_SCENARIOS_PCT]
    report = {"task": "T025", "run_id": RUN_ID, "strategy_id": "r11_forward_paper_v2", "execution_evidence": EVIDENCE, "status": "collecting" if len(all_snapshots) < MIN_OBSERVATIONS else "comparison_ready", "observations": len(all_snapshots), "minimum_observations_for_comparison": MIN_OBSERVATIONS, "comparison_ready": len(all_snapshots) >= MIN_OBSERVATIONS, "synthetic_expectation_monthly_bp": 163.45, "spec_sha256": spec_hash, "t023_spec_sha256": t023_hash, "rule_hash": rule_hash, "schedule_status": snapshot["schedule_status"], "spread_scenarios": spread_table, "snapshot": snapshot, "api_request_count": len(all_requests), "lifecycle_event_counts": {name: sum(1 for event in new_events if event.get("event") == name) for name in ("snapshot", "risk_gate", "fill", "mark", "hedge", "exit")}, "conclusion": "No PnL conclusion until 20-30 daily observations; all fills and hedges are paper-only."}
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T025 R11 forward paper", "", f"- execution_evidence: `{EVIDENCE}`", f"- status: `{report['status']}`", f"- observations: `{report['observations']}` / `{MIN_OBSERVATIONS}` minimum", f"- schedule_status: `{snapshot['schedule_status']}`", f"- spec_sha256: `{spec_hash}`", f"- rule_hash: `{rule_hash}`", "", "Real public Deribit bid/ask/mark/IV/greeks are recorded. Every fill, mark, hedge, and exit is a paper ledger event; no order or hedge endpoint is called.", "", "## Paper unit", "", f"The unit is {OPTION_NOTIONAL:.0f} USDT notional per option leg. Target quantity is `{snapshot['paper_unit']['target_quantity_btc']:.10f} BTC`; executable integer contracts are `{snapshot['paper_unit']['executable_contract_quantity']:.0f}` at a `{snapshot['paper_unit']['contract_step_btc']:.4f} BTC` step; actual notional is `{snapshot['paper_unit']['actual_notional_usdt']:.4f} USDT`; residual is `{snapshot['paper_unit']['notional_residual_usdt']:.4f} USDT`. PnL uses the fractional target quantity for comparability and discloses the residual.", "", "## Current observation", "", f"- expiry: `{snapshot['selected_expiry_iso']}`", f"- strike: `{snapshot['strike']}`", f"- spot index: `{snapshot['spot_index']:.2f}`", f"- raw one-contract premium at bid: `{snapshot['straddle_premium_bid_usd']:.4f} USD`", f"- paper scaled premium at bid: `{snapshot['paper_premium_bid_usd']:.4f} USD`", f"- mark premium scaled: `{snapshot['paper_premium_mark_usd']:.4f} USD`", f"- bid/ask width: `{snapshot['straddle_bid_ask_width_usd']:.4f} USD` (`{snapshot['straddle_bid_ask_width_pct_of_bid']:.2f}%` of bid)", f"- target hedge: `{snapshot['target_hedge_btc']:.10f} BTC` (8 bp paper fee)", "", "## Spread comparison", "", "| option spread | paper entry premium | spread cost | hedge fee |", "|---:|---:|---:|---:|"]
    md.extend(f"| {row['option_spread_pct_of_premium']:.2f}% | {row['paper_entry_premium_usd']:.4f} | {row['synthetic_spread_cost_usd']:.4f} | {row['hedge_fee_bps']:.0f} bp |" for row in spread_table)
    md.extend(["", "A same-snapshot lifecycle is explicitly labeled `dry_run_lifecycle` and is excluded from trading-day PnL conclusions.", ""])
    (output / "REPORT.md").write_text("\n".join(md))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_forward_paper.py --output artifacts/r11_forward_paper_20261025 --once --record-entry\n# Daily cron at 00:00 UTC (the runner skips outside its five-minute UTC window):\n# 0 0 * * * cd /Users/shiyi/Documents/ChatGPT/量化 && python3 tools/run_r11_forward_paper.py --output artifacts/r11_forward_paper_20261025 --daily --record-entry\n")
    reproduce.chmod(0o755)
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            try:
                file_name = str(path.resolve().relative_to(ROOT.resolve()))
            except ValueError:
                file_name = str(path.resolve())
            files[file_name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {"task": "T025", "run_id": RUN_ID, "execution_evidence": EVIDENCE, "spec_sha256": spec_hash, "t023_spec_sha256": t023_hash, "rule_hash": rule_hash, "code_sha256": sha256_file(Path(__file__)), "git_commit": git_value(["git", "rev-parse", "HEAD"]), "git_dirty": bool(git_value(["git", "status", "--porcelain"])), "generated_at_utc": datetime.now(UTC).isoformat(), "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--once", action="store_true", help="capture one snapshot and exit")
    parser.add_argument("--daily", action="store_true", help="capture only inside the 00:00 UTC five-minute window")
    parser.add_argument("--record-entry", action="store_true", help="record a paper-only fill at observed bid")
    parser.add_argument("--dry-run-lifecycle", action="store_true", help="open, mark, hedge, and exit one same-snapshot paper position")
    args = parser.parse_args()
    report = run(args.output, record_entry=args.record_entry, daily=args.daily, dry_run_lifecycle=args.dry_run_lifecycle)
    print(report["snapshot"]["selected_expiry_iso"], report["snapshot"]["strike"], report["snapshot"]["paper_premium_bid_usd"], report["schedule_status"])


if __name__ == "__main__":
    main()
