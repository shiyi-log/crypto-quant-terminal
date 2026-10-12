"""Pure candidate factory for the focused 2x2 execution experiment.

This module only prepares disabled candidate definitions.  It does not add
accounts to the lab or mutate existing definitions; activation requires a
separate runtime integration and operator decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from strategy_2x2_scope import (
    ARMS,
    EXPERIMENT_ID,
    EXPERIMENT_STATUS,
    FOCUS_STRATEGY_IDS,
    arm as get_arm,
    candidate_strategy_id,
    select_focus_specs,
)


def _experimental_execution_variant() -> str:
    """Load the opt-in execution variant without importing simulation at module load."""
    from strategy_simulation import EXPERIMENTAL_EXECUTION_VARIANT

    return EXPERIMENTAL_EXECUTION_VARIANT


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized_base(raw: Mapping[str, Any], normalizer: Callable) -> dict[str, Any]:
    strategy_id = str(raw["strategy_id"])
    budget = copy.deepcopy(raw.get("position_budget") or {})
    budget.setdefault("target_exposure", 0.30)
    budget.setdefault("max_positions", 2)
    budget.setdefault("max_per_coin", 1)
    budget.setdefault("sizing", "剩余预算 / 剩余槽位")
    budget.setdefault("leverage", 100)
    # The source definitions predate the 2x2 execution overlay.  Keep their
    # legacy convention while normalizing the signal-side identity; the
    # materializer replaces it with arm-specific explanatory metadata.
    from strategy_signals import LEGACY_LEVERAGE_CONVENTION

    budget.setdefault("leverage_convention", LEGACY_LEVERAGE_CONVENTION)
    supplied = {
        "strategy_id": strategy_id,
        "params": copy.deepcopy(raw.get("params", raw.get("params_frozen", {}))),
        "timeframe": raw.get("timeframe", "5m"),
        "universe": copy.deepcopy(raw.get("universe")),
        "min_warmup_bars": raw.get("min_warmup_bars"),
        "position_budget": budget,
        "initial_capital": raw.get("initial_capital", 10_000.0),
        "fee_rate_per_side": raw.get("fee_rate_per_side", 0.0002),
    }
    supplied = {key: value for key, value in supplied.items() if value is not None}
    normalized = dict(normalizer(supplied))
    if normalized.get("strategy_id") != strategy_id:
        raise ValueError("normalizer changed canonical strategy identity")
    # Preserve an explicit risk budget when the source carries one.  Adding a
    # configurable budget to a non-ATR strategy would make execution_identity()
    # reject it, so the factory must not invent this field.
    if "max_loss_per_trade_usdt" in raw:
        normalized["max_loss_per_trade_usdt"] = float(raw["max_loss_per_trade_usdt"])
    return normalized


def _execution_plan_hash(base: Mapping[str, Any], arm: Any) -> str:
    """Hash a proposed execution contract separately from signal identity."""
    return _digest({
        "experiment_id": EXPERIMENT_ID,
        "canonical_strategy_id": base["strategy_id"],
        "timeframe": base["timeframe"],
        "execution_universe": base.get("execution_universe", base["universe"]),
        "initial_capital": base["initial_capital"],
        "fee_rate_per_side": base["fee_rate_per_side"],
        "position_budget": copy.deepcopy(base["position_budget"]),
        "max_loss_per_trade_usdt": base.get("max_loss_per_trade_usdt"),
        "risk_sizing_mode": base.get("risk_sizing_mode"),
        "execution_override": {
            "leverage": arm.leverage,
            "stop_execution_mode": arm.stop_execution_mode,
        },
    })


def build_2x2_candidates(
    specs: Iterable[Mapping[str, Any]],
    *,
    normalizer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build 24 disabled candidate definitions from the six focused families.

    Each candidate retains the source strategy parameters and records a
    canonical signal hash that is shared by its four arms.  Leverage and stop
    timing live only in ``execution_override``.  The returned execution plan
    hash is proposal metadata, not the strategy_simulation account hash.
    """
    if normalizer is None:
        from strategy_signals import normalize_spec

        normalizer = normalize_spec
    selected = select_focus_specs(specs)
    result: list[dict[str, Any]] = []
    for raw in selected:
        base = _normalized_base(raw, normalizer)
        signal_hash = str(base["rule_hash"])
        for arm in ARMS:
            override = {"leverage": arm.leverage,
                        "stop_execution_mode": arm.stop_execution_mode}
            candidate = copy.deepcopy(base)
            candidate.update(
                strategy_id=candidate_strategy_id(base["strategy_id"], arm.arm_id),
                canonical_strategy_id=base["strategy_id"],
                variant_of=base["strategy_id"],
                variant_label=f"2x2 execution arm {arm.arm_id}: {arm.label}",
                experiment_id=EXPERIMENT_ID,
                experiment_status=EXPERIMENT_STATUS,
                arm_id=arm.arm_id,
                signal_rule_hash=signal_hash,
                canonical_signal_rule_hash=signal_hash,
                execution_override=override,
                execution_plan_hash=_execution_plan_hash(base, arm),
                execution_enabled=False,
                blocked_execution_reason="manual_activation_required",
            )
            result.append(candidate)
    validate_2x2_candidates(result)
    return result


