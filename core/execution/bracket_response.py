"""Strict offline parsing for Binance USDⓈ-M leverage-bracket responses.

Binance documents ``GET /fapi/v1/leverageBracket`` as a USER_DATA endpoint
whose response contains one object per symbol with ``brackets`` records.  This
module only parses already captured response bytes.  It does not make a
request, inspect credentials, verify HMAC signatures, or mark the resulting
records as account-authenticated evidence.

The parser deliberately rejects a non-unit ``notionalCoef``.  A coefficient
changes the meaning of the returned notional bounds and must be resolved by a
separate, account-aware importer rather than guessed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import re
from typing import Any, Mapping

from .account_risk_preflight import TierBand
from .tier_snapshot import normalize_symbol
from .tier_math import exact_margin_offset


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# A compact exponent such as 1e999999999 must not expand into a huge integer
# during the exact rational continuity check.  This conservative bound is far
# above exchange financial-field precision while keeping malformed inputs
# bounded independently of the 8 MiB response-size limit.
MAX_DECIMAL_COMPLEXITY = 4096
_REQUIRED_BRACKET_FIELDS = frozenset(
    {
        "bracket",
        "initialLeverage",
        "notionalCap",
        "notionalFloor",
        "maintMarginRatio",
        "cum",
    }
)
_CREDENTIAL_KEY_FRAGMENTS = (
    "apikey",
    "apisecret",
    "secret",
    "privatekey",
    "signature",
    "hmac",
    "signingkey",
    "mnemonic",
    "seedphrase",
)


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    """A parsed symbol schedule bound to one raw response hash.

    The object intentionally has no ``verified`` or provenance field.  Parsing
    proves only that the bytes were decoded into a coherent bracket shape.
    """

    symbol: str
    bands: tuple[TierBand, ...]
    notional_coefficient: Decimal | None


@dataclass(frozen=True, slots=True)
class ParsedBracketResponse:
    """Immutable result of :func:`parse_bracket_response`."""

    response_sha256: str
    schedules: tuple[ParsedSchedule, ...]


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _json_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("JSON number is outside Decimal limits") from exc
    if not number.is_finite():
        raise ValueError("non-finite JSON number is not allowed")
    parts = number.as_tuple()
    if len(parts.digits) > MAX_DECIMAL_COMPLEXITY or abs(parts.exponent) > MAX_DECIMAL_COMPLEXITY:
        raise ValueError("JSON number exceeds decimal-complexity limit")
    return number


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _credential_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(fragment in normalized for fragment in _CREDENTIAL_KEY_FRAGMENTS)


def _reject_credential_keys(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if isinstance(key, str) and _credential_key(key):
                    raise ValueError(f"credential-like JSON key is not allowed: {key}")
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _require_decimal(value: Any, field: str) -> Decimal:
    # parse_int/parse_float below ensure JSON numbers arrive as Decimal.  Do
    # not accept numeric strings or bools: accepting either would blur the
    # exact raw-response contract and make malformed payloads look valid.
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{field} must be a JSON number")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _require_positive_integer(value: Any, field: str) -> Decimal:
    number = _require_decimal(value, field)
    if number < 1 or number != number.to_integral_value():
        raise ValueError(f"{field} must be a positive integer")
    return number


def _canonical_symbol(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed symbol")
    try:
        normalized = normalize_symbol(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a supported USDT-M symbol") from exc
    if re.fullmatch(r"[A-Z0-9]+/USDT:USDT", normalized) is None:
        raise ValueError(f"{field} is not a supported USDT-M symbol")
    return normalized


def _exact_boundary_margin(notional: Decimal, band: TierBand) -> Fraction:
    """Return ``notional * MMR - cum`` without Decimal context rounding."""
    return exact_margin_offset(
        notional,
        band.maintenance_margin_ratio,
        band.cumulative_maintenance_usdt,
    )

def _coefficient(raw: Mapping[str, Any], symbol: str) -> Decimal | None:
    if "notionalCoef" not in raw:
        return None
    coefficient = _require_decimal(raw["notionalCoef"], f"{symbol}.notionalCoef")
    if coefficient <= 0:
        raise ValueError(f"{symbol}.notionalCoef must be positive")
    if coefficient != Decimal("1"):
        raise ValueError(
            f"{symbol}.notionalCoef={coefficient} is unsupported; only unit coefficient 1 is allowed"
        )
    return coefficient


def _parse_bands(raw: Any, symbol: str) -> tuple[TierBand, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{symbol}.brackets must be a non-empty array")

    bands: list[TierBand] = []
    previous: TierBand | None = None
    for position, item in enumerate(raw, start=1):
        record = _require_mapping(item, f"{symbol}.brackets[{position - 1}]")
        missing = _REQUIRED_BRACKET_FIELDS.difference(record)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"{symbol}.brackets[{position - 1}] missing fields: {names}")
        bracket_number = _require_positive_integer(
            record["bracket"], f"{symbol}.brackets[{position - 1}].bracket"
        )
        if bracket_number != Decimal(position):
            raise ValueError(
                f"{symbol}.brackets bracket numbers must be consecutive from 1"
            )

        initial_leverage = _require_positive_integer(
            record["initialLeverage"],
            f"{symbol}.brackets[{position - 1}].initialLeverage",
        )
        floor = _require_decimal(
            record["notionalFloor"],
            f"{symbol}.brackets[{position - 1}].notionalFloor",
        )
        cap = _require_decimal(
            record["notionalCap"],
            f"{symbol}.brackets[{position - 1}].notionalCap",
        )
        mmr = _require_decimal(
            record["maintMarginRatio"],
            f"{symbol}.brackets[{position - 1}].maintMarginRatio",
        )
        cumulative = _require_decimal(
            record["cum"], f"{symbol}.brackets[{position - 1}].cum"
        )

        if floor < 0 or cap <= floor:
            raise ValueError(f"{symbol}.brackets has invalid notional bounds")
        if position == 1 and floor != Decimal("0"):
            raise ValueError(f"{symbol}.brackets must start at zero notional")
        if position > 1 and previous is not None:
            if floor != previous.notional_cap_usdt:
                raise ValueError(
                    f"{symbol}.brackets notional ranges must be contiguous"
                )
        if not 0 < mmr < 1:
            raise ValueError(
                f"{symbol}.brackets[{position - 1}].maintMarginRatio must be between 0 and 1"
            )
        if cumulative < 0:
            raise ValueError(f"{symbol}.brackets[{position - 1}].cum must be non-negative")
        if position == 1 and cumulative != Decimal("0"):
            raise ValueError(f"{symbol}.brackets first cum must be zero")
        band = TierBand(
            initial_leverage=initial_leverage,
            notional_floor_usdt=floor,
            notional_cap_usdt=cap,
            maintenance_margin_ratio=mmr,
            cumulative_maintenance_usdt=cumulative,
        )
        if previous is not None:
            previous_offset = _exact_boundary_margin(previous.notional_cap_usdt, previous)
            current_offset = _exact_boundary_margin(floor, band)
            if previous_offset != current_offset:
                raise ValueError(f"{symbol}.brackets cum continuity is broken")

        bands.append(band)
        previous = band
    return tuple(bands)


def parse_bracket_response(
    raw: bytes, *, symbols: tuple[str, ...]
) -> ParsedBracketResponse:
    """Parse an offline ``leverageBracket`` response into immutable bands.

    ``raw`` must be the exact response bytes (at most 8 MiB).  The endpoint may
    return either one object or an array.  Every returned record is validated,
    duplicate symbols are rejected across the full response, and ``symbols``
    selects the output order; extra valid symbols in the response are ignored
    after validation.  Every requested symbol must be present.

    The result is structural evidence only.  It contains no authentication or
    account-scope assertion and must not be used to set ``verified=True``.
    """

    if not isinstance(raw, bytes):
        raise TypeError("raw response must be bytes")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("raw response exceeds 8 MiB limit")
    if not isinstance(symbols, tuple) or not symbols:
        raise ValueError("symbols must be a non-empty tuple")

    requested: list[str] = []
    for index, value in enumerate(symbols):
        canonical = _canonical_symbol(value, f"symbols[{index}]")
        if canonical in requested:
            raise ValueError(f"duplicate requested symbol: {canonical}")
        requested.append(canonical)

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("raw response must be valid UTF-8 JSON") from exc
    try:
        root = json.loads(
            decoded,
            parse_int=_json_decimal,
            parse_float=_json_decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except RecursionError as exc:
        raise ValueError("raw response JSON nesting exceeds parser limits") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(
            ("duplicate JSON key", "non-finite JSON number", "JSON number")
        ):
            raise
        raise ValueError("raw response is not valid JSON") from exc
    _reject_credential_keys(root)

    records = root if isinstance(root, list) else [root]
    if not records:
        raise ValueError("bracket response must contain at least one symbol")

    parsed_by_symbol: dict[str, ParsedSchedule] = {}
    for index, item in enumerate(records):
        record = _require_mapping(item, f"response[{index}]")
        symbol = _canonical_symbol(record.get("symbol"), f"response[{index}].symbol")
        if symbol in parsed_by_symbol:
            raise ValueError(f"duplicate symbol in bracket response: {symbol}")
        coefficient = _coefficient(record, symbol)
        bands = _parse_bands(record.get("brackets"), symbol)
        parsed_by_symbol[symbol] = ParsedSchedule(symbol, bands, coefficient)

    missing = [symbol for symbol in requested if symbol not in parsed_by_symbol]
    if missing:
        raise ValueError(f"requested symbols missing from bracket response: {', '.join(missing)}")

    response_hash = hashlib.sha256(raw).hexdigest()
    return ParsedBracketResponse(
        response_sha256=response_hash,
        schedules=tuple(parsed_by_symbol[symbol] for symbol in requested),
    )
