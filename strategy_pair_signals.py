"""Causal pair hypotheses for the new, separately identified paper accounts.

These are research signals, not claims of hedge neutrality or profitability.
Both mechanisms enter opposite, equal-dollar legs.  A log-price OLS beta is
used to construct s32's statistical signal, never to silently change quantities.
"""

from __future__ import annotations

import copy
import itertools
import math
from collections.abc import Mapping, Sequence
from statistics import mean
from typing import Any

import strategy_signals as core


BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
TRX = "TRX/USDT:USDT"
PAIR_SIGNAL_VERSION = "fnew-pair-signals-eg-prior-window-v1"
PAIR_ATR_POLICY = "synchronous_ohlc_envelope_true_range_actual_entry_v1"
S32 = "s32_cointegration_spread_1h"
S36 = "s36_correlation_regime_1h"
DEFAULTS = {
    S32: {
        "adf_window": 500, "zscore_window": 500, "z_entry": 2.0, "z_exit": 0.5,
        "adf_pvalue_threshold": 0.05, "adf_maxlag": 5, "adf_autolag": "AIC",
        "adf_trend": "c", "spread_model": "log_price_ols_intercept",
        "pair_selection": "max_abs_z_cointegrated_then_lexicographic",
        "positive_beta_required": True, "atr_window": 14,
        "risk_stop_atr_mult": 1.5, "H": 72,
    },
    S36: {
        "rho_window": 168, "rho_compare_lag": 168, "rho_high_threshold": 0.7,
        "rho_drop": 0.15, "rho_exit": 0.05, "strength_window": 168,
        "rho_exit_mode": "rise_from_entry_rho", "return_measure": "simple_close_to_close",
        "atr_window": 14, "risk_stop_atr_mult": 1.5, "H": 72,
    },
}


class CointegrationDependencyUnavailable(RuntimeError):
    """An actual Engle--Granger implementation is required for s32."""


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return result


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    result = _number(value, name)
    if int(result) != result or result < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(result)


def _canonical(spec: Mapping[str, Any]) -> str:
    value = spec.get("canonical_strategy_id", spec.get("strategy_id"))
    if value not in DEFAULTS:
        raise ValueError("unsupported new pair strategy")
    return str(value)


def _params(spec: Mapping[str, Any], canonical: str) -> dict[str, Any]:
    supplied = spec.get("params", spec.get("params_frozen", {}))
    if not isinstance(supplied, Mapping):
        raise ValueError("pair params must be a mapping")
    params = {**DEFAULTS[canonical], **supplied}
    for key in ("atr_window", "H"):
        params[key] = _integer(params[key], key)
    params["risk_stop_atr_mult"] = _number(params["risk_stop_atr_mult"],
                                           "risk_stop_atr_mult", positive=True)
    if canonical == S32:
        for key in ("adf_window", "zscore_window"):
            params[key] = _integer(params[key], key, minimum=20)
        params["adf_maxlag"] = _integer(params["adf_maxlag"], "adf_maxlag", minimum=0)
        if params["adf_window"] != params["zscore_window"]:
            raise ValueError("s32 uses one frozen prior window for fitting and standardization")
        if params["adf_maxlag"] >= params["adf_window"] // 2 - 2:
            raise ValueError("adf_maxlag exceeds the regression sample")
        for key in ("z_entry", "z_exit", "adf_pvalue_threshold"):
            params[key] = _number(params[key], key, positive=True)
        if not 0 < params["adf_pvalue_threshold"] < 1 or params["z_exit"] >= params["z_entry"]:
            raise ValueError("invalid s32 p-value or z-score thresholds")
        fixed = {"adf_trend": "c", "adf_autolag": "AIC",
                 "spread_model": "log_price_ols_intercept",
                 "pair_selection": "max_abs_z_cointegrated_then_lexicographic",
                 "positive_beta_required": True}
    else:
        for key in ("rho_window", "rho_compare_lag", "strength_window"):
            params[key] = _integer(params[key], key, minimum=2)
        for key in ("rho_high_threshold", "rho_drop", "rho_exit"):
            params[key] = _number(params[key], key)
        if not -1 <= params["rho_high_threshold"] <= 1 or not 0 < params["rho_drop"] <= 2 \
                or not 0 < params["rho_exit"] <= 2:
            raise ValueError("invalid correlation thresholds")
        fixed = {"rho_exit_mode": "rise_from_entry_rho",
                 "return_measure": "simple_close_to_close"}
    if any(params[key] != value for key, value in fixed.items()):
        raise ValueError("unsupported pair mechanism policy")
    return params