def materialize_2x2_execution_spec(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Map one disabled candidate to an execution-ready, still-disabled spec.

    The candidate factory deliberately keeps execution changes in metadata so it
    cannot be mistaken for an activated paper account.  This pure adapter is
    the explicit boundary used by offline execution tests and a future,
    separately gated account factory.  It never mutates ``candidate`` and it
    never enables execution.
    """
    if not isinstance(candidate, Mapping):
        raise ValueError("2x2 candidate must be a mapping")
    if candidate.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("candidate does not belong to the focused 2x2 experiment")
    canonical = str(candidate.get("canonical_strategy_id", ""))
    if canonical not in FOCUS_STRATEGY_IDS:
        raise ValueError("candidate has an invalid canonical strategy identity")
    if candidate.get("execution_enabled") is not False:
        raise ValueError("2x2 candidates must remain disabled until manual activation")
    override = candidate.get("execution_override")
    if not isinstance(override, Mapping):
        raise ValueError("2x2 candidate is missing execution_override")
    selected = get_arm(str(candidate.get("arm_id", "")))
    expected_override = {
        "leverage": selected.leverage,
        "stop_execution_mode": selected.stop_execution_mode,
    }
    if dict(override) != expected_override:
        raise ValueError("candidate execution_override does not match its arm")
    signal_hash = candidate.get("canonical_signal_rule_hash") or candidate.get("signal_rule_hash")
    if not isinstance(signal_hash, str) or not signal_hash:
        raise ValueError("2x2 candidate is missing canonical signal rule hash")

    materialized = copy.deepcopy(dict(candidate))
    budget = materialized.get("position_budget")
    if not isinstance(budget, Mapping):
        raise ValueError("2x2 candidate is missing position_budget")
    budget = copy.deepcopy(dict(budget))
    budget["leverage"] = selected.leverage
    from strategy_signals import leverage_convention_for

    budget["leverage_convention"] = leverage_convention_for(selected.leverage)
    materialized["position_budget"] = budget
    materialized["execution_variant"] = _experimental_execution_variant()
    materialized["signal_rule_hash"] = signal_hash
    materialized["canonical_signal_rule_hash"] = signal_hash
    materialized["stop_execution_mode"] = selected.stop_execution_mode
    # The legacy fixed-stop families use the execution model's immutable
    # max-loss default.  An explicit field is valid only for ATR risk sizing;
    # keeping it on a fixed-stop candidate makes execution_identity reject the
    # mixed contract before an operator could activate it.
    if "risk_stop_atr_mult" not in materialized.get("params", {}):
        materialized.pop("max_loss_per_trade_usdt", None)
    # The operator gate remains closed after materialization.  A future runner
    # must make an explicit, separately audited decision to enable a candidate.
    materialized["execution_enabled"] = False
    return materialized


def validate_2x2_candidates(candidates: Iterable[Mapping[str, Any]]) -> None:
    """Fail closed unless the 24 candidates preserve the declared contrasts."""
    rows = list(candidates)
    expected_count = len(FOCUS_STRATEGY_IDS) * len(ARMS)
    if len(rows) != expected_count:
        raise ValueError(f"2x2 experiment requires exactly {expected_count} candidates")
    ids = [str(row.get("strategy_id", "")) for row in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("2x2 candidate identities must be nonempty and unique")
    for canonical in FOCUS_STRATEGY_IDS:
        group = [row for row in rows if row.get("canonical_strategy_id") == canonical]
        if len(group) != len(ARMS):
            raise ValueError(f"{canonical} must have exactly four execution arms")
        signal_hashes = {row.get("canonical_signal_rule_hash") for row in group}
        if len(signal_hashes) != 1 or None in signal_hashes:
            raise ValueError(f"{canonical} execution arms must share one signal rule hash")
        plan_hashes = {row.get("execution_plan_hash") for row in group}
        if len(plan_hashes) != len(ARMS) or None in plan_hashes:
            raise ValueError(f"{canonical} execution arms must have distinct execution plans")
        by_arm = {row.get("arm_id"): row for row in group}
        if set(by_arm) != {arm.arm_id for arm in ARMS}:
            raise ValueError(f"{canonical} has an invalid 2x2 arm set")
        for arm in ARMS:
            row = by_arm[arm.arm_id]
            if row.get("execution_override") != {
                    "leverage": arm.leverage,
                    "stop_execution_mode": arm.stop_execution_mode}:
                raise ValueError(f"{canonical} arm {arm.arm_id} has an invalid execution override")
            if row.get("execution_enabled") is not False:
                raise ValueError("2x2 candidates must remain disabled until manual activation")
        reference = by_arm["A"]
        invariant_fields = ("params", "timeframe", "universe", "initial_capital",
                            "fee_rate_per_side", "max_loss_per_trade_usdt",
                            "risk_sizing_mode")
        for arm_id, row in by_arm.items():
            selected = next((item for item in ARMS if item.arm_id == arm_id), None)
            if selected is None:
                raise ValueError(f"{canonical} has an unknown arm {arm_id}")
            for field in invariant_fields:
                if row.get(field) != reference.get(field):
                    raise ValueError(f"{canonical} arm {arm_id} changes invariant {field}")
            budget = row.get("position_budget")
            reference_budget = reference.get("position_budget")
            if not isinstance(budget, Mapping) or not isinstance(reference_budget, Mapping):
                raise ValueError(f"{canonical} arm {arm_id} requires a position_budget mapping")
            for key, value in reference_budget.items():
                if key != "leverage" and budget.get(key) != value:
                    raise ValueError(f"{canonical} arm {arm_id} changes position budget {key}")
            # Metadata-only candidates intentionally leave the execution
            # overlay unapplied.  Once materialized, these fields must match
            # the declared arm exactly.
            if "execution_variant" in row:
                if row["execution_variant"] != _experimental_execution_variant():
                    raise ValueError("2x2 candidate has an unsupported execution variant")
                if budget.get("leverage") != selected.leverage:
                    raise ValueError(f"{canonical} arm {arm_id} has an invalid leverage")
                if row.get("stop_execution_mode") != selected.stop_execution_mode:
                    raise ValueError(f"{canonical} arm {arm_id} has an invalid stop mode")


def risk_capped_notional(max_loss_usdt: float, stop_distance_ratio: float,
                         round_trip_fee_ratio: float) -> float:
    """Risk-budget notional before margin, cash, and portfolio caps.

    Leverage is intentionally absent: when the stop-risk cap is binding,
    leverage changes margin/liquidation distance, not the risk-capped notional.
    """
    values = (max_loss_usdt, stop_distance_ratio, round_trip_fee_ratio)
    if any(isinstance(value, bool) for value in values):
        raise ValueError("risk inputs must be finite numbers")
    try:
        maximum, stop, fee = map(float, values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("risk inputs must be finite numbers") from exc
    if not all(value >= 0 for value in (maximum, stop, fee)) or maximum <= 0 or stop + fee <= 0:
        raise ValueError("risk budget must be positive and stop plus fees must be positive")
    return maximum / (stop + fee)


def risk_capped_margin(max_loss_usdt: float, stop_distance_ratio: float,
                       round_trip_fee_ratio: float, leverage: int) -> float:
    """Return the margin required to carry the risk-capped notional."""
    if isinstance(leverage, bool) or int(leverage) != leverage or leverage <= 0:
        raise ValueError("leverage must be a positive integer")
    return risk_capped_notional(max_loss_usdt, stop_distance_ratio,
                                round_trip_fee_ratio) / int(leverage)
