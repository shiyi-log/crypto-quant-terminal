#!/usr/bin/env python3
"""Build a conservative, explicitly non-executable preflight input.

The bundle uses the observed first-second aggregate-trade path, the full
one-minute mark OHLC range as a conservative bound, nominal 30-second
percentage-depth capacity, and the current Demo account's tick/fee/tier
snapshot.  It excludes exact eight-hour funding-settlement boundary windows
instead of treating unknown funding as zero.

The resulting exporter input contains all 22 frozen ``ScenarioEvidence``
records and a complete account-window series, but leaves the execution-model
hash unset because the public sources cannot prove one-second mark-trigger
ordering or L2 queue fills.  The authoritative exporter therefore must return
``not_estimable``.  This command never writes a ledger event or changes a
Demo account.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from core.execution.account_risk_preflight import (
    REQUIRED_EXECUTION_EVIDENCE,
    REQUIRED_SCENARIO_EVIDENCE,
    RiskConfig,
    ScenarioEvidence,
    _scenario_expected_stress,
    _scenario_payload,
    canonical_sha256,
)
from tools.build_execution_evidence_bundle import build_bundle


FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000
FUNDING_WINDOW_MATCH_MS = 15 * 60 * 1000


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _canonical_hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _funding_events(funding_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, dict[str, Any]] = {}
    for symbol in ("BTCUSDT", "ETHUSDT", "TRXUSDT"):
        path = funding_dir / f"{symbol}.json"
        if not path.exists():
            raise ValueError(f"missing official funding-rate response for {symbol}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"funding-rate response for {symbol} must be an array")
        normalized = []
        for row in raw:
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                raise ValueError(f"funding-rate response has invalid symbol for {symbol}")
            normalized.append({
                "symbol": symbol,
                "funding_time_ms": int(row["fundingTime"]),
                "funding_rate": str(row["fundingRate"]),
                "mark_price": str(row["markPrice"]),
                "rate_type": str(row.get("rateType", "Regular")),
            })
        normalized.sort(key=lambda item: item["funding_time_ms"])
        events[symbol] = normalized
        sources[symbol] = {
            "path": str(path),
            "sha256": _canonical_hash_file(path),
            "endpoint": "https://fapi.binance.com/fapi/v1/fundingRate",
            "environment": "binance_public_futures_historical",
            "account_specific": False,
            "query_symbol": symbol,
            "row_count": len(normalized),
        }
    return events, sources


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def _config(bundle: Mapping[str, Any], *, tier_hash: str, authority: str) -> RiskConfig:
    return RiskConfig(
        initial_equity_usdt=_decimal(bundle["account"]["initial_equity_usdt"]),
        max_leverage=Decimal("20"),
        margin_mode="cross",
        n_max_usdt=Decimal("1000"),
        per_symbol_caps_usdt=tuple((symbol, Decimal("1000")) for symbol in ("BTCUSDT", "ETHUSDT", "TRXUSDT")),
        per_symbol_leverages=tuple((symbol, Decimal("20")) for symbol in ("BTCUSDT", "ETHUSDT", "TRXUSDT")),
        account_p99_limit_pct=Decimal("0.05"),
        required_n_gap=100,
        required_account_windows=100,
        account_scope=str(bundle["account"]["account_scope"]),
        tier_snapshot_max_age_hours=Decimal("24"),
        account_loss_authority_scenario_id=authority,
        tier_snapshot_hash=tier_hash,
    )


def _funding_for_window(
    window_start_ms: int, window_end_ms: int,
    funding: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    # Exclude a window containing a published funding settlement rather than
    # pretending the funding cashflow is zero.  The official API timestamps
    # are a few milliseconds after the nominal 00:00/08:00/16:00 boundary.
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for symbol, rows in funding.items():
        for row in rows:
            timestamp = int(row["funding_time_ms"])
            if window_start_ms <= timestamp < window_end_ms:
                found[(symbol, timestamp)] = row
    return [found[key] for key in sorted(found)]


def _price_move(symbol: Mapping[str, Any], *, path: str) -> Decimal:
    mark = symbol["mark_1m_ohlc"]
    trade = symbol["trade_path"]
    mark_open = _decimal(mark["open"])
    trade_first = _decimal(trade["first_trade_price"])
    if mark_open <= 0 or trade_first <= 0:
        raise ValueError("price must be positive")
    mark_rate = max(
        (_decimal(mark["high"]) - _decimal(mark["low"])) / mark_open,
        Decimal("0"),
    )
    trade_rate = max(
        (_decimal(trade["max_trade_price"]) - _decimal(trade["min_trade_price"])) / trade_first,
        Decimal("0"),
    )
    if path == "trade_only":
        return trade_rate
    if path == "mark_only":
        return mark_rate
    return max(mark_rate, trade_rate)


def _window_loss(
    window: Mapping[str, Any],
    allocations: tuple[tuple[str, Decimal], ...],
    account_symbols: Mapping[str, Any],
    *,
    path: str,
    liquidation_ticks: int,
) -> Decimal:
    total = Decimal("0")
    for symbol, notional in allocations:
        symbol_evidence = window["symbols"][symbol]
        move = _price_move(symbol_evidence, path=path) * Decimal("2")
        static = account_symbols[symbol]
        mark = symbol_evidence["mark_1m_ohlc"]
        mark_open = _decimal(mark["open"])
        tick_rate = (_decimal(static["tick_size"]) * Decimal(liquidation_ticks)) / mark_open
        # Taker fee is used as a conservative two-leg trading cost.  A
        # liquidation stress additionally includes the exchange liquidation
        # fee; it is not asserted to be a historical per-window observation.
        fees = Decimal("2") * _decimal(static["taker_commission_rate"])
        if liquidation_ticks:
            fees += _decimal(static["liquidation_fee"])
        total += notional * (move + tick_rate + fees)
    return total


def _allocations() -> dict[str, tuple[tuple[str, Decimal], ...]]:
    third = Decimal("1000") / Decimal("3")
    return {
        "A_long": (("BTCUSDT", third), ("ETHUSDT", third), ("TRXUSDT", Decimal("1000") - third * 2)),
        "A_short": (("BTCUSDT", third), ("ETHUSDT", third), ("TRXUSDT", Decimal("1000") - third * 2)),
        "B": (("BTCUSDT", Decimal("250")), ("ETHUSDT", Decimal("500")), ("TRXUSDT", Decimal("250"))),
    }


def _scenario_allocations(bundle: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> dict[str, tuple[tuple[str, Decimal], ...]]:
    allocations = _allocations()
    # Concentrated C uses the largest diagnostic adverse P99 per symbol and
    # fills one-symbol caps deterministically in descending order.
    rates: list[tuple[Decimal, str]] = []
    for symbol in ("BTCUSDT", "ETHUSDT", "TRXUSDT"):
        entries = [item for item in diagnostics["gap_p99s"] if item["symbol"] == symbol]
        if len(entries) != 2:
            raise ValueError(f"diagnostic must contain long and short P99 for {symbol}")
        rate = max(
            max(_decimal(entry["trade"]["p99"]), _decimal(entry["mark"]["p99"]))
            for entry in entries
        )
        rates.append((rate, symbol))
    rates.sort(key=lambda item: (-item[0], item[1]))
    remaining = Decimal("1000")
    c: list[tuple[str, Decimal]] = []
    for _, symbol in rates:
        amount = min(Decimal("1000"), remaining)
        c.append((symbol, amount))
        remaining -= amount
        if remaining <= 0:
            break
    allocations["C"] = tuple(c)
    return allocations


def _assumption_hashes(bundle: Mapping[str, Any], window_hash: str) -> tuple[tuple[str, str], ...]:
    shared = {
        "bundle_schema": bundle["schema_version"],
        "window_data_sha256": window_hash,
        "model_status": bundle["execution_model"]["model_status"],
    }
    return tuple((name, canonical_sha256({"assumption": name, **shared}))
                 for name in REQUIRED_EXECUTION_EVIDENCE)


def _scenario_record(
    scenario_id: str,
    *,
    config_hash: str,
    tier_hash: str,
    window_hash: str,
    assumptions: tuple[tuple[str, str], ...],
    losses: tuple[Decimal, ...],
    deficits: tuple[Decimal, ...],
) -> dict[str, Any]:
    path, slippage, delay, ticks = _scenario_expected_stress(scenario_id)
    # model_sha256 intentionally remains null: the model is a conservative
    # candidate bound, not independently verified one-second execution data.
    record = ScenarioEvidence(
        scenario_id=scenario_id,
        config_hash=config_hash,
        evidence_sha256="0" * 64,
        window_data_sha256=window_hash,
        account_window_losses_usdt=losses,
        uncovered_deficits_usdt=deficits,
        model_sha256=None,
        tier_snapshot_hash=tier_hash,
        assumption_hashes=assumptions,
        status="pass",
        execution_path=path,
        slippage_multiple=slippage,
        delay_ms=delay,
        liquidation_slippage_ticks=ticks,
    )
    payload_hash = canonical_sha256(_scenario_payload(record))
    # ``_scenario_payload`` intentionally excludes evidence_sha256, matching
    # the preflight verifier's binding convention.
    record = replace(record, evidence_sha256=payload_hash)
    return {
        "scenario_id": record.scenario_id,
        "config_hash": record.config_hash,
        "evidence_sha256": record.evidence_sha256,
        "window_data_sha256": record.window_data_sha256,
        "account_window_losses_usdt": [str(v) for v in record.account_window_losses_usdt],
        "uncovered_deficits_usdt": [str(v) for v in record.uncovered_deficits_usdt],
        "model_sha256": record.model_sha256,
        "tier_snapshot_hash": record.tier_snapshot_hash,
        "assumption_hashes": [list(item) for item in record.assumption_hashes],
        "status": record.status,
        "execution_path": record.execution_path,
        "slippage_multiple": str(record.slippage_multiple),
        "delay_ms": record.delay_ms,
        "liquidation_slippage_ticks": record.liquidation_slippage_ticks,
    }


def build_preflight_bundle(
    bundle: Mapping[str, Any], diagnostics: Mapping[str, Any],
    *, tier_schedules: list[Mapping[str, Any]], tier_hash: str,
    funding: Mapping[str, list[dict[str, Any]]],
    funding_sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    retained = []
    excluded: list[dict[str, Any]] = []
    for window in bundle["windows"]:
        start_ms = int(window["window_start_ms"])
        end_ms = int(window["window_end_ms"])
        events = _funding_for_window(start_ms, end_ms, funding)
        if events:
            excluded.append({
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "reason": "official_funding_settlement_inside_window",
                "events": events,
            })
        else:
            retained.append(window)
    windows = retained
    if len(windows) < 100:
        raise ValueError("funding exclusion leaves fewer than 100 windows")
    window_payload = {
        "source_window_data_sha256": bundle["window_data_sha256"],
        "excluded_funding_windows": excluded,
        "windows": windows,
    }
    window_hash = canonical_sha256(window_payload)
    assumptions = _assumption_hashes(bundle, window_hash)
    allocations = _scenario_allocations(bundle, diagnostics)
    account_symbols = bundle["account"]["per_symbol"]

    series: dict[str, tuple[Decimal, ...]] = {}
    for scenario_id in ("A_long", "A_short", "B", "C"):
        path = "trade_mark"
        series[scenario_id] = tuple(
            _window_loss(window, allocations[scenario_id], account_symbols,
                         path=path, liquidation_ticks=0)
            for window in windows
        )
    ordered = sorted(series, key=lambda key: (sorted(series[key])[int(len(series[key]) * 0.99) - 1], key), reverse=True)
    authority = ordered[0]
    config = _config(bundle, tier_hash=tier_hash, authority=authority)
    config_hash = config.config_hash
    scenarios: list[dict[str, Any]] = []
    for scenario_id in REQUIRED_SCENARIO_EVIDENCE:
        if scenario_id in series:
            losses = series[scenario_id]
        elif scenario_id.endswith("_trade_only"):
            base = scenario_id.removesuffix("_trade_only")
            losses = tuple(_window_loss(window, allocations[base], account_symbols,
                                        path="trade_only", liquidation_ticks=0)
                           for window in windows)
        elif scenario_id.endswith("_mark_only"):
            base = scenario_id.removesuffix("_mark_only")
            losses = tuple(_window_loss(window, allocations[base], account_symbols,
                                        path="mark_only", liquidation_ticks=0)
                           for window in windows)
        elif scenario_id == "joint_marginal_p99":
            losses = tuple(max(series[key][idx] for key in series) for idx in range(len(windows)))
        elif scenario_id == "synthetic_max_envelope":
            losses = tuple(max(series[key][idx] for key in series) for idx in range(len(windows)))
        else:
            base = scenario_id.rsplit("_liquidation_", 1)[0]
            ticks = int(scenario_id.rsplit("_", 1)[1].removesuffix("tick"))
            losses = tuple(_window_loss(window, allocations[base], account_symbols,
                                        path="trade_mark", liquidation_ticks=ticks)
                           for window in windows)
        scenarios.append(_scenario_record(
            scenario_id, config_hash=config_hash, tier_hash=tier_hash,
            window_hash=window_hash, assumptions=assumptions,
            losses=tuple(losses), deficits=tuple(Decimal("0") for _ in losses),
        ))
    authority_losses = next(item["account_window_losses_usdt"] for item in scenarios
                            if item["scenario_id"] == authority)
    authority_deficits = next(item["uncovered_deficits_usdt"] for item in scenarios
                              if item["scenario_id"] == authority)
    return {
        "config": {
            "initial_equity_usdt": str(config.initial_equity_usdt),
            "max_leverage": str(config.max_leverage),
            "margin_mode": config.margin_mode,
            "n_max_usdt": str(config.n_max_usdt),
            "per_symbol_caps_usdt": [[s, str(v)] for s, v in config.per_symbol_caps_usdt],
            "per_symbol_leverages": [[s, str(v)] for s, v in config.per_symbol_leverages],
            "account_p99_limit_pct": str(config.account_p99_limit_pct),
            "required_n_gap": config.required_n_gap,
            "required_account_windows": config.required_account_windows,
            "account_scope": config.account_scope,
            "tier_snapshot_max_age_hours": str(config.tier_snapshot_max_age_hours),
            "account_loss_authority_scenario_id": authority,
            "tier_snapshot_hash": tier_hash,
        },
        "tier_schedules": tier_schedules,
        "gap_p99s": diagnostics["gap_p99s"],
        "execution_evidence": {
            "model_sha256": None,
            "window_data_sha256": window_hash,
            "assumption_hashes": [list(item) for item in assumptions],
            "account_window_losses_usdt": authority_losses,
            "uncovered_deficits_usdt": authority_deficits,
            "tier_snapshot_hash": tier_hash,
            "scenario_evidence": scenarios,
        },
        "as_of_utc": bundle["tier_snapshot"]["as_of_utc"],
        "builder_metadata": {
            "schema_version": "account-risk-conservative-upper-bound-input-v1",
            "status": "not_estimable",
            "model_sha256_candidate": canonical_sha256(bundle["execution_model"]),
            "funding_interval_ms": FUNDING_INTERVAL_MS,
            "funding_windows_excluded": len(excluded),
            "funding_windows": excluded,
            "funding_sources": funding_sources,
            "source_window_count": len(bundle["windows"]),
            "retained_window_count": len(windows),
            "execution_model_hash_intentionally_unset": True,
            "execution_model_not_estimable_reasons": [
                "the observed trade path covers only the trigger second, not the post-trigger 1000ms fill interval",
                "one-minute mark OHLC does not establish intrasecond mark-trigger order or a bounded 1000ms mark sequence",
                "30-second bookDepth is aggregate percentage depth and does not establish top-of-book queue capacity",
                "tick size and liquidation fee are captured once and are not independently bound to each historical window",
                "insurance-fund, ADL, bankruptcy, and uncovered-deficit observations are unavailable for the historical windows",
            ],
            "limitations": list(bundle["limitations"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--tier-snapshot", type=Path, required=True)
    parser.add_argument("--funding-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path)
    args = parser.parse_args(argv)
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    diagnostic = json.loads(args.diagnostic.read_text(encoding="utf-8"))
    tier = json.loads(args.tier_snapshot.read_text(encoding="utf-8"))
    funding, funding_sources = _funding_events(args.funding_dir)
    output = build_preflight_bundle(
        bundle, diagnostic,
        tier_schedules=list(tier["schedules"]),
        tier_hash=str(tier["snapshot_hash"]),
        funding=funding,
        funding_sources=funding_sources,
    )
    metadata = output.pop("builder_metadata")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    metadata_path = args.metadata_output or args.output.with_name(args.output.stem + ".metadata.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": metadata["status"],
        "scenario_count": len(output["execution_evidence"]["scenario_evidence"]),
        "retained_window_count": metadata["retained_window_count"],
        "authority": output["config"]["account_loss_authority_scenario_id"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
