"""Online order-flow volume surge detection for closed Binance candles.

The detector deliberately keeps the observation path small and deterministic.  A
closed candle is compared with the median of the preceding 288 closed candles;
the current candle is appended only after that comparison so it cannot enter its
own baseline.  Persistence is supplied by the caller through ``on_surge``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
import math
from statistics import median
from typing import Any, Callable, Deque

from price_spike import PRICE_SPIKE_METHOD_VERSION


PAIR_BASES = frozenset({"BTC", "ETH"})
DEFAULT_THRESHOLDS = (2.0, 4.0, 8.0, 20.0)
METHOD_VERSION = "orderflow-volume-surge-v3"
BAR_INTERVAL_MS = 5 * 60 * 1000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _event_id(method_version: str, market: str, pair: str, timeframe: str, timestamp: int) -> str:
    identity = {
        "method_version": method_version,
        "market": market,
        "pair": pair,
        "timeframe": timeframe,
        "timestamp": timestamp,
    }
    return "volume-surge:" + _fingerprint(identity)


@dataclass
class VolumeSurgeDetector:
    """Detect BTC/ETH 5m quote-volume surges without look-ahead.

    ``on_surge`` is called synchronously exactly once for each deterministic
    event identity.  A sink should enqueue or append the event quickly; it must
    not block the WebSocket receive loop with a database round trip.
    """

    window_size: int = 288
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS
    on_surge: Callable[[dict[str, Any]], Any] | None = None
    method_version: str = METHOD_VERSION
    _volumes: dict[tuple[str, str], Deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _closed_timestamps: dict[tuple[str, str], set[int]] = field(default_factory=lambda: defaultdict(set))
    _emitted: set[str] = field(default_factory=set)
    _last_timestamp: dict[tuple[str, str], int] = field(default_factory=dict)
    last_candidate: dict[str, Any] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.window_size = int(self.window_size)
        if self.window_size < 1:
            raise ValueError("window_size must be positive")
        values = tuple(float(value) for value in self.thresholds)
        if not values or any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("thresholds must be positive finite numbers")
        if tuple(sorted(values)) != values or len(set(values)) != len(values):
            raise ValueError("thresholds must be strictly ascending")
        self.thresholds = values
        self._volumes = defaultdict(
            lambda: deque(maxlen=self.window_size),
            {key: deque(volumes, maxlen=self.window_size)
             for key, volumes in self._volumes.items()},
        )

    @staticmethod
    def _supported(pair: str, timeframe: str) -> bool:
        if timeframe != "5m" or not isinstance(pair, str):
            return False
        base = pair.split("/", 1)[0].upper()
        return base in PAIR_BASES

    @staticmethod
    def _quote_volume(candle: dict[str, Any]) -> float | None:
        value = candle.get("quote_volume")
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _band(ratio: float, thresholds: tuple[float, ...]) -> tuple[float, str]:
        selected = thresholds[0]
        for threshold in thresholds:
            if ratio >= threshold:
                selected = threshold
            else:
                break
        return selected, f"{selected:g}x"

    def seed(
        self,
        pair: str,
        volumes: list[float] | tuple[float, ...],
        *,
        market: str = "futures",
        timestamps: list[int] | tuple[int, ...] | None = None,
    ) -> None:
        """Warm a pair from trusted closed history after a restart.

        Seeding is explicit so callers cannot accidentally use the current
        candle as its own baseline.  Values are validated and retained in the
        same bounded order as live observations.
        """
        if not self._supported(pair, "5m"):
            return
        cleaned = []
        if timestamps is not None and len(timestamps) != len(volumes):
            raise ValueError("timestamps must have the same length as volumes")
        for index, value in enumerate(volumes):
            if isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(number) or number < 0:
                continue
            if timestamps is None:
                cleaned.append(number)
                continue
            timestamp = timestamps[index]
            if isinstance(timestamp, bool):
                continue
            try:
                timestamp = int(timestamp)
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp < 0 or str(timestamp) != str(timestamps[index]):
                continue
            cleaned.append((timestamp, number))
        key = (market, pair)
        if timestamps is None:
            self._volumes[key] = deque(cleaned[-self.window_size:], maxlen=self.window_size)
            self._closed_timestamps.pop(key, None)
            self._last_timestamp.pop(key, None)
            return
        # Keep the newest value for a duplicate timestamp and restore the
        # chronological guard used by live processing.
        by_timestamp = {timestamp: number for timestamp, number in cleaned}
        ordered = sorted(by_timestamp.items())[-self.window_size:]
        contiguous = []
        for item in reversed(ordered):
            if contiguous and contiguous[-1][0] - item[0] != BAR_INTERVAL_MS:
                break
            contiguous.append(item)
        ordered = list(reversed(contiguous))
        self._volumes[key] = deque((number for _, number in ordered), maxlen=self.window_size)
        self._closed_timestamps[key] = {timestamp for timestamp, _ in ordered}
        if ordered:
            self._last_timestamp[key] = ordered[-1][0]
        else:
            self._last_timestamp.pop(key, None)

    def restore_event_ids(self, event_ids: Any) -> None:
        """Restore persisted observation IDs so a process restart stays idempotent."""
        if event_ids is None:
            return
        self._emitted.update(value for value in event_ids if isinstance(value, str))

    def warmup_status(self, pair: str, *, market: str = "futures") -> dict[str, Any]:
        """Expose the current contiguous-bar count needed for a valid baseline."""
        key = (market, pair)
        return {
            "valid_bars": len(self._volumes[key]),
            "required_bars": self.window_size,
            "last_timestamp": self._last_timestamp.get(key),
            "ready": len(self._volumes[key]) >= self.window_size,
        }

    def process(
        self,
        pair: str,
        timeframe: str,
        candle: dict[str, Any],
        *,
        market: str = "futures",
        source_timestamp: int | None = None,
    ) -> dict[str, Any] | None:
        """Observe one normalized candle and return a newly emitted surge event.

        Non-5m, non-BTC/ETH, open, malformed, duplicate, and warm-up candles
        return ``None``.  Every valid closed candle still advances the baseline
        even when it does not qualify as a surge.
        """
        self.last_candidate = None
        if not self._supported(pair, timeframe) or not isinstance(candle, dict):
            return None
        if candle.get("closed") is not True:
            return None
        timestamp = candle.get("timestamp")
        if isinstance(timestamp, bool):
            return None
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp < 0 or str(timestamp) != str(candle.get("timestamp")):
            return None
        key = (market, pair)
        if timestamp in self._closed_timestamps[key]:
            return None
        previous_timestamp = self._last_timestamp.get(key)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            # A late close belongs to an already observed period.  Ignoring it
            # keeps the baseline chronological and prevents replay corruption.
            return None
        if previous_timestamp is not None and timestamp - previous_timestamp > BAR_INTERVAL_MS:
            # A missing closed candle breaks the rolling window. Require a
            # fresh contiguous baseline instead of carrying stale history.
            self._volumes[key].clear()
        quote_volume = self._quote_volume(candle)
        if quote_volume is None:
            return None
        self._closed_timestamps[key].add(timestamp)
        self._last_timestamp[key] = timestamp
        # Keep duplicate tracking bounded in the same spirit as the volume
        # window.  Older duplicates cannot be valid exchange updates anymore.
        if len(self._closed_timestamps[key]) > self.window_size * 2:
            cutoff = timestamp - self.window_size * 2 * 300_000
            self._closed_timestamps[key] = {
                value for value in self._closed_timestamps[key] if value >= cutoff
            }
        volumes = self._volumes[key]
        baseline = float(median(volumes)) if len(volumes) >= self.window_size else None
        ratio = quote_volume / baseline if baseline and baseline > 0 else None
        if ratio is not None:
            band = (
                "lt1" if ratio < 1 else "1to2" if ratio < 2 else
                "2to4" if ratio < 4 else "4to8" if ratio < 8 else
                "8to20" if ratio < 20 else "ge20"
            )
            observation = {
                "timestamp": timestamp,
                "quote_volume": quote_volume,
                "baseline_quote_volume": baseline,
                "volume_ratio": ratio,
                "ratio_band": band,
                "close": candle.get("close"),
                "flow_imbal": candle.get("flow_imbal"),
                "num_trades": candle.get("num_trades"),
                "baseline_start_timestamp": timestamp - self.window_size * BAR_INTERVAL_MS,
                "baseline_end_timestamp": timestamp - BAR_INTERVAL_MS,
                "baseline_source": "binance_websocket_closed_kline",
                "baseline_fingerprint": _fingerprint({
                    "source": "binance_websocket_closed_kline",
                    "start_timestamp": timestamp - self.window_size * BAR_INTERVAL_MS,
                    "end_timestamp": timestamp - BAR_INTERVAL_MS,
                    "interval_ms": BAR_INTERVAL_MS,
                    "quote_volumes": list(volumes),
                }),
            }
            identity = {
                "method_version": PRICE_SPIKE_METHOD_VERSION,
                "market": market,
                "pair": pair,
                "timeframe": timeframe,
                "timestamp": timestamp,
            }
            event_id = "price-spike:" + _fingerprint(identity)
            self.last_candidate = {
                "event_id": event_id,
                "event_type": "price_spike_candidate_observed",
                "method_version": identity["method_version"],
                "data_fingerprint": _fingerprint(observation),
                "market": market,
                "pair": pair,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "observed_at": source_timestamp,
                "status": "unknown",
                "observation": observation,
            }
        volumes.append(quote_volume)
        if ratio is None or ratio < self.thresholds[0]:
            return None
        threshold, threshold_band = self._band(ratio, self.thresholds)
        event_identity = _event_id(self.method_version, market, pair, timeframe, timestamp)
        if event_identity in self._emitted:
            return None
        self._emitted.add(event_identity)
        data = {
            "timestamp": timestamp,
            "quote_volume": quote_volume,
            "baseline_quote_volume": baseline,
            "volume_ratio": ratio,
            "threshold": threshold,
            "threshold_band": threshold_band,
            "num_trades": candle.get("num_trades"),
            "taker_buy_base": candle.get("taker_buy_base"),
            "taker_buy_quote": candle.get("taker_buy_quote"),
            "flow_imbal": candle.get("flow_imbal"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
        }
        event = {
            "event_id": event_identity,
            "event_type": "orderflow_volume_surge_observed",
            "method_version": self.method_version,
            "data_fingerprint": _fingerprint(data),
            "market": market,
            "pair": pair,
            "timeframe": timeframe,
            "observed_at": source_timestamp,
            "observation": data,
            "status": "unknown",
        }
        if self.on_surge is not None:
            self.on_surge(event)
        return event

    observe = process
    process_candle = process
