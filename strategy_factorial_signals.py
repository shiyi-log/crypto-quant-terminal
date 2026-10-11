"""Immutable signal adapter for the ATR-risk factorial paper accounts.

Canonical mechanisms remain in ``strategy_signals``. This adapter owns the
new identities, the extra cross-asset ATR exits, and the execution dependency
contract; it never creates orders or fills.
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
from strategy_factorial import (
    ALLOWED_RESEARCH_PAIRS,
    BTC,
    DESIGN_VERSION,
    ETH,
    parse_factorial_identity,
)


FACTORIAL_SIGNAL_VERSION = "factorial-canonical-signals-frozen-atr-v1"
FACTORIAL_STOP_POLICY = "frozen_entry_atr_close_trigger_next_open_per_trade_leg_v1"
REQUIRED_BLOCKERS = {
    "s04_btc_eth_pairs": ("paired_atr_stop_policy_unimplemented",),
    "s19_multi_coin_breadth": ("paused_research_context_required",
                               "breadth_atr_stop_policy_unimplemented"),
    "s24_cross_sectional_lowvol": ("paused_research_context_required",
                                   "cross_sectional_portfolio_execution_unimplemented",
                                   "cross_sectional_atr_stop_policy_unimplemented"),
}
EXTERNAL_STOP_STRATEGIES = frozenset({"s10_lead_lag_btc_eth",
                                     "s30_dual_asset_confirmation"})


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _pairs(value: Any, name: str, *, restricted: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} must contain explicit exchange pairs")
    pairs = list(value)
    if any(not isinstance(pair, str) or not re.fullmatch(r"[A-Z0-9]+/USDT:USDT", pair)
           for pair in pairs) or len(set(pairs)) != len(pairs):
        raise ValueError(f"{name} contains invalid or duplicate pairs")
    if restricted and any(pair not in ALLOWED_RESEARCH_PAIRS for pair in pairs):
        raise ValueError(f"{name} contains a paused research asset")
    return pairs


def _inner_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Map adapter parameters to exactly the supported canonical contract."""
    inner = dict(spec)
    canonical = spec["canonical_strategy_id"]
    inner["strategy_id"] = canonical
    inner.pop("rule_hash", None)
    params = dict(spec["params"])
    if canonical not in core.ATR_HARD_STOP_STRATEGIES:
        params.pop("risk_stop_atr_mult", None)
        if "atr_window" not in core.LEGACY_DEFAULTS[canonical][0]:
            params.pop("atr_window", None)
    if canonical == "s08_wick_rejection":
        params["stop_atr_mult"] = spec["params"]["risk_stop_atr_mult"]
    inner["params"] = params
    inner.pop("params_frozen", None)
    if canonical == "s10_lead_lag_btc_eth":
        # The public evaluator builds the source history using its configured
        # pairs. BTC is removed from the adapter's outward order legs below.
        inner["execution_universe"] = [BTC, ETH]
    return inner


