"""Shared symbol normalization for execution and account-risk boundaries.

The repository accepts compact Binance USDT-M symbols (``BTCUSDT``) at
configuration boundaries and CCXT-style symbols (``BTC/USDT:USDT``) in
parsed exchange artifacts.  Both spellings identify the same execution
market and must therefore use one canonical representation before joins or
content hashing.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import re
from typing import Any, Iterable


def normalize_symbol(value: Any) -> str:
    """Return the canonical CCXT-style USDT-M symbol.

    Accepted inputs are compact ``BASEUSDT``, ``BASE/USDT`` and
    ``BASE/USDT:USDT`` spellings (a hyphen in a compact spelling is also
    tolerated for compatibility with existing configuration files).  The
    function is intentionally strict about the quote asset so a symbol alias
    cannot silently select a different market.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"invalid USDT-M symbol: {value}")
    text = value.strip().upper().replace("-", "")
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 2:
            raise ValueError(f"invalid USDT-M symbol: {value}")
        base, quote = parts
        if quote.endswith(":USDT"):
            quote = quote[:-len(":USDT")]
        if re.fullmatch(r"[A-Z0-9]+", base) is None or quote != "USDT":
            raise ValueError(f"unsupported quote asset: {value}")
        return f"{base}/USDT:USDT"
    if text.endswith("USDT") and len(text) > len("USDT"):
        base = text[:-len("USDT")]
        if re.fullmatch(r"[A-Z0-9]+", base) is not None:
            return f"{base}/USDT:USDT"
    raise ValueError(f"invalid USDT-M symbol: {value}")


def normalized_schedule_payload(
    account_scope: str,
    as_of_utc: str,
    schedules: Iterable[Any],
) -> dict[str, object]:
    """Build the shared content payload used by both snapshot hash layers.

    Schedule records are converted through :func:`dataclasses.asdict`, their
    symbols are normalized to CCXT form, duplicate aliases are rejected, and
    records are sorted by canonical symbol.  Sorting makes a sequence and a
    mapping loader produce the same hash while preserving every typed field.
    This helper deliberately does not import the TierSchedule type, avoiding
    a cycle between the preflight and execution snapshot modules.
    """

    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for schedule in schedules:
        if not is_dataclass(schedule) or isinstance(schedule, type):
            raise ValueError("schedules must contain dataclass records")
        record = asdict(schedule)
        symbol = normalize_symbol(record.get("symbol"))
        if symbol in seen:
            raise ValueError(f"duplicate tier schedule for {symbol}")
        seen.add(symbol)
        record["symbol"] = symbol
        records.append(record)
    records.sort(key=lambda item: str(item["symbol"]))
    return {
        "account_scope": account_scope,
        "as_of_utc": as_of_utc,
        "schedules": tuple(records),
    }