def required_pair_bars(spec: Mapping[str, Any]) -> int:
    """The mathematical history minimum, separate from a declared larger floor."""
    canonical = _canonical(spec)
    params = _params(spec, canonical)
    minimum = params["atr_window"] + 1
    if canonical == S32:
        minimum = max(minimum, params["adf_window"] + 1)
    else:
        minimum = max(minimum, params["rho_window"] + params["rho_compare_lag"] + 1,
                      params["strength_window"] + 1)
    return minimum


def compute_pair_atr_return(inputs: Mapping[str, Any], legs: Sequence[str],
                            sides: Sequence[int], entry_prices: Mapping[str, Any],
                            window: int) -> float:
    """Mean TR of the bounded equal-dollar return, normalized at actual entry.

    For a long leg, its high contributes to the upper bound; for a short leg,
    its low does.  Sums of separate leg extrema are conservative envelopes:
    the extrema need not have occurred at the same instant.  This is a range
    proxy, not a measured intrabar pair price path.  ``window + 1`` synchronous
    closed rows are needed, with the first close supplying the previous mark.
    """
    window = _integer(window, "pair ATR window")
    if not isinstance(inputs, Mapping) or not isinstance(entry_prices, Mapping):
        raise ValueError("pair ATR input and entry prices must be mappings")
    if len(legs) != 2 or any(not isinstance(pair, str) for pair in legs) \
            or len(set(legs)) != 2 or len(sides) != 2 \
            or any(isinstance(side, bool) or side not in (-1, 1) for side in sides) \
            or sum(sides) != 0:
        raise ValueError("pair ATR requires two unique opposite legs")
    prices = {pair: _number(entry_prices.get(pair), "pair entry price", positive=True)
              for pair in legs}
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for pair in legs:
        value = inputs.get(pair)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) \
                or len(value) < window + 1:
            raise ValueError("pair ATR history is insufficient")
        rows[pair] = []
        previous_stamp = None
        for row in value[-window - 1:]:
            if not isinstance(row, Mapping):
                raise ValueError("pair ATR rows must be mappings")
            stamp = _integer(row.get("timestamp"), "pair ATR timestamp", minimum=0)
            if previous_stamp is not None and stamp != previous_stamp + core.TIMEFRAME_MS["1h"]:
                raise ValueError("pair ATR history must be hourly and contiguous")
            previous_stamp = stamp
            high = _number(row.get("high"), "pair ATR high", positive=True)
            low = _number(row.get("low"), "pair ATR low", positive=True)
            close = _number(row.get("close"), "pair ATR close", positive=True)
            if low > close or close > high:
                raise ValueError("invalid pair ATR bounds")
            rows[pair].append({"timestamp": stamp, "high": high, "low": low, "close": close})
    if [row["timestamp"] for row in rows[legs[0]]] != \
            [row["timestamp"] for row in rows[legs[1]]]:
        raise ValueError("pair ATR timestamps are not aligned")
    true_ranges = []
    for index in range(1, window + 1):
        upper = lower = prior_close = 0.0
        for pair, side in zip(legs, sides, strict=True):
            row, previous = rows[pair][index], rows[pair][index - 1]
            upper += side * row["high" if side > 0 else "low"] / prices[pair]
            lower += side * row["low" if side > 0 else "high"] / prices[pair]
            prior_close += side * previous["close"] / prices[pair]
        true_ranges.append(max(upper - lower, abs(upper - prior_close),
                               abs(lower - prior_close)))
    return _number(mean(true_ranges), "pair ATR return", positive=True)


