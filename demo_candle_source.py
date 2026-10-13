"""Read a precise, completed Demo reference bucket from PostgreSQL.

Only the requested bucket is eligible. Missing higher-timeframe rows can be
derived from every closed 5m component; gaps never fall back to an older bar.
This source reads market data and never writes candles or execution evidence.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from collections.abc import Mapping

from strategy_data import TIMEFRAME_MS


DEMO_PAIRS = frozenset(("BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"))
SOURCE_MS = TIMEFRAME_MS["5m"]
_FIELDS = ("open", "high", "low", "close", "volume")


def _timestamp(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError("invalid candle value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid candle value") from None
    if not result.is_finite():
        raise ValueError("invalid candle value")
    return result


def _closed_row(row, timestamp):
    if not isinstance(row, Mapping) or _timestamp(row.get("timestamp")) != timestamp:
        return None
    extras = row.get("extras")
    if not isinstance(extras, Mapping) or extras.get("closed") is not True:
        return None
    try:
        values = {name: _decimal(row[name]) for name in _FIELDS}
    except (KeyError, ValueError):
        return None
    if (min(values[name] for name in _FIELDS[:-1]) <= 0 or values["volume"] < 0
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
            or values["high"] < values["low"]):
        return None
    return {"timestamp": timestamp, **values, "extras": copy.deepcopy(dict(extras))}


def _sum_exact(values):
    # Decimal's default precision could round a large aggregate even when each
    # source value is exact. Budget digits for integer part, fractional part
    # and carry before summing positive volumes.
    nonzero = [value for value in values if value]
    integer_digits = max((value.adjusted() + 1 for value in nonzero), default=1)
    fractional_digits = max((-value.as_tuple().exponent for value in values), default=0)
    with localcontext() as context:
        context.prec = max(28, integer_digits + max(0, fractional_digits) + len(str(len(values))) + 2)
        return sum(values, Decimal(0))


def closed_demo_candle(store, pair, timeframe, *, now_ms, candle_ms=None):
    """Return native exact or complete 5m-derived data, otherwise ``None``.

    Without an exact timestamp, require the latest fully completed UTC bucket.
    Explicit timestamps may read older closed buckets but never future, open,
    negative or unaligned ones. Values use Decimal without an extra float
    conversion; precision already lost in storage cannot be reconstructed.
    """
    now = _timestamp(now_ms)
    interval = TIMEFRAME_MS.get(timeframe) if isinstance(timeframe, str) else None
    if now is None or interval is None or not isinstance(pair, str) or pair not in DEMO_PAIRS:
        return None
    timestamp = (now // interval - 1) * interval if candle_ms is None else _timestamp(candle_ms)
    if timestamp is None or timestamp < 0 or timestamp % interval or timestamp + interval > now:
        return None
    with store._connection() as conn:
        native = conn.execute(
            "SELECT timestamp,open,high,low,close,volume,extras FROM candles "
            "WHERE exchange='binance' AND market='futures' AND candle_type='futures' "
            "AND pair=%s AND timeframe=%s AND timestamp=%s AND extras->>'closed'='true'",
            (pair, timeframe, timestamp),
        ).fetchone()
        valid = _closed_row(native, timestamp)
        if valid is not None:
            return valid
        if interval <= SOURCE_MS or interval % SOURCE_MS:
            return None
        rows = conn.execute(
            "SELECT timestamp,open,high,low,close,volume,extras FROM candles "
            "WHERE exchange='binance' AND market='futures' AND candle_type='futures' "
            "AND pair=%s AND timeframe='5m' AND timestamp>=%s AND timestamp<%s "
            "AND extras->>'closed'='true' ORDER BY timestamp",
            (pair, timestamp, timestamp + interval),
        ).fetchall()
    count = interval // SOURCE_MS
    if len(rows) != count:
        return None
    source = [_closed_row(row, timestamp + index * SOURCE_MS) for index, row in enumerate(rows)]
    if any(row is None for row in source):
        return None
    facts = [{"timestamp": row["timestamp"], **{name: str(row[name]) for name in _FIELDS}}
             for row in source]
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False).encode()).hexdigest()
    return {"timestamp": timestamp, "open": source[0]["open"],
            "high": max(row["high"] for row in source), "low": min(row["low"] for row in source),
            "close": source[-1]["close"], "volume": _sum_exact([row["volume"] for row in source]),
            "extras": {"closed": True, "source_timeframe": "5m", "source_bars": count,
                       "source_input_fingerprint": fingerprint, "resampled_for_demo_reference": True}}
