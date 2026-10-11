"""Closed, separately identified input feeds for the Fnew research accounts.

Spot observations are never synthesized from perpetual prices, carried
forward, or assigned an inferred close flag.  A stale feed stays stale.  The
caller can inspect ``fnew_data_status`` before evaluating a basis signal.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from strategy_data import closed_candles, load_spec_candles, resample_closed_candles
from strategy_data import timeframe_milliseconds

PERPETUAL_PAIRS = tuple(f"{coin}/USDT:USDT" for coin in ("BTC", "ETH", "TRX"))
SPOT_PAIRS = tuple(f"{coin}/USDT" for coin in ("BTC", "ETH", "TRX"))
PAIR_MAP = dict(zip(PERPETUAL_PAIRS, SPOT_PAIRS))


def _basis_feeds(spec: Mapping[str, Any]) -> tuple[tuple[str, ...], dict[str, str]]:
    feeds = spec.get("data_feeds")
    if not isinstance(feeds, Mapping):
        raise ValueError("basis inputs require explicit spot and futures data_feeds")
    futures, spot = feeds.get("futures"), feeds.get("spot")
    if not isinstance(futures, Mapping) or not isinstance(spot, Mapping):
        raise ValueError("basis input feed declarations are incomplete")
    if (futures.get("market") != "futures" or spot.get("market") != "spot"
            or futures.get("timeframe") != "1h" or spot.get("timeframe") != "1h"
            or spec.get("timeframe") != "1h"):
        raise ValueError("basis feed markets and timeframe must be explicit and unchanged")
    pairs = tuple(futures.get("pairs", ()))
    mapping = spot.get("pair_map")
    if (not pairs or len(pairs) != len(set(pairs))
            or any(pair not in PERPETUAL_PAIRS for pair in pairs)
            or not isinstance(mapping, Mapping)
            or dict(mapping) != {pair: PAIR_MAP[pair] for pair in pairs}
            or set(spot.get("pairs", ())) != {mapping[pair] for pair in pairs}):
        raise ValueError("basis input pairs must use the frozen BTC/ETH/TRX market identities")
    return pairs, dict(mapping)


def _continuous_suffix(rows: list[dict[str, Any]], interval: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    start = len(rows) - 1
    while start and rows[start]["timestamp"] - rows[start - 1]["timestamp"] == interval:
        start -= 1
    return rows[start:]


def _market_rows(store: Any, pair: str, market: str, *, limit: int,
                 as_of_ms: int | None, exchange: str) -> list[dict[str, Any]]:
    """Read one genuine market; aggregate only complete five-minute buckets."""
    interval = timeframe_milliseconds("1h")
    end = None if as_of_ms is None else max(0, int(as_of_ms) - 1)
    direct = store.get_candles(exchange, market, pair, "1h", market,
                               limit=limit + 1, end=end)
    rows = closed_candles(direct, "1h")
    if as_of_ms is not None:
        rows = [row for row in rows if row["timestamp"] + interval <= as_of_ms]
    expected = None if as_of_ms is None else (int(as_of_ms) // interval - 1) * interval
    if len(rows) < limit or (expected is not None and rows and rows[-1]["timestamp"] < expected):
        source_limit = limit * 12 + 2
        if len(rows) >= limit and as_of_ms is not None:
            source_limit = min(source_limit, max(14, (int(as_of_ms) - rows[-1]["timestamp"])
                                                 // 300_000 + 3))
        source = store.get_candles(exchange, market, pair, "5m", market,
                                   limit=source_limit, end=end)
        resampled = resample_closed_candles(source, "5m", "1h", as_of_ms=as_of_ms)
        if resampled:
            # Complete newer source tails may extend native history. Whole
            # rows move together, so quote volume is never borrowed from a
            # different price snapshot.
            merged = {row["timestamp"]: row for row in rows}
            if rows and rows[-1]["timestamp"] >= resampled[-1]["timestamp"]:
                merged = {row["timestamp"]: row for row in resampled} | merged
            else:
                merged.update({row["timestamp"]: row for row in resampled})
            rows = [merged[stamp] for stamp in sorted(merged)]
    return _continuous_suffix(rows[-limit:], interval)


def load_fnew_candles(store: Any, spec: Mapping[str, Any], *, limit: int = 750,
                       as_of_ms: int | None = None, exchange: str = "binance") -> dict[str, Any]:
    """Load native Fnew candles, with an explicit ``spot`` map for s33.

    Each market retains its own continuous suffix. Exact synchronization is
    checked on the required signal tail, without shortening perpetual risk
    inputs to a sparse spot history. Signals must reject spot readiness
    failures; current perpetual rows remain available for risk exits.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("input limit must be a positive integer")
    if str(spec.get("canonical_strategy_id", spec.get("strategy_id", ""))).startswith(
            "s33_basis_momentum_1h"):
        pairs, mapping = _basis_feeds(spec)
        result: dict[str, Any] = {"spot": {}}
        for pair in pairs:
            perpetual = _market_rows(store, pair, "futures", limit=limit,
                                     as_of_ms=as_of_ms, exchange=exchange)
            spot = _market_rows(store, mapping[pair], "spot", limit=limit,
                               as_of_ms=as_of_ms, exchange=exchange)
            result[pair] = perpetual
            result["spot"][mapping[pair]] = spot
        return result
    return load_spec_candles(store, spec, limit=limit, as_of_ms=as_of_ms, exchange=exchange)