def _engle_granger(y: list[float], x: list[float], params: Mapping[str, Any]
                   ) -> tuple[float, float, list[float]]:
    try:
        from statsmodels.tsa.stattools import coint
    except ImportError as exc:
        raise CointegrationDependencyUnavailable("statsmodels coint is required") from exc
    # coint uses residual ADF but the MacKinnon *cointegration* distribution,
    # which differs from the ordinary adfuller residual p-value distribution.
    statistic, pvalue, critical = coint(y, x, trend="c", maxlag=params["adf_maxlag"],
                                      autolag="AIC")
    return (_number(statistic, "Engle-Granger statistic"),
            _number(pvalue, "Engle-Granger p-value"),
            [_number(value, "Engle-Granger critical value") for value in critical])


def _fit_candidate(legs: tuple[str, str], histories: Mapping[str, list[dict[str, Any]]],
                   params: Mapping[str, Any]) -> dict[str, Any]:
    dependent, independent = legs
    window = params["adf_window"]
    train_y = histories[dependent][-window - 1:-1]
    train_x = histories[independent][-window - 1:-1]
    y = [math.log(row["close"]) for row in train_y]
    x = [math.log(row["close"]) for row in train_x]
    x_mean, y_mean = mean(x), mean(y)
    xx = sum((value - x_mean) ** 2 for value in x)
    if xx <= 1e-20:
        raise ValueError("cointegration_regressor_constant")
    beta = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True)) / xx
    if beta <= 0:
        raise ValueError("cointegration_beta_not_positive")
    intercept = y_mean - beta * x_mean
    residuals = [a - intercept - beta * b for a, b in zip(y, x, strict=True)]
    residual_mean = mean(residuals)
    residual_std = math.sqrt(sum((r - residual_mean) ** 2 for r in residuals) / (window - 1))
    if residual_std <= 1e-12:
        raise ValueError("cointegration_residual_variance_zero")
    statistic, pvalue, critical = _engle_granger(y, x, params)
    coefficients = {
        "dependent_pair": dependent, "independent_pair": independent,
        "intercept": intercept, "beta": beta,
        "residual_mean": residual_mean, "residual_std": residual_std,
        "training_start_ms": train_y[0]["timestamp"],
        "training_end_ms": train_y[-1]["timestamp"], "training_bars": window,
        "eg_statistic": statistic, "eg_pvalue": pvalue, "eg_critical_values": critical,
        "eg_trend": "c", "eg_maxlag": params["adf_maxlag"], "eg_autolag": "AIC",
        "test": "engle_granger_mackinnon_cointegration_distribution",
    }
    z = _frozen_z(coefficients, histories)
    return {"legs": list(legs), "zscore": z, "cointegrated": pvalue <= params["adf_pvalue_threshold"],
            "coefficients": coefficients}


def _frozen_z(coefficients: Mapping[str, Any], histories: Mapping[str, Any]) -> float:
    if not isinstance(coefficients, Mapping):
        raise ValueError("frozen pair coefficients must be a mapping")
    dependent, independent = coefficients.get("dependent_pair"), coefficients.get("independent_pair")
    if dependent == independent or dependent not in histories or independent not in histories:
        raise ValueError("frozen pair coefficients reference invalid legs")
    residual = (math.log(histories[dependent][-1]["close"])
                - _number(coefficients.get("intercept"), "frozen intercept")
                - _number(coefficients.get("beta"), "frozen beta", positive=True)
                * math.log(histories[independent][-1]["close"]))
    return ((residual - _number(coefficients.get("residual_mean"), "frozen residual mean"))
            / _number(coefficients.get("residual_std"), "frozen residual std", positive=True))


