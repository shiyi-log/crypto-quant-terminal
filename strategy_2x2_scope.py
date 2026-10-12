"""Metadata for the focused 2x2 leverage/stop execution experiment.

This module is intentionally declarative.  Importing it never creates paper
accounts, changes an existing definition, or enables the laboratory service.
The account factory can use these identities after a separate operator gate.

The signal mechanism is held constant across the four arms:

* A: 100x, close-confirmed stop (baseline)
* B: 30x, close-confirmed stop (leverage only)
* C: 100x, intrabar OHLC stop (stop timing only)
* D: 30x, intrabar OHLC stop (both changes)

The scope deliberately starts with six families that had either the largest
liquidation count or the clearest risk-cap evidence in the forward audit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


EXPERIMENT_ID = "risk-execution-2x2-v1"
EXPERIMENT_STATUS = "defined_manual_activation_required"

# Keep this list explicit.  A silent expansion to all 29/30 mechanisms would
# multiply storage and event volume before the six-family comparison is read.
FOCUS_STRATEGY_IDS: tuple[str, ...] = (
    "s05_bollinger_fade",
    "s06_volume_climax_reversal",
    "s08_wick_rejection",
    "s12_surge_expansion_breakout",
    "s14_vol_compression_release",
    "s17_overnight_range_fade",
)


@dataclass(frozen=True)
class ExecutionArm:
    """One cell in the 2x2 execution design."""

    arm_id: str
    label: str
    leverage: int
    stop_execution_mode: str
    contrast: str


ARMS: tuple[ExecutionArm, ...] = (
    ExecutionArm("A", "100x close-confirmed", 100, "close_confirmed", "baseline"),
    ExecutionArm("B", "30x close-confirmed", 30, "close_confirmed", "leverage_only"),
    ExecutionArm("C", "100x intrabar", 100, "intrabar", "stop_timing_only"),
    ExecutionArm("D", "30x intrabar", 30, "intrabar", "leverage_and_stop_timing"),
)

_ARM_BY_ID = {arm.arm_id: arm for arm in ARMS}


def arm(arm_id: str) -> ExecutionArm:
    """Return a declared arm or fail closed for a typo."""

    try:
        return _ARM_BY_ID[str(arm_id)]
    except KeyError as exc:
        raise ValueError(f"unknown 2x2 execution arm: {arm_id}") from exc


def candidate_strategy_id(base_strategy_id: str, arm_id: str) -> str:
    """Build a visible, immutable candidate identity for one base mechanism."""

    base = str(base_strategy_id)
    if base not in FOCUS_STRATEGY_IDS:
        raise ValueError(f"strategy is outside the focused 2x2 scope: {base}")
    selected = arm(arm_id)
    mode = "close" if selected.stop_execution_mode == "close_confirmed" else "intrabar"
    return f"{base}_2x2_{selected.arm_id}_L{selected.leverage}_{mode}"


def select_focus_specs(specs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select exactly the six frozen base definitions for the pilot.

    Returned mappings are copies and may be annotated by a factory.  This
    function never mutates the input list or the source JSON representation.
    """

    by_id: dict[str, dict[str, Any]] = {}
    for raw in specs:
        if not isinstance(raw, Mapping):
            raise ValueError("each strategy definition must be a mapping")
        strategy_id = str(raw.get("strategy_id", ""))
        if strategy_id in by_id:
            raise ValueError(f"duplicate strategy definition: {strategy_id}")
        by_id[strategy_id] = dict(raw)
    missing = [strategy_id for strategy_id in FOCUS_STRATEGY_IDS if strategy_id not in by_id]
    if missing:
        raise ValueError(f"focused 2x2 strategies are missing: {', '.join(missing)}")
    return [dict(by_id[strategy_id]) for strategy_id in FOCUS_STRATEGY_IDS]


def candidate_metadata(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Describe the 24 pilot candidates without enabling or persisting them.

    ``rule_hash`` is copied as the signal-side comparison key.  The execution
    factory must calculate a separate execution hash from the arm override;
    this metadata intentionally does not pretend to create an account identity.
    """

    base_id = str(spec.get("strategy_id", ""))
    if base_id not in FOCUS_STRATEGY_IDS:
        raise ValueError(f"strategy is outside the focused 2x2 scope: {base_id}")
    result = []
    for selected in ARMS:
        result.append({
            "experiment_id": EXPERIMENT_ID,
            "experiment_status": EXPERIMENT_STATUS,
            "strategy_id": candidate_strategy_id(base_id, selected.arm_id),
            "variant_of": base_id,
            "signal_rule_hash": spec.get("rule_hash"),
            "arm_id": selected.arm_id,
            "leverage": selected.leverage,
            "stop_execution_mode": selected.stop_execution_mode,
            "contrast": selected.contrast,
            "execution_enabled": False,
            "blocked_execution_reason": "manual_activation_required",
        })
    return result


def build_candidate_specs(specs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build disabled, executable-shaped candidates for offline validation.

    This is deliberately separate from the laboratory loader.  It derives the
    canonical signal hash from each base definition, then puts leverage and
    stop timing in the execution overlay consumed by ``execution_identity``.
    No account is inserted and no candidate is enabled by this function.
    """
    from strategy_signals import normalize_spec

    result: list[dict[str, Any]] = []
    for raw in select_focus_specs(specs):
        base = normalize_spec(raw)
        for selected in ARMS:
            candidate = dict(base)
            candidate.update(
                strategy_id=candidate_strategy_id(base["strategy_id"], selected.arm_id),
                variant_of=base["strategy_id"],
                experiment_id=EXPERIMENT_ID,
                execution_variant=EXPERIMENT_ID,
                signal_rule_hash=base["rule_hash"],
                stop_execution_mode=selected.stop_execution_mode,
                execution_enabled=False,
                blocked_execution_reason="manual_activation_required",
            )
            budget = dict(base.get("position_budget") or {})
            budget["leverage"] = selected.leverage
            candidate["position_budget"] = budget
            result.append(candidate)
    return result


def build_scope_metadata(specs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return an auditable plan for the focused pilot."""

    selected = select_focus_specs(specs)
    candidates = [candidate for spec in selected for candidate in candidate_metadata(spec)]
    if len(candidates) != len(FOCUS_STRATEGY_IDS) * len(ARMS):
        raise ValueError("2x2 candidate catalog has an unexpected size")
    return {
        "experiment_id": EXPERIMENT_ID,
        "status": EXPERIMENT_STATUS,
        "scope": {
            "strategy_ids": list(FOCUS_STRATEGY_IDS),
            "strategy_count": len(FOCUS_STRATEGY_IDS),
            "arms_per_strategy": len(ARMS),
            "candidate_count": len(candidates),
        },
        "arms": [asdict(selected) for selected in ARMS],
        "candidates": candidates,
        "invariants": {
            "signal_parameters_unchanged": True,
            "risk_budgets_unchanged": True,
            "fee_model_unchanged": True,
            "execution_universe_unchanged": True,
            "existing_accounts_mutated": False,
            "default_lab_activation": False,
        },
        "intrabar_policy": {
            "source": "5m_ohlc",
            "long_trigger": "low <= stop_level",
            "short_trigger": "high >= stop_level",
            "same_candle_stop_target": "stop_first_conservative",
            "gap_through_stop": "next_open_price",
            "tick_data_used": False,
        },
    }