def fnew_data_status(spec: Mapping[str, Any], candles: Mapping[str, Any], *,
                     as_of_ms: int, minimum: int | None = None) -> dict[str, Any]:
    """Describe actual current closed feed coverage, without changing inputs."""
    timeframe = str(spec.get("timeframe", "15m"))
    interval = timeframe_milliseconds(timeframe)
    expected = (int(as_of_ms) // interval - 1) * interval
    required = max(1, int(minimum if minimum is not None else spec.get("min_warmup_bars", 1)))
    basis = str(spec.get("canonical_strategy_id", spec.get("strategy_id", ""))).startswith(
        "s33_basis_momentum_1h")
    if basis:
        pairs, mapping = _basis_feeds(spec)
    else:
        pairs = tuple(spec.get("data_universe", spec.get("universe", PERPETUAL_PAIRS)))
        mapping = {}
    by_pair = {}
    for pair in pairs:
        raw = candles.get(pair, [])
        rows = _continuous_suffix(closed_candles(raw, timeframe), interval)
        rows = [row for row in rows if row["timestamp"] + interval <= as_of_ms]
        feeds = {"futures": {"bars": len(rows), "last_timestamp": rows[-1]["timestamp"]
                            if rows else None}}
        reasons = []
        if len(rows) < required:
            reasons.append("futures_warmup_incomplete")
        if not rows or rows[-1]["timestamp"] != expected:
            reasons.append("futures_data_stale")
        if basis:
            spot_map = candles.get("spot", {})
            spot_raw = spot_map.get(mapping[pair], []) if isinstance(spot_map, Mapping) else []
            spot_rows = _continuous_suffix(closed_candles(spot_raw, timeframe), interval)
            spot_rows = [row for row in spot_rows if row["timestamp"] + interval <= as_of_ms]
            feeds["spot"] = {"bars": len(spot_rows), "last_timestamp": spot_rows[-1]["timestamp"]
                             if spot_rows else None}
            if len(spot_rows) < required:
                reasons.append("spot_warmup_incomplete")
            if not spot_rows or spot_rows[-1]["timestamp"] != expected:
                reasons.append("spot_data_stale")
            if [row["timestamp"] for row in rows[-required:]] != [
                    row["timestamp"] for row in spot_rows[-required:]]:
                reasons.append("basis_timestamps_not_exactly_synchronized")
        by_pair[pair] = {"ready": not reasons, "reasons": reasons, "feeds": feeds}
    return {"ready": bool(by_pair) and all(row["ready"] for row in by_pair.values()),
            "expected_last_timestamp": expected, "required_bars": required, "by_pair": by_pair}


__all__ = ["fnew_data_status", "load_fnew_candles"]
