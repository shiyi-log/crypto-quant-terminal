"""Fetch closed exchange candles solely for recovering an in-memory baseline.

The returned rows are not live observations.  Callers must not persist a live
marker on them or replay them through the event detector as candidate candles.
"""

from __future__ import annotations

import math
import time
from typing import Any

import aiohttp


BAR_INTERVAL_MS = 5 * 60 * 1000
BASELINE_SOURCE = "binance_futures_rest_closed_kline"
REST_URL = "https://fapi.binance.com/fapi/v1/klines"
SUPPORTED_PAIRS = frozenset({"BTC/USDT:USDT", "ETH/USDT:USDT"})


def _window_start(end_timestamp: int, window_size: int, now_ms: int) -> int:
    if (isinstance(end_timestamp, bool) or not isinstance(end_timestamp, int)
            or end_timestamp < 0 or end_timestamp % BAR_INTERVAL_MS):
        raise ValueError("baseline end_timestamp must be an aligned nonnegative integer")
    if (isinstance(window_size, bool) or not isinstance(window_size, int)
            or not 1 <= window_size <= 1500):
        raise ValueError("baseline window_size must be between 1 and 1500")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or end_timestamp > now_ms:
        raise ValueError("baseline window includes a candle that has not closed")
    start = end_timestamp - window_size * BAR_INTERVAL_MS
    if start < 0:
        raise ValueError("baseline window precedes the Unix epoch")
    return start


def validate_closed_baseline(
    payload: Any, *, end_timestamp: int, window_size: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Require the exact ordered, contiguous and fully closed requested window.

    Binance USDⓈ-M REST kline fields 0, 6 and 7 are respectively the open
    timestamp, close timestamp and quote asset volume.
    """
    start = _window_start(end_timestamp, window_size, now_ms)
    if not isinstance(payload, list) or len(payload) != window_size:
        raise ValueError("REST baseline does not contain the full requested window")
    rows = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, list) or len(raw) < 8:
            raise ValueError("REST baseline contains a malformed kline")
        expected_timestamp = start + index * BAR_INTERVAL_MS
        timestamp, close_timestamp = raw[0], raw[6]
        if (isinstance(timestamp, bool) or not isinstance(timestamp, int)
                or timestamp != expected_timestamp):
            raise ValueError("REST baseline timestamps are not the requested contiguous window")
        if (isinstance(close_timestamp, bool) or not isinstance(close_timestamp, int)
                or close_timestamp != timestamp + BAR_INTERVAL_MS - 1):
            raise ValueError("REST baseline kline has an invalid close timestamp")
        if isinstance(raw[7], bool):
            raise ValueError("REST baseline quote volume must be finite and nonnegative")
        try:
            quote_volume = float(raw[7])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("REST baseline quote volume must be finite and nonnegative") from exc
        if not math.isfinite(quote_volume) or quote_volume < 0:
            raise ValueError("REST baseline quote volume must be finite and nonnegative")
        rows.append({
            "timestamp": timestamp, "quote_volume": quote_volume, "source": BASELINE_SOURCE,
        })
    return rows


async def fetch_closed_baseline(
    pair: str, market: str, end_timestamp: int, window_size: int,
    *, now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch one exact futures window; errors propagate for caller fallback.

    The exclusive end is a 5m opening boundary.  The HTTP request uses end-1,
    so the currently opening candle cannot enter its own preceding baseline.
    A caller controls retry/backoff; this function makes one bounded request.
    """
    if market != "futures" or pair not in SUPPORTED_PAIRS:
        raise ValueError("REST baseline recovery only supports BTC/ETH USDT futures")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start = _window_start(end_timestamp, window_size, now_ms)
    params = {
        "symbol": pair.split(":", 1)[0].replace("/", ""),
        "interval": "5m", "startTime": start,
        "endTime": end_timestamp - 1, "limit": window_size,
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10), trust_env=True,
    ) as session:
        async with session.get(REST_URL, params=params) as response:
            response.raise_for_status()
            payload = await response.json()
    return validate_closed_baseline(
        payload, end_timestamp=end_timestamp, window_size=window_size, now_ms=now_ms,
    )
