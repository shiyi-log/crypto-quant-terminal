"""Causal signals for the separately versioned Fnew paper hypotheses.

Only completed, aligned candles initialize indicators.  This module emits
instructions for the latest close; it does not replay trades or create fills.
The basis rule trades a perpetual outright and therefore retains spot-price
directional exposure: its P&L is not the return of a hedged basis portfolio.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from statistics import mean, stdev
from typing import Any


TIMEFRAME_MS = {"15m": 900_000, "1h": 3_600_000}
DAY_MS = 86_400_000
SINGLE_STRATEGIES = frozenset({"s33_basis_momentum_1h",
                               "s34_vwap_anchor_reversion_15m",
                               "s35_cascade_continuation_15m"})


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("nonfinite or nonpositive number")
    return number


def _integer(value: Any) -> int:
    number = _number(value)
    if number != int(number):
        raise ValueError("fractional integer")
    return int(number)


def _as_of_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("as_of_utc requires a timezone")
        return int(value.timestamp() * 1000)
    if isinstance(value, str) and not value.isdigit():
        return _as_of_ms(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return _integer(value)


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict") and hasattr(value, "index"):
        rows = value.to_dict("records")
        for row, index in zip(rows, value.index, strict=True):
            if "timestamp" not in row:
                row["timestamp"] = int(index.timestamp() * 1000)
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [dict(row) if isinstance(row, Mapping) else {} for row in value]
    raise ValueError("invalid candle container")


def _history(value: Any, as_of_ms: int | None, *, bar_ms: int, validate_quote: bool = True
             ) -> tuple[list[dict[str, Any]], str | None]:
    """Validate OHLCV and return only its final contiguous closed suffix.

    The quote-volume input survives sanitization for the VWAP anchor. A missing
    field is distinct from a supplied invalid field; only the former permits
    the hash-bound HLC3/base-volume fallback.
    """
    try:
        raw_rows = _records(value)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return [], "invalid_candle_container"
    prepared = []
    for raw in raw_rows:
        extras = raw.get("extras", {})
        extras = extras if isinstance(extras, Mapping) else {}
        if extras.get("closed", raw.get("closed")) is not True:
            continue
        try:
            timestamp = _integer(raw.get("timestamp"))
            if timestamp < 0 or timestamp % bar_ms:
                raise ValueError("unaligned candle")
        except (ValueError, TypeError, OverflowError):
            return [], "invalid_candle_timestamp"
        if as_of_ms is not None and timestamp + bar_ms > as_of_ms:
            continue
        row = {"timestamp": timestamp, "extras": {"closed": True}}
        try:
            for field in ("open", "high", "low", "close"):
                row[field] = _number(raw.get(field), positive=True)
            row["volume"] = _number(raw.get("volume"))
            if row["volume"] < 0 or row["high"] < max(
                    row["open"], row["close"], row["low"]) or row["low"] > min(
                    row["open"], row["close"], row["high"]):
                raise ValueError("invalid OHLCV")
        except (ValueError, TypeError, OverflowError):
            return [], "invalid_closed_candle_ohlcv"
        quote = extras.get("quote_volume", raw.get("quote_volume"))
        if validate_quote and quote is not None:
            try:
                quote = _number(quote)
                if quote < 0 or (row["volume"] == 0 and quote != 0) or (
                        row["volume"] > 0 and quote == 0):
                    raise ValueError("invalid quote volume")
                if row["volume"] > 0:
                    average_price = quote / row["volume"]
                    tolerance = max(1e-8, row["high"] * 1e-6)
                    if not math.isfinite(average_price) or not (
                            row["low"] - tolerance <= average_price <= row["high"] + tolerance):
                        raise ValueError("quote/base price lies outside OHLC bounds")
            except (ValueError, TypeError, OverflowError):
                return [], "invalid_quote_volume"
            row["extras"]["quote_volume"] = quote
        prepared.append(row)
    prepared.sort(key=lambda row: row["timestamp"])
    if len({row["timestamp"] for row in prepared}) != len(prepared):
        return [], "duplicate_closed_candle_timestamp"
    start = len(prepared) - 1
    while start > 0 and prepared[start]["timestamp"] - prepared[start - 1]["timestamp"] == bar_ms:
        start -= 1
    return prepared[max(start, 0):], None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _atr(rows: list[dict[str, Any]], window: int) -> float:
    """Mean true range of the prior window, excluding the decision candle."""
    return mean(max(rows[index]["high"], rows[index - 1]["close"])
                - min(rows[index]["low"], rows[index - 1]["close"])
                for index in range(len(rows) - window - 1, len(rows) - 1))


def _position(value: Any, timestamp: int, bar_ms: int
              ) -> tuple[dict[str, Any] | None, str | None]:
    if value is None or value == {}:
        return None, None
    if not isinstance(value, Mapping):
        return None, "invalid_position"
    try:
        side = value.get("side", value.get("direction"))
        side = {"long": 1, "short": -1}.get(side, side)
        side = _integer(side)
        entry = _number(value.get("entry_price"), positive=True)
        opened = _integer(value.get("opened_candle_ms"))
        if side not in (-1, 1) or opened < 0 or opened > timestamp or opened % bar_ms:
            raise ValueError("invalid position")
        return {**value, "side": side, "entry_price": entry,
                "opened_candle_ms": opened,
                "held_bars": (timestamp - opened) // bar_ms + 1}, None
    except (ValueError, TypeError, OverflowError):
        return None, "invalid_position"


def _stop(position: Mapping[str, Any], close: float, params: Mapping[str, Any],
          evidence: dict[str, Any]) -> str | None:
    value = position.get("entry_atr")
    if value is None:
        entry_evidence = position.get("entry_evidence") or {}
        value = entry_evidence.get("atr") if isinstance(entry_evidence, Mapping) else None
    try:
        frozen_atr = _number(value, positive=True)
    except (ValueError, TypeError, OverflowError):
        return "frozen_entry_atr_unavailable"
    stop = Decimal(str(position["entry_price"])) - Decimal(str(position["side"])) \
        * Decimal(str(params["risk_stop_atr_mult"])) * Decimal(str(frozen_atr))
    evidence.update(entry_atr=frozen_atr, stop_level=float(stop),
                    risk_stop_atr_mult=params["risk_stop_atr_mult"])
    if (Decimal(str(close)) - stop) * Decimal(str(position["side"])) <= 0:
        return "stop_loss_close_confirmed"
    return None


def _risk_exit(canonical: str, rows: list[dict[str, Any]], position: Mapping[str, Any],
               params: Mapping[str, Any], evidence: dict[str, Any]
               ) -> tuple[int | None, str | None]:
    """Fresh current marks and frozen risk suffice for an existing position."""
    stop_reason = _stop(position, rows[-1]["close"], params, evidence)
    if stop_reason == "stop_loss_close_confirmed":
        return 0, stop_reason
    timestamp = rows[-1]["timestamp"]
    if canonical == "s34_vwap_anchor_reversion_15m":
        day = timestamp // DAY_MS * DAY_MS
        entry_evidence = position.get("entry_evidence") or {}
        entry_day = entry_evidence.get("vwap_day_ms") if isinstance(entry_evidence, Mapping) else None
        if (entry_day is not None and entry_day != day) or (
                params["force_flat_at_day_end"] and timestamp + TIMEFRAME_MS["15m"] >= day + DAY_MS):
            return 0, "vwap_anchor_session_elapsed"
    if position["held_bars"] >= params["H"]:
        return 0, "holding_period_elapsed"
    if stop_reason:
        return None, stop_reason
    return None, None


def _basis(rows: list[dict[str, Any]], spot: list[dict[str, Any]],
           position: Mapping[str, Any] | None, params: Mapping[str, Any],
           evidence: dict[str, Any]) -> tuple[int | None, str]:
    window, lag = params["sigma_window"], params["basis_delta_lag"]
    needed = window + lag + 1
    futures_tail, spot_tail = rows[-needed:], spot[-needed:]
    if len(spot_tail) != needed or [row["timestamp"] for row in futures_tail] != [
            row["timestamp"] for row in spot_tail]:
        return None, "basis_timestamps_not_aligned"
    bases = [perp["close"] / cash["close"] - 1
             for perp, cash in zip(futures_tail, spot_tail, strict=True)]
    deltas = [bases[index] - bases[index - lag] for index in range(lag, len(bases))]
    sigma = stdev(deltas[:-1])
    delta = deltas[-1]
    evidence.update(basis=bases[-1], basis_previous=bases[-1 - lag], basis_delta=delta,
                    basis_sigma=sigma, sigma_window=window, basis_delta_lag=lag,
                    basis_sigma_policy="sample_stdev_prior_deltas_excludes_current_v1",
                    spot_history_fingerprint=_digest(spot_tail),
                    spot_close=spot_tail[-1]["close"],
                    spot_candle_ms=spot_tail[-1]["timestamp"],
                    pnl_exposure="unhedged_perpetual;not_pure_basis_return")
    if position:
        if position["held_bars"] >= params["H"]:
            return 0, "holding_period_elapsed"
        if delta * position["side"] >= -params["exit_sigma"] * sigma:
            return 0, "basis_delta_reverted"
        return None, "position_held"
    if sigma <= 0:
        return None, "basis_delta_sigma_zero"
    side = -1 if delta >= params["k_sigma"] * sigma else \
        1 if delta <= -params["k_sigma"] * sigma else None
    return side, "basis_delta_reversion" if side else "no_basis_delta_extreme"


def _vwap(rows: list[dict[str, Any]], position: Mapping[str, Any] | None,
          params: Mapping[str, Any], evidence: dict[str, Any]) -> tuple[int | None, str]:
    timestamp, close = rows[-1]["timestamp"], rows[-1]["close"]
    day = timestamp // DAY_MS * DAY_MS
    day_rows = [row for row in rows if row["timestamp"] >= day]
    if not day_rows or day_rows[0]["timestamp"] != day:
        return None, "vwap_session_start_missing"
    total_volume = sum(row["volume"] for row in day_rows)
    if total_volume <= 0:
        return None, "vwap_session_volume_zero"
    quote_rows = sum("quote_volume" in row["extras"] for row in day_rows)
    total_quote = sum(row["extras"].get("quote_volume", (
        row["high"] + row["low"] + row["close"]) / 3 * row["volume"]) for row in day_rows)
    vwap = total_quote / total_volume
    deviation = close - vwap
    atr = evidence["atr"]
    if not math.isfinite(vwap) or vwap <= 0:
        return None, "vwap_numeric_unavailable"
    evidence.update(vwap=vwap, vwap_day_ms=day, vwap_base_volume=total_volume,
                    vwap_quote_volume=total_quote, vwap_bars=len(day_rows),
                    vwap_source="quote_volume" if quote_rows == len(day_rows) else
                    "hlc3_volume_proxy" if quote_rows == 0 else "mixed_quote_hlc3_volume_proxy",
                    vwap_quote_rows=quote_rows, vwap_proxy_rows=len(day_rows) - quote_rows,
                    vwap_policy=params["vwap_price"], deviation_atr=deviation / atr if atr else None,
                    liquidation_data="not_used")
    end_of_day = timestamp + TIMEFRAME_MS["15m"] >= day + DAY_MS
    if position:
        entry_evidence = position.get("entry_evidence") or {}
        entry_day = entry_evidence.get("vwap_day_ms") if isinstance(entry_evidence, Mapping) else None
        if entry_day is None:
            return None, "frozen_vwap_anchor_day_unavailable"
        if entry_day != day or params["force_flat_at_day_end"] and end_of_day:
            return 0, "vwap_anchor_session_elapsed"
        if position["held_bars"] >= params["H"]:
            return 0, "holding_period_elapsed"
        # Crossing the anchor also completes the reversion, even if a close
        # jumps over the narrow symmetric band.
        if deviation * position["side"] >= -params["exit_frac"] * atr:
            return 0, "vwap_anchor_reached"
        return None, "position_held"
    if params["force_flat_at_day_end"] and end_of_day:
        return None, "vwap_session_end_no_entry"
    side = -1 if deviation >= params["k_atr"] * atr else \
        1 if deviation <= -params["k_atr"] * atr else None
    return side, "vwap_anchor_reversion" if side else "no_vwap_deviation"


def _cascade(rows: list[dict[str, Any]], position: Mapping[str, Any] | None,
             params: Mapping[str, Any], evidence: dict[str, Any]) -> tuple[int | None, str]:
    run = rows[-params["run_bars"]:]
    changes = [row["close"] - row["open"] for row in run]
    side = 1 if all(change > 0 for change in changes) else \
        -1 if all(change < 0 for change in changes) else None
    origin = rows[-params["run_bars"] - 1]["close"]
    move = rows[-1]["close"] - origin
    evidence.update(cascade_origin=origin, cascade_move_price=move,
                    cascade_move_atr=move / evidence["atr"] if evidence["atr"] else None,
                    cascade_run_bars=params["run_bars"],
                    cascade_direction=side, cascade_proxy=params["cascade_proxy"],
                    liquidation_data="ohlcv_proxy;no_observed_liquidation_feed",
                    comparison_to_s21="different_run_and_atr_threshold;no_wick_exhaustion_filter")
    if position:
        return (0, "holding_period_elapsed") if position["held_bars"] >= params["H"] else \
            (None, "position_held")
    if side and move * side >= params["cum_move_atr"] * evidence["atr"]:
        return side, "cascade_continuation_proxy"
    return None, "no_cascade_continuation"


def _result(spec: Mapping[str, Any], histories: Mapping[str, list[dict[str, Any]]],
            errors: Mapping[str, str | None]) -> dict[str, Any]:
    required = spec["min_warmup_bars"]
    signals = {}
    for pair in spec["execution_universe"]:
        rows = histories.get(pair, [])
        ready = not errors.get(pair) and len(rows) >= required
        signals[pair] = {"target_side": None, "reason": errors.get(pair) or (
            "no_signal" if ready else "warmup_incomplete"), "evidence": {},
            "candle_utc": rows[-1]["timestamp"] if rows else None, "size_multiplier": 1.0,
            "warmup": {"ready": ready, "valid_bars": len(rows), "required_bars": required}}
    ready = bool(signals) and all(signal["warmup"]["ready"] for signal in signals.values())
    latest = [signal["candle_utc"] for signal in signals.values()]
    reason = "ready" if ready else next((signal["reason"] for signal in signals.values()
                                         if not signal["warmup"]["ready"]), "no_execution_pairs")
    return {"strategy_id": spec["strategy_id"], "canonical_strategy_id": spec["canonical_strategy_id"],
            "rule_hash": spec["rule_hash"], "ready": ready,
            "execution_enabled": spec["execution_enabled"],
            "execution_blockers": list(spec["execution_blockers"]),
            "warmup": {"ready": ready,
                       "valid_bars": min((len(histories.get(pair, [])) for pair in signals), default=0),
                       "required_bars": required,
                       "last_closed_at": min(latest) if latest and all(
                           value is not None for value in latest) else None,
                       "reason": reason, "by_pair": {pair: signal["warmup"]
                                                       for pair, signal in signals.items()}},
            "signals": signals, "group": None}


def _unready(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["ready"] = False
    result["warmup"].update(ready=False, reason=reason)
    for signal in result["signals"].values():
        signal["target_side"] = None
        if signal["warmup"]["ready"]:
            signal["reason"] = reason
        signal["warmup"]["ready"] = False
    return result


def _evidence(spec: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    params = spec["params"]
    return {"history_start_ms": rows[0]["timestamp"],
            "history_end_ms": rows[-1]["timestamp"], "history_bars": len(rows),
            "data_fingerprint": _digest(rows),
            "fnew_signal_version": spec["fnew_signal_version"],
            "fnew_stop_policy": spec["fnew_stop_policy"],
            "trigger_reference": "closed_close", "fill_model": "next_closed_bar_open_proxy",
            "atr": _atr(rows, params["atr_window"])
            if len(rows) >= params["atr_window"] + 2 else None,
            "atr_policy": "prior_mean_true_range_excludes_decision_candle_v1",
            "risk_stop_atr_mult": params["risk_stop_atr_mult"]}


def evaluate_fnew(spec: Mapping[str, Any], candles_by_pair: Mapping[str, Any],
                  positions: Mapping[str, Any] | None = None,
                  as_of_utc: Any = None) -> dict[str, Any]:
    """Evaluate one immutable Fnew account, never changing source data.

    Futures use the ordinary pair mapping. For s33 an additional ``spot``
    mapping is required, keyed by the actual spot pairs in ``data_feeds``.
    A stale, gapped or shifted spot sequence cannot be forward-filled.
    """
    from strategy_fnew import normalize_fnew_spec

    normalized = normalize_fnew_spec(spec)
    as_of_ms = _as_of_ms(as_of_utc)
    bar_ms = TIMEFRAME_MS[normalized["timeframe"]]
    canonical = normalized["canonical_strategy_id"]
    histories, errors = {}, {}
    risk_histories = {}
    for pair in normalized["data_universe"]:
        histories[pair], errors[pair] = _history(candles_by_pair.get(pair), as_of_ms, bar_ms=bar_ms,
                                                validate_quote=canonical == "s34_vwap_anchor_reversion_15m")
        if not errors[pair] and histories[pair] and as_of_ms is not None and \
                histories[pair][-1]["timestamp"] != (as_of_ms // bar_ms - 1) * bar_ms:
            errors[pair] = "fnew_closed_history_stale"
        # An invalid VWAP numerator prevents anchor decisions, but its valid
        # OHLC mark can still protect an already open perpetual position.
        if errors[pair] == "invalid_quote_volume" and isinstance(positions, Mapping) \
                and positions.get(pair):
            risk_rows, risk_error = _history(candles_by_pair.get(pair), as_of_ms,
                                              bar_ms=bar_ms, validate_quote=False)
            if not risk_error and risk_rows and (as_of_ms is None or
                    risk_rows[-1]["timestamp"] == (as_of_ms // bar_ms - 1) * bar_ms):
                risk_histories[pair] = risk_rows
    if not normalized["execution_enabled"]:
        reason = normalized["blocked_execution_reason"] or "execution_disabled_by_definition"
        result = _result(normalized, histories, errors)
        if canonical not in SINGLE_STRATEGIES:
            from strategy_pair_signals import evaluate_pair_fnew

            result = evaluate_pair_fnew(normalized, histories, positions or {}, as_of_ms)
            for signal in result["signals"].values():
                signal["evidence"].update(research_candidate_side=signal["target_side"],
                                           research_signal_reason=signal["reason"])
                signal["reason"] = reason
        else:
            for signal in result["signals"].values():
                signal["reason"] = reason
        result.update(execution_enabled=False,
                      execution_blockers=list(normalized["execution_blockers"]))
        return _unready(result, reason)
    if canonical not in SINGLE_STRATEGIES:
        from strategy_pair_signals import evaluate_pair_fnew

        # Its frozen selected pair, rather than every eligible research coin,
        # controls the history requirement of an existing atomic position.
        result = evaluate_pair_fnew(normalized, histories, positions or {}, as_of_ms)
        result.update(canonical_strategy_id=canonical, execution_enabled=True,
                      execution_blockers=[])
        return result
    result = _result(normalized, histories, errors)
    params = normalized["params"]
    positions = positions or {}
    if not isinstance(positions, Mapping):
        raise ValueError("positions must be a mapping")
    entry_ready = result["ready"]
    entry_blocker = None if entry_ready else result["warmup"]["reason"]
    prepared_positions = {}
    spots = {}
    spot_errors = {}
    for pair in normalized["execution_universe"]:
        rows = risk_histories.get(pair, histories[pair])
        prepared_positions[pair] = None
        if rows and (not errors[pair] or pair in risk_histories):
            position, error = _position(positions.get(pair), rows[-1]["timestamp"], bar_ms)
            if error:
                result["signals"][pair]["reason"] = error
                entry_ready, entry_blocker = False, entry_blocker or error
            else:
                prepared_positions[pair] = position
        if canonical == "s33_basis_momentum_1h":
            spot_pair = normalized["data_feeds"]["spot"]["pair_map"][pair]
            spot_mapping = candles_by_pair.get("spot", {})
            spot_mapping = spot_mapping if isinstance(spot_mapping, Mapping) else {}
            spot_rows, error = _history(spot_mapping.get(spot_pair), as_of_ms, bar_ms=bar_ms,
                                         validate_quote=False)
            spots[pair] = spot_rows
            needed = params["sigma_window"] + params["basis_delta_lag"] + 1
            if error or len(spot_rows) < needed:
                spot_errors[pair] = error or "basis_spot_warmup_incomplete"
            elif [row["timestamp"] for row in spot_rows[-needed:]] != [
                    row["timestamp"] for row in rows[-needed:]]:
                spot_errors[pair] = "basis_timestamps_not_aligned"
    if canonical == "s33_basis_momentum_1h":
        result["basis_feed_ready"] = not spot_errors and result["ready"]
    if spot_errors:
        entry_ready, entry_blocker = False, entry_blocker or next(iter(spot_errors.values()))
    result["entry_ready"] = entry_ready
    if not entry_ready:
        has_positions = any(position for position in prepared_positions.values())
        if has_positions:
            result["ready"] = True
            active_stamps = [risk_histories.get(pair, histories[pair])[-1]["timestamp"]
                             for pair, position in prepared_positions.items() if position]
            result["warmup"].update(ready=True, reason="ready_for_risk_exits_only",
                                    last_closed_at=min(active_stamps))
            for pair, position in prepared_positions.items():
                if position:
                    result["signals"][pair]["warmup"].update(
                        ready=True, risk_only=True, risk_required_bars=1,
                        valid_bars=len(risk_histories.get(pair, histories[pair])))
        else:
            _unready(result, entry_blocker or "account_warmup_incomplete")
        result["entry_blocked_reason"] = entry_blocker
    for pair, signal in result["signals"].items():
        rows = risk_histories.get(pair, histories[pair])
        if not rows or (errors[pair] and pair not in risk_histories):
            continue
        evidence = _evidence(normalized, rows)
        if canonical == "s33_basis_momentum_1h":
            evidence["basis_feed_ready"] = pair not in spot_errors and \
                len(rows) >= normalized["min_warmup_bars"]
        if not entry_ready:
            evidence["entry_blocked_reason"] = entry_blocker
        if pair in risk_histories:
            evidence["quote_volume_validation_error"] = errors[pair]
            signal["candle_utc"] = rows[-1]["timestamp"]
        signal["evidence"] = evidence
        position = prepared_positions[pair]
        if position:
            side, reason = _risk_exit(canonical, rows, position, params, evidence)
            if reason:
                signal.update(target_side=side, reason=reason)
                continue
            if pair in risk_histories:
                signal["reason"] = errors[pair]
                continue
            if len(rows) < normalized["min_warmup_bars"]:
                signal["reason"] = "position_signal_history_unavailable"
                continue
        else:
            if not entry_ready:
                signal["reason"] = entry_blocker or "account_warmup_incomplete"
                continue
            evidence["entry_atr"] = evidence["atr"]
            if evidence["atr"] is None or evidence["atr"] <= 0:
                signal.update(target_side=None, reason="entry_atr_unavailable_or_invalid")
                continue
        if canonical == "s33_basis_momentum_1h":
            if pair in spot_errors:
                signal["reason"] = spot_errors[pair]
                continue
            side, reason = _basis(rows, spots[pair], position, params, evidence)
        elif canonical == "s34_vwap_anchor_reversion_15m":
            side, reason = _vwap(rows, position, params, evidence)
        else:
            side, reason = _cascade(rows, position, params, evidence)
        signal.update(target_side=side, reason=reason)
    return result