def _correlation(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("correlation history is insufficient")
    aa, bb = mean(a), mean(b)
    vx = sum((value - aa) ** 2 for value in a)
    vy = sum((value - bb) ** 2 for value in b)
    if vx <= 1e-24 or vy <= 1e-24:
        raise ValueError("correlation_return_variance_zero")
    return max(-1.0, min(1.0, sum((x - aa) * (y - bb) for x, y in zip(a, b, strict=True))
                                 / math.sqrt(vx * vy)))


def _returns(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    return [rows[index]["close"] / rows[index - 1]["close"] - 1
            for index in range(1, len(rows))]


def _frozen(position: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if key in position:
        return position[key]
    evidence = position.get("entry_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if key == "entry_pair_coefficients":
        return evidence.get(key, evidence.get("pair_coefficients", default))
    return evidence.get(key, default)


def _position_group(positions: Mapping[str, Any], execution: Sequence[str], timestamp: int,
                    k: float) -> tuple[list[str], list[int], dict[str, Any], str | None]:
    actual = {pair: pos for pair, pos in positions.items() if pos}
    if not actual:
        return [], [], {}, None
    first = next(iter(actual.values()))
    first = first if isinstance(first, Mapping) else {}
    legs = _frozen(first, "pair_legs", list(actual))
    legs = list(legs) if isinstance(legs, (list, tuple)) else list(actual)
    close_legs = sorted(set(actual) | {pair for pair in legs if isinstance(pair, str) and pair in execution})
    if len(legs) != 2 or any(not isinstance(pair, str) for pair in legs) \
            or len(set(legs)) != 2 or any(pair not in execution for pair in legs) \
            or set(actual) != set(legs):
        return close_legs, [], {}, "atomic_position_inconsistent"
    normalized = {}
    for pair in legs:
        pos, error = core._position(actual[pair], timestamp, bar_ms=core.TIMEFRAME_MS["1h"])
        if error or pos is None:
            return close_legs, [], {}, "atomic_position_inconsistent"
        if "direction" in pos and (isinstance(pos["direction"], bool)
                                   or pos["direction"] != pos["side"]):
            return close_legs, [], {}, "atomic_position_inconsistent"
        normalized[pair] = pos
    a, b = (normalized[pair] for pair in legs)
    sides = [a["side"], b["side"]]
    if sides[0] != -sides[1] or a["opened_candle_ms"] != b["opened_candle_ms"] \
            or not a.get("group_id") or a.get("group_id") != b.get("group_id"):
        return close_legs, sides, {}, "atomic_position_inconsistent"
    # The return proxy is valid only for the actual equal-dollar quantities.
    # Matching frozen labels cannot conceal a damaged or partially resized leg.
    try:
        notionals = []
        for pos in (a, b):
            notional = _number(_number(pos.get("quantity"), "pair quantity", positive=True)
                                * pos["entry_price"], "pair entry notional", positive=True)
            if "entry_notional" in pos and not math.isclose(
                    _number(pos["entry_notional"], "entry_notional", positive=True), notional,
                    rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError("stored pair entry notional disagrees with quantity")
            notionals.append(notional)
        if not math.isclose(*notionals, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("actual pair quantities are not equal dollar")
    except ValueError:
        return close_legs, sides, {}, "atomic_position_inconsistent"
    keys = ("pair_group", "pair_legs", "pair_sides", "pair_entry_prices", "entry_pair_atr_return",
            "entry_pair_coefficients", "pair_entry_rho")
    frozen = {key: copy.deepcopy(_frozen(a, key)) for key in keys}
    if not isinstance(frozen["pair_group"], str) or not frozen["pair_group"] \
            or any(_frozen(b, key) != value for key, value in frozen.items()) \
            or frozen["pair_legs"] != legs or frozen["pair_sides"] != sides:
        return close_legs, sides, frozen, "atomic_position_inconsistent"
    try:
        entries = frozen["pair_entry_prices"]
        if not isinstance(entries, Mapping) or set(entries) != set(legs):
            raise ValueError("missing frozen entries")
        for pair in legs:
            if _number(entries[pair], "pair frozen entry", positive=True) != normalized[pair]["entry_price"]:
                raise ValueError("frozen entry disagrees with position")
        frozen["entry_pair_atr_return"] = _number(frozen["entry_pair_atr_return"],
                                                  "frozen pair ATR", positive=True)
        for pos in (a, b):
            frozen_k = _frozen(pos, "risk_stop_atr_mult", k)
            if _number(frozen_k, "frozen pair k", positive=True) != k:
                raise ValueError("frozen pair k disagrees with rule")
    except ValueError:
        return close_legs, sides, frozen, "pair_frozen_risk_unavailable"
    frozen["held_bars"] = max(a["held_bars"], b["held_bars"])
    return legs, sides, frozen, None


def _apply(result: dict[str, Any], legs: Sequence[str], sides: Sequence[int | None],
           reason: str, evidence: Mapping[str, Any], *, atomic: bool = True) -> dict[str, Any]:
    for pair, side in zip(legs, sides, strict=True):
        if pair in result["signals"]:
            result["signals"][pair].update(target_side=side, reason=reason,
                                           evidence={**result["signals"][pair]["evidence"],
                                                     **copy.deepcopy(evidence)})
    result["group"] = ({"atomic": True, "legs": list(legs),
                        "pair_group": evidence.get("pair_group")}
                       if atomic and len(legs) == 2 else None)
    return result


def _entry_evidence(canonical: str, legs: Sequence[str], sides: Sequence[int],
                    histories: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    reference = {pair: histories[pair][-1]["close"] for pair in legs}
    inputs = {pair: [{key: row[key] for key in ("timestamp", "open", "high", "low", "close")}
                     for row in histories[pair][-params["atr_window"] - 1:]] for pair in legs}
    atr = compute_pair_atr_return(inputs, legs, sides, reference, params["atr_window"])
    return {
        "pair_group": canonical + ":" + "|".join(legs), "pair_legs": list(legs),
        "pair_sides": list(sides), "entry_pair_atr_return": atr,
        "pair_entry_reference_prices": reference, "pair_atr_inputs": inputs,
        "pair_atr_window": params["atr_window"], "pair_atr_policy": PAIR_ATR_POLICY,
        "risk_stop_atr_mult": params["risk_stop_atr_mult"],
        "equal_notional": True, "beta_hedged": False,
        "planned_risk_scope": "one_combined_pair_including_four_execution_fees",
        "trigger_reference": "closed_close", "fill_model": "next_closed_bar_open_proxy",
        "pair_signal_version": PAIR_SIGNAL_VERSION,
    }


def evaluate_pair_fnew(spec: Mapping[str, Any], histories: Mapping[str, Any],
                       positions: Mapping[str, Any] | None = None,
                       as_of_ms: Any = None) -> dict[str, Any]:
    """Return the common signals/group shape; never mutate a caller's state."""
    canonical = _canonical(spec)
    params = _params(spec, canonical)
    execution = list(spec.get("execution_universe", [BTC, ETH, TRX] if canonical == S32 else [BTC, ETH]))
    if len(set(execution)) != len(execution) or set(execution) != \
            ({BTC, ETH, TRX} if canonical == S32 else {BTC, ETH}):
        raise ValueError("pair strategy requires the registered execution universe")
    positions = positions or {}
    if not isinstance(positions, Mapping):
        raise ValueError("positions must be a mapping")
    observed = core._as_of_ms(as_of_ms)
    required_entry = max(required_pair_bars(spec), _integer(spec.get("min_warmup_bars", 1),
                                                           "min_warmup_bars"))
    active = {pair: pos for pair, pos in positions.items() if pos}
    # A frozen pair stop and frozen s32 z-score need current marks, not another
    # entry regression.  Unselected context must not obstruct a risk exit.
    relevant = execution
    required = required_entry
    if active:
        first = next(iter(active.values()))
        frozen_legs = _frozen(first, "pair_legs", []) if isinstance(first, Mapping) else []
        if not isinstance(frozen_legs, (list, tuple)) or any(not isinstance(pair, str) for pair in frozen_legs):
            frozen_legs = []
        relevant = [pair for pair in execution if pair in set(active) | set(frozen_legs)]
        if not relevant:
            raise ValueError("positions are outside the registered execution universe")
        required = 1
    clean: dict[str, list[dict[str, Any]]] = {}
    signals = {}
    for pair in execution:
        rows, error = core._history(histories.get(pair), observed, bar_ms=core.TIMEFRAME_MS["1h"])
        clean[pair] = rows
        ready = error is None and len(rows) >= required
        signals[pair] = core._signal(rows, required, "no_pair_trigger" if ready else "warmup_incomplete",
                                     ready=ready, error=error)
        if rows:
            signals[pair]["evidence"] = core._evidence(rows)
    ready = all(signals[pair]["warmup"]["ready"] for pair in relevant)
    reason = "ready" if ready else next(signals[pair]["reason"] for pair in relevant
                                        if not signals[pair]["warmup"]["ready"])
    result = {
        "strategy_id": spec["strategy_id"], "rule_hash": spec.get("rule_hash"),
        "ready": ready, "signals": signals, "group": None,
        "warmup": {"ready": ready, "valid_bars": min(len(clean[pair]) for pair in relevant),
                   "required_bars": required,
                   "entry_required_bars": required_entry,
                   "last_closed_at": min(clean[pair][-1]["timestamp"] for pair in relevant)
                   if all(clean[pair] for pair in relevant) else None, "reason": reason,
                   "by_pair": {pair: signal["warmup"] for pair, signal in signals.items()}},
    }
    if not ready:
        return result
    stamps = [[row["timestamp"] for row in clean[pair][-required:]] for pair in relevant]
    if any(value != stamps[0] for value in stamps[1:]):
        result["ready"] = False
        result["warmup"].update(ready=False, reason="pair_timestamps_not_aligned")
        for signal in signals.values():
            signal["reason"] = "pair_timestamps_not_aligned"
        return result
    timestamp = clean[relevant[0]][-1]["timestamp"]
    if observed is not None and observed - timestamp - core.TIMEFRAME_MS["1h"] > 90_000:
        result["ready"] = False
        result["warmup"].update(ready=False, reason="closed_candle_stale")
        for signal in signals.values():
            signal["reason"] = "closed_candle_stale"
        return result
    legs, sides, frozen, error = _position_group(positions, execution, timestamp,
                                                params["risk_stop_atr_mult"])
    if error:
        return _apply(result, legs, [0] * len(legs), error, frozen)
    if legs:
        pair_return = sum(side * (clean[pair][-1]["close"] / frozen["pair_entry_prices"][pair] - 1)
                          for pair, side in zip(legs, sides, strict=True))
        stop = -params["risk_stop_atr_mult"] * frozen["entry_pair_atr_return"]
        evidence = {**frozen, "pair_return": pair_return, "pair_stop_return": stop,
                    "risk_stop_atr_mult": params["risk_stop_atr_mult"],
                    "pair_atr_policy": PAIR_ATR_POLICY, "pair_signal_version": PAIR_SIGNAL_VERSION}
        if pair_return <= stop:
            return _apply(result, legs, [0, 0], "pair_stop_loss_close_confirmed", evidence)
        if frozen["held_bars"] >= params["H"]:
            return _apply(result, legs, [0, 0], "holding_period_elapsed", evidence)
        try:
            if canonical == S32:
                coefficients = frozen.get("entry_pair_coefficients")
                if not isinstance(coefficients, Mapping) or \
                        {coefficients.get("dependent_pair"), coefficients.get("independent_pair")} != set(legs):
                    raise ValueError("frozen coint legs disagree")
                z = _frozen_z(coefficients, clean)
                evidence.update(zscore=z, frozen_coefficients_used=True)
                if abs(z) <= params["z_exit"]:
                    return _apply(result, legs, [0, 0], "cointegration_spread_converged", evidence)
            else:
                entry_rho = _number(frozen.get("pair_entry_rho"), "frozen entry rho")
                if not -1 <= entry_rho <= 1:
                    raise ValueError("invalid frozen rho")
                if any(len(clean[pair]) < params["rho_window"] + 1 for pair in (BTC, ETH)):
                    return _apply(result, legs, [None, None], "pair_signal_history_unavailable", evidence)
                rho = _correlation(_returns(clean[BTC])[-params["rho_window"]:],
                                   _returns(clean[ETH])[-params["rho_window"]:])
                evidence.update(rho=rho, rho_rebound_from_entry=rho - entry_rho)
                if rho - entry_rho >= params["rho_exit"]:
                    return _apply(result, legs, [0, 0], "correlation_regime_rebounded", evidence)
        except (ValueError, TypeError, KeyError, IndexError):
            return _apply(result, legs, [0, 0], "pair_frozen_signal_unavailable", evidence)
        return _apply(result, legs, [None, None], "paired_position_held", evidence)
    if canonical == S32:
        candidates, rejected = [], []
        for candidate_legs in itertools.combinations(sorted(execution), 2):
            try:
                candidate = _fit_candidate(candidate_legs, clean, params)
                candidates.append(candidate)
            except CointegrationDependencyUnavailable:
                result["ready"] = False
                result["warmup"].update(ready=False, reason="cointegration_dependency_unavailable")
                for signal in signals.values():
                    signal["reason"] = "cointegration_dependency_unavailable"
                return result
            except (ValueError, TypeError, ArithmeticError) as exc:
                rejected.append({"legs": list(candidate_legs), "reason": str(exc)})
        eligible = [candidate for candidate in candidates if candidate["cointegrated"]
                    and abs(candidate["zscore"]) >= params["z_entry"]]
        common = {"pair_candidates": candidates, "pair_candidates_rejected": rejected,
                  "current_observation_excluded_from_training": True,
                  "multiple_pair_tests": len(list(itertools.combinations(execution, 2))),
                  "pair_signal_version": PAIR_SIGNAL_VERSION}
        for signal in signals.values():
            signal["evidence"].update(copy.deepcopy(common))
        if not eligible:
            for signal in signals.values():
                signal["reason"] = "no_cointegrated_spread_trigger"
            return result
        candidate = min(eligible, key=lambda item: (-abs(item["zscore"]), tuple(item["legs"])))
        legs = candidate["legs"]
        sides = [-1, 1] if candidate["zscore"] > 0 else [1, -1]
        try:
            evidence = _entry_evidence(canonical, legs, sides, clean, params)
        except (ValueError, TypeError, ArithmeticError):
            return _apply(result, legs, [None, None], "pair_entry_atr_unavailable", common)
        evidence.update(zscore=candidate["zscore"], pair_coefficients=candidate["coefficients"],
                        **common)
        return _apply(result, legs, sides, "cointegration_spread_reversion", evidence)
    window, lag = params["rho_window"], params["rho_compare_lag"]
    a, b = _returns(clean[BTC]), _returns(clean[ETH])
    try:
        current = _correlation(a[-window:], b[-window:])
        previous = _correlation(a[-window - lag:-lag], b[-window - lag:-lag])
    except ValueError as exc:
        return _apply(result, execution, [None, None], str(exc), {})
    strength = {pair: clean[pair][-1]["close"] / clean[pair][-params["strength_window"] - 1]["close"] - 1
                for pair in execution}
    common = {"rho": current, "prior_rho": previous, "rho_drop": previous - current,
              "rho_compare_lag": lag, "rho_high_threshold": params["rho_high_threshold"],
              "strength_returns": strength, "rho_window": window,
              "pair_signal_version": PAIR_SIGNAL_VERSION, "correlation_is_not_a_traded_asset": True}
    if previous < params["rho_high_threshold"] or previous - current < params["rho_drop"]:
        return _apply(result, execution, [None, None], "no_correlation_regime_trigger", common)
    if strength[BTC] == strength[ETH]:
        return _apply(result, execution, [None, None], "correlation_strength_tied", common)
    legs = [BTC, ETH]
    sides = [1, -1] if strength[BTC] > strength[ETH] else [-1, 1]
    try:
        evidence = _entry_evidence(canonical, legs, sides, clean, params)
    except (ValueError, TypeError, ArithmeticError):
        return _apply(result, legs, [None, None], "pair_entry_atr_unavailable", common)
    evidence.update(**common, pair_entry_rho=current)
    return _apply(result, legs, sides, "correlation_regime_divergence", evidence)