def normalize_factorial_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Bind all factor semantics and verify any claimed immutable rule hash."""
    if not isinstance(spec, Mapping):
        raise ValueError("factorial spec must be a mapping")
    identity = parse_factorial_identity(spec.get("strategy_id"))
    if identity is None:
        raise ValueError("unsupported factorial strategy identity")
    canonical = identity["canonical_strategy_id"]
    if canonical not in core.LEGACY_DEFAULTS:
        raise ValueError("unsupported factorial canonical strategy")
    if spec.get("canonical_strategy_id") != canonical:
        raise ValueError("factor identity does not match canonical_strategy_id")
    if spec.get("factorial_design_version") != DESIGN_VERSION:
        raise ValueError("unsupported factorial_design_version")
    if spec.get("risk_sizing_mode") != "entry_atr":
        raise ValueError("factorial accounts require entry_atr sizing")
    risk = _positive(spec.get("max_loss_per_trade_usdt"), "max_loss_per_trade_usdt")
    if risk != identity["max_loss_per_trade_usdt"]:
        raise ValueError("factor identity does not match risk budget")
    params = spec.get("params", spec.get("params_frozen"))
    if not isinstance(params, Mapping):
        raise ValueError("factorial params must be a mapping")
    params = dict(params)
    k = _positive(params.get("risk_stop_atr_mult"), "risk_stop_atr_mult")
    if k != identity["risk_stop_atr_mult"]:
        raise ValueError("factor identity does not match stop width")
    window_value = _positive(params.get("atr_window"), "atr_window")
    if window_value != int(window_value):
        raise ValueError("atr_window must be a positive integer")
    window = int(window_value)
    params.update(atr_window=window, risk_stop_atr_mult=k)
    if canonical == "s08_wick_rejection":
        if _positive(params.get("stop_atr_mult", k), "stop_atr_mult") != k:
            raise ValueError("s08 stop_atr_mult must match factor stop width")
        params["stop_atr_mult"] = k
    frozen = spec.get("params_frozen")
    if frozen is not None:
        if not isinstance(frozen, Mapping):
            raise ValueError("params_frozen must be a mapping")
        # A normalized params mapping may include defaults absent from the
        # source snapshot, but two supplied values may never disagree.
        for key, value in frozen.items():
            if key not in params or (_digest(value) != _digest(params[key])
                                     and value != params[key]):
                raise ValueError("params and params_frozen disagree")
    execution = _pairs(spec.get("execution_universe"), "execution_universe")
    research = _pairs(spec.get("research_universe", execution), "research_universe",
                      restricted=canonical not in REQUIRED_BLOCKERS)
    data = _pairs(spec.get("data_universe", execution), "data_universe")
    universe = _pairs(spec.get("universe", execution), "universe")
    if canonical == "s10_lead_lag_btc_eth" and (
            execution != [ETH] or research != [BTC, ETH] or data != [BTC, ETH]):
        raise ValueError("s10 requires BTC source context and only ETH execution")
    if canonical in {"s04_btc_eth_pairs", "s30_dual_asset_confirmation"} and (
            execution != [BTC, ETH] or research != [BTC, ETH] or data != [BTC, ETH]):
        raise ValueError("dual-asset strategy requires BTC/ETH legs and context")
    if any(pair not in data for pair in execution):
        raise ValueError("execution pairs must have a data dependency")
    supplied_blockers = spec.get("execution_blockers", [])
    if not isinstance(supplied_blockers, (list, tuple)) or any(
            not isinstance(reason, str) or not reason for reason in supplied_blockers):
        raise ValueError("execution_blockers must contain non-empty reason codes")
    blockers = list(dict.fromkeys((*REQUIRED_BLOCKERS.get(canonical, ()),
                                   *supplied_blockers)))
    enabled = spec.get("execution_enabled", not blockers)
    if not isinstance(enabled, bool):
        raise ValueError("execution_enabled must be a boolean")
    if not enabled and not blockers:
        blockers = ["execution_disabled_by_definition"]
    enabled = enabled and not blockers
    reason = blockers[0] if blockers else None
    if spec.get("blocked_execution_reason") is not None and \
            spec["blocked_execution_reason"] not in blockers:
        raise ValueError("blocked_execution_reason must identify an execution blocker")
    factors = {"strategy": canonical, "risk_budget": identity["risk_budget"],
               "stop_width_k": k}
    if "factorial_factors" in spec and spec["factorial_factors"] != factors:
        raise ValueError("factorial_factors do not match account identity")
    for name, value in (("factorial_signal_version", FACTORIAL_SIGNAL_VERSION),
                        ("factorial_stop_policy", FACTORIAL_STOP_POLICY),
                        ("risk_budget_scope", "per_trade_leg")):
        if name in spec and spec[name] != value:
            raise ValueError(f"unsupported {name}")
    minimum_value = _positive(spec.get("min_warmup_bars", 1), "min_warmup_bars")
    if minimum_value != int(minimum_value):
        raise ValueError("min_warmup_bars must be a positive integer")
    prepared = {**spec, "canonical_strategy_id": canonical, "params": params,
                "min_warmup_bars": max(int(minimum_value), window + 2)}
    inner = core.normalize_spec(_inner_spec(prepared))
    if spec.get("signal_rule_hash") not in (None, inner["rule_hash"]):
        raise ValueError("canonical signal rule hash mismatch")
    normalized = {**inner, "strategy_id": spec["strategy_id"],
                  "canonical_strategy_id": canonical,
                  "params": {**inner["params"], "atr_window": window,
                             "risk_stop_atr_mult": k},
                  "signal_rule_hash": inner["rule_hash"],
                  "execution_universe": execution, "research_universe": research,
                  "data_universe": data, "universe": universe,
                  "factorial_design_version": DESIGN_VERSION,
                  "factorial_signal_version": FACTORIAL_SIGNAL_VERSION,
                  "factorial_stop_policy": FACTORIAL_STOP_POLICY,
                  "factorial_factors": factors, "risk_sizing_mode": "entry_atr",
                  "max_loss_per_trade_usdt": risk, "risk_budget_scope": "per_trade_leg",
                  "execution_enabled": enabled, "execution_blockers": blockers,
                  "blocked_execution_reason": reason}
    normalized.pop("rule_hash")
    rule_hash = _digest(normalized)
    if spec.get("rule_hash") not in (None, rule_hash):
        raise ValueError("factorial rule hash mismatch requires a new strategy identity")
    normalized["rule_hash"] = rule_hash
    return normalized


def factorial_rule_hash(spec: Mapping[str, Any]) -> str:
    return normalize_factorial_spec(spec)["rule_hash"]


def _blocked_result(normalized: Mapping[str, Any]) -> dict[str, Any]:
    required = normalized["min_warmup_bars"]
    reason = normalized["blocked_execution_reason"]
    by_pair = {pair: {"valid_bars": 0, "required_bars": required, "ready": False}
               for pair in normalized["execution_universe"]}
    return {"strategy_id": normalized["strategy_id"], "rule_hash": normalized["rule_hash"],
            "ready": False, "execution_enabled": False,
            "execution_blockers": list(normalized["execution_blockers"]),
            "warmup": {"ready": False, "valid_bars": 0, "required_bars": required,
                       "last_closed_at": None, "reason": reason, "by_pair": by_pair},
            "signals": {pair: {"target_side": None, "reason": reason,
                               "evidence": {"execution_blockers": list(
                                   normalized["execution_blockers"])},
                               "candle_utc": None, "size_multiplier": 1.0,
                               "warmup": copy.deepcopy(warmup)}
                        for pair, warmup in by_pair.items()},
            "group": None}


def _frozen_atr(position: Mapping[str, Any]) -> float | None:
    value = position.get("entry_atr")
    if value is None:
        evidence = position.get("entry_evidence") or {}
        value = evidence.get("atr") if isinstance(evidence, Mapping) else None
    try:
        return _positive(value, "entry_atr")
    except ValueError:
        return None


def evaluate_factorial(spec: Mapping[str, Any], candles_by_pair: Mapping[str, Any],
                       positions: Mapping[str, Any] | None = None,
                       as_of_utc: Any = None) -> dict[str, Any]:
    """Evaluate canonical rules, exposing only this account's real trade legs."""
    normalized = normalize_factorial_spec(spec)
    if not normalized["execution_enabled"]:
        return _blocked_result(normalized)
    positions = positions or {}
    inner = _inner_spec(normalized)
    # A BTC context series in s10 cannot acquire a synthetic paper position.
    actual_positions = {pair: positions.get(pair)
                        for pair in normalized["execution_universe"]}
    result = core.evaluate(inner, candles_by_pair, actual_positions, as_of_utc)
    result.update(strategy_id=normalized["strategy_id"], rule_hash=normalized["rule_hash"],
                  canonical_strategy_id=normalized["canonical_strategy_id"],
                  execution_enabled=True, execution_blockers=[])
    if normalized["canonical_strategy_id"] == "s10_lead_lag_btc_eth":
        result["signals"].pop(BTC)
        btc_warmup = result["warmup"]["by_pair"].pop(BTC)
        result["warmup"]["context_by_pair"] = {BTC: btc_warmup}
    if not result["ready"]:
        return result
    window = normalized["params"]["atr_window"]
    k = normalized["params"]["risk_stop_atr_mult"]
    external = normalized["canonical_strategy_id"] in EXTERNAL_STOP_STRATEGIES
    for pair, signal in result["signals"].items():
        rows, error = core._history(candles_by_pair.get(pair), core._as_of_ms(as_of_utc),
                                    bar_ms=core.TIMEFRAME_MS[normalized["timeframe"]])
        if error or not rows:
            signal.update(target_side=None, reason=error or "risk_atr_history_not_ready")
            continue
        evidence = signal["evidence"]
        evidence.update(factorial_signal_version=FACTORIAL_SIGNAL_VERSION,
                        factorial_stop_policy=FACTORIAL_STOP_POLICY,
                        risk_stop_atr_mult=k, atr_window=window,
                        risk_budget_scope="per_trade_leg")
        position, position_error = core._position(
            actual_positions.get(pair), rows[-1]["timestamp"],
            bar_ms=core.TIMEFRAME_MS[normalized["timeframe"]])
        if position_error:
            signal.update(target_side=None, reason=position_error)
            continue
        if position and external:
            frozen = _frozen_atr(position)
            if frozen is None:
                signal.update(target_side=None, reason="frozen_entry_atr_unavailable")
                continue
            side = position["side"]
            stop = (Decimal(str(position["entry_price"]))
                    - side * Decimal(str(k)) * Decimal(str(frozen)))
            evidence.update(entry_atr=frozen, stop_level=float(stop))
            if (Decimal(str(rows[-1]["close"])) - stop) * side <= 0:
                signal.update(target_side=0, reason="stop_loss_close_confirmed")
                continue
        target = signal["target_side"]
        held_side = position.get("side", position.get("direction")) if position else None
        held_side = {"long": 1, "short": -1}.get(held_side, held_side)
        new_entry = target in (-1, 1) and (not position or target != held_side)
        if external and new_entry:
            evidence["atr"] = core._atr(rows, window) if len(rows) >= window + 2 else None
        if new_entry:
            try:
                atr = _positive(evidence.get("atr"), "entry_atr")
            except ValueError:
                signal.update(target_side=None, reason="risk_atr_unavailable")
            else:
                evidence["atr"] = atr
                evidence["entry_atr"] = atr
                evidence["stop_distance_pct_at_signal"] = k * atr / rows[-1]["close"]
    return result
