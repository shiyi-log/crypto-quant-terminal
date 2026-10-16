"""Independent, immutable definitions for the five Fnew paper hypotheses.

The proposal file is evidence/input, not an executable frozen account. This
module turns exactly s32..s36 into explicit economic contracts without touching
the 29 legacy strategies or the 261 ATR factorial definitions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from strategy_fee_schedule import (
    DEMO_FEE_MODEL_VERSION,
    LEGACY_TAKER_FEE_PER_SIDE,
    MAKER_FEE_PER_SIDE,
    TAKER_FEE_PER_SIDE,
)


ROOT = Path(__file__).resolve().parent
SPECS_PATH = ROOT / "research_multi_strategy" / "strategy_specs.json"
FNEW_DESIGN_VERSION = "fnew-20261012-v1"
FNEW_SIGNAL_VERSION = "fnew-causal-native-market-separated-signals-v1"
FNEW_SINGLE_STOP_POLICY = "frozen_entry_atr_close_trigger_next_open_v1"
FNEW_PAIR_EXECUTION_POLICY = "atomic-pair-equal-notional-ohlc-v1"
PAIR_BLOCKER = "paired_atr_stop_policy_unimplemented"
LEGACY_PAIRED_NOTIONAL_FORMULA = (
    "per_leg_notional=R/(k*entry_pair_atr_return+4*fee_rate_per_side)"
)
LEGACY_NOTIONAL_FORMULA = "R/(k*entry_atr/entry_price+2*fee_rate_per_side)"
BTC, ETH, TRX = "BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"
ALLOWED_RESEARCH_PAIRS = (BTC, ETH, TRX)
RISK_BUDGETS = (("R50", 50.0), ("R200", 200.0), ("R1000", 1000.0))
STOP_WIDTHS = (1.0, 1.5, 2.5)
FNEW_CANONICAL_IDS = (
    "s32_cointegration_spread_1h", "s33_basis_momentum_1h",
    "s34_vwap_anchor_reversion_15m", "s35_cascade_continuation_15m",
    "s36_correlation_regime_1h",
)
PAIR_STRATEGIES = frozenset((FNEW_CANONICAL_IDS[0], FNEW_CANONICAL_IDS[4]))
CANONICAL_TIMEFRAMES = dict(zip(FNEW_CANONICAL_IDS, ("1h", "1h", "15m", "15m", "1h")))
CANONICAL_UNIVERSES = {
    canonical: list(ALLOWED_RESEARCH_PAIRS if index < 3 else (BTC, ETH))
    for index, canonical in enumerate(FNEW_CANONICAL_IDS)
}
DEFAULT_WARMUP = dict(zip(FNEW_CANONICAL_IDS, (520, 120, 60, 60, 180)))
PARAMETER_DEFAULTS = {
    FNEW_CANONICAL_IDS[0]: {
        "z_entry": 2.0, "z_exit": 0.5, "adf_window": 500,
        "adf_maxlag": 5, "adf_autolag": "AIC", "adf_trend": "c", "adf_pvalue_threshold": 0.05,
        "spread_model": "log_price_ols_intercept", "zscore_window": 500,
        "pair_selection": "max_abs_z_cointegrated_then_lexicographic",
        "positive_beta_required": True, "freeze_entry_regression": True,
        "H": 72, "atr_window": 14, "risk_stop_atr_mult": 1.5,
    },
    FNEW_CANONICAL_IDS[1]: {
        "k_sigma": 2.0, "sigma_window": 100, "basis_delta_lag": 1,
        "exit_sigma": 0.5, "H": 24, "atr_window": 14,
        "risk_stop_atr_mult": 1.5,
    },
    FNEW_CANONICAL_IDS[2]: {
        "k_atr": 2.0, "exit_frac": 0.25, "anchor_timezone": "UTC",
        "vwap_price": "quote_volume_over_base_with_hlc3_fallback_v1",
        "force_flat_at_day_end": True, "H": 32, "atr_window": 14,
        "risk_stop_atr_mult": 1.5,
    },
    FNEW_CANONICAL_IDS[3]: {
        "run_bars": 3, "cum_move_atr": 2.0, "H": 8,
        "cascade_proxy": "same_body_run_atr_move_without_exhaustion_v1",
        "atr_window": 14, "risk_stop_atr_mult": 1.5,
    },
    FNEW_CANONICAL_IDS[4]: {
        "rho_window": 168, "rho_drop": 0.15, "rho_exit": 0.05,
        "rho_compare_lag": 168, "rho_high_threshold": 0.7,
        "strength_window": 168, "rho_exit_mode": "rise_from_entry_rho",
        "return_measure": "simple_close_to_close",
        "H": 72, "atr_window": 14, "risk_stop_atr_mult": 1.5,
    },
}
IDENTITY_PATTERN = re.compile(
    r"(?P<canonical>s(?:32|33|34|35|36)_[a-z][a-z0-9_]*)_Fnew_"
    r"(?P<demo>Demo_)?"
    r"(?P<risk>R50|R200|R1000)_k(?P<k>1\.0|1\.5|2\.5)_100x_"
    r"(?P<fee>maker|taker)\Z"
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def parse_fnew_identity(strategy_id: Any) -> dict[str, Any] | None:
    """Identify Fnew contracts while retaining the original frozen fee model.

    The Demo marker gives new fee-table metadata a distinct account identity,
    including maker accounts whose numerical fee already matches Demo.
    """
    if not isinstance(strategy_id, str) or len(strategy_id) > 80:
        return None
    match = IDENTITY_PATTERN.fullmatch(strategy_id)
    if match is None:
        return None
    canonical = match["canonical"]
    if canonical not in FNEW_CANONICAL_IDS:
        raise ValueError("Fnew identity has an unsupported canonical strategy")
    expected_fee = "taker" if canonical == FNEW_CANONICAL_IDS[1] else "maker"
    if match["fee"] != expected_fee:
        raise ValueError("Fnew identity does not match its frozen fee convention")
    demo_fee_model = match["demo"] is not None
    identity = {
        "canonical_strategy_id": canonical, "arm": "Fnew",
        "risk_budget": match["risk"],
        "max_loss_per_trade_usdt": dict(RISK_BUDGETS)[match["risk"]],
        "risk_stop_atr_mult": float(match["k"]), "fee_convention": expected_fee,
        "fee_rate_per_side": (TAKER_FEE_PER_SIDE if demo_fee_model else LEGACY_TAKER_FEE_PER_SIDE)
        if expected_fee == "taker" else MAKER_FEE_PER_SIDE,
    }
    if demo_fee_model:
        identity["fee_model_version"] = DEMO_FEE_MODEL_VERSION
        identity["exit_fee_rate_per_side"] = TAKER_FEE_PER_SIDE
    return identity


def _number(value: Any, field: str, *, positive: bool = True) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite {'positive ' if positive else ''}number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{field} must be a finite {'positive ' if positive else ''}number")
    return result


def _integer(value: Any, field: str, *, minimum: int = 1) -> int:
    result = _number(value, field, positive=False)
    if result != int(result) or result < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return int(result)


def _pairs(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must contain explicit exchange pairs")
    result = list(value)
    if (any(pair not in ALLOWED_RESEARCH_PAIRS for pair in result)
            or len(set(result)) != len(result)):
        raise ValueError(f"{field} contains invalid, paused, or duplicate pairs")
    return result


def data_feed_contract(canonical: str) -> dict[str, Any]:
    """Freeze markets separately. A futures series can never substitute spot."""
    pairs = CANONICAL_UNIVERSES[canonical]
    timeframe = CANONICAL_TIMEFRAMES[canonical]
    feeds = {"futures": {"market": "futures", "timeframe": timeframe,
                         "pairs": list(pairs)}}
    if canonical == FNEW_CANONICAL_IDS[1]:
        pair_map = {pair: pair.removesuffix(":USDT") for pair in pairs}
        feeds["spot"] = {"market": "spot", "timeframe": timeframe,
                         "pairs": list(pair_map.values()), "pair_map": pair_map}
    return feeds


def required_fnew_bars(canonical: str, params: Mapping[str, Any]) -> int:
    """Minimum causal input; the stated proposal warmup may be stricter."""
    atr = params["atr_window"] + 2
    if canonical == FNEW_CANONICAL_IDS[0]:
        return max(atr, params["adf_window"] + 1, params["zscore_window"] + 1)
    if canonical == FNEW_CANONICAL_IDS[1]:
        return max(atr, params["sigma_window"] + params["basis_delta_lag"] + 1)
    if canonical == FNEW_CANONICAL_IDS[3]:
        return max(atr, params["run_bars"] + 1)
    if canonical == FNEW_CANONICAL_IDS[4]:
        return max(atr, params["rho_window"] + params["rho_compare_lag"] + 1,
                   params["strength_window"] + 1)
    return atr


def _parameters(canonical: str, supplied: Any, k: float) -> dict[str, Any]:
    if not isinstance(supplied, Mapping):
        raise ValueError("Fnew params must be a mapping")
    defaults = PARAMETER_DEFAULTS[canonical]
    if set(supplied) - set(defaults):
        raise ValueError("unsupported Fnew parameter")
    params = {**copy.deepcopy(defaults), **copy.deepcopy(dict(supplied))}
    if "risk_stop_atr_mult" not in supplied:
        params["risk_stop_atr_mult"] = k
    for field, default in defaults.items():
        if isinstance(default, bool) or isinstance(default, str):
            if params[field] != default or type(params[field]) is not type(default):
                raise ValueError(f"unsupported {field} convention")
        elif isinstance(default, int):
            params[field] = _integer(params[field], field, minimum=0 if field == "adf_maxlag" else 1)
        else:
            params[field] = _number(params[field], field)
    if params["risk_stop_atr_mult"] != k:
        raise ValueError("Fnew identity does not match stop width")
    if canonical == FNEW_CANONICAL_IDS[0]:
        if params["z_exit"] >= params["z_entry"]:
            raise ValueError("z_exit must be below z_entry")
        if not 0 < params["adf_pvalue_threshold"] < 1:
            raise ValueError("adf_pvalue_threshold must be in (0, 1)")
        if params["adf_window"] < 20 or params["zscore_window"] < 20:
            raise ValueError("cointegration windows must be >= 20")
        if params["adf_window"] != params["zscore_window"]:
            raise ValueError("s32 requires one prior window for fitting and standardization")
        if params["adf_maxlag"] * 2 + 5 >= params["adf_window"]:
            raise ValueError("adf_maxlag is too large for adf_window")
    elif canonical == FNEW_CANONICAL_IDS[1]:
        if params["sigma_window"] < 2 or params["exit_sigma"] >= params["k_sigma"]:
            raise ValueError("basis sigma_window or exit_sigma is invalid")
    elif canonical == FNEW_CANONICAL_IDS[2]:
        if params["exit_frac"] >= params["k_atr"]:
            raise ValueError("VWAP exit_frac must be below k_atr")
    elif canonical == FNEW_CANONICAL_IDS[4]:
        if min(params[field] for field in ("rho_window", "rho_compare_lag", "strength_window")) < 2:
            raise ValueError("correlation windows and comparison lag must be >= 2")
        if not -1 < params["rho_high_threshold"] < 1:
            raise ValueError("rho_high_threshold must be in (-1, 1)")
        if params["rho_drop"] > 2 or params["rho_exit"] > 2:
            raise ValueError("correlation changes cannot exceed 2")
    return params


def normalize_fnew_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the complete Fnew economics and reject frozen-identity changes.

    Description, provenance, and source research claims are preserved but do
    not enter the economic hash. All executable parameters, timing, markets,
    risk, fees, enabled state and portfolio policies do enter it.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("Fnew spec must be a mapping")
    identity = parse_fnew_identity(spec.get("strategy_id"))
    if identity is None:
        raise ValueError("unsupported Fnew strategy identity")
    canonical = identity["canonical_strategy_id"]
    if spec.get("canonical_strategy_id", canonical) != canonical:
        raise ValueError("Fnew identity does not match canonical_strategy_id")
    for field, expected in (("fnew_design_version", FNEW_DESIGN_VERSION),
                            ("fnew_signal_version", FNEW_SIGNAL_VERSION),
                            ("sample_population", FNEW_DESIGN_VERSION),
                            ("timeframe", CANONICAL_TIMEFRAMES[canonical])):
        if spec.get(field, expected) != expected:
            raise ValueError(f"unsupported {field}")
    k = identity["risk_stop_atr_mult"]
    if "risk_stop_atr_mult" in spec and _number(spec["risk_stop_atr_mult"], "risk_stop_atr_mult") != k:
        raise ValueError("top-level risk_stop_atr_mult disagrees with Fnew identity")
    supplied = spec.get("params", spec.get("params_frozen", {}))
    params = _parameters(canonical, supplied, k)
    frozen = spec.get("params_frozen")
    if frozen is not None:
        if not isinstance(frozen, Mapping) or any(key not in params or value != params[key]
                                                for key, value in frozen.items()):
            raise ValueError("params and params_frozen disagree")
    expected_pairs = CANONICAL_UNIVERSES[canonical]
    pairs = {}
    for field in ("universe", "execution_universe", "research_universe", "data_universe"):
        pairs[field] = _pairs(spec.get(field, expected_pairs), field)
        if pairs[field] != expected_pairs:
            raise ValueError(f"{field} does not match the frozen Fnew asset universe")
    feeds = data_feed_contract(canonical)
    if spec.get("data_feeds", feeds) != feeds:
        raise ValueError("data_feeds does not match the market-separated Fnew contract")
    budget = {"target_exposure": 0.3, "max_positions": 2, "max_per_coin": 1,
              "sizing": "剩余预算 / 剩余槽位", "leverage": 100,
              "leverage_convention": "A：保证金不变，名义 ×100，逐根强平"}
    supplied_budget = spec.get("position_budget", {})
    if not isinstance(supplied_budget, Mapping):
        raise ValueError("position_budget must be a mapping")
    if set(supplied_budget) - set(budget):
        raise ValueError("unsupported Fnew position_budget field")
    if any(value != budget[key] for key, value in supplied_budget.items()):
        raise ValueError("Fnew execution requires 30% margin, two slots and 100x leverage")
    for field in ("target_exposure", "max_positions", "max_per_coin", "leverage"):
        _number(supplied_budget.get(field, budget[field]), field)
    risk = _number(spec.get("max_loss_per_trade_usdt", identity["max_loss_per_trade_usdt"]),
                   "max_loss_per_trade_usdt")
    fee = _number(spec.get("fee_rate_per_side", identity["fee_rate_per_side"]),
                  "fee_rate_per_side")
    if risk != identity["max_loss_per_trade_usdt"] or fee != identity["fee_rate_per_side"]:
        raise ValueError("Fnew identity does not match risk budget or fee convention")
    if spec.get("fee_convention", identity["fee_convention"]) != identity["fee_convention"]:
        raise ValueError("Fnew fee_convention mismatch")
    fee_model_version = identity.get("fee_model_version")
    if fee_model_version is None:
        if "fee_model_version" in spec:
            raise ValueError("Fnew Demo fee model requires a new Demo-marked strategy identity")
    elif spec.get("fee_model_version", fee_model_version) != fee_model_version:
        raise ValueError("Fnew identity does not match fee_model_version")
    exit_fee = None
    if fee_model_version == DEMO_FEE_MODEL_VERSION:
        exit_fee = _number(spec.get("exit_fee_rate_per_side", TAKER_FEE_PER_SIDE),
                           "exit_fee_rate_per_side")
        if exit_fee != TAKER_FEE_PER_SIDE:
            raise ValueError("Fnew Demo identity requires the verified taker exit fee")
    paired = canonical in PAIR_STRATEGIES
    legacy_formula = (
        spec.get("paired_notional_formula") == LEGACY_PAIRED_NOTIONAL_FORMULA
        if paired else spec.get("notional_formula") == LEGACY_NOTIONAL_FORMULA
    )
    sizing_mode = "entry_pair_atr" if paired else "entry_atr"
    scope = "atomic_pair_portfolio" if paired else "per_trade_leg"
    stop_policy = FNEW_PAIR_EXECUTION_POLICY if paired else FNEW_SINGLE_STOP_POLICY
    for field, expected in (("risk_sizing_mode", sizing_mode), ("risk_budget_scope", scope),
                            ("fnew_stop_policy", stop_policy)):
        if spec.get(field, expected) != expected:
            raise ValueError(f"unsupported {field}")
    if paired and spec.get("paired_execution_policy", FNEW_PAIR_EXECUTION_POLICY) != FNEW_PAIR_EXECUTION_POLICY:
        raise ValueError("unsupported paired_execution_policy")
    supplied_blockers = spec.get("execution_blockers", [PAIR_BLOCKER] if paired else [])
    if (not isinstance(supplied_blockers, (list, tuple))
            or any(not isinstance(reason, str) or not reason for reason in supplied_blockers)
            or len(set(supplied_blockers)) != len(supplied_blockers)):
        raise ValueError("execution_blockers must contain unique non-empty reason codes")
    blockers = list(supplied_blockers)
    enabled = spec.get("execution_enabled", not blockers)
    if not isinstance(enabled, bool):
        raise ValueError("execution_enabled must be a boolean")
    if enabled and blockers:
        raise ValueError("enabled Fnew execution cannot retain blockers")
    if not enabled and not blockers:
        raise ValueError("disabled Fnew execution requires a blocker")
    reason = blockers[0] if blockers else None
    if spec.get("blocked_execution_reason", reason) != reason:
        raise ValueError("blocked_execution_reason must be the first blocker")
    minimum = max(_integer(spec.get("min_warmup_bars", DEFAULT_WARMUP[canonical]),
                           "min_warmup_bars"), required_fnew_bars(canonical, params))
    factors = {"strategy": canonical, "risk_budget": identity["risk_budget"], "stop_width_k": k}
    if spec.get("fnew_factors", factors) != factors:
        raise ValueError("fnew_factors do not match account identity")
    capital = _number(spec.get("initial_capital", 10000.0), "initial_capital")
    economics = {
        "strategy_id": spec["strategy_id"], "canonical_strategy_id": canonical,
        "fnew_design_version": FNEW_DESIGN_VERSION, "fnew_signal_version": FNEW_SIGNAL_VERSION,
        "fnew_stop_policy": stop_policy, "fnew_factors": factors,
        "sample_population": FNEW_DESIGN_VERSION, "params": params,
        "timeframe": CANONICAL_TIMEFRAMES[canonical], "min_warmup_bars": minimum,
        **pairs, "data_feeds": feeds, "position_budget": budget,
        "initial_capital": capital, "fee_rate_per_side": fee,
        "fee_convention": identity["fee_convention"], "risk_sizing_mode": sizing_mode,
        "max_loss_per_trade_usdt": risk, "risk_budget_scope": scope,
        "execution_enabled": enabled, "execution_blockers": blockers,
        "blocked_execution_reason": reason,
        "signal_timing": "closed_native_bar_prior_baseline_no_future_rows",
        "fill_model": "next_closed_bar_open_proxy",
        "non_fill_risk": "unknown_limit_order_non_fill;next_open_proxy_assumes_fill",
        "data_alignment_policy": "exact_closed_timestamp_inner_join_no_forward_fill",
        "risk_limit_semantics": "planned_stop_and_fee_budget;close_trigger_next_open_gap_and_liquidation_risk",
        "archive_policy": "joint_confirmation_required;default_keep;insufficient_evidence_is_not_refutation",
    }
    if fee_model_version is not None:
        economics["fee_model_version"] = fee_model_version
        # Demo identities created before taker exits were added do not carry
        # this field. Keep it out of their legacy hash while retaining the
        # strict value check above when the field is present.
        if "exit_fee_rate_per_side" in spec or not legacy_formula:
            economics["exit_fee_rate_per_side"] = exit_fee
    if paired:
        economics.update(
            paired_execution_policy=FNEW_PAIR_EXECUTION_POLICY,
            paired_notional_formula=(
                LEGACY_PAIRED_NOTIONAL_FORMULA if legacy_formula else
                ("per_leg_notional=R/(k*entry_pair_atr_return+"
                 "2*(entry_fee_rate_per_side+exit_fee_rate_per_side))"
                 if fee_model_version == DEMO_FEE_MODEL_VERSION else
                 LEGACY_PAIRED_NOTIONAL_FORMULA)
            ),
            paired_stop_trigger="signed_sum_return_from_actual_entries<=-k*frozen_entry_pair_atr_return",
            paired_atr_method="synchronous_ohlc_envelope_true_range_actual_entry_v1",
            paired_liquidation_policy="conservative_peer_bar_extreme_after_leg_liquidation",
            pair_trade_sample_unit="one_atomic_group_not_two_independent_legs",
        )
    else:
        economics["notional_formula"] = (
            LEGACY_NOTIONAL_FORMULA if legacy_formula else
            ("R/(k*entry_atr/entry_price+entry_fee_rate_per_side+exit_fee_rate_per_side)"
             if fee_model_version == DEMO_FEE_MODEL_VERSION else
             LEGACY_NOTIONAL_FORMULA)
        )
    # Supplied executable convention fields may not silently override the
    # implementation, even if a caller strips the claimed hash.
    for field, expected in economics.items():
        if field not in {"params", "min_warmup_bars", "position_budget"} and field in spec and spec[field] != expected:
            raise ValueError(f"unsupported or inconsistent {field}")
    rule_hash = _digest(economics)
    if spec.get("rule_hash") not in (None, rule_hash):
        raise ValueError("Fnew rule hash mismatch requires a new strategy identity")
    if legacy_formula and spec.get("rule_hash") is not None:
        # Frozen pre-taker-exit definitions must remain byte-for-byte intact;
        # the checks above still validate every supplied executable field.
        return copy.deepcopy(dict(spec))
    result = {**copy.deepcopy(dict(spec)), **economics, "params_frozen": copy.deepcopy(params),
              "rule_hash": rule_hash, "arm_id": "Fnew_20261012", "variant_of": canonical,
              "forward_hypothesis": True, "not_profit_claim": True,
              "status": "forward_research_hypothesis", "archive_status": "keep",
              "research_timeframe": CANONICAL_TIMEFRAMES[canonical],
              "data_status": "paired_execution_pending" if blockers else "forward_data_required"}
    result["stop_spec"] = {
        "type": "atomic_pair_atr_close_confirmed" if paired else "atr_hard_stop_close_confirmed",
        "k": k, "atr_window": params["atr_window"], "budget_scope": scope,
        "notional": economics.get("paired_notional_formula", economics.get("notional_formula")),
        "fee_rate_per_side": fee,
        "execution_policy": stop_policy,
        "actual_loss_limit": False,
    }
    if exit_fee is not None:
        result["stop_spec"]["exit_fee_rate_per_side"] = exit_fee
    return result


def fnew_rule_hash(spec: Mapping[str, Any]) -> str:
    return normalize_fnew_spec(spec)["rule_hash"]


def _source_candidates(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    proposals = document.get("strategy_proposals_by_dsh", {})
    if not isinstance(proposals, Mapping) or not isinstance(proposals.get("candidates"), list):
        raise ValueError("research document must contain strategy_proposals_by_dsh.candidates")
    selected = {}
    for item in proposals["candidates"]:
        if not isinstance(item, Mapping):
            raise ValueError("each candidate must be a mapping")
        canonical = item.get("strategy_id")
        if canonical not in FNEW_CANONICAL_IDS:
            # In particular no old strategy is converted into a Fnew account.
            if isinstance(canonical, str) and re.match(r"s(?:32|33|34|35|36)_", canonical):
                raise ValueError("unsupported Fnew candidate identity")
            continue
        if canonical in selected:
            raise ValueError("duplicate Fnew candidate")
        selected[canonical] = copy.deepcopy(dict(item))
    if set(selected) != set(FNEW_CANONICAL_IDS):
        raise ValueError("Fnew population requires all five s32..s36 candidates")
    return [selected[canonical] for canonical in FNEW_CANONICAL_IDS]


def build_fnew_specs(document: Mapping[str, Any], *, paired_execution_enabled: bool = False) -> list[dict[str, Any]]:
    """Build 45 Demo-fee definitions; no storage, trading, or migration.

    The pair flag is for a caller that has verified the declared paired engine
    before the first account creation. Existing frozen definitions cannot be
    upgraded by this factory: their old hash must remain in account storage.
    """
    if not isinstance(document, Mapping):
        raise ValueError("research document must be a mapping")
    if not isinstance(paired_execution_enabled, bool):
        raise ValueError("paired_execution_enabled must be a boolean")
    results = []
    for candidate in _source_candidates(document):
        canonical = candidate["strategy_id"]
        supplied = candidate.get("params_frozen", {})
        if not isinstance(supplied, Mapping):
            raise ValueError("candidate params_frozen must be a mapping")
        fee_mode = "taker" if canonical == FNEW_CANONICAL_IDS[1] else "maker"
        paired = canonical in PAIR_STRATEGIES
        for risk_id, risk in RISK_BUDGETS:
            for k in STOP_WIDTHS:
                spec = copy.deepcopy(candidate)
                for field in ("rule_hash", "execution_hash", "state", "definition", "stop_spec"):
                    spec.pop(field, None)
                params = {**copy.deepcopy(dict(supplied)), "risk_stop_atr_mult": k}
                enabled = not paired or paired_execution_enabled
                spec.update(
                    strategy_id=f"{canonical}_Fnew_Demo_{risk_id}_k{k:.1f}_100x_{fee_mode}",
                    canonical_strategy_id=canonical, params=params, params_frozen=copy.deepcopy(params),
                    max_loss_per_trade_usdt=risk,
                    fee_rate_per_side=TAKER_FEE_PER_SIDE if fee_mode == "taker" else MAKER_FEE_PER_SIDE,
                    exit_fee_rate_per_side=TAKER_FEE_PER_SIDE,
                    fee_model_version=DEMO_FEE_MODEL_VERSION,
                    execution_enabled=enabled, execution_blockers=[] if enabled else [PAIR_BLOCKER],
                    blocked_execution_reason=None if enabled else PAIR_BLOCKER,
                    label=f"{candidate.get('name', canonical)} · {risk_id} · k{k:.1f} · 100× · {fee_mode}",
                    proposal_source="strategy_proposals_by_dsh.candidates",
                    source_min_warmup_bars=copy.deepcopy(candidate.get("min_warmup_bars", DEFAULT_WARMUP[canonical])),
                    implementation_parameter_defaults={key: copy.deepcopy(value)
                        for key, value in PARAMETER_DEFAULTS[canonical].items() if key not in supplied},
                )
                results.append(normalize_fnew_spec(spec))
    if len(results) != 45 or len({spec["strategy_id"] for spec in results}) != 45:
        raise ValueError("Fnew account population must contain 45 distinct definitions")
    return results


def load_fnew_specs(path: str | Path | None = None, *, paired_execution_enabled: bool = False) -> list[dict[str, Any]]:
    document = json.loads(Path(path or SPECS_PATH).read_text(encoding="utf-8"))
    return build_fnew_specs(document, paired_execution_enabled=paired_execution_enabled)
