"""Closed-candle data supply for forward strategy experiments.

The first strategy-lab runner only consumed a paired 5m BTC/ETH stream.  This
module keeps the data contract separate from signal code so experiments with
15m/1h/1d bars and cross-sectional inputs can be added without silently using
partial candles or a different universe.  Directly stored target-timeframe
data is preferred; resampling is an explicit fallback from a lower timeframe.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping


TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
SOL = "SOL/USDT:USDT"
BNB = "BNB/USDT:USDT"

# The breadth specification names a 20-coin research whitelist.  Keep this
# list explicit and ordered: adding/removing a constituent changes the rule
# identity and must be reviewed as a new strategy variant.
BREADTH_PAIRS = (
    BTC, ETH, SOL, BNB,
    "SUI/USDT:USDT", "AVAX/USDT:USDT", "LTC/USDT:USDT", "XRP/USDT:USDT",
    "LINK/USDT:USDT", "TRX/USDT:USDT", "AAVE/USDT:USDT", "DOGE/USDT:USDT",
    "ADA/USDT:USDT", "SAND/USDT:USDT", "FIL/USDT:USDT", "ZEC/USDT:USDT",
    "NEAR/USDT:USDT", "APT/USDT:USDT", "DOT/USDT:USDT", "UNI/USDT:USDT",
)

_TWO_BY_TWO_SUFFIXES = (
    "_2x2_A_L100_close",
    "_2x2_B_L30_close",
    "_2x2_C_L100_intrabar",
    "_2x2_D_L30_intrabar",
)

# Direct native-timeframe tables are maintained on demand (for example, a
# 15m table can stop advancing while the 5m feed remains live).  Keep a
# bounded tolerance for delayed writes, but never hand a stopped native tail
# to signal evaluation when no fresher lower-timeframe fallback is available.
DEFAULT_NATIVE_MAX_AGE_BARS = 3


def timeframe_milliseconds(timeframe: str) -> int:
    """Return the fixed UTC duration for a supported Binance interval."""
    try:
        return TIMEFRAME_MS[str(timeframe)]
    except KeyError as exc:
        raise ValueError(f"unsupported strategy timeframe: {timeframe!r}") from exc


def strategy_timeframe(spec: Mapping[str, Any]) -> str:
    timeframe = str(spec.get("timeframe", "5m"))
    timeframe_milliseconds(timeframe)
    return timeframe


def strategy_pairs(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Resolve the frozen data universe, including the cross-sectional inputs."""
    strategy_id = str(spec.get("strategy_id", ""))
    for suffix in _TWO_BY_TWO_SUFFIXES:
        if strategy_id.endswith(suffix):
            strategy_id = strategy_id[:-len(suffix)]
            break
    for suffix in ("_B15m_100x_maker", "_Cgen2_100x_maker",
                   "_Dgen3_100x_maker", "_100x_maker", "_100x", "_v2"):
        if strategy_id.endswith(suffix):
            strategy_id = strategy_id[:-len(suffix)]
    if strategy_id == "s13_daily_trend_multi_horizon":
        return (BTC, ETH, SOL, BNB)
    if strategy_id in {"s19_multi_coin_breadth", "s24_cross_sectional_lowvol"}:
        return BREADTH_PAIRS
    universe = spec.get("universe", (BTC, ETH))
    pairs = []
    for value in universe:
        text = str(value)
        # s19's research prose contains descriptive entries; its explicit
        # identity above is authoritative, so never pass those to the DB.
        if "/USDT:USDT" in text and text == text.strip():
            pairs.append(text)
    if not pairs:
        pairs = [BTC, ETH]
    return tuple(dict.fromkeys(pairs))


