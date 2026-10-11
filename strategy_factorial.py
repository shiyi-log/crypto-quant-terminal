"""Pure definitions for the new ATR-risk factorial paper population.

The existing accounts are never read or changed here.  These definitions need
normalization and immutable execution-identity checks before a caller persists
them.  A disabled definition is still an experiment identity; its named blocker
must be honored by both the service and execution engine.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SPECS_PATH = ROOT / "research_multi_strategy" / "strategy_specs.json"
DESIGN_VERSION = "atr-risk-full-factorial-v1"
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
TRX = "TRX/USDT:USDT"
ALLOWED_RESEARCH_PAIRS = (BTC, ETH, TRX)
RISK_BUDGETS = (("R50", 50.0), ("R200", 200.0), ("R1000", 1000.0))
STOP_WIDTHS = (1.0, 1.5, 2.5)
STRATEGY_NUMBERS = frozenset(range(1, 31)) - {15}
BASE_ID = re.compile(r"s(?P<number>\d{2})_[a-z][a-z0-9_]*\Z")
FACTOR_ID = re.compile(
    r"(?P<canonical>s\d{2}_[a-z][a-z0-9_]*)_"
    r"(?P<arm>B15m|Cgen2|Dgen3)_"
    r"(?P<risk>R50|R200|R1000)_k(?P<k>1\.0|1\.5|2\.5)_100x_maker\Z"
)

# These are capabilities of the current causal signal implementation, not
# verdicts about whether the underlying hypotheses can make money.
RISK_BLOCKERS = {
    "s04_btc_eth_pairs": "paired_atr_stop_policy_unimplemented",
    "s19_multi_coin_breadth": "breadth_atr_stop_policy_unimplemented",
    "s24_cross_sectional_lowvol": "cross_sectional_atr_stop_policy_unimplemented",
}


def _arm(number: int) -> tuple[str, str]:
    if number <= 11:
        return "B15m", "B_15m_maker"
    if number <= 21:
        return "Cgen2", "C_gen2_multitf"
    return "Dgen3", "D_gen3_multitf"


def parse_factorial_identity(strategy_id: Any) -> dict[str, Any] | None:
    """Parse only the explicit new suffix; legacy identities return ``None``.

    The arm must agree with the mechanism number.  This parser is intentionally
    independent of strategy_signals so the canonical resolver may reuse it.
    """
    match = FACTOR_ID.fullmatch(str(strategy_id))
    if match is None:
        return None
    canonical = match["canonical"]
    number = int(BASE_ID.fullmatch(canonical)["number"])
    if number not in STRATEGY_NUMBERS or match["arm"] != _arm(number)[0]:
        raise ValueError("factorial identity has an invalid strategy or arm")
    return {
        "canonical_strategy_id": canonical,
        "arm": match["arm"],
        "risk_budget": match["risk"],
        "max_loss_per_trade_usdt": dict(RISK_BUDGETS)[match["risk"]],
        "risk_stop_atr_mult": float(match["k"]),
    }


def _pairs(values: Any, field: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f"{field} must contain explicit exchange pairs")
    pairs = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]+/USDT:USDT", value):
            raise ValueError(f"{field} contains an invalid exchange pair")
        if value in pairs:
            raise ValueError(f"{field} contains duplicate exchange pairs")
        pairs.append(value)
    return pairs


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_strategies(document: Mapping[str, Any]) -> list[tuple[int, dict]]:
    items = document.get("strategies")
    if not isinstance(items, list):
        raise ValueError("research document must contain a strategies list")
    selected = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each research strategy must be a mapping")
        match = BASE_ID.fullmatch(str(item.get("strategy_id", "")))
        if match is None:
            raise ValueError("research strategy must have an unversioned base identity")
        number = int(match["number"])
        if number in (15, 31):
            continue
        if number not in STRATEGY_NUMBERS:
            raise ValueError("unexpected strategy in factorial research population")
        if number in selected:
            raise ValueError("duplicate strategy number in factorial research population")
        selected[number] = dict(item)
    if set(selected) != STRATEGY_NUMBERS:
        raise ValueError("factorial research population must contain all 29 retained strategies")
    return sorted(selected.items())


def _validate_declared_axes(document: Mapping[str, Any]) -> None:
    design = document.get("risk_budget_design", {})
    if not isinstance(design, Mapping):
        raise ValueError("invalid declared factorial axes")
    declared = design.get("full_factorial")
    if declared is None:
        return
    if not isinstance(declared, Mapping) or not isinstance(declared.get("axes"), Mapping):
        raise ValueError("invalid declared factorial axes")
    axes = declared["axes"]
    if not all(isinstance(axes.get(axis), Mapping)
               for axis in ("strategy", "risk_budget", "stop_width_k")):
        raise ValueError("invalid declared factorial axes")
    expected = (
        axes.get("strategy", {}).get("levels") == 29,
        axes.get("risk_budget", {}).get("levels") == [name for name, _ in RISK_BUDGETS],
        axes.get("risk_budget", {}).get("max_loss_usdt") == dict(RISK_BUDGETS),
        axes.get("stop_width_k", {}).get("levels") == list(STOP_WIDTHS),
        declared.get("total_accounts") == 261,
    )
    if not all(expected):
        raise ValueError("declared factorial axes do not match the frozen 29x3x3 design")


def _universe_contract(item: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    canonical = item["strategy_id"]
    source_universe = _pairs(item.get("universe"), "universe")
    target = [BTC, ETH] if timeframe in {"5m", "15m"} else list(ALLOWED_RESEARCH_PAIRS)
    context = item.get("research_universe_context")
    context = _pairs(context, "research_universe_context") if context is not None else []
    blockers = []
    if canonical in {"s19_multi_coin_breadth", "s24_cross_sectional_lowvol"}:
        # Reducing the declared breadth/ranking constituents to three symbols
        # would be a new mechanism. Preserve the dependency and pause execution.
        if not context:
            blockers.append("research_context_unavailable")
        elif any(pair not in ALLOWED_RESEARCH_PAIRS for pair in context):
            blockers.append("paused_research_context_required")
        research = context or source_universe
    else:
        # The ordinary signal mechanisms evaluate each asset independently.
        # Their old wider universe does not justify researching paused assets.
        research = list(target)
    if canonical == "s04_btc_eth_pairs":
        execution, research = [BTC, ETH], [BTC, ETH]
    elif canonical == "s10_lead_lag_btc_eth":
        execution, research = [ETH], [BTC, ETH]
    elif canonical == "s19_multi_coin_breadth":
        execution = [BTC]
    elif canonical == "s30_dual_asset_confirmation":
        execution, research = [BTC, ETH], [BTC, ETH]
    else:
        execution = list(target)
    if canonical == "s24_cross_sectional_lowvol":
        blockers.append("cross_sectional_portfolio_execution_unimplemented")
    return {
        "universe": list(target),
        "execution_universe": execution,
        "research_universe": research,
        # A blocked research dependency remains visible but is not fetched.
        "data_universe": [pair for pair in research if pair in ALLOWED_RESEARCH_PAIRS],
        "source_original_universe": source_universe,
        "execution_blockers": blockers,
    }


def build_factorial_specs(
    document: Mapping[str, Any],
    *,
    normalizer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build 261 independent identities without storage or account mutations.

    ``normalizer`` can be strategy_signals.normalize_spec once its canonical
    resolver accepts these identities and its opt-in ATR params. Raw execution
    metadata survives merging the normalized signal definition. A caller must
    not fall back to fixed-percent sizing if normalization or support fails.
    """
    if not isinstance(document, Mapping):
        raise ValueError("research document must be a mapping")
    _validate_declared_axes(document)
    strategies = _source_strategies(document)
    result = []
    for number, item in strategies:
        canonical = item["strategy_id"]
        timeframe = str(item.get("timeframe", ""))
        if timeframe not in {"5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("unsupported factorial strategy timeframe")
        supplied = item.get("params_frozen")
        if not isinstance(supplied, Mapping):
            raise ValueError("params_frozen must be a mapping")
        atr_window = _positive_integer(supplied.get("atr_window", 14), "atr_window")
        warmup = _positive_integer(item.get("min_warmup_bars", atr_window + 2),
                                   "min_warmup_bars")
        universe = _universe_contract(item, timeframe)
        blockers = list(universe.pop("execution_blockers"))
        if canonical in RISK_BLOCKERS:
            blockers.append(RISK_BLOCKERS[canonical])
        arm, arm_id = _arm(number)
        for risk_id, max_loss in RISK_BUDGETS:
            for k in STOP_WIDTHS:
                # No mutable dict/list is shared between accounts or with the
                # research file. Neither legacy hash nor existing state travels.
                spec = copy.deepcopy(item)
                for field in ("rule_hash", "execution_hash", "state", "definition"):
                    spec.pop(field, None)
                params = copy.deepcopy(dict(supplied))
                params.update(atr_window=atr_window, risk_stop_atr_mult=k)
                if canonical == "s08_wick_rejection":
                    params["stop_atr_mult"] = k
                strategy_id = f"{canonical}_{arm}_{risk_id}_k{k:.1f}_100x_maker"
                budget = dict(item.get("position_budget") or {}, target_exposure=0.3,
                              max_positions=2, max_per_coin=1,
                              sizing="剩余预算 / 剩余槽位", leverage=100,
                              leverage_convention="A：保证金不变，名义 ×100，逐根强平")
                spec.update(
                    **copy.deepcopy(universe),
                    strategy_id=strategy_id, canonical_strategy_id=canonical,
                    variant_of=canonical, variant_label="ATR 止损因子实验",
                    arm_id=arm_id, params=params, params_frozen=copy.deepcopy(params),
                    min_warmup_bars=max(warmup, atr_window + 2),
                    position_budget=budget, initial_capital=10000.0,
                    fee_rate_per_side=0.0002, risk_sizing_mode="entry_atr",
                    max_loss_per_trade_usdt=max_loss,
                    research_timeframe=timeframe,
                    factorial_design_version=DESIGN_VERSION,
                    factorial_factors={"strategy": canonical, "risk_budget": risk_id,
                                       "stop_width_k": k},
                    sample_population=DESIGN_VERSION,
                    legacy_samples_policy="preserve_separate_from_factorial_evaluation",
                    archive_status="keep",
                    execution_enabled=not blockers,
                    execution_blockers=list(blockers),
                    blocked_execution_reason=blockers[0] if blockers else None,
                    label=f"{item.get('name', canonical)} · {risk_id} · k{k:.1f} · 100×",
                    risk_limit_semantics="sizing_target_close_trigger_next_open_gap_risk",
                )
                if blockers:
                    spec["data_status"] = ("research_context_paused" if
                                           "paused_research_context_required" in blockers
                                           else "atr_risk_execution_pending")
                if normalizer is not None:
                    normalized = dict(normalizer(spec))
                    if normalized.get("strategy_id") != strategy_id:
                        raise ValueError("normalizer changed factorial account identity")
                    spec.update(normalized)
                    if (spec.get("max_loss_per_trade_usdt") != max_loss
                            or spec.get("risk_sizing_mode") != "entry_atr"
                            or spec.get("params", {}).get("risk_stop_atr_mult") != k
                            or (canonical == "s08_wick_rejection" and
                                spec.get("params", {}).get("stop_atr_mult") != k)):
                        raise ValueError("normalizer changed factorial risk contract")
                    if blockers and (spec.get("execution_enabled") is not False
                                     or spec.get("blocked_execution_reason") != blockers[0]
                                     or spec.get("execution_blockers") != blockers):
                        raise ValueError("normalizer removed an unsupported execution blocker")
                result.append(spec)
    if len(result) != 261 or len({spec["strategy_id"] for spec in result}) != 261:
        raise ValueError("factorial account identities must be 261 distinct definitions")
    return result


def load_factorial_specs(
    path: str | Path | None = None,
    *,
    normalizer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read one research snapshot and build the new population only."""
    document = json.loads(Path(path or SPECS_PATH).read_text(encoding="utf-8"))
    return build_factorial_specs(document, normalizer=normalizer)
