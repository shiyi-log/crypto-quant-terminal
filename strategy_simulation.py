"""Independent, forward-only research accounts with an immutable execution ledger.

Signals are decisions at a closed bar. The following *received closed bar's open*
is a paper fill proxy, not an exchange fill. Historical bars never create orders.
Funding and slippage remain unknown; reported PnL includes modeled fees and,
at the selected 100x leverage, exchange-forced liquidation losses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from typing import Any


BAR_MS = 300_000
PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
TIMEFRAME_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                "1d": 86_400_000}
EXECUTION_VERSION = "independent-paper-next-close-open-100x-liquidation-v2"
EXECUTION_MODEL = {
    "version": EXECUTION_VERSION,
    "fill_model": "next_closed_bar_open_proxy",
    "signal_timing": "closed_5m_bar",
    "leverage": 100,
    "target_exposure": 0.30,
    "max_loss_per_trade_usdt": 50.0,
    "risk_stop_pct": 0.005,
    "risk_round_trip_fee": 0.0004,
    "risk_sizing": "notional * (stop_pct + round_trip_fee) <= max_loss_per_trade_usdt",
    "max_positions": 2,
    "max_decision_lag_ms": 90_000,
    "fee_rate_per_side": 0.0002,
    "maker_fee_per_side": 0.0002,
    "taker_fee_per_side": 0.0005,
    "fee_basis": "maker_limit_orders_observed_in_live_trades",
    "non_fill_risk": "unknown_limit_order_non_fill; next_open_proxy_assumes_fill",
    "slippage": "unknown",
    "funding": "unknown",
    "budget_rule": "new entries use remaining margin budget; 30% is equity margin, notional is margin times leverage",
    "budget_basis": "margin",
    "liquidation": {
        "enabled": True,
        "trigger": "closed_candle_low_high",
        "settlement": "bankruptcy_price_full_margin_loss",
    },
    "simulation_fill_model": True,
}
LIMITATIONS = [
    "前向研究模拟，不向交易所发送订单；成交价是下一根已收盘 K 线开盘价代理。",
    "主口径按实测限价单 maker 费率 0.02%/边扣费；taker 0.05%/边仅作为保守敏感性边界。",
    "next-open 代理假设限价单必定成交，未建模限价单未成交风险；滑点与资金费 unknown，不代表真实可成交净收益。",
    "100 倍杠杆下 30% 是保证金预算，名义仓位为保证金乘杠杆；行情波动可能使已有保证金预算被动超标，超标期间拒绝新增仓位。",
    "强平逐根使用已收盘 K 线的 low/high 检查，按破产价结算并损失该仓保证金。",
    "停机或数据断档取消待成交订单，不补造历史成交。",
]

# A fee-model change is an immutable execution change. Older `_100x`
# accounts remain queryable for audit but are excluded from the active loop.
ACTIVE_VARIANT_SUFFIX = "_100x_maker"
# The earlier 100x run used this suffix and a different fee contract.  It is
# retained for audit, but must never enter the current preview/position loop.
ARCHIVED_VARIANT_SUFFIX = "_100x"
SAMPLE_QUALITY_CUTOFF_MS = 1791734400000  # 2026-10-12 00:00 Asia/Shanghai


def sample_annotation(spec: dict, payload: dict) -> dict:
    # Event envelopes keep trade entry time inside their payload. Classify
    # the same trade consistently in both trade and event API projections.
    nested = payload.get("payload")
    if isinstance(nested, dict):
        payload = {**payload, **nested}
    stamp = payload.get("opened_candle_ms", payload.get("candle_ms"))
    if stamp is None:
        return {"sample_quality": "unknown", "eligible_for_factorial_evaluation": False}
    if int(stamp) < SAMPLE_QUALITY_CUTOFF_MS:
        return {"sample_quality": "stop_definition_defect_period",
                "eligible_for_factorial_evaluation": False}
    factorial = bool(spec.get("factorial_design_version"))
    return {"sample_quality": "factorial_forward" if factorial else "legacy_frozen_control",
            "eligible_for_factorial_evaluation": factorial}


def _is_archived_taker_account(strategy_id: str) -> bool:
    return str(strategy_id).startswith("s") and str(strategy_id).endswith(ARCHIVED_VARIANT_SUFFIX)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return value


def _ms(value: Any, name: str = "timestamp") -> int:
    result = int(value)
    if isinstance(value, bool) or float(value) != result or result < 0:
        raise ValueError(f"{name} must be nonnegative integer milliseconds")
    return result


def _rule_hash(spec: dict) -> str:
    # The signal implementation owns the economic-rule identity. Execution
    # assumptions have an additional independent immutable hash below.
    from strategy_factorial import parse_factorial_identity

    if parse_factorial_identity(spec.get("strategy_id")) is not None:
        from strategy_factorial_signals import factorial_rule_hash

        return factorial_rule_hash(spec)
    from strategy_signals import strategy_rule_hash

    return strategy_rule_hash(spec)


def _atr_execution_risk(spec: dict, fee: float) -> dict | None:
    """Opt in without changing any frozen legacy execution assumptions."""
    params = spec.get("params", spec.get("params_frozen", {}))
    mode = spec.get("risk_sizing_mode")
    enabled = isinstance(params, dict) and "risk_stop_atr_mult" in params
    if mode not in (None, "entry_atr"):
        raise ValueError("unsupported risk_sizing_mode")
    if mode == "entry_atr" and not enabled:
        raise ValueError("entry_atr sizing requires explicit risk_stop_atr_mult")
    if not enabled:
        if "max_loss_per_trade_usdt" in spec:
            raise ValueError("configurable risk budget requires entry_atr sizing")
        return None
    multiplier = _number(params["risk_stop_atr_mult"], "risk_stop_atr_mult", positive=True)
    maximum = _number(spec.get("max_loss_per_trade_usdt", 50),
                      "max_loss_per_trade_usdt", positive=True)
    return {
        "version": "independent-paper-next-close-open-100x-entry-atr-v3",
        "risk_sizing_mode": "entry_atr",
        "risk_stop_atr_mult": multiplier,
        "max_loss_per_trade_usdt": maximum,
        "risk_round_trip_fee": 2 * fee,
        "risk_sizing": "notional * (risk_stop_atr_mult * entry_atr / entry_price + round_trip_fee) <= max_loss_per_trade_usdt",
        "entry_atr_policy": "freeze_signal_evidence_atr_at_next_open_fill",
        "stop_policy": "fixed_entry_atr_close_confirmed_next_open",
        "open_bankruptcy_breach_policy": "bankruptcy_settlement_before_pending_exits_and_entries;cancel_superseded_exits;intrabar_check_after_next_open_fills",
        "liquidation": {**EXECUTION_MODEL["liquidation"],
                        "trigger": "next_open_then_closed_candle_low_high",
                        "order_precedence": "open_bankruptcy_breach_before_pending_orders"},
        "risk_budget_limit": "planned_stop_and_fee_only; gaps, close_confirmation, liquidation and unknown costs may exceed it",
        "atomic_atr_stop_policy": "unsupported_until_pair_stop_is_implemented",
    }


def _entry_atr(evidence: Any) -> float | None:
    if not isinstance(evidence, dict):
        return None
    try:
        return _number(evidence.get("atr"), "entry_atr", positive=True)
    except (TypeError, ValueError, OverflowError):
        return None


def _audit_atr_evidence(evidence: Any) -> Any:
    """Keep rejected non-finite ATR evidence serializable in the audit log."""
    result = copy.deepcopy(evidence)
    if isinstance(result, dict) and "atr" in result:
        value = result["atr"]
        try:
            _json(value)
        except (TypeError, ValueError):
            result["atr"] = {"invalid_value": repr(value), "invalid_type": type(value).__name__}
    try:
        _json(result)
    except (TypeError, ValueError):
        result = {"invalid_evidence": repr(result), "invalid_type": type(result).__name__}
    return result


def _entry_atr_plan(order: dict, price: float, model: dict) -> dict | None:
    atr = _entry_atr(order.get("evidence"))
    if atr is None:
        return None
    multiplier = model["risk_stop_atr_mult"]
    distance = multiplier * atr
    stop = price - order["direction"] * distance
    distance_pct = distance / price
    denominator = distance_pct + model["risk_round_trip_fee"]
    if not math.isfinite(denominator) or denominator <= 0:
        return None
    notional = model["max_loss_per_trade_usdt"] / denominator
    if (not all(math.isfinite(value) for value in (distance, stop, distance_pct, notional))
            or min(stop, distance_pct, notional) <= 0):
        return None
    return {"mode": "entry_atr", "entry_atr": atr, "entry_price": price,
            "risk_stop_atr_mult": multiplier, "stop_level": stop,
            "stop_distance_pct": distance_pct,
            "max_loss_per_trade_usdt": model["max_loss_per_trade_usdt"],
            "round_trip_fee": model["risk_round_trip_fee"],
            "risk_cap_notional": notional,
            "atr_signal_candle_ms": order["signal_candle_ms"],
            "limit_basis": "planned_stop_and_fee_only"}


def execution_pairs(spec: dict) -> tuple[str, ...]:
    """Return the actual trade legs, separate from research/data universe.

    Generation-2 specifications can include auxiliary symbols (for example
    the 20 symbols used to calculate breadth).  Only explicit exchange pair
    strings are eligible for orders; callers may keep the wider research
    universe in ``data_universe``/``universe`` for indicator inputs.
    """
    configured = spec.get("execution_universe")
    if configured is None:
        configured = spec.get("universe", PAIRS)
    result = []
    for value in configured:
        value = str(value)
        if "/" in value and value not in result:
            result.append(value)
    if not result:
        result = list(PAIRS)
    return tuple(result)


def execution_identity(spec: dict) -> dict:
    initial = _number(spec.get("initial_capital", 10_000), "initial_capital", positive=True)
    fee = _number(spec.get("fee_rate_per_side", 0.0002), "fee_rate_per_side")
    if fee < 0 or fee >= 0.01:
        raise ValueError("fee_rate_per_side must be in [0, 0.01)")
    timeframe = str(spec.get("timeframe", "5m"))
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unsupported execution timeframe: {timeframe}")
    pairs = execution_pairs(spec)
    risk = spec.get("position_budget", {})
    leverage = _number(risk.get("leverage", 100), "leverage", positive=True)
    if risk.get("target_exposure", .30) != .30 or risk.get("max_positions", 2) != 2 or leverage != 100:
        raise ValueError("this execution version requires 30% margin exposure, two slots and leverage 100")
    model = dict(EXECUTION_MODEL, initial_capital=initial, fee_rate_per_side=fee,
                 leverage=leverage, timeframe=timeframe,
                 bar_ms=TIMEFRAME_MS[timeframe], execution_universe=list(pairs),
                 signal_timing=f"closed_{timeframe}_bar",
                 research_timeframe=str(spec.get("research_timeframe", timeframe)),
                 research_universe=list(spec.get("research_universe",
                                                spec.get("universe", pairs))))
    atr_risk = _atr_execution_risk(spec, fee)
    if atr_risk is not None:
        model.update(atr_risk)
        model.pop("risk_stop_pct")
    if "execution_enabled" in spec:
        if not isinstance(spec["execution_enabled"], bool):
            raise ValueError("execution_enabled must be boolean")
        model["execution_enabled"] = spec["execution_enabled"]
        if not spec["execution_enabled"]:
            reason = spec.get("blocked_execution_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("disabled execution requires blocked_execution_reason")
            model["blocked_execution_reason"] = reason
    return {"rule_hash": _rule_hash(spec), "execution_hash": _hash(model), "execution": model}


def initial_state(spec: dict, observed_boundary_ms: int) -> dict:
    identity = execution_identity(spec)
    capital = identity["execution"]["initial_capital"]
    return {
        "strategy_id": str(spec["strategy_id"]), "cash": capital,
        "realized_pnl": 0.0, "fees_paid": 0.0, "positions": {}, "pending_orders": [],
        "last_candle_ms": None, "last_candle_fingerprint": None,
        "last_observed_at_ms": None, "observed_boundary_ms": _ms(observed_boundary_ms),
        "closed_trade_count": 0, "winning_trade_count": 0, "peak_equity": capital,
        "max_drawdown_ratio": 0.0, "warmup": {"ready": False, "reason": "awaiting_data"},
        "status": "warming_up", "rule_hash": identity["rule_hash"],
        "execution_hash": identity["execution_hash"],
    }


def _mark(state: dict, prices: dict[str, float] | None = None) -> dict:
    prices = prices or {}
    margin, unrealized, gross, open_fees = 0.0, 0.0, 0.0, 0.0
    for pair, position in state["positions"].items():
        price = prices.get(pair, position.get("mark_price", position["entry_price"]))
        position["mark_price"] = price
        pnl = position["direction"] * position["quantity"] * (price - position["entry_price"])
        position["unrealized_pnl"] = pnl
        position["notional"] = position["quantity"] * price
        margin += position["margin"]
        unrealized += pnl
        gross += position["notional"]
        open_fees += position["entry_fee"]
    equity = state["cash"] + margin + unrealized
    return {"equity": equity, "margin_used": margin, "gross_notional": gross,
            "unrealized_pnl": unrealized, "open_entry_fees": open_fees}


def _warmup(signal: dict, fallback: dict,
            required_pairs: tuple[str, ...] = PAIRS) -> dict:
    value = copy.deepcopy(signal.get("warmup", fallback))
    if isinstance(value, dict) and required_pairs and all(pair in value for pair in required_pairs):
        pairs = [value[pair] for pair in required_pairs]
        return {"ready": bool(signal.get("ready")),
                "valid_bars": min(item.get("valid_bars", 0) for item in pairs),
                "required_bars": max(item.get("required_bars", 0) for item in pairs),
                "reason": "ready" if signal.get("ready") else "; ".join(
                    str(item.get("reason", "awaiting_data")) for item in pairs if not item.get("ready")),
                "by_pair": value}
    return value


def _validate_candles(candles_by_pair: dict, observed_at_ms: int, *, bar_ms: int = BAR_MS,
                      allowed_pairs: tuple[str, ...] = PAIRS) -> tuple[dict, int]:
    if not candles_by_pair:
        raise ValueError("closed candle batch is empty")
    candles = {}
    for pair, values in candles_by_pair.items():
        if pair not in allowed_pairs:
            raise ValueError("unexpected pair in closed candle batch")
        if isinstance(values, list):
            if not values:
                continue
            values = values[-1]
        if not isinstance(values, dict):
            raise ValueError("closed candle must be a dict")
        candle = copy.deepcopy(values)
        candle["timestamp"] = _ms(candle["timestamp"])
        if candle["timestamp"] % bar_ms:
            raise ValueError("candle timestamp is not aligned to execution timeframe")
        if candle.get("extras", {}).get("closed") is not True:
            raise ValueError("execution requires explicitly closed candles")
        if candle["timestamp"] + bar_ms > observed_at_ms:
            raise ValueError("execution rejects future/unclosed candles")
        for field in ("open", "high", "low", "close"):
            candle[field] = _number(candle[field], field, positive=True)
        if not (candle["low"] <= min(candle["open"], candle["close"])
                <= max(candle["open"], candle["close"]) <= candle["high"]):
            raise ValueError("inconsistent OHLC")
        candle["volume"] = _number(candle.get("volume", 0), "volume")
        if candle["volume"] < 0:
            raise ValueError("negative volume")
        candles[pair] = candle
    timestamps = {bar["timestamp"] for bar in candles.values()}
    if len(timestamps) != 1:
        raise ValueError("closed candle batch timestamps must match")
    return candles, timestamps.pop()


def apply_closed_batch(state: dict, spec: dict, candles_by_pair: dict, signal: dict,
                       observed_at_ms: int, session_boundary_ms: int) -> tuple[dict, list[dict]]:
    """Pure state transition; caller atomically persists state plus every event.

    The only place that creates fills. Repeated bars are idempotent, revised bars
    are rejected, and a missed contiguous bar cancels orders rather than replaying.
    """
    observed_at_ms = _ms(observed_at_ms, "observed_at_ms")
    session_boundary_ms = _ms(session_boundary_ms, "session_boundary_ms")
    identity = execution_identity(spec)
    bar_ms = identity["execution"]["bar_ms"]
    allowed_pairs = tuple(identity["execution"].get("execution_universe", PAIRS))
    if state["rule_hash"] != identity["rule_hash"] or state["execution_hash"] != identity["execution_hash"]:
        raise ValueError("rule/execution change requires a new strategy identity")
    if signal.get("strategy_id") != state["strategy_id"] or signal.get("rule_hash") != state["rule_hash"]:
        raise ValueError("signal strategy/rule hash does not match account")
    candles, timestamp = _validate_candles(candles_by_pair, observed_at_ms,
                                           bar_ms=bar_ms, allowed_pairs=allowed_pairs)
    state = copy.deepcopy(state)
    fingerprint = _hash(candles)
    previous = state["last_candle_ms"]
    if previous is not None and timestamp <= previous:
        if timestamp == previous and fingerprint != state["last_candle_fingerprint"]:
            raise ValueError("processed closed candle changed; refusing to rewrite evidence")
        if timestamp < previous:
            raise ValueError("out-of-order batch cannot create historical simulation fills")
        state["warmup"] = _warmup(signal, state["warmup"], allowed_pairs)
        return state, []
    if timestamp < max(session_boundary_ms, state["observed_boundary_ms"]):
        raise ValueError("historical warmup must not enter the execution path")
    events: list[dict] = []
    strategy_id = state["strategy_id"]

    def event(kind: str, payload: dict) -> None:
        if identity["execution"].get("risk_sizing_mode") == "entry_atr" and "evidence" in payload:
            payload = dict(payload, evidence=_audit_atr_evidence(payload["evidence"]))
        ordinal = len(events)
        event_id = _hash([strategy_id, timestamp, kind, ordinal, payload])
        events.append({"event_id": event_id, "strategy_id": strategy_id,
                       "event_type": kind, "candle_ms": timestamp,
                       "observed_at_ms": observed_at_ms, "rule_hash": state["rule_hash"],
                       "data_fingerprint": fingerprint, "payload": dict(payload, strategy_id=strategy_id)})

    def cancel(order: dict, reason: str, **evidence) -> None:
        event("order_cancelled", dict(order, status="cancelled", reason=reason,
                                       observed_at_ms=observed_at_ms, **evidence))

    def close_position(order: dict, price: float, *, liquidation: bool = False) -> None:
        pair = order["pair"]
        position = state["positions"].pop(pair, None)
        if position is None:
            cancel(order, "position_already_closed")
            return
        fee = position["quantity"] * price * fee_rate
        gross = position["direction"] * position["quantity"] * (price - position["entry_price"])
        if liquidation:
            # The bankruptcy-price formula makes the loss exactly the posted
            # margin. Do not return the margin to cash; the exit fee is still
            # charged on the forced notional execution.
            gross = -position["margin"]
        fees = position["entry_fee"] + fee
        net = gross - fees
        if liquidation:
            state["cash"] -= fee
        else:
            state["cash"] += position["margin"] + gross - fee
        state["fees_paid"] += fee
        state["realized_pnl"] += net
        state["closed_trade_count"] += 1
        state["winning_trade_count"] += int(net > 0)
        filled = dict(order, status="filled", fill_price=price, quantity=position["quantity"],
                      fee=fee, observed_at_ms=observed_at_ms, fill_candle_ms=timestamp,
                      liquidation=liquidation,
                      fill_model=(order.get("fill_model", "intrabar_liquidation_price") if liquidation
                                  else EXECUTION_MODEL["fill_model"]),
                      simulation_fill_model=True)
        if "risk_sizing" in position:
            filled.update(entry_atr=position["entry_atr"], stop_level=position["stop_level"],
                          risk_stop_atr_mult=position["risk_stop_atr_mult"],
                          risk_sizing=copy.deepcopy(position["risk_sizing"]))
        event("order_filled", filled)
        trade = {"trade_id": position["trade_id"], "strategy_id": strategy_id,
                 "pair": pair, "side": position["side"], "quantity": position["quantity"],
                 "margin": position["margin"], "leverage": position["leverage"],
                 "notional": position["quantity"] * position["entry_price"],
                 "entry_price": position["entry_price"], "exit_price": price,
                 "liquidation_price": position.get("liquidation_price") if liquidation else None,
                 "opened_candle_ms": position["opened_candle_ms"], "closed_candle_ms": timestamp,
                 "observed_at_ms": observed_at_ms, "gross_pnl": gross, "fees": fees,
                 "net_pnl": net, "return_ratio": net / position["margin"],
                 "entry_order_id": position["paper_order_id"], "exit_order_id": order["paper_order_id"],
                 "group_id": position.get("group_id"),
                 "exchange_order_id": None, "exit_reason": order["reason"],
                 "funding": "unknown", "slippage": "unknown", "liquidation": liquidation,
                 "rule_hash": state["rule_hash"]}
        if "risk_sizing" in position:
            trade.update(entry_atr=position["entry_atr"], stop_level=position["stop_level"],
                         risk_stop_atr_mult=position["risk_stop_atr_mult"],
                         risk_sizing=copy.deepcopy(position["risk_sizing"]))
        event("trade_closed", trade)

    def liquidate(pair: str, position: dict, trigger_price: float, *, phase: str) -> None:
        liquidation_price = position["liquidation_price"]
        liquidation_order_id = _hash([strategy_id, timestamp, pair, "liquidation",
                                      position["trade_id"]])
        model = "open_bankruptcy_price" if phase == "next_open_bankruptcy" else "intrabar_liquidation_price"
        liquidation_order = {
            "order_id": liquidation_order_id,
            "paper_order_id": f"{timestamp}-{pair.split('/')[0]}-{strategy_id}-liq-{liquidation_order_id[:12]}",
            "exchange_order_id": None, "strategy_id": strategy_id, "pair": pair,
            "side": "sell" if position["direction"] == 1 else "buy",
            "direction": -position["direction"], "kind": "exit", "reason": "liquidation",
            "status": "forced", "signal_candle_ms": timestamp,
            "expected_fill_candle_ms": timestamp, "observed_at_ms": observed_at_ms,
            "fill_model": model, "data_fingerprint": fingerprint,
        }
        payload = {"pair": pair, "trade_id": position["trade_id"],
                   "side": position["side"], "quantity": position["quantity"],
                   "margin": position["margin"], "leverage": position["leverage"],
                   "liquidation_price": liquidation_price, "trigger_price": trigger_price,
                   "reason": "liquidation"}
        if identity["execution"].get("risk_sizing_mode") == "entry_atr":
            liquidation_order["liquidation_phase"] = phase
            payload["liquidation_phase"] = phase
        event("liquidation", payload)
        close_position(liquidation_order, liquidation_price, liquidation=True)

    fee_rate = identity["execution"]["fee_rate_per_side"]
    open_prices = {pair: candle["open"] for pair, candle in candles.items()}
    liquidated_at_open = {}
    if identity["execution"].get("risk_sizing_mode") == "entry_atr":
        # Bankruptcy is forced before a queued close can fill at a worse open.
        # A latest received open is current evidence even after a data gap;
        # pending discretionary orders still require continuity and freshness.
        for pair, position in list(state["positions"].items()):
            price = open_prices.get(pair)
            if price is None:
                continue
            bankrupt = ((position["direction"] == 1 and price <= position["liquidation_price"])
                        or (position["direction"] == -1 and price >= position["liquidation_price"]))
            if bankrupt:
                liquidated_at_open[pair] = position["trade_id"]
                liquidate(pair, position, price, phase="next_open_bankruptcy")
    # Mark every leg to the same next-open prices before calculating capital.
    _mark(state, open_prices)
    pending = state["pending_orders"]
    state["pending_orders"] = []
    grouped: dict[str, list[dict]] = {}
    for order in pending:
        if order["kind"] == "exit" and order["pair"] in liquidated_at_open:
            cancel(order, "superseded_by_open_liquidation", superseded_exit_reason=order["reason"],
                   liquidated_trade_id=liquidated_at_open[order["pair"]])
            continue
        grouped.setdefault(order.get("group_id") or order["order_id"], []).append(order)
    fresh = observed_at_ms - (timestamp + bar_ms) <= identity["execution"]["max_decision_lag_ms"]
    eligible_groups = []
    for orders in grouped.values():
        reason = None
        if any(order["signal_candle_ms"] < session_boundary_ms for order in orders):
            reason = "restart_cancel"
        elif previous is None or timestamp != previous + bar_ms:
            reason = "gap_cancel"
        elif any(order["expected_fill_candle_ms"] != timestamp for order in orders):
            reason = "missed_fill_bar"
        elif any(order["pair"] not in candles for order in orders):
            reason = "atomic_leg_missing" if orders[0].get("atomic") else "fill_bar_missing"
        elif not fresh:
            reason = "stale_closed_bar"
        if reason:
            for order in orders:
                cancel(order, reason)
            continue
        eligible_groups.append(orders)
    # Every risk-reducing order at this same open releases its capital before
    # any new entry is assessed, including non-atomic switches between pairs.
    for orders in eligible_groups:
        for order in orders:
            if order["kind"] == "exit":
                close_position(order, open_prices[order["pair"]])
    for orders in eligible_groups:
        entries = [order for order in orders if order["kind"] == "entry"]
        if not entries:
            continue
        atr_sizing = identity["execution"].get("risk_sizing_mode") == "entry_atr"
        blocked = ("execution_disabled" if identity["execution"].get("execution_enabled") is False
                   else "atomic_atr_stop_policy_unsupported" if atr_sizing and orders[0].get("atomic")
                   else None)
        if blocked:
            for order in entries:
                event("order_rejected", dict(order, status="rejected", reason=blocked,
                      blocked_execution_reason=identity["execution"].get("blocked_execution_reason")))
            continue
        plans = []
        if atr_sizing:
            valid_entries = []
            for order in entries:
                plan = _entry_atr_plan(order, open_prices[order["pair"]], identity["execution"])
                if plan is None:
                    rejected = dict(order, evidence=_audit_atr_evidence(order.get("evidence")),
                                    status="rejected", reason="entry_atr_unavailable_or_invalid")
                    event("order_rejected", rejected)
                else:
                    valid_entries.append(order)
                    plans.append(plan)
            entries = valid_entries
            if not entries:
                continue
        valuation = _mark(state, open_prices)
        budget = identity["execution"]["target_exposure"] * valuation["equity"]
        # Convention A: target exposure is posted margin. Leverage expands
        # notional but must not consume the margin budget a second time.
        remaining = budget - valuation["margin_used"]
        slots = identity["execution"]["max_positions"] - len(state["positions"])
        ready = set(candles) == set(allowed_pairs)
        if not ready or remaining <= 0 or slots < len(entries) or state["cash"] <= 0:
            for order in entries:
                event("order_rejected", dict(order, status="rejected", observed_at_ms=observed_at_ms,
                      reason="entry_data_missing" if not ready else "account_budget_exhausted"))
            continue
        leverage = identity["execution"]["leverage"]
        # The 30% target is posted margin, per the selected leverage
        # convention. Entry fees are an additional cash cost charged on
        # leveraged notional; only the available cash cap reserves them here.
        weights = [min(1.0, max(0.0, order.get("size_multiplier", 1.0))) for order in entries]
        if orders[0].get("atomic"):
            weights = [min(weights)] * len(weights)
        leveraged_fee_rate = leverage * fee_rate
        # The configured risk budget limits the planned stop plus modeled
        # round-trip fee. Exposure, slots and cash remain additional caps.
        cash_unit = state["cash"] / ((1 + leveraged_fee_rate) * max(1, sum(weights)))
        if atr_sizing:
            amounts = [min(remaining / slots, plan["risk_cap_notional"] / leverage, cash_unit) * weight
                       for plan, weight in zip(plans, weights, strict=True)]
        else:
            risk_cap_margin = identity["execution"]["max_loss_per_trade_usdt"] / (
                leverage * (identity["execution"]["risk_stop_pct"]
                            + identity["execution"]["risk_round_trip_fee"]))
            unit = min(remaining / slots, risk_cap_margin, cash_unit)
            amounts = [unit * weight for weight in weights]
        if any(amount <= 0 for amount in amounts):
            for order in entries:
                event("order_rejected", dict(order, status="rejected", reason="zero_sizing"))
            continue
        for index, (order, margin) in enumerate(zip(entries, amounts, strict=True)):
            pair, direction, price = order["pair"], order["direction"], open_prices[order["pair"]]
            if pair in state["positions"]:
                raise ValueError("pending entry would overwrite an existing position")
            fee = margin * leverage * fee_rate
            quantity = margin * leverage / price
            notional = quantity * price
            liquidation_price = price * (1 - direction / leverage)
            state["cash"] -= margin + fee
            state["fees_paid"] += fee
            position = {"strategy_id": strategy_id, "pair": pair, "direction": direction,
                        "side": "long" if direction == 1 else "short", "quantity": quantity,
                        "leverage": leverage, "notional": notional,
                        "entry_price": price, "mark_price": price, "margin": margin,
                        "liquidation_price": liquidation_price,
                        "opened_candle_ms": timestamp, "entry_fee": fee,
                        "entry_evidence": copy.deepcopy(order.get("evidence", {})),
                        "entry_atr": order.get("evidence", {}).get("atr"),
                        "group_id": order.get("group_id"),
                        "paper_order_id": order["paper_order_id"], "exchange_order_id": None,
                        "trade_id": "trade-" + order["order_id"], "unrealized_pnl": 0.0}
            risk_payload = {}
            if atr_sizing:
                plan = dict(plans[index], actual_notional=notional,
                            planned_loss_usdt=notional * (plans[index]["stop_distance_pct"]
                                                         + plans[index]["round_trip_fee"]))
                risk_payload = {"entry_atr": plan["entry_atr"], "stop_level": plan["stop_level"],
                                "risk_stop_atr_mult": plan["risk_stop_atr_mult"], "risk_sizing": plan}
                position.update(copy.deepcopy(risk_payload))
            state["positions"][pair] = position
            event("order_filled", dict(order, status="filled", fill_price=price, quantity=quantity,
                  margin=margin, leverage=leverage, notional=notional,
                  liquidation_price=liquidation_price, fee=fee, observed_at_ms=observed_at_ms,
                  fill_candle_ms=timestamp,
                  fill_model=EXECUTION_MODEL["fill_model"], simulation_fill_model=True, **risk_payload))
            event("position_opened", position)

    # A liquidation is an exchange-forced exit and is the only execution path
    # allowed to use intrabar high/low. Check after next-open fills so a newly
    # opened position is exposed to the full current candle as it would be in
    # the market; regular strategy exits remain close-confirmed next-open orders.
    for pair, position in list(state["positions"].items()):
        candle = candles.get(pair)
        if candle is None:
            continue
        liquidation_price = position["liquidation_price"]
        triggered = ((position["direction"] == 1 and candle["low"] <= liquidation_price)
                     or (position["direction"] == -1 and candle["high"] >= liquidation_price))
        if not triggered:
            continue
        liquidate(pair, position,
                  candle["low"] if position["direction"] == 1 else candle["high"],
                  phase="intrabar_after_next_open_fills")

    close_prices = {pair: candle["close"] for pair, candle in candles.items()}
    valuation = _mark(state, close_prices)
    state["warmup"] = _warmup(signal, state["warmup"], allowed_pairs)
    state["status"] = ("execution_blocked" if identity["execution"].get("execution_enabled") is False
                       else "running" if signal.get("ready") else "warming_up")
    group = signal.get("group") or {}
    atomic = bool(group.get("atomic"))
    group_legs = group.get("legs", list(signal.get("signals", {})))
    targets = signal.get("signals", {})
    if atomic and (set(group_legs) != set(allowed_pairs)
                   or any(pair not in targets for pair in group_legs)):
        raise ValueError("atomic signal requires both explicit legs for every execution leg")
    decisions = []
    for pair in allowed_pairs:
        info = targets.get(pair)
        if info is None:
            continue
        target = info.get("target_side")
        if target is not None and (isinstance(target, bool) or target not in (-1, 0, 1)):
            raise ValueError("target_side must be -1/0/1/null")
        signal_time = info.get("candle_utc", timestamp)
        if signal_time is not None and _ms(signal_time) != timestamp:
            raise ValueError("signal evidence timestamp does not match closed bar")
        multiplier = _number(info.get("size_multiplier", 1), "size_multiplier")
        if not 0 <= multiplier <= 1:
            raise ValueError("size_multiplier must be in [0,1]")
        evidence = info.get("evidence", {})
        decisions.append({"pair": pair, "target_side": target, "reason": info.get("reason", "signal"),
                          "evidence": (_audit_atr_evidence(evidence) if identity["execution"].get("risk_sizing_mode") == "entry_atr"
                                       else copy.deepcopy(evidence)),
                          "size_multiplier": multiplier})
    if atomic:
        sides = [decision["target_side"] for decision in decisions]
        if sides not in ([None, None], [0, 0], [-1, 1], [1, -1]):
            raise ValueError("atomic pair targets must hold/close both legs or take opposite directions")
        if len(state["positions"]) not in (0, 2) and sides != [0, 0]:
            raise ValueError("atomic pair account has an incomplete hedge; refusing single-leg decisions")
    fingerprint_signal = signal
    if identity["execution"].get("risk_sizing_mode") == "entry_atr":
        fingerprint_signal = copy.deepcopy(signal)
        for info in fingerprint_signal.get("signals", {}).values():
            if "evidence" in info:
                info["evidence"] = _audit_atr_evidence(info["evidence"])
    event("decision", {"ready": bool(signal.get("ready")), "warmup": state["warmup"],
                       "signals": decisions, "group": group or None,
                       "signal_fingerprint": _hash(fingerprint_signal)})
    entry_allowed = bool(signal.get("ready")) and set(candles) == set(allowed_pairs) and fresh
    group_id = _hash([strategy_id, timestamp, "atomic"]) if atomic else None
    for decision in decisions:
        pair, target = decision["pair"], decision["target_side"]
        if target is None:
            continue
        existing = state["positions"].get(pair)
        old_direction = existing["direction"] if existing else 0
        if target == old_direction:
            continue
        intents = []
        if existing:
            intents.append(("exit", old_direction))
        if target:
            denied = None
            if identity["execution"].get("execution_enabled") is False:
                denied = "execution_disabled"
            elif identity["execution"].get("risk_sizing_mode") == "entry_atr":
                if atomic:
                    denied = "atomic_atr_stop_policy_unsupported"
                elif _entry_atr(decision["evidence"]) is None:
                    denied = "entry_atr_unavailable_or_invalid"
            if denied:
                event("entry_denied", dict(decision, reason=denied,
                      blocked_execution_reason=identity["execution"].get("blocked_execution_reason")))
            elif entry_allowed:
                intents.append(("entry", target))
            else:
                event("entry_denied", dict(decision, reason="warmup_or_stale_or_missing_data"))
        for kind, direction in intents:
            order_id = _hash([strategy_id, timestamp, pair, kind, direction])
            paper_order_id = f"{timestamp}-{pair.split('/')[0]}-{strategy_id}-{order_id[:12]}"
            order = dict(decision, order_id=order_id, paper_order_id=paper_order_id,
                         exchange_order_id=None, strategy_id=strategy_id, side="long" if direction == 1 else "short",
                         direction=direction, kind=kind, status="pending", signal_candle_ms=timestamp,
                         expected_fill_candle_ms=timestamp + bar_ms, observed_at_ms=observed_at_ms,
                         signal_observed_at_ms=observed_at_ms,
                         group_id=group_id, atomic=atomic, rule_hash=state["rule_hash"],
                         data_fingerprint=fingerprint, fill_model=EXECUTION_MODEL["fill_model"],
                         buy_sell="buy" if (kind == "entry") == (direction == 1) else "sell")
            state["pending_orders"].append(order)
            event("order_submitted", order)
    valuation = _mark(state)
    state["peak_equity"] = max(state["peak_equity"], valuation["equity"])
    drawdown = 1 - valuation["equity"] / state["peak_equity"]
    state["max_drawdown_ratio"] = max(state["max_drawdown_ratio"], drawdown)
    state["last_candle_ms"] = timestamp
    state["last_candle_fingerprint"] = fingerprint
    state["last_observed_at_ms"] = observed_at_ms
    event("equity_snapshot", dict(valuation, timestamp=timestamp, observed_at_ms=observed_at_ms,
          cash=state["cash"], realized_pnl=state["realized_pnl"], fees_paid=state["fees_paid"],
          open_position_count=len(state["positions"])))
    # Fail closed, atomically rolling back if capital accounting is corrupted.
    expected_equity = (identity["execution"]["initial_capital"] + state["realized_pnl"]
                       + valuation["unrealized_pnl"] - valuation["open_entry_fees"])
    if (not math.isfinite(valuation["equity"]) or not math.isfinite(state["cash"])
            or state["cash"] < -1e-7 or abs(expected_equity - valuation["equity"]) > 1e-6):
        raise ValueError("invalid account capital; refusing to commit simulation batch")
    return state, events


class StrategySimulation:
    """Storage adapter: one row lock and transaction per independent account."""

    def __init__(self, store, clock_ms=None):
        self.store = store
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._preview_states = {}

    def ensure_accounts(self, specs: list[dict], observed_boundary_ms: int) -> list[str]:
        requested = [str(item["strategy_id"]) for item in specs]
        if any(not item for item in requested) or len(requested) != len(set(requested)):
            raise ValueError("strategy IDs must be nonempty and unique")
        ids = []
        for spec in specs:
            strategy_id = str(spec["strategy_id"])
            if not strategy_id or strategy_id in ids:
                raise ValueError("strategy IDs must be nonempty and unique")
            identity = execution_identity(spec)
            state = initial_state(spec, observed_boundary_ms)
            with self.store._connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("strategy-sim:" + strategy_id,))
                existing = conn.execute("SELECT * FROM strategy_sim_accounts WHERE strategy_id=%s FOR UPDATE",
                                        (strategy_id,)).fetchone()
                if existing:
                    if existing["rule_hash"] != identity["rule_hash"] or existing["execution_hash"] != identity["execution_hash"]:
                        raise ValueError("rule/execution change requires a new strategy identity")
                else:
                    conn.execute("""INSERT INTO strategy_sim_accounts
                        (strategy_id,rule_hash,execution_hash,definition,execution,initial_capital,state)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (strategy_id, identity["rule_hash"], identity["execution_hash"], self.store._json(spec),
                         self.store._json(identity["execution"]), identity["execution"]["initial_capital"],
                         self.store._json(state)))
                    payload = {"strategy_id": strategy_id, "initial_capital": state["cash"],
                               "observed_boundary_ms": _ms(observed_boundary_ms),
                               "rule_hash": identity["rule_hash"], "execution_hash": identity["execution_hash"]}
                    self._append(conn, [{"event_id": _hash([strategy_id, "account_created"]),
                        "strategy_id": strategy_id, "event_type": "account_created",
                        "candle_ms": None, "observed_at_ms": self.clock_ms(), "rule_hash": identity["rule_hash"],
                        "data_fingerprint": _hash(payload), "payload": payload}])
            ids.append(strategy_id)
        return ids

    def _append(self, conn, events: list[dict]) -> None:
        for item in events:
            conn.execute("""INSERT INTO strategy_sim_events
                (event_id,strategy_id,event_type,candle_ms,observed_at_ms,rule_hash,data_fingerprint,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (item["event_id"], item["strategy_id"], item["event_type"], item["candle_ms"],
                 item["observed_at_ms"], item["rule_hash"], item["data_fingerprint"], self.store._json(item["payload"])))

    def update_warmup(self, signals_by_strategy: dict, observed_at_ms: int | None = None,
                      *, diagnostic_only: bool = False) -> None:
        """Refresh indicator readiness without entering the execution path.

        Periodic diagnostics preserve execution status and skip unchanged
        values. Startup callers retain the original status transition.
        """
        observed_at_ms = _ms(observed_at_ms if observed_at_ms is not None else self.clock_ms())
        for strategy_id, signal in signals_by_strategy.items():
            with self.store._connection() as conn:
                row = conn.execute("SELECT * FROM strategy_sim_accounts WHERE strategy_id=%s FOR UPDATE",
                                   (strategy_id,)).fetchone()
                if row is None or signal.get("rule_hash") != row["rule_hash"]:
                    raise ValueError("warmup rule/strategy does not match existing account")
                state = row["state"]
                execution = row.get("execution") or {}
                required_pairs = tuple(execution.get("execution_universe", PAIRS))
                warmup = _warmup(signal, {"ready": bool(signal.get("ready"))},
                                 required_pairs)
                if diagnostic_only and state.get("warmup") == warmup:
                    continue
                state["warmup"] = warmup
                if not diagnostic_only:
                    state["status"] = "awaiting_new_closed_bar" if signal.get("ready") else "warming_up"
                state["warmup_observed_at_ms"] = observed_at_ms
                conn.execute("UPDATE strategy_sim_accounts SET state=%s,updated_at=CURRENT_TIMESTAMP WHERE strategy_id=%s",
                             (self.store._json(state), strategy_id))

    def process_batch(self, candles_by_pair: dict, signals_by_strategy: dict,
                      observed_at_ms: int | None = None, session_boundary_ms: int = 0,
                      *, timeframe: str | None = None) -> dict:
        observed_at_ms = _ms(observed_at_ms if observed_at_ms is not None else self.clock_ms())
        if timeframe is not None:
            timeframe = str(timeframe)
            if timeframe not in TIMEFRAME_MS:
                raise ValueError(f"unsupported execution timeframe: {timeframe}")
        processed = {}
        for strategy_id, signal in signals_by_strategy.items():
            with self.store._connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("strategy-sim:" + strategy_id,))
                row = conn.execute("SELECT * FROM strategy_sim_accounts WHERE strategy_id=%s FOR UPDATE",
                                   (strategy_id,)).fetchone()
                if row is None:
                    raise ValueError("simulation account must be explicitly initialized")
                if timeframe is not None:
                    account_timeframe = str((row.get("execution") or {}).get("timeframe", "5m"))
                    if account_timeframe != timeframe:
                        raise ValueError("process batch timeframe does not match strategy account")
                preview = self._preview_states.get(strategy_id)
                if preview is not None:
                    preview_identity = execution_identity(row["definition"])
                    preview_candles, _ = _validate_candles(
                        candles_by_pair, observed_at_ms,
                        bar_ms=preview_identity["execution"]["bar_ms"],
                        allowed_pairs=tuple(preview_identity["execution"].get("execution_universe", PAIRS)))
                    actual = {"state_hash": _hash(row["state"]), "candle_hash": _hash(preview_candles),
                              "observed_at_ms": observed_at_ms, "session_boundary_ms": session_boundary_ms}
                    if preview != actual:
                        raise ValueError("inputs/account changed after fill preview; reevaluate signals before committing")
                state, events = apply_closed_batch(row["state"], row["definition"], candles_by_pair,
                                                  signal, observed_at_ms, session_boundary_ms)
                self._append(conn, events)
                conn.execute("UPDATE strategy_sim_accounts SET state=%s,updated_at=CURRENT_TIMESTAMP WHERE strategy_id=%s",
                             (self.store._json(state), strategy_id))
                processed[strategy_id] = {"events": len(events), "last_candle_ms": state["last_candle_ms"],
                                           "open_position_count": len(state["positions"])}
            self._preview_states.pop(strategy_id, None)
        return {"accounts": processed, "observed_at_ms": observed_at_ms}

    def preview_positions(self, candles_by_pair: dict, observed_at_ms: int,
                          session_boundary_ms: int, *, timeframe: str = "5m",
                          strategy_ids: list[str] | tuple[str, ...] | None = None) -> dict:
        """Read-only projection of next-open fills before evaluating close exits.

        Preview and commit share apply_closed_batch, so prices, fees, sizing and
        cancellations cannot diverge. Commit also checks the state has not changed.

        ``timeframe`` selects the execution arm whose candles are supplied.  The
        default remains 5m for callers that predate native multi-timeframe arms;
        callers processing 15m/1h/1d must select that timeframe explicitly so a
        candle batch cannot accidentally be applied to another arm.

        Explicit ``strategy_ids`` restricts the projection to service-selected
        accounts and selects each account's trade legs from a shared candle
        batch. Omitted IDs preserve the earlier whole-arm preview contract.
        """
        timeframe = str(timeframe)
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported execution timeframe: {timeframe}")
        with self.store._connection() as conn:
            # Archived 1x and superseded 100x accounts remain queryable for
            # audit, but are not part of the active maker-priced 100x loop.
            # Evaluating their old spec here would run it through the current
            # identity and could mix fee contracts in one sample.
            rows = conn.execute("""SELECT * FROM strategy_sim_accounts
                WHERE (execution->>'leverage')::numeric=%s
                  AND COALESCE(execution->>'timeframe', '5m')=%s
                ORDER BY strategy_id""",
                                (EXECUTION_MODEL["leverage"], timeframe)).fetchall()
        rows = [row for row in rows if not _is_archived_taker_account(row["strategy_id"])]
        if strategy_ids is not None:
            selected = set(strategy_ids)
            rows = [row for row in rows if row["strategy_id"] in selected]
        result = {}
        for row in rows:
            strategy_id = row["strategy_id"]
            preview_identity = execution_identity(row["definition"])
            allowed_pairs = tuple(preview_identity["execution"].get("execution_universe", PAIRS))
            account_candles = candles_by_pair
            if strategy_ids is not None:
                account_candles = {pair: candles_by_pair[pair] for pair in allowed_pairs
                                   if pair in candles_by_pair}
            neutral = {"strategy_id": strategy_id, "rule_hash": row["rule_hash"],
                       "ready": True, "warmup": row["state"]["warmup"], "signals": {}}
            state, _ = apply_closed_batch(row["state"], row["definition"], account_candles,
                                          neutral, observed_at_ms, session_boundary_ms)
            normalized_candles, _ = _validate_candles(
                account_candles, observed_at_ms,
                bar_ms=preview_identity["execution"]["bar_ms"],
                allowed_pairs=allowed_pairs)
            self._preview_states[strategy_id] = {"state_hash": _hash(row["state"]),
                "candle_hash": _hash(normalized_candles), "observed_at_ms": observed_at_ms,
                "session_boundary_ms": session_boundary_ms}
            result[strategy_id] = self._signal_positions(state)
        return result

    @staticmethod
    def _signal_positions(state: dict) -> dict:
        return {pair: dict(position, side=position["direction"])
                for pair, position in state["positions"].items()}

    def positions_by_strategy(self, *, timeframe: str = "5m") -> dict:
        timeframe = str(timeframe)
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported execution timeframe: {timeframe}")
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT strategy_id,state FROM strategy_sim_accounts
                WHERE (execution->>'leverage')::numeric=%s
                  AND COALESCE(execution->>'timeframe', '5m')=%s
                ORDER BY strategy_id""",
                                (EXECUTION_MODEL["leverage"], timeframe)).fetchall()
        rows = [row for row in rows if not _is_archived_taker_account(row["strategy_id"])]
        return {row["strategy_id"]: self._signal_positions(row["state"]) for row in rows}

    @staticmethod
    def _account(row: dict) -> dict:
        state = copy.deepcopy(row["state"])
        valuation = _mark(state)
        spec, capital = row["definition"], float(row["initial_capital"])
        execution = row["execution"]
        active = (execution.get("leverage") == EXECUTION_MODEL.get("leverage", 100)
                  and not str(row.get("strategy_id", "")).endswith(ARCHIVED_VARIANT_SUFFIX))
        count = state["closed_trade_count"]
        positions = list(state["positions"].values())
        return {"strategy_id": row["strategy_id"], "label": spec.get("label", spec.get("name", row["strategy_id"])),
                # Project immutable experiment metadata at the account root so
                # the API/UI can explain arm isolation and blocked data without
                # requiring callers to unpack the full frozen definition.
                "arm_id": spec.get("arm_id"), "variant_of": spec.get("variant_of"),
                "variant_label": spec.get("variant_label"),
                "data_status": spec.get("data_status"),
                "sample_population": spec.get("sample_population", "legacy_frozen_control"),
                "sample_quality_policy": {"cutoff_ms": SAMPLE_QUALITY_CUTOFF_MS,
                                          "timezone": "Asia/Shanghai",
                                          "pre_cutoff": "stop_definition_defect_period",
                                          "pre_cutoff_eligible": False},
                "initial_capital": capital, "currency": "USDT", "cash": state["cash"],
                **valuation, "realized_pnl": state["realized_pnl"], "fees_paid": state["fees_paid"],
                "total_return_ratio": valuation["equity"] / capital - 1,
                "max_drawdown_ratio": state["max_drawdown_ratio"],
                "open_positions": positions, "open_position_count": len(positions),
                "pending_order_count": len(state["pending_orders"]), "closed_trade_count": count,
                "win_rate": state["winning_trade_count"] / count if count else None,
                "rule_hash": row["rule_hash"], "execution_hash": row["execution_hash"],
                "definition": spec, "execution": execution, "warmup": state["warmup"],
                "status": state["status"] if active else "archived",
                "lifecycle": "active" if active else "archived",
                "last_candle_ms": state["last_candle_ms"],
                "last_observed_at_ms": state["last_observed_at_ms"],
                "observed_boundary_ms": state["observed_boundary_ms"],
                "performance_scope": "modeled_fees_only", "funding": "unknown", "slippage": "unknown"}

    def overview(self) -> dict:
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM strategy_sim_accounts ORDER BY strategy_id").fetchall()
        # Keep superseded accounts in the append-only database for audit, while
        # the active lab view exposes only the current maker-priced contract.
        active_leverage = EXECUTION_MODEL.get("leverage", 100)
        rows = [row for row in rows
                if (row.get("execution") or {}).get("leverage") == active_leverage
                and not _is_archived_taker_account(row.get("strategy_id", ""))]
        return {"accounts": [self._account(row) for row in rows], "execution": dict(EXECUTION_MODEL),
                "execution_model": dict(EXECUTION_MODEL),
                "scope": "forward_research", "limitations": list(LIMITATIONS)}

    def account_detail(self, strategy_id: str, limit: int = 100) -> dict | None:
        limit = max(1, min(1000, int(limit)))
        with self.store._connection() as conn:
            row = conn.execute("SELECT * FROM strategy_sim_accounts WHERE strategy_id=%s", (strategy_id,)).fetchone()
            if row is None:
                return None
            events = conn.execute("""SELECT event_id,event_type,candle_ms,observed_at_ms,rule_hash,
                data_fingerprint,payload FROM strategy_sim_events WHERE strategy_id=%s
                ORDER BY sequence_id DESC LIMIT %s""", (strategy_id, limit)).fetchall()
            trade_rows = conn.execute("""SELECT payload FROM strategy_sim_events
                WHERE strategy_id=%s AND event_type='trade_closed' ORDER BY sequence_id DESC LIMIT %s""",
                (strategy_id, limit)).fetchall()
            equity_rows = conn.execute("""SELECT payload FROM strategy_sim_events
                WHERE strategy_id=%s AND event_type='equity_snapshot' ORDER BY sequence_id DESC LIMIT %s""",
                (strategy_id, limit)).fetchall()
            # Latest state per order; a fill/cancel never overwrites its audit row.
            orders = conn.execute("""SELECT DISTINCT ON (payload->>'order_id') payload,sequence_id
                FROM strategy_sim_events WHERE strategy_id=%s
                AND event_type IN ('order_submitted','order_filled','order_cancelled','order_rejected')
                ORDER BY payload->>'order_id',sequence_id DESC""", (strategy_id,)).fetchall()
        orders.sort(key=lambda item: item["sequence_id"], reverse=True)
        return {"account": self._account(row), "orders": [item["payload"] for item in orders[:limit]],
                "trades": [dict(item["payload"], **sample_annotation(row["definition"], item["payload"]))
                           for item in trade_rows],
                "equity_curve": [item["payload"] for item in reversed(equity_rows)],
                "events": [dict(item, strategy_id=strategy_id,
                                **sample_annotation(row["definition"], item)) for item in events],
                "limitations": list(LIMITATIONS)}
