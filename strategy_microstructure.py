"""Frozen, low-frequency order-book confirmation paper experiment.

The s14 hourly compression rule generates both populations' baseline targets.
Only a new entry in the filter population depends on the published book input;
position exits and frozen ATR protection never depend on book availability.
This module neither reads live tables nor creates fills or accounts in storage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import strategy_signals as core
from strategy_factorial import ALLOWED_RESEARCH_PAIRS, RISK_BUDGETS, STOP_WIDTHS


EXPERIMENT_VERSION = "orderbook-confirmation-forward-v1"
FEATURE_METHOD_VERSION = "orderbook-hourly-v1"
CANONICAL_STRATEGY_ID = "s14_vol_compression_release"
EXPERIMENT_STRATEGY_ID = "s37_vol_compression_book"
GROUPS = ("control", "filter")
BAR_MS = 3_600_000
MAX_PUBLISH_LAG_MS = 90_000
BASE_PARAMS = {"atr_window": 14, "compression_lookback": 100,
               "pctile": 0.2, "H": 72}
BOOK_GATE = {"obi_depth": 5, "obi_directional_minimum": 0.1,
             "relative_spread_maximum": 0.0002,
             "entry_only": True, "lag_directional_consistency_role": "audit_only"}
FEATURE_QUALITY = {"coverage_ratio_minimum": 0.95, "minute_bins_minimum": 55,
                   "largest_gap_ms_maximum": 30_000,
                   "age_at_close_ms_maximum": 3_000,
                   "valid_snapshots_minimum": 100,
                   "max_publish_lag_ms": MAX_PUBLISH_LAG_MS}
POSITION_BUDGET = {"target_exposure": 0.3, "max_positions": 2,
                   "max_per_coin": 1, "sizing": "剩余预算 / 剩余槽位",
                   "leverage": 100,
                   "leverage_convention": "A：保证金不变，名义 ×100，逐根强平"}
IDENTITY = re.compile(
    r"s37_vol_compression_book_(?P<group>control|filter)_"
    r"(?P<risk>R50|R200|R1000)_k(?P<k>1\.0|1\.5|2\.5)_100x_maker\Z"
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def parse_microstructure_identity(strategy_id: Any) -> dict[str, Any] | None:
    """Recognize only the 18 explicitly frozen experiment identities."""
    if not isinstance(strategy_id, str) or len(strategy_id) > 80:
        return None
    match = IDENTITY.fullmatch(strategy_id)
    if match is None:
        return None
    return {"canonical_strategy_id": CANONICAL_STRATEGY_ID,
            "experiment_strategy_id": EXPERIMENT_STRATEGY_ID,
            "microstructure_group": match["group"], "risk_budget": match["risk"],
            "max_loss_per_trade_usdt": dict(RISK_BUDGETS)[match["risk"]],
            "risk_stop_atr_mult": float(match["k"])}


def _require(spec: Mapping[str, Any], key: str, expected: Any) -> None:
    if key not in spec or spec[key] != expected:
        raise ValueError(f"microstructure {key} does not match frozen experiment")


def _inner_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Both arms get exactly the same canonical signal definition."""
    inner = {key: copy.deepcopy(spec[key]) for key in
             ("params", "timeframe", "universe", "execution_universe",
              "research_universe", "min_warmup_bars", "position_budget",
              "initial_capital", "fee_rate_per_side")}
    inner.update(strategy_id=CANONICAL_STRATEGY_ID,
                 name="波动压缩释放·订单簿实验共同基线",
                 mechanism="frozen_s14_hourly_compression_release")
    return inner


