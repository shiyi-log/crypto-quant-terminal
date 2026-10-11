"""Online close-to-close price-spike labels for live BTC/ETH 5m trials."""

from __future__ import annotations

import hashlib
import json
import math
import time
from decimal import Decimal, localcontext
from typing import Any


BAR_INTERVAL_MS = 5 * 60 * 1000
FORWARD_BARS = 3
PROFILE_RADIUS = 12
PRICE_SPIKE_THRESHOLD = 0.015
PRICE_SPIKE_METHOD_VERSION = "orderflow-price-spike-close3-v3"


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def mature_price_spike_observations(
    store: Any,
    *,
    exchange: str = "binance",
    candle_type: str = "futures",
    limit: int | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Append outcomes and complete profiles using exact online candle windows.

    This function only labels candidates already persisted by the live stream.
    It never creates candidates from archived candles. Missing bars become an
    invalid result once the relevant window has passed; they are never skipped.
    """
    candidates_reader = getattr(store, "read_pending_price_candidates", None)
    profiles_reader = getattr(store, "read_pending_price_profiles", None)
    result_writer = getattr(store, "append_price_candidate_result", None)
    candle_reader = getattr(store, "get_candles", None)
    observation_reader = getattr(store, "read_price_observations", None)
    if not all(callable(value) for value in (
        candidates_reader, profiles_reader, result_writer, candle_reader,
        observation_reader,
    )):
        return []
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    appended: list[dict[str, Any]] = []
    pending = candidates_reader(limit=limit)
    for row in pending or []:
        event_id = row.get("event_id")
        observation = row.get("observation")
        if not event_id or not isinstance(observation, dict):
            continue
        timestamp = int(row.get("timestamp", observation.get("timestamp", -1)))
        pair = row.get("pair")
        if not pair or timestamp < 0:
            continue
        target = timestamp + FORWARD_BARS * BAR_INTERVAL_MS
        candles = candle_reader(
            exchange, row.get("market", "futures"), pair, "5m", candle_type,
            limit=None, start=timestamp + BAR_INTERVAL_MS, end=target,
        ) or []
        by_time = {}
        for item in candles:
            if not isinstance(item, dict) or item.get("timestamp") is None:
                continue
            extras = item.get("extras")
            if not isinstance(extras, dict) or extras.get("live_event_timestamp") is None:
                continue
            by_time[int(item["timestamp"])] = item
        expected = [timestamp + step * BAR_INTERVAL_MS
                    for step in range(1, FORWARD_BARS + 1)]
        close0 = _number(observation.get("close"))
        if close0 is None or close0 <= 0:
            continue
        if all(value in by_time for value in expected):
            close3 = _number(by_time[expected[-1]].get("close"))
            if close3 is None or close3 <= 0:
                direction, return_pct, status = "none", None, "invalid"
            else:
                return_pct = close3 / close0 - 1
                # 十进制价格精确包含 ±1.5% 边界，避免 float 相减误差漏判。
                # 展示收益仍保留原公式；分类不通过 epsilon 扩大阈值。
                with localcontext() as context:
                    context.prec = 40
                    anchor = Decimal(str(close0))
                    future = Decimal(str(close3))
                    threshold = Decimal(str(PRICE_SPIKE_THRESHOLD))
                    direction = (
                        "up" if future >= anchor * (1 + threshold) else
                        "down" if future <= anchor * (1 - threshold) else "none"
                    )
                status = "matured"
            future_facts = [{
                "timestamp": value,
                "close": by_time[value].get("close"),
            } for value in expected]
            future_fingerprint = _fingerprint(future_facts)
            result = {
                "status": status,
                "direction": direction,
                "return_pct": return_pct,
                "event_threshold_pct": PRICE_SPIKE_THRESHOLD,
                "forward_bars": FORWARD_BARS,
                "detected_at_utc": now_ms,
                "matured_at_utc": now_ms,
                "outcome_window_end_utc": target,
                "future_candle_fingerprint": future_fingerprint,
                "method_version": PRICE_SPIKE_METHOD_VERSION,
            }
            result["data_fingerprint"] = _fingerprint(result)
            result_writer(event_id, "outcome", result)
            appended.append({"event_id": event_id, "result_kind": "outcome", **result})
        else:
            latest_rows = candle_reader(
                exchange, row.get("market", "futures"), pair, "5m", candle_type,
                limit=1, start=target, end=None,
            ) or []
            latest_timestamp = -1
            for item in latest_rows:
                extras = item.get("extras") if isinstance(item, dict) else None
                if (isinstance(extras, dict) and extras.get("live_event_timestamp") is not None
                        and item.get("timestamp") is not None):
                    latest_timestamp = max(latest_timestamp, int(item["timestamp"]))
            if latest_timestamp < target:
                continue
            result = {
                "status": "invalid", "direction": None, "return_pct": None,
                "event_threshold_pct": PRICE_SPIKE_THRESHOLD,
                "forward_bars": FORWARD_BARS,
                "detected_at_utc": now_ms, "matured_at_utc": now_ms,
                "outcome_window_end_utc": target,
                "missing_timestamps": [value for value in expected if value not in by_time],
                "method_version": PRICE_SPIKE_METHOD_VERSION,
            }
            result["data_fingerprint"] = _fingerprint(result)
            result_writer(event_id, "outcome", result)
            appended.append({"event_id": event_id, "result_kind": "outcome", **result})

    # Profiles require all 25 online candidate ratios. They mature one hour
    # after the candidate bar and are recorded as a separate immutable fact.
    events = profiles_reader(limit=None)
    for row in events or []:
        outcome = row.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("direction") not in {"up", "down"}:
            continue
        if row.get("profile") is not None or row.get("profile_status") is not None:
            continue
        timestamp = int(row.get("timestamp", -1))
        pair = row.get("pair")
        if not pair or timestamp < 0:
            continue
        start, end = (timestamp - PROFILE_RADIUS * BAR_INTERVAL_MS,
                      timestamp + PROFILE_RADIUS * BAR_INTERVAL_MS)
        observations = observation_reader(pair, start, end) or []
        by_time = {int(item["timestamp"]): item for item in observations
                   if isinstance(item, dict) and item.get("timestamp") is not None}
        expected = [timestamp + offset * BAR_INTERVAL_MS
                    for offset in range(-PROFILE_RADIUS, PROFILE_RADIUS + 1)]
        points = []
        missing = []
        for offset, point_time in zip(range(-PROFILE_RADIUS, PROFILE_RADIUS + 1), expected):
            item = by_time.get(point_time)
            ratio = _number((item or {}).get("observation", {}).get("volume_ratio"))
            if ratio is None:
                missing.append(point_time)
            else:
                points.append({"offset": offset, "timestamp": point_time,
                               "volume_ratio": ratio})
        status = "matured" if not missing else "unknown"
        if missing:
            latest_rows = candle_reader(
                exchange, row.get("market", "futures"), pair, "5m",
                candle_type, limit=1, start=end, end=None,
            ) or []
            latest_timestamp = -1
            for item in latest_rows:
                extras = item.get("extras") if isinstance(item, dict) else None
                if (isinstance(extras, dict) and extras.get("live_event_timestamp") is not None
                        and item.get("timestamp") is not None):
                    latest_timestamp = max(latest_timestamp, int(item["timestamp"]))
            if latest_timestamp < end:
                continue
            status = "invalid"
        profile = {
            "status": status, "direction": outcome["direction"],
            "points": points, "missing_timestamps": missing,
            "profile_start_utc": start, "profile_end_utc": end,
            "matured_at_utc": now_ms,
            "method_version": PRICE_SPIKE_METHOD_VERSION,
        }
        profile["data_fingerprint"] = _fingerprint(profile)
        result_writer(row["event_id"], "profile", profile)
        appended.append({"event_id": row["event_id"], "result_kind": "profile", **profile})
    return appended
