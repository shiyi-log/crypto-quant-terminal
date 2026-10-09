#!/usr/bin/env python3
"""Deterministic, offline paper-scenario runner.

This is a replay harness, not a live exchange client. Inputs are frozen candle
files or DataFrames and outputs are immutable JSON artifacts. Strategy runs
always use event_backtest.run_v2; fees are modeled, funding and slippage remain
unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import event_backtest as engine  # noqa: E402

SCHEMA_VERSION = "paper-dryrun-v1"
ENGINE_NAME = "event_backtest.run_v2"
COMPARISON_SCHEMA_VERSION = "paired-trades-v1"
DEFAULT_VARIANTS = [
    {"variant_id": "trend-20-20", "hypothesis": "reference Donchian 20/20", "chan_entry": 20, "chan_exit": 20},
    {"variant_id": "trend-10-20", "hypothesis": "earlier entry with unchanged exit", "chan_entry": 10, "chan_exit": 20},
    {"variant_id": "trend-30-20", "hypothesis": "later entry with unchanged exit", "chan_entry": 30, "chan_exit": 20},
]


def canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return stamp.isoformat()
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        out = float(value)
        return out if np.isfinite(out) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def sha256(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def normalize_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Sort symbols/timestamps and normalize prices to UTC-naive engine dates."""
    normalized = {}
    for symbol in sorted(data):
        frame = data[symbol].copy()
        if not {"open", "close"}.issubset(frame.columns):
            raise ValueError(f"{symbol}: open and close columns are required")
        idx = pd.to_datetime(frame.index, utc=True).tz_localize(None)
        if len(idx) == 0 or idx.hasnans:
            raise ValueError(f"{symbol}: candle timestamps must be present")
        frame.index = idx
        frame = frame.sort_index()
        if frame.index.has_duplicates:
            raise ValueError(f"{symbol}: duplicate candle timestamps")
        frame = frame[["open", "close"]].apply(pd.to_numeric, errors="coerce")
        normalized[str(symbol)] = frame.astype("float64")
    if not normalized:
        raise ValueError("no candle series supplied")
    return normalized


def data_fingerprint(data: dict[str, pd.DataFrame]) -> str:
    """Hash exact run_v2 inputs, including missing-value positions."""
    normalized = normalize_data(data)
    digest = hashlib.sha256()
    digest.update(b"paper-dryrun-data-v1\0")
    for symbol, frame in normalized.items():
        digest.update(symbol.encode("utf-8") + b"\0")
        stamps = frame.index.asi8.astype("<i8", copy=False)
        digest.update(np.asarray([len(frame)], dtype="<i8").tobytes())
        digest.update(stamps.tobytes())
        for column in ("open", "close"):
            values = frame[column].to_numpy(dtype="<f8", copy=True)
            values[np.isnan(values)] = np.nan
            digest.update(column.encode("ascii") + b"\0")
            digest.update(values.tobytes())
    return digest.hexdigest()


def engine_fingerprint() -> str:
    return sha256(Path(engine.__file__).read_bytes())


def runtime_fingerprint() -> dict[str, str | None]:
    versions: dict[str, str | None] = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }
    try:
        versions["scipy"] = importlib.metadata.version("scipy")
    except importlib.metadata.PackageNotFoundError:
        versions["scipy"] = None
    return versions


def freeze_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not variants:
        raise ValueError("at least one variant is required")
    frozen = []
    seen = set()
    for raw in variants:
        unknown = set(raw) - {"variant_id", "hypothesis", "chan_entry", "chan_exit"}
        if unknown:
            raise ValueError(f"unsupported variant fields: {sorted(unknown)}")
        variant = {
            "variant_id": str(raw["variant_id"]),
            "hypothesis": str(raw.get("hypothesis", "")),
            "chan_entry": int(raw.get("chan_entry", 20)),
            "chan_exit": int(raw.get("chan_exit", 20)),
        }
        if not variant["variant_id"] or variant["variant_id"] in {".", ".."}:
            raise ValueError("variant_id must be a non-empty name")
        if any(ord(char) < 32 for char in variant["variant_id"]):
            raise ValueError("variant_id must not contain control characters")
        if variant["variant_id"] in seen:
            raise ValueError(f"duplicate variant_id: {variant['variant_id']}")
        if variant["chan_entry"] < 2 or variant["chan_exit"] < 2:
            raise ValueError("channel periods must be >= 2")
        seen.add(variant["variant_id"])
        executable_rules = {
            "chan_entry": variant["chan_entry"],
            "chan_exit": variant["chan_exit"],
        }
        variant["rule_hash"] = sha256(canonical_json(executable_rules))
        frozen.append(variant)
    return sorted(frozen, key=lambda item: item["variant_id"])