def normalize_microstructure_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Bind identity, baseline, gate, input timing and comparison semantics."""
    if not isinstance(spec, Mapping):
        raise ValueError("microstructure spec must be a mapping")
    identity = parse_microstructure_identity(spec.get("strategy_id"))
    if identity is None:
        raise ValueError("unsupported microstructure identity")
    risk, k = identity["max_loss_per_trade_usdt"], identity["risk_stop_atr_mult"]
    group = identity["microstructure_group"]
    expected_params = {**BASE_PARAMS, "risk_stop_atr_mult": k}
    supplied_params = spec.get("params", spec.get("params_frozen"))
    if not isinstance(supplied_params, Mapping) or dict(supplied_params) != expected_params:
        raise ValueError("microstructure params do not match frozen baseline and stop")
    if "params_frozen" in spec and spec["params_frozen"] != expected_params:
        raise ValueError("microstructure params and params_frozen disagree")
    pairs = list(ALLOWED_RESEARCH_PAIRS)
    fixed = {"canonical_strategy_id": CANONICAL_STRATEGY_ID,
             "microstructure_experiment_version": EXPERIMENT_VERSION,
             "microstructure_group": group, "sample_population": EXPERIMENT_VERSION,
             "feature_method_version": FEATURE_METHOD_VERSION,
             "book_gate": BOOK_GATE, "feature_quality_policy": FEATURE_QUALITY,
             "risk_sizing_mode": "entry_atr", "risk_budget_scope": "per_trade_leg",
             "max_loss_per_trade_usdt": risk, "timeframe": "1h",
             "research_timeframe": "1h", "universe": pairs,
             "execution_universe": pairs, "research_universe": pairs,
             "data_universe": pairs, "min_warmup_bars": 116,
             "position_budget": POSITION_BUDGET, "initial_capital": 10000.0,
             "fee_rate_per_side": 0.0002, "execution_enabled": True,
             "execution_blockers": [], "blocked_execution_reason": None,
             "forward_hypothesis": True,
             "feature_availability_policy": "published_by_observation_time_close_plus_90s",
             "attribution_policy": "paired_published_quality_eligible_bar_subsample",
             "book_unavailable_policy": "control_baseline_filter_no_new_entry_exits_unchanged",
             "risk_limit_semantics": "sizing_target_close_trigger_next_open_gap_risk"}
    factors = {"strategy": EXPERIMENT_STRATEGY_ID, "risk_budget": identity["risk_budget"],
               "stop_width_k": k, "book_group": group}
    fixed["factorial_factors"] = factors
    for key, value in fixed.items():
        _require(spec, key, value)
    # Include original canonical policy fields in the outer immutable contract;
    # normalization verifies they cannot silently diverge on resumed accounts.
    prepared = {**spec, "params": expected_params}
    inner_input = _inner_spec(prepared)
    for key in ("schema_version", "algorithm_version", "closed_only", "fill_model",
                "slippage", "funding", "input_revision_policy", "history_policy",
                "closed_freshness_ms", "baseline_policy", "true_range_policy",
                "standard_deviation_ddof", "holding_bars_policy", "flow_imbal_convention",
                "orderflow_threshold_policy", "atr_risk_policy", "annualization_bars",
                "same_side_rebalance"):
        if key in spec:
            inner_input[key] = copy.deepcopy(spec[key])
    inner = core.normalize_spec(inner_input)
    if spec.get("signal_rule_hash") not in (None, inner["rule_hash"]):
        raise ValueError("microstructure canonical signal rule hash mismatch")
    normalized = {**inner, **copy.deepcopy(fixed), "strategy_id": spec["strategy_id"],
                  "signal_rule_hash": inner["rule_hash"],
                  "params_frozen": copy.deepcopy(inner["params"]),
                  "name": "波动压缩订单簿确认·" + ("无过滤对照" if group == "control" else "有过滤"),
                  "mechanism": "hourly_s14_compression_with_published_book_entry_confirmation",
                  "label": f"订单簿{'无过滤对照' if group == 'control' else '确认过滤'} · "
                           f"{identity['risk_budget']} · k{k:.1f} · 100×",
                  "arm_id": "F_microstructure_forward", "variant_of": CANONICAL_STRATEGY_ID,
                  "variant_label": "订单簿低频确认前向假设",
                  "archive_status": "keep"}
    normalized.pop("rule_hash", None)
    rule_hash = _digest(normalized)
    if spec.get("rule_hash") not in (None, rule_hash):
        raise ValueError("microstructure rule hash mismatch requires a new strategy identity")
    normalized["rule_hash"] = rule_hash
    return normalized


def microstructure_rule_hash(spec: Mapping[str, Any]) -> str:
    return normalize_microstructure_spec(spec)["rule_hash"]


def build_microstructure_specs() -> list[dict[str, Any]]:
    """Return 18 independent normalized definitions, with no source-file writes."""
    definitions = []
    pairs = list(ALLOWED_RESEARCH_PAIRS)
    for risk_id, risk in RISK_BUDGETS:
        for k in STOP_WIDTHS:
            for group in GROUPS:
                params = {**BASE_PARAMS, "risk_stop_atr_mult": k}
                spec = {"strategy_id": f"{EXPERIMENT_STRATEGY_ID}_{group}_{risk_id}_k{k:.1f}_100x_maker",
                        "canonical_strategy_id": CANONICAL_STRATEGY_ID,
                        "microstructure_experiment_version": EXPERIMENT_VERSION,
                        "microstructure_group": group, "sample_population": EXPERIMENT_VERSION,
                        "feature_method_version": FEATURE_METHOD_VERSION,
                        "book_gate": copy.deepcopy(BOOK_GATE),
                        "feature_quality_policy": copy.deepcopy(FEATURE_QUALITY),
                        "params": params, "params_frozen": copy.deepcopy(params),
                        "risk_sizing_mode": "entry_atr", "risk_budget_scope": "per_trade_leg",
                        "max_loss_per_trade_usdt": risk, "timeframe": "1h",
                        "research_timeframe": "1h", "universe": list(pairs),
                        "execution_universe": list(pairs), "research_universe": list(pairs),
                        "data_universe": list(pairs), "min_warmup_bars": 116,
                        "position_budget": copy.deepcopy(POSITION_BUDGET),
                        "initial_capital": 10000.0, "fee_rate_per_side": 0.0002,
                        "execution_enabled": True, "execution_blockers": [],
                        "blocked_execution_reason": None, "forward_hypothesis": True,
                        "factorial_factors": {"strategy": EXPERIMENT_STRATEGY_ID,
                                             "risk_budget": risk_id, "stop_width_k": k,
                                             "book_group": group},
                        "feature_availability_policy": "published_by_observation_time_close_plus_90s",
                        "attribution_policy": "paired_published_quality_eligible_bar_subsample",
                        "book_unavailable_policy": "control_baseline_filter_no_new_entry_exits_unchanged",
                        "risk_limit_semantics": "sizing_target_close_trigger_next_open_gap_risk"}
                definitions.append(normalize_microstructure_spec(spec))
    return definitions


def load_microstructure_specs() -> list[dict[str, Any]]:
    """Compatibility entry point for the service; definitions are code-frozen."""
    return build_microstructure_specs()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _finite_number(value)
    return int(number) if number is not None and number == int(number) else None


def _json_safe(value: Any) -> Any:
    """Untrusted feature diagnostics cannot inject NaN into persisted events."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal):
        number = _finite_number(value)
        return number
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _book_audit(record: Any, pair: str, candle_ms: Any, observed_at_ms: int | None,
                group: str, baseline: Mapping[str, Any]) -> dict[str, Any]:
    audit = {"experiment_version": EXPERIMENT_VERSION, "group": group,
             "feature_key": None, "method_version": FEATURE_METHOD_VERSION,
             "source_hash": None, "features": {}, "quality": {},
             "forward_eligible": False, "feature_validity": False,
             "gate_pass": False, "gate_reason": "orderbook_feature_missing",
             "paired_subsample_eligible": False,
             "baseline_target_side": baseline.get("target_side"),
             "baseline_reason": baseline.get("reason")}
    if not isinstance(record, Mapping):
        return audit
    for key in ("feature_key", "method_version", "source_hash", "features", "quality",
                "forward_eligible", "candle_ms", "cutoff_ms", "received_cutoff_ms",
                "observed_at_ms", "published_at_ms"):
        if key in record:
            audit[key] = _json_safe(record[key])
    def reject(reason: str) -> dict[str, Any]:
        audit["gate_reason"] = reason
        return audit

    if observed_at_ms is None:
        return reject("orderbook_observation_time_unavailable")
    stamp = _integer(candle_ms)
    if (stamp is None or record.get("exchange") != "binance" or record.get("market") != "futures"
            or record.get("pair") != pair or record.get("timeframe") != "1h"
            or record.get("method_version") != FEATURE_METHOD_VERSION
            or _integer(record.get("candle_ms")) != stamp
            or _integer(record.get("cutoff_ms")) != stamp + BAR_MS
            or _integer(record.get("received_cutoff_ms")) != stamp + BAR_MS
            or not isinstance(record.get("feature_key"), str) or not record["feature_key"]
            or not isinstance(record.get("source_hash"), str) or not record["source_hash"]):
        return reject("orderbook_feature_identity_mismatch")
    cutoff = stamp + BAR_MS
    source_observed = _integer(record.get("observed_at_ms"))
    published = _integer(record.get("published_at_ms"))
    if (source_observed is None or published is None or source_observed < cutoff
            or published < source_observed or published > observed_at_ms):
        return reject("orderbook_feature_not_available_as_of")
    if published > cutoff + MAX_PUBLISH_LAG_MS:
        return reject("orderbook_feature_expired")
    quality = record.get("quality")
    if (not isinstance(quality, Mapping) or quality.get("eligible") is not True
            or quality.get("status") != "ready" or record.get("forward_eligible") is not True):
        return reject("orderbook_feature_quality_unavailable")
    coverage = _finite_number(quality.get("coverage_ratio"))
    minutes = _integer(quality.get("minute_bins"))
    gap = _finite_number(quality.get("largest_gap_ms"))
    age = _finite_number(quality.get("age_at_close_ms"))
    snapshots = _integer(quality.get("valid_snapshots"))
    if (coverage is None or not 0.95 <= coverage <= 1.0
            or minutes is None or not 55 <= minutes <= 60
            or gap is None or not 0 <= gap <= 30_000
            or age is None or not 0 <= age <= 3_000
            or snapshots is None or snapshots < 100):
        return reject("orderbook_feature_quality_unavailable")
    features = record.get("features")
    obi = _finite_number(features.get("obi_5_time_mean")) if isinstance(features, Mapping) else None
    spread = (_finite_number(features.get("relative_spread_time_mean"))
              if isinstance(features, Mapping) else None)
    if obi is None or not -1 <= obi <= 1 or spread is None or spread < 0:
        return reject("orderbook_feature_values_unavailable")
    audit.update(feature_validity=True, paired_subsample_eligible=True)
    target = baseline.get("target_side")
    if target not in (-1, 1):
        return reject("orderbook_confirmation_not_applicable")
    directional = Decimal(str(obi)) * target >= Decimal("0.1")
    narrow = Decimal(str(spread)) <= Decimal("0.0002")
    audit["gate_pass"] = bool(directional and narrow)
    audit["gate_reason"] = ("orderbook_confirmation_passed" if audit["gate_pass"]
                            else "orderbook_confirmation_not_met")
    return audit


