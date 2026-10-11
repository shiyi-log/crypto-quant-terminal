"""Independent, causal second-level research inputs and paper controls.

This module neither orders nor writes a database. A decision consumes only the
latest received Binance futures depth snapshot; it creates no candle or frozen
strategy event. Paper prices are subsequent Demo quotes, never actual fills.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any

from orderbook_features import ALLOWED_PAIRS, snapshot_features

METHOD_VERSION = "demo-second-level-obi-v1"
_POLICY = {
    "method_version": METHOD_VERSION,
    "research_status": "forward_research_hypothesis",
    "input": "latest_received_Binance_futures_partial_depth20",
    "poll_interval_ms": 1_000,
    "max_source_age_ms": 5_000,
    "entry_abs_obi5": 0.9,
    "rearm_abs_obi5": 0.3,
    "hold_ms": 10_000,
    "maker_entry_ttl_ms": 3_000,
    "arm_schedule": [
        {"arm": "maker", "maker_distance_bp": 0},
        {"arm": "taker", "maker_distance_bp": None},
        {"arm": "maker", "maker_distance_bp": 1},
        {"arm": "taker", "maker_distance_bp": None},
        {"arm": "maker", "maker_distance_bp": 2},
        {"arm": "taker", "maker_distance_bp": None},
        {"arm": "maker", "maker_distance_bp": 5},
        {"arm": "taker", "maker_distance_bp": None},
    ],
    "assignment": "eligible_signal_index_mod_8_independent_of_results",
    "replay_policy": "latest_only_no_duplicate_backward_or_stale_inputs",
    "paper_model": "next_Demo_quote_crossing_proxy_no_depth_or_queue",
    "paper_max_horizon_lag_ms": 5_000,
    "paper_quote_clock_window_ms": 5_000,
    "paper_quote_clock_policy": "known_exchange_time_within_5000ms_of_local_receipt_and_nonbackward;missing_unknown",
    "maker_paper_fill": "unknown_no_quote_touch_implies_fill",
    "live_launch_allowed": False,
}


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("received_at_requires_timezone")
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite_input")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported_snapshot_value")


def _hash(value: Any) -> str:
    encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


POLICY_HASH = _hash(_POLICY)


def frozen_policy() -> dict[str, Any]:
    """A copy prevents callers from mutating the already-hashed rule definition."""
    return deepcopy(_POLICY)


def _integer_ms(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"invalid_{field}") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError(f"invalid_{field}")
    return int(number)


def _received_ms(value: Any, field: str) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"invalid_{field}")
        value = value.timestamp() * 1_000
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field}")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"invalid_{field}") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid_{field}")
    # Keep microseconds: flooring would admit receipts after the decision.
    return number


def _unknown(pair: str, now_ms: int, reason: str) -> dict[str, Any]:
    return {"quality": "unknown", "reason": reason, "pair": pair,
            "decision_ms": now_ms, "source": _POLICY["input"], "signal": None}


def _normalize_snapshot(row: dict[str, Any], *, pair: str, now_ms: int,
                        max_age_ms: int) -> dict[str, Any]:
    try:
        event = _integer_ms(row.get("timestamp"), "source_event_ms")
        database_received = _received_ms(row.get("received_at"), "database_received_at_ms")
        extras = row.get("extras") or {}
        if not isinstance(extras, dict):
            raise ValueError("invalid_extras")
        original_value = row.get("received_timestamp", extras.get("received_timestamp"))
        original_received = (None if original_value is None else
                             _received_ms(original_value, "original_received_at_ms"))
        update_id = str(row.get("last_update_id", ""))
        if not re.fullmatch(r"0|[1-9][0-9]*", update_id):
            raise ValueError("invalid_last_update_id")
        if event > now_ms or database_received > now_ms or (
                original_received is not None and original_received > now_ms):
            return _unknown(pair, now_ms, "snapshot_not_yet_observable")
        if now_ms - event > max_age_ms or now_ms - database_received > max_age_ms or (
                original_received is not None and now_ms - original_received > max_age_ms):
            return _unknown(pair, now_ms, "stale_snapshot")
        features = snapshot_features(row)
        raw_snapshot = {
            "exchange": "binance", "market": "futures", "pair": pair,
            "timestamp": event, "last_update_id": update_id,
            "bids": deepcopy(row["bids"]), "asks": deepcopy(row["asks"]),
            "extras": deepcopy(extras), "received_at": row["received_at"],
            "received_timestamp": original_value,
        }
        digest = _hash(raw_snapshot)
    except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError) as exc:
        return _unknown(pair, now_ms, str(exc) if isinstance(exc, ValueError) else "invalid_snapshot")
    return {
        "quality": "valid", "reason": "fresh_received_snapshot", "pair": pair,
        "source": _POLICY["input"], "source_exchange": "binance", "source_market": "futures",
        "source_event_ms": event, "database_received_at_ms": database_received,
        "original_received_at_ms": original_received,
        "original_receive_time_status": "unknown" if original_received is None else "known",
        "decision_ms": now_ms, "source_age_ms": now_ms - event,
        "last_update_id": update_id, "raw_snapshot_hash": digest,
        "source_best_bid": str(row["bids"][0][0]),
        "source_best_ask": str(row["asks"][0][0]), "features": features,
    }


def latest_closed_snapshot(data_store: Any, pair: str, now_ms: int,
                           max_age_ms: int = 5_000) -> dict[str, Any]:
    """Read one latest causally available snapshot with the existing pair index.

    ``closed`` means the snapshot had arrived by the supplied decision clock;
    there is no second-level candle. Delayed originals are rejected, never
    replaced with a fabricated receive time or an older quote search.
    """
    if pair not in ALLOWED_PAIRS:
        raise ValueError("only_BTC_ETH_TRX_Binance_futures_supported")
    now_ms = _integer_ms(now_ms, "decision_ms")
    max_age_ms = _integer_ms(max_age_ms, "max_age_ms")
    if not 1 <= max_age_ms <= _POLICY["max_source_age_ms"]:
        raise ValueError("source_age_must_be_between_1_and_5000ms")
    with data_store._connection() as conn:
        row = conn.execute("""SELECT timestamp,last_update_id,bids,asks,extras,received_at
            FROM orderbooks WHERE exchange=%s AND market=%s AND pair=%s
            AND timestamp <= %s AND received_at <= to_timestamp(%s / 1000.0)
            ORDER BY timestamp DESC,length(last_update_id) DESC,last_update_id DESC LIMIT 1""",
            ("binance", "futures", pair, now_ms, now_ms)).fetchone()
    if row is None:
        return _unknown(pair, now_ms, "no_observable_snapshot")
    return _normalize_snapshot(row, pair=pair, now_ms=now_ms, max_age_ms=max_age_ms)


class FrozenSecondLevelSignalSource:
    """One-second latest-only OBI evaluator; caller persists the returned state.

    Recovery state contains the high-water mark and next arm index. A caller
    may pass ``started_ms`` to exclude snapshots preceding this experiment.
    """
    def __init__(self, data_store: Any = None, pair: str = "BTC/USDT:USDT", *,
                 state: dict[str, Any] | None = None, started_ms: int | None = None):
        if pair not in ALLOWED_PAIRS:
            raise ValueError("only_BTC_ETH_TRX_Binance_futures_supported")
        self.store, self.pair = data_store, pair
        recovery_start = None if state is None else state.get("started_ms")
        if started_ms is None:
            started_ms = recovery_start
        elif state is not None and _integer_ms(started_ms, "started_ms") != recovery_start:
            raise ValueError("signal_state_start_mismatch")
        self.started_ms = None if started_ms is None else _integer_ms(started_ms, "started_ms")
        self._state = {
            "method_version": METHOD_VERSION, "policy_hash": POLICY_HASH, "pair": pair,
            "started_ms": self.started_ms,
            "armed": True, "eligible_signal_count": 0, "last_poll_ms": None,
            "last_source_event_ms": None, "last_update_id": None, "last_raw_snapshot_hash": None,
            "decision_count": 0, "consumed_snapshot_count": 0, "reason_counts": {},
            "first_decision_ms": None, "latest_decision_ms": None,
        }
        if state is not None:
            self._restore(state)
        self.last_decision: dict[str, Any] | None = None

    def _restore(self, state: dict[str, Any]) -> None:
        if set(state) != set(self._state) or any(state.get(key) != value for key, value in (
                ("method_version", METHOD_VERSION), ("policy_hash", POLICY_HASH),
                ("pair", self.pair), ("started_ms", self.started_ms))):
            raise ValueError("signal_state_identity_mismatch")
        if not isinstance(state["armed"], bool):
            raise ValueError("invalid_signal_state")
        restored = deepcopy(state)
        for name in ("eligible_signal_count", "decision_count", "consumed_snapshot_count"):
            restored[name] = _integer_ms(state[name], name)
        reasons = state["reason_counts"]
        if not isinstance(reasons, dict) or any(not isinstance(reason, str) for reason in reasons):
            raise ValueError("invalid_reason_counts")
        restored["reason_counts"] = {reason: _integer_ms(count, "reason_count") for reason, count in reasons.items()}
        if sum(restored["reason_counts"].values()) != restored["decision_count"]:
            raise ValueError("invalid_reason_counts")
        for name in ("last_poll_ms", "last_source_event_ms", "first_decision_ms", "latest_decision_ms"):
            if state[name] is not None:
                restored[name] = _integer_ms(state[name], name)
        marker = state["last_update_id"]
        digest = state["last_raw_snapshot_hash"]
        if (marker is None) != (state["last_source_event_ms"] is None) or (marker is None) != (digest is None):
            raise ValueError("invalid_signal_state")
        if marker is not None and (not isinstance(marker, str) or
                                  not re.fullmatch(r"0|[1-9][0-9]*", marker)):
            raise ValueError("invalid_signal_state")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid_signal_state")
        self._state = restored

    @property
    def state(self) -> dict[str, Any]:
        return deepcopy(self._state)

    @property
    def metrics(self) -> dict[str, Any]:
        """Actual observed triggers/counts; no independent-sample assertion."""
        first, last = self._state["first_decision_ms"], self._state["latest_decision_ms"]
        elapsed = None if first is None or last is None else max(0, last - first)
        rate = (None if not elapsed else self._state["eligible_signal_count"] * 60_000 / elapsed)
        return {
            "decision_count": self._state["decision_count"],
            "consumed_snapshot_count": self._state["consumed_snapshot_count"],
            "eligible_signal_count": self._state["eligible_signal_count"],
            "reason_counts": deepcopy(self._state["reason_counts"]),
            "first_decision_ms": first, "latest_decision_ms": last,
            "observed_elapsed_ms": elapsed, "observed_trigger_rate_per_minute": rate,
            "trigger_count_is_independent_completed_trade_count": False,
        }

    def _decision(self, reason: str, signal: dict[str, Any] | None = None,
                  observation: dict[str, Any] | None = None) -> dict[str, Any]:
        self._state["decision_count"] += 1
        self._state["reason_counts"][reason] = self._state["reason_counts"].get(reason, 0) + 1
        if self._state["first_decision_ms"] is None:
            self._state["first_decision_ms"] = self._decision_clock_ms
        last = self._state["latest_decision_ms"]
        self._state["latest_decision_ms"] = (self._decision_clock_ms if last is None else
                                             max(last, self._decision_clock_ms))
        self.last_decision = {"reason": reason, "signal": signal, "state": self.state,
                              "decision_ms": self._decision_clock_ms, "metrics": self.metrics,
                              "observation": deepcopy(observation)}
        return deepcopy(self.last_decision)

    def latest_signal(self, now_ms: int) -> dict[str, Any] | None:
        if self.store is None:
            raise ValueError("data_store_is_required_for_latest_signal")
        observation = latest_closed_snapshot(self.store, self.pair, now_ms)
        return self.observe(observation, decision_ms=now_ms)["signal"]

    def __call__(self, now_ms: int) -> dict[str, Any] | None:
        return self.latest_signal(now_ms)

    def observe(self, observation: dict[str, Any], *, decision_ms: int | None = None) -> dict[str, Any]:
        now = _integer_ms(observation.get("decision_ms") if decision_ms is None else decision_ms,
                          "decision_ms")
        self._decision_clock_ms = now
        previous_poll = self._state["last_poll_ms"]
        if previous_poll is not None and now < previous_poll:
            return self._decision("backward_decision_clock", observation=observation)
        if previous_poll is not None and now - previous_poll < _POLICY["poll_interval_ms"]:
            return self._decision("poll_interval_not_elapsed", observation=observation)
        self._state["last_poll_ms"] = now
        if observation.get("pair") != self.pair:
            return self._decision("snapshot_pair_mismatch", observation=observation)
        if observation.get("quality") != "valid":
            return self._decision(observation.get("reason", "snapshot_quality_unknown"), observation=observation)
        try:
            event = _integer_ms(observation.get("source_event_ms"), "source_event_ms")
            received = _received_ms(observation.get("database_received_at_ms"), "database_received_at_ms")
            original_value = observation.get("original_received_at_ms")
            original = None if original_value is None else _received_ms(original_value, "original_received_at_ms")
            update_id = str(observation.get("last_update_id", ""))
            digest = observation.get("raw_snapshot_hash")
            if not re.fullmatch(r"0|[1-9][0-9]*", update_id) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid_snapshot_identity")
            if any(value > now for value in (event, received) + (() if original is None else (original,))):
                raise ValueError("snapshot_not_yet_observable")
            if any(now - value > _POLICY["max_source_age_ms"] for value in
                   (event, received) + (() if original is None else (original,))):
                raise ValueError("stale_snapshot")
            if self.started_ms is not None and event < self.started_ms:
                raise ValueError("snapshot_precedes_session")
            obi = float(observation["features"]["obi_5"])
            if not math.isfinite(obi) or abs(obi) > 1:
                raise ValueError("invalid_obi5")
            for feature in ("obi_1", "obi_20", "relative_spread", "mid"):
                value = float(observation["features"][feature])
                if not math.isfinite(value) or (feature.startswith("obi_") and abs(value) > 1):
                    raise ValueError("invalid_snapshot_features")
                if feature in {"relative_spread", "mid"} and value <= 0:
                    raise ValueError("invalid_snapshot_features")
            bid = _positive_decimal(observation.get("source_best_bid"), "source_best_bid")
            ask = _positive_decimal(observation.get("source_best_ask"), "source_best_ask")
            if bid >= ask:
                raise ValueError("invalid_snapshot_prices")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return self._decision(str(exc) if isinstance(exc, ValueError) else "invalid_snapshot",
                                  observation=observation)
        last_event, last_id = self._state["last_source_event_ms"], self._state["last_update_id"]
        if last_event is not None:
            if event == last_event and update_id == last_id:
                reason = ("duplicate_snapshot" if digest == self._state["last_raw_snapshot_hash"]
                          else "duplicate_snapshot_content_conflict")
                return self._decision(reason, observation=observation)
            if event < last_event or int(update_id) <= int(last_id):
                return self._decision("backward_snapshot", observation=observation)
        self._state.update(last_source_event_ms=event, last_update_id=update_id, last_raw_snapshot_hash=digest)
        self._state["consumed_snapshot_count"] += 1
        if not self._state["armed"]:
            if abs(obi) <= _POLICY["rearm_abs_obi5"]:
                self._state["armed"] = True
                return self._decision("obi_rearmed", observation=observation)
            return self._decision("waiting_obi_rearm", observation=observation)
        if abs(obi) < _POLICY["entry_abs_obi5"]:
            return self._decision("obi_below_entry_threshold", observation=observation)
        index = self._state["eligible_signal_count"]
        assignment = deepcopy(_POLICY["arm_schedule"][index % len(_POLICY["arm_schedule"])])
        side = "buy" if obi > 0 else "sell"
        signal_id = "hft-" + _hash({"policy_hash": POLICY_HASH, "pair": self.pair,
                                   "event_ms": event, "update_id": update_id,
                                   "input_fingerprint": digest})[:40]
        signal = {
            "signal_id": signal_id, "strategy_id": "second_level_obi_research",
            "pair": self.pair, "side": side, "direction": 1 if obi > 0 else -1,
            "signal_ms": now, "decision_ms": now, "method_version": METHOD_VERSION,
            "policy_hash": POLICY_HASH, "rule_hash": POLICY_HASH,
            "input_fingerprint": digest, "raw_snapshot_hash": digest,
            "source_event_ms": event, "database_received_at_ms": received,
            "original_received_at_ms": original, "last_update_id": update_id,
            "source": observation.get("source", _POLICY["input"]),
            "source_bid": observation["source_best_bid"], "source_ask": observation["source_best_ask"],
            "source_best_bid": observation["source_best_bid"], "source_best_ask": observation["source_best_ask"],
            "features": deepcopy(observation["features"]),
            "obi_1": observation["features"]["obi_1"], "obi_5": obi,
            "obi_20": observation["features"]["obi_20"],
            "relative_spread": observation["features"]["relative_spread"],
            "eligible_signal_index": index, **assignment,
            "hold_ms": _POLICY["hold_ms"], "maker_entry_ttl_ms": _POLICY["maker_entry_ttl_ms"],
            "research_status": "forward_research_hypothesis", "live_launch_allowed": False,
        }
        self._state["eligible_signal_count"] += 1
        self._state["armed"] = False
        return self._decision("obi_entry_signal", signal, observation)


def _positive_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field}")
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError(f"invalid_{field}") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"invalid_{field}")
    return number


def _quoted_rate(quotes: dict[str, Any] | None, name: str) -> Decimal | None:
    if not isinstance(quotes, dict) or quotes.get(name) is None:
        return None
    value = quotes[name]
    if isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return rate if rate.is_finite() and 0 <= rate <= 1 else None


class SecondLevelPaperControl:
    """Same-signal Demo quote benchmark; never infer a maker queue fill.

    Crossing buy uses the first subsequent ask and exits at the horizon bid;
    crossing sell uses the subsequent bid and horizon ask. Hypothetical fees
    are quoted account taker rates, separately labeled from actual commissions.
    """
    def __init__(self, signal: dict[str, Any], quantity: Any,
                 fee_quotes: dict[str, Any] | None = None):
        if signal.get("policy_hash") != POLICY_HASH or signal.get("pair") not in ALLOWED_PAIRS:
            raise ValueError("paper_signal_identity_mismatch")
        if signal.get("side") not in {"buy", "sell"}:
            raise ValueError("paper_signal_side_invalid")
        self.signal = deepcopy(signal)
        self.quantity = _positive_decimal(quantity, "paper_quantity")
        self.decision_ms = _integer_ms(signal.get("decision_ms"), "decision_ms")
        self.horizon_ms = self.decision_ms + _POLICY["hold_ms"]
        self.fee_quotes = deepcopy(fee_quotes)
        self._entry: dict[str, Any] | None = None
        self._horizon: dict[str, Any] | None = None
        self._last_observed_ms: int | None = None
        self._last_request_started_ms: int | None = None
        self._last_exchange_ms: int | None = None
        self._finished_reason: str | None = None
        self.last_reason = "waiting_next_Demo_quote"

    @property
    def terminal(self) -> bool:
        return self._horizon is not None or self._finished_reason is not None

    @property
    def result(self) -> dict[str, Any]:
        sign = Decimal(1 if self.signal["side"] == "buy" else -1)
        entry = None if self._entry is None else Decimal(self._entry["ask"] if sign > 0 else self._entry["bid"])
        exit_price = None if self._horizon is None else Decimal(self._horizon["bid"] if sign > 0 else self._horizon["ask"])
        gross = None if entry is None or exit_price is None else sign * self.quantity * (exit_price - entry)
        rate = _quoted_rate(self.fee_quotes, "takerCommissionRate")
        hypothetical_fees = (None if gross is None or rate is None else
                             self.quantity * (entry + exit_price) * rate)
        entry_mid = None if self._entry is None else Decimal(self._entry["mid"])
        horizon_mid = None if self._horizon is None else Decimal(self._horizon["mid"])
        mid_change = (None if horizon_mid is None or entry_mid is None else
                      sign * (horizon_mid / entry_mid - 1) * 10_000)
        maker_limit = None
        if self._entry is not None and self.signal.get("arm") == "maker":
            distance = Decimal(str(self.signal["maker_distance_bp"])) / 10_000
            maker_limit = (Decimal(self._entry["bid"]) * (1 - distance) if sign > 0 else
                           Decimal(self._entry["ask"]) * (1 + distance))
        def show(value):
            return None if value is None else format(value, "f")

        return {
            "signal_id": self.signal["signal_id"], "input_fingerprint": self.signal["input_fingerprint"],
            "policy_hash": POLICY_HASH, "pair": self.signal["pair"], "side": self.signal["side"],
            "assigned_arm": self.signal.get("arm"), "maker_distance_bp": self.signal.get("maker_distance_bp"),
            "status": ("reference_complete" if self._horizon is not None else
                       "reference_unavailable" if self._finished_reason is not None else "pending_reference"),
            "last_reason": self.last_reason, "terminal": self.terminal,
            "decision_ms": self.decision_ms, "reference_horizon_ms": self.horizon_ms,
            "quantity": format(self.quantity, "f"), "entry_quote": deepcopy(self._entry),
            "horizon_quote": deepcopy(self._horizon),
            "paper_model": _POLICY["paper_model"], "price_source": "binance_demo_bookTicker",
            "actual_fill": False, "crossing_fill_assumption": "top_of_book_no_depth_or_latency_model",
            "maker_fill_status": "unknown", "maker_limit_price_unrounded": show(maker_limit),
            "maker_queue_or_nonfill_probability": None,
            "hypothetical_crossing_entry_price": show(entry),
            "hypothetical_crossing_exit_price": show(exit_price),
            "hypothetical_crossing_gross_pnl_usdt": show(gross),
            "quoted_taker_rate_per_side": show(rate),
            "quoted_maker_rate_per_side": show(_quoted_rate(self.fee_quotes, "makerCommissionRate")),
            "hypothetical_crossing_fee_usdt": show(hypothetical_fees),
            "hypothetical_crossing_net_pnl_usdt": show(None if hypothetical_fees is None else gross - hypothetical_fees),
            "hypothetical_fee_source": "demo_account_commission_rate_quote_not_fill",
            "actual_commission": None, "signed_reference_mid_change_bp": show(mid_change),
            "funding_cost": None, "funding_included": False,
            "clock_basis": "local_HTTP_request_and_receipt_time_exchange_time_preserved_separately",
            "quote_freshness_policy": _POLICY["paper_quote_clock_policy"],
            "max_reference_horizon_lag_ms": _POLICY["paper_max_horizon_lag_ms"],
            "research_status": "forward_research_hypothesis", "live_launch_allowed": False,
        }

    def observe_book_ticker(self, quote: dict[str, Any], *, observed_ms: int,
                            request_started_ms: int) -> dict[str, Any]:
        if self.terminal:
            return self.result
        observed = _integer_ms(observed_ms, "quote_observed_ms")
        started = _integer_ms(request_started_ms, "quote_request_started_ms")
        if started <= self.decision_ms or observed < started:
            self.last_reason = "quote_not_strictly_after_decision"
            return self.result
        if ((self._last_observed_ms is not None and observed <= self._last_observed_ms) or
                (self._last_request_started_ms is not None and started <= self._last_request_started_ms)):
            self.last_reason = "duplicate_or_backward_quote_observation"
            return self.result
        if observed > self.horizon_ms + _POLICY["paper_max_horizon_lag_ms"]:
            self.last_reason = "horizon_reference_window_missed"
            self._finished_reason = self.last_reason
            return self.result
        if quote.get("pair") != self.signal["pair"]:
            self.last_reason = "quote_pair_mismatch"
            return self.result
        try:
            bid = _positive_decimal(quote.get("bid_price"), "quote_bid")
            ask = _positive_decimal(quote.get("ask_price"), "quote_ask")
            if bid >= ask:
                raise ValueError("quote_crossed_or_locked")
            exchange_ms = quote.get("timestamp_ms")
            if exchange_ms is not None:
                exchange_ms = _integer_ms(exchange_ms, "quote_exchange_ms")
                if abs(observed - exchange_ms) > _POLICY["paper_quote_clock_window_ms"]:
                    raise ValueError("quote_exchange_clock_outside_frozen_window")
                if self._last_exchange_ms is not None and exchange_ms < self._last_exchange_ms:
                    raise ValueError("backward_quote_exchange_time")
        except ValueError as exc:
            self.last_reason = str(exc)
            return self.result
        evidence = {
            "bid": format(bid, "f"), "ask": format(ask, "f"), "mid": format((bid + ask) / 2, "f"),
            "request_started_ms": started, "observed_ms": observed,
            "exchange_event_ms": exchange_ms,
            "exchange_time_status": "unknown" if exchange_ms is None else "known",
            "exchange_freshness_status": "unknown" if exchange_ms is None else "within_frozen_cross_clock_window",
            "local_receipt_minus_exchange_ms": None if exchange_ms is None else observed - exchange_ms,
            "request_latency_ms": observed - started,
            "raw_quote_hash": _hash(quote.get("raw", quote)),
        }
        self._last_observed_ms, self._last_request_started_ms = observed, started
        if exchange_ms is not None:
            self._last_exchange_ms = exchange_ms
        if self._entry is None:
            if observed >= self.horizon_ms:
                self.last_reason = "no_entry_quote_before_horizon"
                self._finished_reason = self.last_reason
                return self.result
            self._entry = evidence
            self.last_reason = "entry_quote_recorded_maker_fill_unknown"
        elif started >= self.horizon_ms:
            evidence["horizon_lag_ms"] = observed - self.horizon_ms
            self._horizon = evidence
            self.last_reason = "horizon_reference_recorded_maker_fill_unknown"
        else:
            self.last_reason = "waiting_horizon_reference"
        return self.result
