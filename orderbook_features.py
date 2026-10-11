"""Causal, time-weighted hourly book observations; this module never orders.

The input clock is the exchange event timestamp. A book must also have reached
the local database by the hour cutoff. The first accepted publication for a
method/pair/hour is immutable, including unknown observations.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any

METHOD_VERSION = "orderbook-hourly-v1"
HOUR_MS = 3_600_000
ALLOWED_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT")
METHOD_SPEC = {
    "timeframe": "1h", "depth_levels": [1, 5, 20],
    "mean_weighting": "event_time_hold_capped_at_3000ms",
    "receive_policy": "database_received_at_and_original_received_timestamp_lte_cutoff",
    "clock_policy": "event_and_local_receive_individually_lte_cutoff;clock_skew_audited",
    "max_hold_ms": 3_000, "min_coverage_ratio": 0.95,
    "min_minute_bins": 55, "max_gap_ms": 30_000,
    "max_age_at_close_ms": 3_000, "min_valid_snapshots": 100,
    "consistency_grid_ms": 1_000, "min_directional_comparisons": 30,
    "max_publication_lag_ms": 90_000,
    "depth_slope": "OLS_cumulative_quantity_share_on_relative_distance_from_mid",
    "source": "Binance_partial_depth20_snapshot_not_reconstructed_L2",
    "independence": "snapshot_count_is_not_independent_trade_or_research_sample_count",
}


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _encoded(value: Any) -> bytes:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _receive_ms(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("received_at must include timezone")
        value = value.timestamp() * 1000
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid timestamp")
    # Preserve PostgreSQL microseconds for the strict receive <= cutoff test.
    # Flooring would admit a snapshot received just after the decision cutoff.
    return number


def _event_ms(value: Any) -> int:
    result = int(_receive_ms(value))
    if float(value) != result:
        raise ValueError("event timestamp must be integer milliseconds")
    return result


def _validate_identity(pair: str, bar_open_ms: int, timeframe: str = "1h",
                       exchange: str = "binance", market: str = "futures") -> None:
    if pair not in ALLOWED_PAIRS or timeframe != "1h" or exchange != "binance" or market != "futures":
        raise ValueError("only Binance BTC/ETH/TRX futures hourly observations are supported")
    if _event_ms(bar_open_ms) % HOUR_MS:
        raise ValueError("bar_open_ms must be an aligned UTC hour")


def _slope(levels: list[tuple[float, float]], mid: float) -> float:
    total = sum(quantity for _, quantity in levels)
    xs, ys, running = [], [], 0.0
    for price, quantity in levels:
        running += quantity
        xs.append(abs(price - mid) / mid)
        ys.append(running / total)
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - xmean) ** 2 for x in xs)
    return sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) / denominator


def snapshot_features(row: dict[str, Any]) -> dict[str, float]:
    """Validate the full 20-level book, then derive scale-normalized features."""
    book = {}
    for side in ("bids", "asks"):
        raw = row.get(side)
        if not isinstance(raw, (list, tuple)) or len(raw) < 20:
            raise ValueError("insufficient_depth")
        levels = []
        for level in raw[:20]:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValueError("invalid_level")
            if isinstance(level[0], bool) or isinstance(level[1], bool):
                raise ValueError("nonpositive_or_nonfinite")
            try:
                price, quantity = float(level[0]), float(level[1])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("nonpositive_or_nonfinite") from exc
            if not math.isfinite(price) or not math.isfinite(quantity) or price <= 0 or quantity <= 0:
                raise ValueError("nonpositive_or_nonfinite")
            levels.append((price, quantity))
        prices = [price for price, _ in levels]
        if any((a <= b if side == "bids" else a >= b) for a, b in zip(prices, prices[1:])):
            raise ValueError("unsorted_or_duplicate_levels")
        book[side] = levels
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    if bid >= ask:
        raise ValueError("crossed_or_locked_book")
    mid = (bid + ask) / 2
    result = {"mid": mid, "relative_spread": (ask - bid) / mid}
    for n in (1, 5, 20):
        bvol, avol = (sum(q for _, q in book[side][:n]) for side in ("bids", "asks"))
        result[f"obi_{n}"] = (bvol - avol) / (bvol + avol)
    result["depth_slope_bid"] = _slope(book["bids"], mid)
    result["depth_slope_ask"] = _slope(book["asks"], mid)
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("nonfinite_feature")
    return result


def _sequence_sort(row: dict[str, Any]) -> tuple[int, int, str]:
    seq = str(row.get("last_update_id", ""))
    return (_event_ms(row["timestamp"]), len(seq), seq)


def compute_hour_features(rows: list[dict[str, Any]], *, pair: str, bar_open_ms: int,
                          observed_at_ms: int, forward_start_ms: int | None = None,
                          query_truncated: bool = False) -> dict[str, Any]:
    """Compute one closed hour. Historical preview is never a forward input.

    Missing or stale books produce unknown quality; feature means remain
    diagnostic only. Each hold interval is capped, so an old quote cannot
    falsely cover a collector outage. Consistency uses a 1-second grid.
    """
    _validate_identity(pair, bar_open_ms)
    cutoff_ms = bar_open_ms + HOUR_MS
    observed_at_ms = _event_ms(observed_at_ms)
    if observed_at_ms < cutoff_ms:
        raise ValueError("cannot compute a forming hour")
    if forward_start_ms is not None:
        _validate_identity(pair, forward_start_ms)
    rejected, digest, accepted, receive_skews = Counter(), hashlib.sha256(), [], []
    digest.update(_encoded({"method_version": METHOD_VERSION, "method_spec": METHOD_SPEC,
                            "pair": pair, "bar_open_ms": bar_open_ms, "cutoff_ms": cutoff_ms,
                            "query_truncated": query_truncated}))
    sortable = []
    for row in rows:
        try:
            _sequence_sort(row)
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected["invalid_event_timestamp"] += 1
            digest.update(_encoded(row))
            continue
        sortable.append(row)
    seen = set()
    for row in sorted(sortable, key=_sequence_sort):
        timestamp = _event_ms(row["timestamp"])
        digest.update(_encoded(row))
        if not bar_open_ms <= timestamp < cutoff_ms:
            rejected["outside_bar"] += 1
            continue
        try:
            received = _receive_ms(row.get("received_at"))
            original = row.get("received_timestamp", (row.get("extras") or {}).get("received_timestamp"))
            original = received if original is None else _receive_ms(original)
        except (ValueError, TypeError, OverflowError):
            rejected["receive_time_unknown"] += 1
            continue
        if max(received, original) > cutoff_ms:
            rejected["received_after_cutoff"] += 1
            continue
        # Exchange and collector clocks can differ. Causality requires both
        # observation clocks <= cutoff, not event <= local receive timestamp.
        receive_skews.append(timestamp - original)
        identity = (timestamp, str(row.get("last_update_id", "")))
        if not re.fullmatch(r"0|[1-9][0-9]*", identity[1]):
            rejected["sequence_unknown"] += 1
            continue
        if identity in seen:
            rejected["duplicate_snapshot"] += 1
            continue
        seen.add(identity)
        try:
            features = snapshot_features(row)
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            rejected[str(exc)] += 1
            continue
        accepted.append({"timestamp": timestamp, "last_update_id": identity[1],
                         "received_at_ms": received, "features": features})

    # More than one update can share an exchange millisecond: the highest
    # sequence is the state that begins at that instant; the others have zero
    # duration and cannot inflate the second-grid comparisons.
    states_by_ms = {row["timestamp"]: row for row in accepted}
    states = list(states_by_ms.values())
    timestamps = [row["timestamp"] for row in states]
    weighted, covered_ms, minute_ids = Counter(), 0, set()
    for i, row in enumerate(states):
        start = row["timestamp"]
        following = timestamps[i + 1] if i + 1 < len(states) else cutoff_ms
        end = min(following, start + METHOD_SPEC["max_hold_ms"], cutoff_ms)
        duration = max(0, end - start)
        if not duration:
            continue
        covered_ms += duration
        for name, value in row["features"].items():
            weighted[name] += value * duration
        minute_ids.update(range((start - bar_open_ms) // 60_000, (end - 1 - bar_open_ms) // 60_000 + 1))
    gaps = ([timestamps[0] - bar_open_ms] +
            [b - a for a, b in zip(timestamps, timestamps[1:])] +
            [cutoff_ms - timestamps[-1]]) if timestamps else [HOUR_MS]
    age = cutoff_ms - timestamps[-1] if timestamps else HOUR_MS
    comparisons, consistent, grid_seconds = 0, 0, 0
    previous = None
    for boundary in range(bar_open_ms, cutoff_ms, METHOD_SPEC["consistency_grid_ms"]):
        idx = bisect_right(timestamps, boundary) - 1
        state = states[idx] if idx >= 0 and boundary - timestamps[idx] <= METHOD_SPEC["max_hold_ms"] else None
        if state is not None:
            grid_seconds += 1
        if previous is not None and state is not None:
            move = state["features"]["mid"] - previous["features"]["mid"]
            obi = previous["features"]["obi_5"]
            if move and obi:
                comparisons += 1
                consistent += int(move * obi > 0)
        previous = state
    coverage = covered_ms / HOUR_MS
    reasons = []
    checks = ((query_truncated, "query_truncated"),
              (len(states) < METHOD_SPEC["min_valid_snapshots"], "insufficient_snapshots"),
              (coverage < METHOD_SPEC["min_coverage_ratio"], "insufficient_time_coverage"),
              (len(minute_ids) < METHOD_SPEC["min_minute_bins"], "insufficient_minute_coverage"),
              (max(gaps) > METHOD_SPEC["max_gap_ms"], "collector_gap"),
              (age > METHOD_SPEC["max_age_at_close_ms"], "stale_at_close"))
    for failed, reason in checks:
        if failed:
            reasons.append(reason)
    feature_values = {}
    for name in ("obi_1", "obi_5", "obi_20", "relative_spread", "depth_slope_bid", "depth_slope_ask", "mid"):
        feature_values[f"{name}_time_mean"] = weighted[name] / covered_ms if covered_ms else None
        feature_values[f"{name}_last"] = states[-1]["features"][name] if states else None
    feature_values["lagged_obi_mid_directional_consistency"] = (
        consistent / comparisons if comparisons >= METHOD_SPEC["min_directional_comparisons"] else None)
    source_hash = digest.hexdigest()
    quality = {
        "eligible": not reasons, "status": "ready" if not reasons else "unknown", "reasons": reasons,
        "selected_rows": len(rows), "valid_snapshots": len(states), "accepted_updates": len(accepted),
        "rejected_by_reason": dict(sorted(rejected.items())), "query_truncated": query_truncated,
        "covered_seconds": covered_ms / 1000, "coverage_ratio": coverage,
        "minute_bins": len(minute_ids), "covered_minute_ids": sorted(minute_ids),
        "largest_gap_ms": max(gaps), "age_at_close_ms": age, "stale": age > 3000,
        "grid_seconds": grid_seconds, "directional_comparisons": comparisons,
        "independent_research_samples": 0,
        "positive_event_receive_skew_count": sum(value > 0 for value in receive_skews),
        "max_positive_event_receive_skew_ms": max([0] + receive_skews),
    }
    record = {
        "method_version": METHOD_VERSION, "method_spec": deepcopy(METHOD_SPEC),
        "exchange": "binance", "market": "futures", "pair": pair, "timeframe": "1h",
        "candle_ms": bar_open_ms, "bar_open_ms": bar_open_ms, "cutoff_ms": cutoff_ms,
        "received_cutoff_ms": cutoff_ms, "observed_at_ms": observed_at_ms,
        "forward_start_ms": forward_start_ms,
        "forward_eligible": forward_start_ms is not None and bar_open_ms >= forward_start_ms,
        "source_hash": source_hash, "quality": quality, "features": feature_values,
        "input_sequence": {
            "first_event_ms": timestamps[0] if timestamps else None,
            "last_event_ms": timestamps[-1] if timestamps else None,
            "first_update_id": states[0]["last_update_id"] if states else None,
            "last_update_id": states[-1]["last_update_id"] if states else None,
        },
        "research_status": "forward_hypothesis_unvalidated",
    }
    record["feature_key"] = feature_document_key(record)
    return record


def selector_document_key(pair: str, bar_open_ms: int, *, timeframe: str = "1h",
                          exchange: str = "binance", market: str = "futures",
                          method_version: str = METHOD_VERSION) -> str:
    _validate_identity(pair, bar_open_ms, timeframe, exchange, market)
    if method_version != METHOD_VERSION:
        raise ValueError("unsupported feature method version")
    symbol = pair.split(":")[0].replace("/", "_")
    return f"orderbook_features/{method_version}/{exchange}/{market}/{symbol}/{timeframe}/{bar_open_ms}/accepted.json"


def feature_document_key(record: dict[str, Any]) -> str:
    selector = selector_document_key(record["pair"], record["candle_ms"],
                                     method_version=record["method_version"])
    return selector.removesuffix("accepted.json") + record["source_hash"] + ".json"


def valid_record_identity(record: dict[str, Any], *, require_forward: bool = True) -> bool:
    """Recheck derived metadata rather than trusting a caller's eligibility flag."""
    try:
        opening = _event_ms(record["candle_ms"])
        _validate_identity(record["pair"], opening, record["timeframe"], record["exchange"], record["market"])
        cutoff = opening + HOUR_MS
        if (record["method_version"] != METHOD_VERSION or record.get("method_spec") != METHOD_SPEC or
                record.get("bar_open_ms") != opening or record.get("cutoff_ms") != cutoff or
                record.get("received_cutoff_ms") != cutoff or
                not re.fullmatch(r"[0-9a-f]{64}", record.get("source_hash", "")) or
                record.get("feature_key") != feature_document_key(record)):
            return False
        observed = _event_ms(record["observed_at_ms"])
        if observed < cutoff:
            return False
        if require_forward:
            forward_start = _event_ms(record["forward_start_ms"])
            if forward_start % HOUR_MS or opening < forward_start or record.get("forward_eligible") is not True:
                return False
        return True
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def load_published_feature(store: Any, pair: str, bar_open_ms: int, *, timeframe: str = "1h",
                           exchange: str = "binance", market: str = "futures",
                           method_version: str = METHOD_VERSION) -> dict[str, Any] | None:
    """One indexed selector read followed by its immutable full evidence read."""
    key = selector_document_key(pair, bar_open_ms, timeframe=timeframe, exchange=exchange,
                                market=market, method_version=method_version)
    selector = store.get_document(key)
    if not isinstance(selector, dict) or not isinstance(selector.get("feature_key"), str):
        return None
    record = store.get_document(selector["feature_key"])
    if not isinstance(record, dict):
        return None
    if (not valid_record_identity(record) or
            record.get("pair") != pair or record.get("candle_ms") != bar_open_ms or
            record.get("method_version") != method_version or record.get("feature_key") != selector["feature_key"] or
            record.get("source_hash") != selector.get("source_hash") or
            selector.get("method_version") != method_version or
            record.get("published_at_ms") != selector.get("published_at_ms")):
        return None
    try:
        if not record["cutoff_ms"] <= record["observed_at_ms"] <= _event_ms(record["published_at_ms"]) <= record["cutoff_ms"] + 90_000:
            return None
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    return record
