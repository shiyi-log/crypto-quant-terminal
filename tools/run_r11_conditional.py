#!/usr/bin/env python3
"""T031 preregistered trailing-DVOL conditional R11 rule.

The primary rule is deliberately parameter-free in absolute IV terms:
enter a 30-day short ATM straddle only when the exact-entry DVOL 1h close is
at least the median of the preceding 252 valid daily entry observations.
This module also provides the live forward-paper gate used by the conditional
runner; it never places orders or hedges.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import math
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_r11_vol_risk_premium import DAY_MS, END_MS, HEDGE_FEE_GRID_BPS, OPTION_SPREAD_GRID, sha256_file
from tools.run_r11_vol_risk_premium import VAR_NOTIONAL, finite_positive
from tools.run_r11_portfolio import SOURCE_POSITIONS

SPEC_PATH = ROOT / "research_multi_strategy/R11_CONDITIONAL_T031_20261025.md"
T023_SPEC_PATH = ROOT / "research_multi_strategy/R11_VOL_RISK_PREMIUM_T023_20261024.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_conditional_20261025"
TRAILING_DAYS = 252
HOLDING_DAYS = 30
EXECUTABLE_UNITS_PER_LEG = 0.1
OPTION_SPREAD_PRIMARY = 0.0225
HEDGE_FEE_PRIMARY_BPS = 16.0
PRIMARY_ASSET = "BTC"
BOOTSTRAP_ITERATIONS = 20_000
SEED = 20261031
DVOL_HOUR_MS = 3_600_000
UTC = timezone.utc


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows))


def load_candidates(path: Path = SOURCE_POSITIONS, asset: str = PRIMARY_ASSET) -> list[dict[str, Any]]:
    rows = []
    for row in read_jsonl(path):
        if row.get("method") != "short_atm_straddle" or row.get("frequency") != "daily" or row.get("asset") != asset:
            continue
        if any(row.get(k) is None for k in ("entry_ts", "expiry_ts", "iv", "gross_pnl", "premium_received", "hedge_turnover")):
            continue
        rows.append(dict(row))
    rows.sort(key=lambda x: int(x["entry_ts"]))
    return rows


def trailing_median(rows: list[dict[str, Any]], index: int, window: int = TRAILING_DAYS) -> float | None:
    if index < window:
        return None
    prior = [float(row["iv"]) for row in rows[index - window:index]]
    if len(prior) != window or not all(math.isfinite(x) and x > 0 for x in prior):
        return None
    return statistics.median(prior)


def decision_log(rows: list[dict[str, Any]], *, threshold_multiplier: float = 1.0) -> list[dict[str, Any]]:
    if threshold_multiplier <= 0 or not math.isfinite(threshold_multiplier):
        raise ValueError("threshold_multiplier must be finite and positive")
    out = []
    next_available = -1
    for i, row in enumerate(rows):
        median = trailing_median(rows, i)
        ts = int(row["entry_ts"])
        base = {"entry_ts": ts, "entry_date": row.get("entry_date"), "iv": float(row["iv"]),
                "trailing_window_days": TRAILING_DAYS, "trailing_median_iv": median,
                "threshold_multiplier": threshold_multiplier, "threshold_iv": None if median is None else median * threshold_multiplier,
                "expiry_ts": int(row["expiry_ts"])}
        if median is None:
            out.append(base | {"decision": "skipped_warmup"})
        elif float(row["iv"]) < median * threshold_multiplier:
            out.append(base | {"decision": "skipped_low_iv"})
        elif ts < next_available:
            out.append(base | {"decision": "skipped_overlap"})
        else:
            out.append(base | {"decision": "enter"})
            next_available = int(row["expiry_ts"])
    return out


def _scaled_pnl(row: dict[str, Any], spread: float, fee_bps: float) -> tuple[float, float]:
    spot = finite_positive(row.get("strike"), "entry spot")
    executable_leg_notional = EXECUTABLE_UNITS_PER_LEG * spot
    scale = executable_leg_notional / VAR_NOTIONAL
    pnl = scale * (float(row["gross_pnl"]) - abs(float(row["premium_received"])) * spread
                   - float(row.get("hedge_turnover", 0.0)) * fee_bps / 10_000.0)
    return pnl, executable_leg_notional


def _stats(rows: list[dict[str, Any]], *, spread: float, fee_bps: float) -> dict[str, Any]:
    selected = []
    for row in rows:
        pnl, notional = _scaled_pnl(row, spread, fee_bps)
        selected.append({"entry_ts": int(row["entry_ts"]), "net_pnl": pnl, "notional": notional,
                         "net_bps": pnl / notional * 10_000.0})
    if not selected:
        return {"n": 0, "n_unique": 0, "mean_bps": None, "ci95_bps": [None, None], "p": None,
                "mde_bps": None, "effective_independent_n": 0, "status": "not_estimable"}
    blocks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        blocks[int(row["entry_ts"]) // (30 * DAY_MS)].append(row)
    block_means = [statistics.mean(x["net_bps"] for x in block) for block in blocks.values()]
    observed = statistics.mean(x["net_bps"] for x in selected)
    rng = __import__("random").Random(SEED + int(spread * 10000) + int(fee_bps))
    boot = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        boot.append(statistics.mean(block_means[rng.randrange(len(block_means))] for _ in block_means))
    boot.sort()
    ci = [boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]]
    p = min(1.0, 2 * min(sum(x <= 0 for x in boot), sum(x >= 0 for x in boot)) / len(boot))
    se = statistics.stdev(block_means) / math.sqrt(len(block_means)) if len(block_means) > 1 else None
    mde = None if se is None else (1.96 + 0.84) * se
    return {"n": len(selected), "n_unique": len({x["entry_ts"] for x in selected}), "mean_bps": observed,
            "ci95_bps": ci, "p": p, "mde_bps": mde, "block_count": len(block_means),
            "effective_independent_n": len(block_means), "status": "not_estimable" if mde is None or mde > 12 else "estimable",
            "spread": spread, "hedge_fee_bps": fee_bps}


def _year_stats(rows: list[dict[str, Any]], *, spread: float, fee_bps: float) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        year = datetime.fromtimestamp(int(row["entry_ts"]) / 1000, tz=UTC).strftime("%Y")
        groups[year].append(row)
    return {year: _stats(group, spread=spread, fee_bps=fee_bps) for year, group in sorted(groups.items())}


def run(output: Path = OUTPUT_DEFAULT, *, asset: str = PRIMARY_ASSET) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates(asset=asset)
    decisions = decision_log(candidates)
    selected = [row for row, decision in zip(candidates, decisions) if decision["decision"] == "enter"]
    selected_rows = []
    for row, decision in zip(candidates, decisions):
        if decision["decision"] == "enter":
            selected_rows.append(row | {"trailing_median_iv": decision["trailing_median_iv"], "threshold_iv": decision["threshold_iv"],
                                        "condition": "entry_dvol_1h >= trailing_252d_median", "selection": "non_overlapping"})
    train = [row for row in selected_rows if datetime.fromtimestamp(int(row["entry_ts"]) / 1000, tz=UTC).year <= 2023]
    test = [row for row in selected_rows if datetime.fromtimestamp(int(row["entry_ts"]) / 1000, tz=UTC).year >= 2024]
    baseline = [row for row in candidates if datetime.fromtimestamp(int(row["entry_ts"]) / 1000, tz=UTC).year >= 2024]
    scenarios = {}
    for spread, fee in ((0.002, 8.0), (OPTION_SPREAD_PRIMARY, HEDGE_FEE_PRIMARY_BPS)):
        key = f"spread_{spread:.4f}_fee_{int(fee)}bp"
        scenarios[key] = {"train": _stats(train, spread=spread, fee_bps=fee), "test": _stats(test, spread=spread, fee_bps=fee),
                          "all": _stats(selected_rows, spread=spread, fee_bps=fee), "by_year": _year_stats(selected_rows, spread=spread, fee_bps=fee),
                          "unconditional_test": _stats(baseline, spread=spread, fee_bps=fee)}
    sensitivity = {}
    for multiplier in (0.9, 1.0, 1.1):
        decisions_s = decision_log(candidates, threshold_multiplier=multiplier)
        rows_s = [row for row, decision in zip(candidates, decisions_s) if decision["decision"] == "enter"]
        sensitivity[f"median_x_{multiplier:.1f}"] = {"multiplier": multiplier, "n": len(rows_s),
            "test_n": sum(datetime.fromtimestamp(int(r["entry_ts"]) / 1000, tz=UTC).year >= 2024 for r in rows_s),
            "test": _stats([r for r in rows_s if datetime.fromtimestamp(int(r["entry_ts"]) / 1000, tz=UTC).year >= 2024], spread=OPTION_SPREAD_PRIMARY, fee_bps=HEDGE_FEE_PRIMARY_BPS)}
    latest = candidates[-1] if candidates else None
    latest_median = trailing_median(candidates, len(candidates) - 1) if candidates else None
    report = {"task": "T031", "run_id": "r11-conditional-20261025-v1", "strategy_id": "r11_conditional_dvol_median_v1",
              "asset": asset, "execution_evidence": "unknown", "conclusion": "preregistered_conditional_synthetic_only",
              "spec_sha256": sha256_file(SPEC_PATH), "t023_spec_sha256": sha256_file(T023_SPEC_PATH),
              "source_positions": str(SOURCE_POSITIONS.relative_to(ROOT)), "source_positions_sha256": sha256_file(SOURCE_POSITIONS),
              "rule": {"entry_iv_source": "Deribit DVOL 1h close at exact UTC entry", "trailing_window_valid_daily_observations": TRAILING_DAYS,
                       "condition": "entry_iv >= median(previous_252_entry_iv)", "holding_days": HOLDING_DAYS,
                       "selection": "non_overlapping", "executable_units_per_leg": EXECUTABLE_UNITS_PER_LEG,
                       "primary_spread_pct": OPTION_SPREAD_PRIMARY, "primary_hedge_fee_bps": HEDGE_FEE_PRIMARY_BPS,
                       "no_absolute_iv_threshold": True},
              "counts": {"candidate_rows": len(candidates), "selected_nonoverlap": len(selected_rows),
                         "skipped_low_iv": sum(d["decision"] == "skipped_low_iv" for d in decisions),
                         "skipped_warmup": sum(d["decision"] == "skipped_warmup" for d in decisions),
                         "skipped_overlap": sum(d["decision"] == "skipped_overlap" for d in decisions)},
              "oos": {"train_period": "2022-01-01..2023-12-31", "test_period": "2024-01-01..2026-09-30", "scenarios": scenarios},
              "threshold_sensitivity": sensitivity,
              "current_condition": {"latest_available_entry_ts": latest.get("entry_ts") if latest else None,
                                     "latest_available_entry_iv": latest.get("iv") if latest else None,
                                     "trailing_252d_median": latest_median,
                                     "meets_entry_condition": bool(latest is not None and latest_median is not None and latest["iv"] >= latest_median),
                                     "decision": "enter" if latest is not None and latest_median is not None and latest["iv"] >= latest_median else "skipped_low_iv"},
              "forward_policy": {"conditional_entry": "below median records skipped_low_iv and does not create a position", "unconditional_baseline_retained": True}}
    write_jsonl(output / "conditional_positions.jsonl", selected_rows)
    write_jsonl(output / "decision_log.jsonl", decisions)
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md = ["# T031 R11 conditional DVOL-median rule", "", f"- asset: `{asset}`", "- execution_evidence: `unknown`", f"- spec_sha256: `{report['spec_sha256']}`", "", "Entry IV is the exact-entry Deribit DVOL 1h close. The rule enters only when that value is at least the median of the previous 252 valid daily entry observations; the historical selection is non-overlapping with a 30-day hold.", "", f"- candidate daily rows: `{len(candidates)}`", f"- selected non-overlapping rows: `{len(selected_rows)}`", f"- skipped low-IV rows: `{report['counts']['skipped_low_iv']}`", f"- skipped warmup rows: `{report['counts']['skipped_warmup']}`", "", "## OOS results", "", "| scenario | sample | n | mean bp | CI95 bp | p | MDE bp | status |", "|---|---|---:|---:|---|---:|---:|---|"]
    for key, scenario in scenarios.items():
        for sample in ("train", "test", "unconditional_test"):
            s = scenario[sample]
            md.append(f"| {key} | {sample} | {s['n']} | {s['mean_bps']:.2f} | [{s['ci95_bps'][0]:.2f}, {s['ci95_bps'][1]:.2f}] | {s['p']:.4g} | {s['mde_bps'] if s['mde_bps'] is not None else 'unknown'} | {s['status']} |" if s['n'] else f"| {key} | {sample} | 0 | unknown | unknown | unknown | unknown | not_estimable |")
    md += ["", "## Current condition", "", f"- latest available BTC entry IV: `{report['current_condition']['latest_available_entry_iv']}`", f"- trailing 252-observation median: `{report['current_condition']['trailing_252d_median']}`", f"- decision: `{report['current_condition']['decision']}`", "", "The 0.2%+8bp row is retained as a synthetic comparison. The primary executable-cost scenario is 2.25% premium spread plus 16bp hedge fee. No order or hedge endpoint is called.", ""]
    (output / "REPORT.md").write_text("\n".join(md))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_conditional.py --output artifacts/r11_conditional_20261025\n")
    reproduce.chmod(0o755)
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.resolve().relative_to(ROOT.resolve()))] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {"task": "T031", "run_id": report["run_id"], "execution_evidence": "unknown", "spec_sha256": report["spec_sha256"],
                "t023_spec_sha256": report["t023_spec_sha256"], "source_positions_sha256": report["source_positions_sha256"],
                "code_sha256": sha256_file(Path(__file__)), "files": files,
                "generated_at_utc": datetime.now(UTC).isoformat()}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def _dvol_url(start_ms: int, end_ms: int) -> str:
    params = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": start_ms, "end_timestamp": end_ms, "resolution": 3600})
    return "https://www.deribit.com/api/v2/public/get_volatility_index_data?" + params


def _fetch_dvol_hourly(start_ms: int, end_ms: int, *, fetcher: Any = None) -> tuple[dict[int, float], list[str]]:
    """Fetch enough 1h pages to cover the full trailing daily window.

    Deribit's public endpoint may cap a response at roughly 1000 hourly rows.
    The old implementation accepted that cap and accidentally treated hours as
    days. Page in bounded hourly chunks, then let ``live_entry_gate`` retain
    only UTC 00:00 observations.
    """
    hourly: dict[int, float] = {}
    urls: list[str] = []
    cursor = int(start_ms)
    while cursor <= end_ms:
        chunk_end = min(int(end_ms), cursor + 999 * DVOL_HOUR_MS)
        url = _dvol_url(cursor, chunk_end)
        urls.append(url)
        if fetcher is None:
            req = urllib.request.Request(url, headers={"User-Agent": "r11-conditional/1.1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = response.read()
        else:
            payload = fetcher(url)
        decoded = json.loads(payload)
        data = decoded.get("result", {}).get("data", [])
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 5:
                ts = int(item[0])
                iv = float(item[4])
                if start_ms <= ts <= end_ms and ts % DVOL_HOUR_MS == 0 and math.isfinite(iv) and iv > 0:
                    hourly[ts] = iv
        if chunk_end >= end_ms:
            break
        cursor = chunk_end + DVOL_HOUR_MS
    return hourly, urls


def live_entry_gate(now_ms: int, output: Path, *, fetcher: Any = None) -> dict[str, Any]:
    """Fetch a no-lookahead *daily* DVOL window for the forward-paper gate.

    The source is 1h DVOL, but one observation per UTC day is retained: the
    close at the 00:00 entry-hour timestamp.  A 252-hour cache therefore cannot
    satisfy the 252-day requirement.
    """
    history_path = output / "conditional_dvol_history.jsonl"
    # Migrate any legacy hourly cache by retaining only exact UTC-day entries.
    existing_hourly: dict[int, float] = {}
    for row in read_jsonl(history_path):
        ts = row.get("timestamp")
        iv = row.get("iv")
        if ts is None or iv is None:
            continue
        ts_i = int(ts)
        iv_f = float(iv)
        if ts_i % DAY_MS == 0 and math.isfinite(iv_f) and iv_f > 0:
            existing_hourly[ts_i] = iv_f

    start = now_ms - (TRAILING_DAYS + 2) * DAY_MS
    # Deribit's public endpoint occasionally resets a single hourly page.
    # Retry the whole paged fetch before failing the daily gate.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            hourly, urls = _fetch_dvol_hourly(start, now_ms, fetcher=fetcher)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    else:  # pragma: no cover
        raise RuntimeError("DVOL fetch failed") from last_exc
    existing_hourly.update(hourly)
    # Only the 00:00 UTC 1h close is an entry-day observation.  This produces
    # spacing exactly DAY_MS and makes the 252-day requirement auditable.
    daily = {ts: iv for ts, iv in existing_hourly.items() if ts <= now_ms and ts % DAY_MS == 0}
    rows = [{"timestamp": ts, "iv": daily[ts]} for ts in sorted(daily)]
    write_jsonl(history_path, rows)
    current = daily.get(now_ms)
    prior = [daily[ts] for ts in sorted(daily) if ts < now_ms]
    prior = prior[-TRAILING_DAYS:]
    median = statistics.median(prior) if len(prior) == TRAILING_DAYS else None
    decision = "enter" if current is not None and median is not None and current >= median else ("skipped_low_iv" if current is not None and median is not None else "unknown")
    span_days = ((rows[-1]["timestamp"] - rows[0]["timestamp"]) // DAY_MS) if len(rows) >= 2 else 0
    return {"entry_dvol_1h": current, "trailing_252d_median": median, "decision": decision,
            "history_rows": len(rows), "history_spacing_ms": DAY_MS, "history_span_days": span_days,
            "daily_observation_policy": "one exact 00:00 UTC 1h close per day", "urls": urls,
            "url": urls[-1] if urls else _dvol_url(start, now_ms)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--asset", default=PRIMARY_ASSET, choices=("BTC", "ETH"))
    args = parser.parse_args()
    report = run(args.output, asset=args.asset)
    print(report["task"], report["asset"], report["counts"]["selected_nonoverlap"], report["current_condition"]["decision"])


if __name__ == "__main__":
    main()