def _closed_row(row: Mapping[str, Any], interval_ms: int) -> dict[str, Any] | None:
    extras = row.get("extras")
    extras = extras if isinstance(extras, Mapping) else {}
    if extras.get("closed", row.get("closed")) is not True:
        return None
    try:
        timestamp = int(row["timestamp"])
        if timestamp < 0 or timestamp % interval_ms:
            return None
        values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
        if not all(math.isfinite(value) for value in values.values()):
            return None
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            return None
        if values["low"] > min(values["open"], values["close"]):
            return None
        if values["high"] < max(values["open"], values["close"]):
            return None
        if values["high"] < values["low"] or values["volume"] < 0:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return {"timestamp": timestamp, **values, "extras": dict(extras, closed=True)}


def closed_candles(rows: list[Mapping[str, Any]], timeframe: str) -> list[dict[str, Any]]:
    """Filter and validate closed rows, refusing duplicate timestamps."""
    interval = timeframe_milliseconds(timeframe)
    prepared = []
    seen = set()
    for row in rows or []:
        clean = _closed_row(row, interval)
        if clean is None:
            continue
        if clean["timestamp"] in seen:
            raise ValueError("duplicate closed candle timestamp")
        seen.add(clean["timestamp"])
        prepared.append(clean)
    return sorted(prepared, key=lambda row: row["timestamp"])