def evaluate_microstructure(spec: Mapping[str, Any], candles_by_pair: Mapping[str, Any],
                            positions: Mapping[str, Any] | None = None,
                            as_of_utc: Any = None, *, book_features: Mapping[str, Any] | None = None
                            ) -> dict[str, Any]:
    """Evaluate the common baseline and an entry-only immutable book gate.

    ``book_features`` comes from ``load_published_feature`` for each *signal*
    bar. ``as_of_utc`` is the actual observation clock, not merely the candle's
    close timestamp. Both flat populations retry a missing publication within
    the same 90-second close grace. Accounts with positions never wait on books.
    """
    normalized = normalize_microstructure_spec(spec)
    positions = positions or {}
    book_features = book_features or {}
    result = core.evaluate(_inner_spec(normalized), candles_by_pair, positions, as_of_utc)
    result.update(strategy_id=normalized["strategy_id"], rule_hash=normalized["rule_hash"],
                  canonical_strategy_id=CANONICAL_STRATEGY_ID, execution_enabled=True,
                  execution_blockers=[], microstructure_group=normalized["microstructure_group"],
                  microstructure_experiment_version=EXPERIMENT_VERSION, feature_waiting=False)
    observed_at_ms = core._as_of_ms(as_of_utc)
    group = normalized["microstructure_group"]
    can_wait = not any(positions.get(pair) for pair in normalized["execution_universe"])
    waiting_pairs = []
    for pair, signal in result["signals"].items():
        audit = _book_audit(book_features.get(pair), pair, signal.get("candle_utc"),
                            observed_at_ms, group, signal)
        evidence = dict(signal.get("evidence") or {})
        evidence["orderbook"] = audit
        signal["evidence"] = evidence
        target = signal.get("target_side")
        if group == "filter" and target in (-1, 1) and not audit["gate_pass"]:
            # s14 itself exits using target 0 and never flips directly. Keep
            # that exit component safe if a later adapter returns a reversal.
            signal["target_side"] = 0 if positions.get(pair) else None
            signal["reason"] = audit["gate_reason"]
        stamp = _integer(signal.get("candle_utc"))
        # The service may take its observation clock just before a feature is
        # committed. Do not consume that bar with a denied entry; retry using
        # the next real clock, while still refusing the future publication now.
        published = _integer(audit.get("published_at_ms"))
        source_observed = _integer(audit.get("observed_at_ms"))
        pending_publication = (book_features.get(pair) is None or
                               (audit["gate_reason"] == "orderbook_feature_not_available_as_of"
                                and stamp is not None and observed_at_ms is not None
                                and source_observed is not None and published is not None
                                and stamp + BAR_MS <= source_observed <= published
                                and observed_at_ms < published <= stamp + BAR_MS + MAX_PUBLISH_LAG_MS))
        if (can_wait and result.get("ready") and pending_publication
                and stamp is not None and observed_at_ms is not None
                and stamp + BAR_MS <= observed_at_ms < stamp + BAR_MS + MAX_PUBLISH_LAG_MS):
            waiting_pairs.append(pair)
    if waiting_pairs:
        result["ready"] = False
        result["feature_waiting"] = True
        result["feature_waiting_pairs"] = waiting_pairs
        result["warmup"] = {**result["warmup"], "ready": False,
                            "reason": "waiting_published_orderbook_feature"}
    return result
