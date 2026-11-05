#!/usr/bin/env python3
"""T032 daily conditional R11 forward-paper orchestrator.

The conditional and unconditional tracks share the read-only public quote cache
but keep independent ledgers, positions, equity state, and daily reports. No
order or hedge endpoint is called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_r11_forward_paper_t027 import (
    DAY_MS,
    INITIAL_EQUITY,
    MARGIN_PER_BTC_STRADDLE_USD,
    MARGIN_PER_POSITION_USD,
    MAX_ACTIVE_POSITIONS,
    MIN_OBSERVATIONS,
    POSITION_SIZE_BTC,
    process,
)
from tools.run_r11_vol_risk_premium import sha256_file

SPEC_PATH = ROOT / "research_multi_strategy/R11_FORWARD_PAPER_CONDITIONAL_T032_20261025.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_forward_paper_t032_20261025"
RUN_ID = "r11-forward-paper-t032-20261025-v1"
LABEL = "com.quant.r11forward.t032"
UTC = timezone.utc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _latest_real_boundary_ts(output: Path, prefix: str) -> int | None:
    daily_rows = [row for row in read_jsonl(output / f"{prefix}equity_daily.jsonl") if row.get("run_mode") == "real_boundary"]
    if not daily_rows:
        return None
    latest = max(daily_rows, key=lambda row: int(row["timestamp"]))
    observed_ts = int(latest["timestamp"])
    matching_snapshot = next(
        (row for row in reversed(read_jsonl(output / f"{prefix}snapshots.jsonl")) if int(row.get("timestamp", -1)) == observed_ts),
        {},
    )
    return int(matching_snapshot.get("scheduled_decision_ts", observed_ts))


def _append_forward_gap(output: Path, previous_ts: int | None, current_ts: int) -> dict[str, Any] | None:
    if previous_ts is None or current_ts - previous_ts <= DAY_MS:
        return None
    previous_date = datetime.fromtimestamp(previous_ts / 1000, UTC).date()
    current_date = datetime.fromtimestamp(current_ts / 1000, UTC).date()
    missed_dates = [(previous_date + timedelta(days=offset)).isoformat()
                    for offset in range(1, (current_date - previous_date).days)]
    if not missed_dates:
        return None
    path = output / "forward_gaps.jsonl"
    existing = read_jsonl(path)
    if any(int(row.get("current_observation_ts", -1)) == current_ts for row in existing):
        return None
    record = {
        "previous_observation_ts": previous_ts,
        "previous_observation_iso": datetime.fromtimestamp(previous_ts / 1000, UTC).isoformat(),
        "current_observation_ts": current_ts,
        "current_observation_iso": datetime.fromtimestamp(current_ts / 1000, UTC).isoformat(),
        "missed_dates": missed_dates,
        "gap_days": len(missed_dates),
        "reason": "watchdog_down",
    }
    with path.open("a") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def _performance_metrics(daily_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = sorted(
        (row for row in daily_rows if row.get("run_mode") == "real_boundary"),
        key=lambda row: int(row["timestamp"]),
    )
    if len(rows) < MIN_OBSERVATIONS:
        return None
    equity_values = [INITIAL_EQUITY] + [float(row["equity"]) for row in rows]
    returns: list[float] = []
    for previous, current in zip(equity_values, equity_values[1:]):
        returns.append((current / previous) - 1.0 if previous > 0 else -1.0)
    elapsed_days = max(1.0, (int(rows[-1]["timestamp"]) - int(rows[0]["timestamp"])) / DAY_MS + 1.0)
    final_equity = equity_values[-1]
    annualized_return = (final_equity / INITIAL_EQUITY) ** (365.0 / elapsed_days) - 1.0 if final_equity > 0 else -1.0
    daily_std = statistics.stdev(returns) if len(returns) > 1 else 0.0
    mean_return = statistics.mean(returns) if returns else 0.0
    sharpe = mean_return / daily_std * math.sqrt(365.0) if daily_std > 0 else (0.0 if mean_return == 0 else None)
    peak = equity_values[0]
    drawdowns = []
    for equity in equity_values:
        peak = max(peak, equity)
        drawdowns.append(equity / peak - 1.0 if peak > 0 else -1.0)
    tail_count = max(1, math.ceil(len(returns) * 0.01))
    cvar99 = statistics.mean(sorted(returns)[:tail_count]) if returns else None
    return {
        "observations": len(rows),
        "annualized_return": annualized_return,
        "sharpe": sharpe,
        "max_drawdown": min(drawdowns),
        "cvar99": cvar99,
        "gate": "comparison_ready" if annualized_return >= 0.057 else "rejected",
    }


def _render_forward_status(report: dict[str, Any]) -> str:
    lines = [
        "# R11 Forward Status",
        "",
        f"- run mode: `{report['run_mode']}`",
        f"- forward gap count: `{report['forward_gap_count']}`",
        f"- rejection threshold: annualized return >= `{0.057:.1%}` after `{MIN_OBSERVATIONS}` real observations",
        "",
    ]
    for name in ("conditional", "unconditional"):
        row = report[name]
        lines += [
            f"## {name.title()}",
            "",
            f"- observations: `{row['observations']} / {MIN_OBSERVATIONS}`",
            f"- decision: `{row['decision']}`",
            f"- entry IV: `{row['entry_dvol_1h'] if row['entry_dvol_1h'] is not None else 'unknown'}`",
            f"- trailing median: `{row['trailing_252d_median'] if row['trailing_252d_median'] is not None else 'unknown'}`",
            f"- equity: `{row['equity']}`; cumulative PnL: `{row['cumulative_pnl_usd']}` USDT",
            f"- active positions: `{row.get('active_positions')}`; cap: `{row.get('position_cap', MAX_ACTIVE_POSITIONS)}`; margin: `{row.get('margin_used_usd')}` / `{row.get('margin_per_position_usd', MARGIN_PER_POSITION_USD)}` USDT per position; spread: `{row['observed_spread_pct']}`%",
        ]
        metrics = row.get("performance")
        if metrics is None:
            lines.append(f"- annualized / Sharpe / maxDD / CVaR99: pending until {MIN_OBSERVATIONS} real observations")
        else:
            sharpe = "undefined" if metrics["sharpe"] is None else f"{metrics['sharpe']:.3f}"
            lines += [
                f"- annualized: `{metrics['annualized_return']:.2%}`; Sharpe: `{sharpe}`; maxDD: `{metrics['max_drawdown']:.2%}`; CVaR99: `{metrics['cvar99']:.2%}`",
                f"- gate: `{metrics['gate']}`",
            ]
        lines.append("")
    gaps = report.get("forward_gaps", [])
    if gaps:
        lines += ["## Missed Dates", "", "| missed dates | gap days | reason |", "|---|---:|---|"]
        lines.extend(f"| {', '.join(gap['missed_dates'])} | {gap['gap_days']} | {gap['reason']} |" for gap in gaps)
        lines.append("")
    return "\n".join(lines)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _track_summary(output: Path, prefix: str, report: dict[str, Any]) -> dict[str, Any]:
    ledger = read_jsonl(output / f"{prefix}ledger_events.jsonl")
    skips = sum(event.get("event") == "skipped_low_iv" for event in ledger)
    unknowns = sum(event.get("event") == "conditional_unknown" for event in ledger)
    latest = report.get("latest_daily") or {}
    decision = (report.get("conditional_entry") or {}).get("decision", "unconditional")
    conditional_entry = report.get("conditional_entry") or {}
    return {
        "prefix": prefix,
        "decision": decision,
        "entry_dvol_1h": conditional_entry.get("entry_dvol_1h"),
        "trailing_252d_median": conditional_entry.get("trailing_252d_median"),
        "history_rows": conditional_entry.get("history_rows"),
        "history_spacing_ms": conditional_entry.get("history_spacing_ms"),
        "history_span_days": conditional_entry.get("history_span_days"),
        "active_positions": latest.get("active_positions"),
        "position_cap": latest.get("position_cap"),
        "margin_per_position_usd": latest.get("margin_per_position_usd"),
        "margin_used_usd": latest.get("margin_used_usd"),
        "initial_equity": latest.get("initial_equity", INITIAL_EQUITY),
        "equity": latest.get("equity"),
        "cumulative_pnl_usd": (float(latest["equity"]) - INITIAL_EQUITY) if latest.get("equity") is not None else None,
        "cash_equity": latest.get("cash_equity"),
        "daily_pnl_usd": latest.get("daily_pnl_usd"),
        "observed_spread_pct": latest.get("observed_spread_pct"),
        "observations": report.get("observations"),
        "live_observations": report.get("live_observations"),
        "skipped_low_iv": skips,
        "conditional_unknown": unknowns,
        "lifecycle_event_counts": report.get("lifecycle_event_counts", {}),
    }


def _write_plist(output: Path) -> None:
    script = ROOT / "tools/run_r11_forward_paper_t032.py"
    plist = output / f"{LABEL}.plist"
    plist.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>
<string>/usr/bin/python3</string><string>{script}</string>
<string>--output</string><string>{output}</string><string>--daily</string><string>--record-entry</string>
</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>{output / 'launchd.log'}</string>
<key>StandardErrorPath</key><string>{output / 'launchd.error.log'}</string>
</dict></plist>
''')


def _write_manifest(output: Path, report: dict[str, Any]) -> None:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            try:
                name = str(path.resolve().relative_to(ROOT.resolve()))
            except ValueError:
                name = str(path.resolve())
            files[name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {
        "task": "T032",
        "run_id": RUN_ID,
        "execution_evidence": "forward_paper",
        "spec_sha256": report["spec_sha256"],
        "code_sha256": sha256_file(Path(__file__)),
        "state_runner_code_sha256": sha256_file(ROOT / "tools/run_r11_forward_paper_t027.py"),
        "conditional_rule": "entry_dvol_1h >= median(previous_252_valid_entry_observations)",
        "conditional_prefix": "conditional_",
        "unconditional_prefix": "unconditional_",
        "git_commit": git_value(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(git_value(["git", "status", "--porcelain"])),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "files": files,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def run(output: Path = OUTPUT_DEFAULT, *, now_ms: int | None = None, record_entry: bool = False,
        daily: bool = False, dry_run_lifecycle: bool = False) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    previous_boundary_ts = None if dry_run_lifecycle else _latest_real_boundary_ts(output, "conditional_")
    conditional = process(output, now_ms=now_ms, record_entry=record_entry, daily=daily,
                          dry_run_lifecycle=dry_run_lifecycle, conditional_entry=True,
                          file_prefix="conditional_")
    unconditional = process(output, now_ms=now_ms, record_entry=record_entry, daily=daily,
                            dry_run_lifecycle=dry_run_lifecycle, conditional_entry=False,
                            file_prefix="unconditional_")
    if not dry_run_lifecycle and daily and conditional.get("run_mode") == "real_boundary":
        current_snapshot = conditional.get("snapshot", {})
        current_boundary_ts = int(current_snapshot.get("scheduled_decision_ts", current_snapshot["timestamp"]))
        _append_forward_gap(output, previous_boundary_ts, current_boundary_ts)
    forward_gaps = read_jsonl(output / "forward_gaps.jsonl")
    conditional_summary = _track_summary(output, "conditional_", conditional)
    unconditional_summary = _track_summary(output, "unconditional_", unconditional)
    conditional_summary["performance"] = _performance_metrics(read_jsonl(output / "conditional_equity_daily.jsonl"))
    unconditional_summary["performance"] = _performance_metrics(read_jsonl(output / "unconditional_equity_daily.jsonl"))
    file_mode = "dryrun_" if dry_run_lifecycle else ""
    report = {
        "task": "T032",
        "run_id": RUN_ID,
        "strategy_id": "r11_forward_paper_conditional_dual_track_v1",
        "execution_evidence": "forward_paper",
        "run_mode": "dry_run" if dry_run_lifecycle else ("real_boundary" if daily and conditional.get("run_mode") == "real_boundary" else "capture"),
        "spec_sha256": sha256_file(SPEC_PATH),
        "conditional_rule": {
            "entry_iv_source": "Deribit DVOL 1h close at exact UTC entry",
            "trailing_window": 252,
            "condition": "entry_dvol_1h >= median(previous_252_valid_entry_observations)",
            "holding_days": 30,
            "quantity_per_leg_btc": 0.1,
            "position_capacity": {
                "max_active_positions": MAX_ACTIVE_POSITIONS,
                "margin_per_btc_straddle_usd": MARGIN_PER_BTC_STRADDLE_USD,
                "position_size_btc": POSITION_SIZE_BTC,
                "margin_per_position_usd": MARGIN_PER_POSITION_USD,
                "formula": "min(max_active_positions, floor(initial_equity / margin_per_position_usd))",
                "source": "T026",
            },
            "low_iv_event": "skipped_low_iv",
            "unknown_event": "conditional_unknown",
        },
        "conditional": conditional_summary,
        "unconditional": unconditional_summary,
        "forward_gap_count": len(forward_gaps),
        "forward_gaps": forward_gaps,
        "conditional_report": conditional,
        "unconditional_report": unconditional,
        "state_paths": {
            "conditional": str(output / f"conditional_{file_mode}equity_state.json"),
            "unconditional": str(output / f"unconditional_{file_mode}equity_state.json"),
        },
        "no_real_orders": True,
        "conclusion": "Collect at least 20-30 UTC-boundary paper observations before comparing conditional and unconditional execution; historical synthetic MDE warnings remain applicable.",
    }
    report_json_path = output / f"{file_mode}REPORT.json"
    report_md_path = output / f"{file_mode}REPORT.md"
    status_path = output / f"{file_mode}FORWARD_STATUS.md"
    status_path.write_text(_render_forward_status(report))
    report_json_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = [
        "# T032 R11 conditional daily forward paper",
        "",
        "- execution_evidence: `forward_paper`",
        f"- run_mode: `{report['run_mode']}`",
        f"- spec_sha256: `{report['spec_sha256']}`",
        f"- forward_gap_count: `{report['forward_gap_count']}`",
        "- paper-only: no order or hedge endpoint is called",
        "",
        "The conditional and unconditional tracks share the read-only quote cache and keep separate ledgers and equity state files.",
        "",
        "## Daily decision and equity",
        "",
        "| track | decision | entry DVOL 1h | trailing median | active / cap | margin used | equity | daily PnL | observations | skipped_low_iv |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, key in (("conditional", "conditional"), ("unconditional", "unconditional")):
        row = report[key]
        md.append(f"| {name} | {row['decision']} | {row['entry_dvol_1h'] if row['entry_dvol_1h'] is not None else 'unknown'} | {row['trailing_252d_median'] if row['trailing_252d_median'] is not None else 'unknown'} | {row['active_positions']} / {row.get('position_cap', MAX_ACTIVE_POSITIONS)} | {row.get('margin_used_usd')} / {row.get('margin_per_position_usd', MARGIN_PER_POSITION_USD)} | {row['equity']} | {row['daily_pnl_usd']} | {row['observations']} | {row['skipped_low_iv']} |")
    file_mode = "dryrun_" if dry_run_lifecycle else ""
    md += [
        "",
        "## Files",
        "",
        f"- Conditional: `conditional_{file_mode}ledger_events.jsonl`, `conditional_{file_mode}equity_state.json`, `conditional_{file_mode}equity_daily.jsonl`.",
        f"- Baseline: `unconditional_{file_mode}ledger_events.jsonl`, `unconditional_{file_mode}equity_state.json`, `unconditional_{file_mode}equity_daily.jsonl`.",
        "- Shared DVOL history: `conditional_dvol_history.jsonl`.",
        f"- DVOL gate history: `{report['conditional']['history_rows']}` daily rows, spacing `{report['conditional']['history_spacing_ms']}` ms, span `{report['conditional']['history_span_days']}` days.",
        "- Dry-run lifecycle files are isolated and do not advance live state." if dry_run_lifecycle else "- Live files are appended only at a UTC boundary and are idempotent by timestamp.",
        "",
        "At least 20–30 daily observations are required before an execution comparison. A low-IV decision is recorded as `skipped_low_iv` and creates no conditional fill. A missing current DVOL or incomplete trailing window is fail-closed as `conditional_unknown`.",
        "",
        "## Schedule",
        "",
        f"Cron (UTC): `CRON_TZ=UTC` then `0 0 * * * cd {ROOT} && /usr/bin/python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --daily --record-entry >> artifacts/r11_forward_paper_t032_20261025/cron.log 2>&1`.",
        "Launchd on an Asia/Shanghai machine: use the generated `com.quant.r11forward.t032.plist` at local 08:00 (00:00 UTC).",
        "",
    ]
    report_md_path.write_text("\n".join(md))
    if not dry_run_lifecycle:
        reproduce = output / "reproduce.sh"
        reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --once --record-entry --dry-run-lifecycle\n# Live daily: --daily --record-entry at 00:00 UTC.\n")
        reproduce.chmod(0o755)
        _write_plist(output)
        _write_manifest(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--record-entry", action="store_true")
    parser.add_argument("--dry-run-lifecycle", action="store_true")
    parser.add_argument("--timestamp-ms", type=int, default=None)
    args = parser.parse_args()
    report = run(args.output, now_ms=args.timestamp_ms, record_entry=args.record_entry, daily=args.daily, dry_run_lifecycle=args.dry_run_lifecycle)
    print(report["run_mode"], report["conditional"]["decision"], report["conditional"]["equity"], report["unconditional"]["equity"])


if __name__ == "__main__":
    main()