def _sum_optional(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row["extras"].get(key) for row in rows]
    if any(value is None for value in values):
        return None
    try:
        result = sum(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def resample_closed_candles(rows: list[Mapping[str, Any]], source_timeframe: str,
                            target_timeframe: str, *, as_of_ms: int | None = None
                            ) -> list[dict[str, Any]]:
    """Aggregate contiguous closed candles without filling gaps.

    A target bucket is emitted only when every source bar is present and the
    target close has passed ``as_of_ms``.  This prevents a partially formed
    higher-timeframe candle from becoming a signal input.
    """
    source_ms = timeframe_milliseconds(source_timeframe)
    target_ms = timeframe_milliseconds(target_timeframe)
    if target_ms < source_ms or target_ms % source_ms:
        raise ValueError("target timeframe must be an integral multiple of source timeframe")
    source = closed_candles(list(rows or []), source_timeframe)
    if source_timeframe == target_timeframe:
        return [row for row in source if as_of_ms is None or row["timestamp"] + target_ms <= as_of_ms]
    expected = target_ms // source_ms
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        start = row["timestamp"] // target_ms * target_ms
        buckets[start].append(row)
    result = []
    for start in sorted(buckets):
        group = buckets[start]
        if len(group) != expected:
            continue
        if [row["timestamp"] for row in group] != [start + source_ms * i for i in range(expected)]:
            continue
        if as_of_ms is not None and start + target_ms > as_of_ms:
            continue
        extras: dict[str, Any] = {"closed": True, "source_timeframe": source_timeframe,
                                  "source_bars": expected}
        for key in ("quote_volume", "taker_buy_base", "taker_buy_quote", "num_trades"):
            value = _sum_optional(group, key)
            if value is not None:
                extras[key] = int(value) if key == "num_trades" else value
        result.append({
            "timestamp": start,
            "open": group[0]["open"], "high": max(row["high"] for row in group),
            "low": min(row["low"] for row in group), "close": group[-1]["close"],
            "volume": sum(row["volume"] for row in group), "extras": extras,
        })
    return result


def load_spec_candles(store: Any, spec: Mapping[str, Any], *, limit: int = 750,
                      as_of_ms: int | None = None, source_timeframe: str = "5m",
                      exchange: str = "binance", market: str = "futures",
                      candle_type: str = "futures",
                      native_max_age_bars: int = DEFAULT_NATIVE_MAX_AGE_BARS
                      ) -> dict[str, list[dict[str, Any]]]:
    """Read closed inputs for one strategy from ``DataStore``.

    The target timeframe is queried first.  If it is stale or has fewer than
    ``limit`` closed rows, continuously resampled lower-timeframe rows extend
    it.  The fresher source owns overlapping rows in full; no interpolation
    or cross-symbol substitution is performed.  With an ``as_of_ms`` clock,
    the final native tail must be no more than ``native_max_age_bars`` bars
    behind the latest complete target bar.  This turns an on-demand native
    cache that stopped advancing into an explicit empty input instead of
    silently evaluating on old prices.
    """
    if (isinstance(native_max_age_bars, bool) or
            not isinstance(native_max_age_bars, int) or native_max_age_bars < 0):
        raise ValueError("native_max_age_bars must be a non-negative integer")
    timeframe = strategy_timeframe(spec)
    target_ms = timeframe_milliseconds(timeframe)
    pairs = strategy_pairs(spec)
    result: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        end = None if as_of_ms is None else max(0, int(as_of_ms) - 1)
        direct = store.get_candles(exchange, market, pair, timeframe, candle_type,
                                   limit=limit + 1, end=end)
        rows = closed_candles(direct, timeframe)
        if as_of_ms is not None:
            rows = [row for row in rows if row["timestamp"] + target_ms <= as_of_ms]
        direct_is_stale = bool(
            as_of_ms is not None and rows and
            rows[-1]["timestamp"] < (int(as_of_ms) // target_ms - 1) * target_ms
        )
        # Same reasoning as ``strategy_fnew_data._market_rows``: a window can be
        # long and current yet still contain an internal hole (for example one
        # missing 1h candle).  The signal layer keeps only the final contiguous
        # suffix, so an unrepaired hole silently shortens the history and the
        # account never reaches its warmup minimum.  Detect the hole so the
        # lower-timeframe fallback runs and can repair it.
        stamps = [row["timestamp"] for row in rows]
        has_internal_gap = any(stamps[index] - stamps[index - 1] != target_ms
                               for index in range(1, len(stamps)))
        if (len(rows) < limit or direct_is_stale or has_internal_gap) and timeframe != source_timeframe:
            source_ms = timeframe_milliseconds(source_timeframe)
            source_limit = max(limit * (target_ms // source_ms) + 2, limit + 2)
            # Only a complete native history may be extended from its newer tail;
            # a hole starts after the missing bar and cannot fill it.
            if len(rows) >= limit and as_of_ms is not None and not has_internal_gap:
                # With complete native history only its newer tail is needed.
                # Daily inputs must not reread 216,000 five-minute rows per poll.
                tail_bars = (int(as_of_ms) - rows[-1]["timestamp"] + source_ms - 1) // source_ms
                source_limit = min(source_limit, max(target_ms // source_ms + 2, tail_bars + 2))
            raw = store.get_candles(exchange, market, pair, source_timeframe,
                                    candle_type, limit=source_limit, end=end)
            resampled = resample_closed_candles(raw, source_timeframe, timeframe,
                                                as_of_ms=as_of_ms)
            if resampled:
                # A short live tail must not discard valid direct history.
                # Select whole rows so order-flow extras stay with their OHLCV.
                if rows and rows[-1]["timestamp"] >= resampled[-1]["timestamp"]:
                    merged = {row["timestamp"]: row for row in resampled}
                    merged.update({row["timestamp"]: row for row in rows})
                else:
                    merged = {row["timestamp"]: row for row in rows}
                    merged.update({row["timestamp"]: row for row in resampled})
                rows = [merged[stamp] for stamp in sorted(merged)]
        if as_of_ms is not None:
            rows = [row for row in rows if row["timestamp"] + target_ms <= as_of_ms]
            if rows:
                expected_latest = (int(as_of_ms) // target_ms - 1) * target_ms
                age_bars = max(0, (expected_latest - rows[-1]["timestamp"]) // target_ms)
                if age_bars > native_max_age_bars:
                    # Do not expose an old native cache when the lower
                    # timeframe source could not provide a current tail.
                    rows = []
        result[pair] = rows[-limit:]
    return result


__all__ = [
    "BREADTH_PAIRS", "DEFAULT_NATIVE_MAX_AGE_BARS", "TIMEFRAME_MS",
    "closed_candles", "load_spec_candles",
    "resample_closed_candles", "strategy_pairs", "strategy_timeframe",
    "timeframe_milliseconds",
]