def _artifact_stems(variants: list[dict[str, Any]]) -> dict[str, str]:
    """Return deterministic, filesystem-safe and collision-free artifact names."""
    stems: dict[str, str] = {}
    used: set[str] = set()
    for variant in variants:
        raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", variant["variant_id"]).strip("._")
        base = raw or "variant"
        stem = base
        if stem in used:
            stem = f"{base}--{variant['rule_hash'][:12]}"
        while stem in used:
            stem += "-x"
        used.add(stem)
        stems[variant["variant_id"]] = stem
    return stems


def _paired_comparisons(trades_by_variant: dict[str, pd.DataFrame],
                        variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare exact entry matches and report uncertainty without ranking claims.

    A trade is paired only when coin, entry timestamp, and side match.  Different
    exits remain comparable: their realized profit is the outcome being tested.
    The paired t statistic is always emitted when estimable. If scipy is
    available, compute its Student-t p-value; without scipy we retain the
    descriptive statistic and leave inferential fields unset.
    """
    rows: list[dict[str, Any]] = []
    pair_count = len(variants) * (len(variants) - 1) // 2
    if not pair_count:
        return rows

    def key(row: dict[str, Any]) -> tuple[str, str, bool]:
        return _trade_key(row)

    for left_index, left in enumerate(variants):
        left_id = left["variant_id"]
        left_trades = trades_by_variant[left_id]
        left_rows = [dict(row) for row in left_trades.to_dict(orient="records")]
        left_map = {key(row): row for row in left_rows}
        for right in variants[left_index + 1:]:
            right_id = right["variant_id"]
            right_rows = [dict(row) for row in trades_by_variant[right_id].to_dict(orient="records")]
            right_map = {key(row): row for row in right_rows}
            common_keys = sorted(set(left_map) & set(right_map))
            differences = [float(left_map[item]["profit_abs"])
                           - float(right_map[item]["profit_abs"])
                           for item in common_keys]
            n = len(differences)
            mean = float(np.mean(differences)) if n else None
            std = float(np.std(differences, ddof=1)) if n > 1 else None
            if n > 1 and std is not None and std > 0:
                t_stat = mean / (std / math.sqrt(n))
                try:
                    from scipy.stats import t as student_t
                except ImportError:
                    p_value = None
                    p_method = "unavailable: scipy is not installed"
                else:
                    p_value = float(2.0 * student_t.sf(abs(t_stat), df=n - 1))
                    p_method = "two-sided paired Student t"
            elif n > 1 and std == 0 and mean == 0:
                t_stat, p_value = 0.0, 1.0
                p_method = "degenerate zero differences"
            else:
                t_stat, p_value = None, None
                p_method = "not estimable"
            rows.append({
                "left_variant_id": left_id,
                "right_variant_id": right_id,
                "left_rule_hash": left["rule_hash"],
                "right_rule_hash": right["rule_hash"],
                "pair_key": "pair,open_date,is_short",
                "matched_count": n,
                "left_trade_count": len(left_rows),
                "right_trade_count": len(right_rows),
                "left_unmatched_count": len(left_rows) - n,
                "right_unmatched_count": len(right_rows) - n,
                "mean_profit_difference": mean,
                "std_profit_difference": std,
                "t_stat": t_stat,
                "p_value": p_value,
                "p_value_method": p_method,
            })
    for row in rows:
        row["bonferroni_comparisons"] = pair_count
        row["p_value_bonferroni"] = (
            min(1.0, row["p_value"] * pair_count) if row["p_value"] is not None else None
        )
        row["significant_after_bonferroni"] = bool(
            row["p_value_bonferroni"] is not None and row["p_value_bonferroni"] < 0.05
        )
    return rows


def _trade_key(trade: dict[str, Any]) -> tuple[str, str, str]:
    return (str(trade["pair"]), _plain(trade["open_date"]),
            "short" if bool(trade["is_short"]) else "long")


def _variant_summary(trades: pd.DataFrame, equity: pd.Series, diag: dict[str, Any],
                     data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    realized_after_fee = (float(trades["profit_abs"].sum()) if not trades.empty else None)
    end_positions = diag["open_positions"]
    mark = {}
    for position in end_positions:
        coin = position["pair"]
        key = next((s for s in data if s.split("/")[0] == coin), None)
        last_close = None
        if key is not None:
            series = data[key]["close"].dropna()
            if not series.empty:
                last_close = float(series.iloc[-1])
        if last_close and position["open_rate"]:
            sign = -1.0 if position["is_short"] else 1.0
            mark[coin] = float(position["stake"] * sign * (last_close / position["open_rate"] - 1))
        else:
            mark[coin] = None
    return {
        "closed_trade_count": int(len(trades)),
        "realized_profit_after_fee_before_unknown_costs": realized_after_fee,
        "slippage": "unknown",
        "funding": "unknown",
        "net_profit": None,
        "open_position_count": int(len(end_positions)),
        "open_positions": end_positions,
        "unrealized_mark_to_market_before_unknown_costs": mark,
        "ending_equity_marked": float(equity.iloc[-1]) if not equity.empty else None,
        "diagnostics": diag,
    }


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"immutable artifact conflict: {path}")
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def run_batch(data: dict[str, pd.DataFrame], variants: list[dict[str, Any]],
              output_dir: str | os.PathLike[str], *, start: str | None = None,
              top_n: int = 8, max_open: int = 10, exposure: float = 0.30,
              cost_one: float = 0.0005, wallet: float = 10_000.0) -> dict[str, Any]:
    """Replay variants serially on one immutable input snapshot and persist evidence."""
    if not isinstance(top_n, int) or not isinstance(max_open, int) or top_n < 1 or max_open < 1:
        raise ValueError("top_n and max_open must be positive")
    if not math.isfinite(exposure) or exposure <= 0 or exposure > 1:
        raise ValueError("exposure must be in (0, 1]")
    if not math.isfinite(cost_one) or cost_one < 0:
        raise ValueError("cost_one must be non-negative")
    if not math.isfinite(wallet) or wallet <= 0:
        raise ValueError("wallet must be positive")
    normalized = normalize_data(data)
    common_start = max(frame.index.min() for frame in normalized.values())
    common_end = min(frame.index.max() for frame in normalized.values())
    if common_start > common_end:
        raise ValueError("symbols have no overlapping candle interval")
    common_index = normalized[next(iter(normalized))].index
    for frame in normalized.values():
        common_index = common_index.intersection(frame.index)
    if common_index.empty:
        raise ValueError("symbols have no shared candle timestamps")
    # Keep pre-overlap history for indicator warmup, but cap the replay at the
    # last timestamp available for every symbol. run_v2 will fail closed on any
    # interior missing close instead of silently shrinking the candidate pool.
    normalized = {symbol: frame.loc[frame.index <= common_end].copy()
                  for symbol, frame in normalized.items()}
    variants_frozen = freeze_variants(variants)
    artifact_stems = _artifact_stems(variants_frozen)
    fingerprint = data_fingerprint(normalized)
    engine_hash = engine_fingerprint()
    runtime = runtime_fingerprint()
    simulation_start = None
    if start is not None:
        simulation_start = pd.Timestamp(start)
        if simulation_start.tzinfo is not None:
            simulation_start = simulation_start.tz_convert("UTC").tz_localize(None)
        if simulation_start > common_end:
            raise ValueError("start is later than the common data interval")
    common = {"top_n": int(top_n), "max_open": int(max_open), "exposure": float(exposure),
              "cost_one": float(cost_one), "wallet": float(wallet),
              "start": _plain(simulation_start) if simulation_start is not None else None}
    run_id = sha256(canonical_json({"schema": SCHEMA_VERSION, "data_fingerprint": fingerprint,
                                    "engine_hash": engine_hash, "runtime": runtime,
                                    "common": common,
                                    "variants": variants_frozen}))[:24]
    date_min = min(frame.index.min() for frame in normalized.values())
    date_max = max(frame.index.max() for frame in normalized.values())
    output = Path(output_dir) / run_id
    manifest_variants = [dict(variant, artifact_stem=artifact_stems[variant["variant_id"]])
                         for variant in variants_frozen]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "engine": ENGINE_NAME,
        "engine_sha256": engine_hash,
        "data_fingerprint": fingerprint,
        "data_timeframe": "caller supplied; expected 1h for primary study",
        "data_symbols": sorted(normalized),
        "data_rows": {symbol: len(frame) for symbol, frame in normalized.items()},
        "data_from_utc": _plain(date_min),
        "data_through_utc": _plain(date_max),
        "common_window_utc": {"from": _plain(common_start), "through": _plain(common_end),
                              "shared_timestamp_count": int(len(common_index))},
        "start": common["start"],
        "portfolio_rules": common,
        "variants": manifest_variants,
        "runtime": runtime,
        "cost_completeness": {"fee": "modeled by run_v2", "slippage": "unknown", "funding": "unknown"},
        "interpretation": "deterministic offline replay; not a live result or net-return estimate",
    }
    manifest["manifest_hash"] = sha256(canonical_json(manifest))
    _write_immutable(output / "manifest.json", (canonical_json(manifest) + "\n").encode())

    summaries = {}
    trades_by_variant: dict[str, pd.DataFrame] = {}
    for variant in variants_frozen:
        raw_events: list[dict[str, Any]] = []
        trades, equity, _, diag = engine.run_v2(
            normalized, top_n=top_n, max_open=max_open, exposure=exposure,
            cost_one=cost_one, wallet=wallet, start=simulation_start,
            chan_entry=variant["chan_entry"], chan_exit=variant["chan_exit"],
            event_sink=raw_events,
        )
        trades_by_variant[variant["variant_id"]] = trades
        event_priority = {"decision": 0, "fill": 1}
        fill_priority = {"exit": 0, "entry": 1}
        raw_events.sort(key=lambda event: (
            event.get("execution_at_utc", event.get("filled_at_utc", "")),
            event_priority.get(event.get("event_type"), 2),
            fill_priority.get(event.get("action"), 2), event.get("coin", ""),
        ))
        events = []
        for ordinal, event in enumerate(raw_events):
            if event.get("event_type") == "decision":
                # Decision records are frozen facts; fill details live only in
                # separately ordered fill events and are never nested back in.
                event = dict(event)
                event["candidates"] = [
                    {key: value for key, value in candidate.items() if key != "actual_fill"}
                    for candidate in event.get("candidates", [])
                ]
                event["exits"] = [
                    {key: value for key, value in exit_event.items() if key != "actual_fill"}
                    for exit_event in event.get("exits", [])
                ]
            body = {"run_id": run_id, "variant_id": variant["variant_id"],
                    "rule_hash": variant["rule_hash"], "data_fingerprint": fingerprint,
                    "ordinal": ordinal, **event}
            body["event_id"] = sha256(canonical_json(body))
            events.append(body)
        event_lines = "".join(canonical_json(event) + "\n" for event in events)
        safe_name = artifact_stems[variant["variant_id"]]
        _write_immutable(output / f"{safe_name}.events.jsonl", event_lines.encode())
        trade_rows = [_plain(row) for row in trades.to_dict(orient="records")]
        trade_payload = {"run_id": run_id, "variant_id": variant["variant_id"],
                         "rule_hash": variant["rule_hash"], "trades": trade_rows,
                         "equity": [{"candle_utc": _plain(ts), "value": float(value)}
                                    for ts, value in equity.items()]}
        _write_immutable(output / f"{safe_name}.result.json",
                         (canonical_json(trade_payload) + "\n").encode())
        summary = _variant_summary(trades, equity, diag, normalized)
        summary["event_count"] = len(events)
        summary["decision_count"] = sum(event["event_type"] == "decision" for event in events)
        summaries[variant["variant_id"]] = summary

    comparisons = _paired_comparisons(trades_by_variant, variants_frozen)
    _write_immutable(output / "comparisons.json",
                     (canonical_json({"schema_version": COMPARISON_SCHEMA_VERSION,
                                      "run_id": run_id,
                                      "comparisons": comparisons}) + "\n").encode())
    summary_payload = {"run_id": run_id, "data_fingerprint": fingerprint,
                       "rule_hashes": {v["variant_id"]: v["rule_hash"] for v in variants_frozen},
                       "variants": summaries,
                       "comparisons": comparisons,
                       "comparison_note": "Matched trades and unmatched entries must be reviewed; estimable p-values use a two-sided paired Student t test with Bonferroni correction; no statistical ranking is claimed.",
                       "negative_control": run_negative_control()}
    _write_immutable(output / "summary.json", (canonical_json(summary_payload) + "\n").encode())
    return {"manifest": manifest, "summary": summary_payload, "output_dir": str(output)}


def run_negative_control() -> dict[str, Any]:
    """Exercise accounting with a known-loss, synthetic flat-then-fall fixture."""
    index = pd.date_range("2025-01-01", periods=70, freq="h")
    close = np.full(len(index), 100.0)
    close[25] = 110.0
    close[26:48] = np.linspace(109.0, 80.0, 22)
    close[48:] = 80.0
    open_price = np.r_[100.0, close[:-1]]
    open_price[26] = 110.0
    fixture = {"LOSS/USDT:USDT": pd.DataFrame({"open": open_price, "close": close}, index=index)}
    trades, _, _, _ = engine.run_v2(fixture, top_n=1, max_open=1, exposure=0.30,
                                    cost_one=0.0005, wallet=1_000.0,
                                    chan_entry=20, chan_exit=20)
    pnl = float(trades["profit_abs"].sum()) if not trades.empty else None
    return {"control_id": "synthetic-breakout-reversal-loss-v1", "seed": None,
            "data_kind": "synthetic_known_loss_fixture", "closed_trade_count": int(len(trades)),
            "realized_after_fee": pnl, "expected_negative": True,
            "passed": bool(len(trades) > 0 and pnl is not None and pnl < 0)}


def load_feather_1h(data_dir: str | os.PathLike[str], coins: list[str] | None = None) -> dict[str, pd.DataFrame]:
    base = Path(data_dir)
    allowed = set(coins or [])
    found = {}
    for path in sorted(base.glob("*_USDT_USDT-1h-futures.feather")):
        coin = path.name.split("_", 1)[0]
        if allowed and coin not in allowed:
            continue
        frame = pd.read_feather(path)
        if "date" not in frame:
            raise ValueError(f"{path}: date column missing")
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        found[coin] = frame.set_index("date")[["open", "close"]]
    if allowed:
        missing = sorted(allowed - set(found))
        if missing:
            raise ValueError(f"requested symbols have no 1h futures data: {missing}")
    return normalize_data(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministic offline paper scenario replay")
    parser.add_argument("--data-dir", default=str(HERE / "user_data/data/binance/futures"))
    parser.add_argument("--coins", default="", help="comma-separated symbol list; default is every available 1h futures file")
    parser.add_argument("--start", default=None)
    parser.add_argument("--output", default=str(HERE.parent / "artifacts/paper_dryrun"))
    args = parser.parse_args(argv)
    coins = [item.strip() for item in args.coins.split(",") if item.strip()]
    data = load_feather_1h(args.data_dir, coins or None)
    result = run_batch(data, DEFAULT_VARIANTS, args.output, start=args.start)
    print(json.dumps(_plain(result), ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["summary"]["negative_control"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
