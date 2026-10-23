import hashlib
import json
from decimal import Decimal, localcontext

import pytest

from core.execution.bracket_response import (
    MAX_RESPONSE_BYTES,
    ParsedBracketResponse,
    parse_bracket_response,
)


def _record(symbol="BTCUSDT", *, coefficient=None):
    record = {
        "symbol": symbol,
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": 125,
                "notionalCap": 10000,
                "notionalFloor": 0,
                "maintMarginRatio": 0.004,
                "cum": 0,
            },
            {
                "bracket": 2,
                "initialLeverage": 100,
                "notionalCap": 50000,
                "notionalFloor": 10000,
                "maintMarginRatio": 0.005,
                "cum": 10,
            },
        ],
    }
    if coefficient is not None:
        record["notionalCoef"] = coefficient
    return record


def _raw(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_parses_object_and_preserves_exact_decimal_values_and_hash():
    raw = _raw(_record())
    parsed = parse_bracket_response(raw, symbols=("BTC/USDT:USDT",))

    assert isinstance(parsed, ParsedBracketResponse)
    assert parsed.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert parsed.schedules[0].symbol == "BTC/USDT:USDT"
    assert parsed.schedules[0].notional_coefficient is None
    assert parsed.schedules[0].bands[0].maintenance_margin_ratio == Decimal("0.004")
    assert parsed.schedules[0].bands[1].cumulative_maintenance_usdt == Decimal("10")


def test_decimal_json_tokens_keep_precision_without_a_float_round_trip():
    raw = b'{"symbol":"BTCUSDT","brackets":[{"bracket":1,"initialLeverage":125,"notionalCap":123456789123456789.123456789,"notionalFloor":0,"maintMarginRatio":0.004000000000000000000000001,"cum":0}]}'
    band = parse_bracket_response(raw, symbols=("BTCUSDT",)).schedules[0].bands[0]
    assert band.notional_cap_usdt == Decimal("123456789123456789.123456789")
    assert band.maintenance_margin_ratio == Decimal("0.004000000000000000000000001")
    assert all(
        isinstance(value, Decimal)
        for value in (
            band.initial_leverage,
            band.notional_floor_usdt,
            band.notional_cap_usdt,
            band.maintenance_margin_ratio,
            band.cumulative_maintenance_usdt,
        )
    )


def test_raw_response_hash_changes_for_mutated_bytes_without_authenticating_content():
    raw = _raw(_record())
    original = parse_bracket_response(raw, symbols=("BTCUSDT",))
    whitespace_mutated = parse_bracket_response(raw + b"\n", symbols=("BTCUSDT",))
    payload_mutated = parse_bracket_response(raw.replace(b'"initialLeverage":125', b'"initialLeverage":120'), symbols=("BTCUSDT",))
    assert original.schedules == whitespace_mutated.schedules
    assert original.response_sha256 != whitespace_mutated.response_sha256
    assert original.response_sha256 != payload_mutated.response_sha256
    assert original.schedules != payload_mutated.schedules
    assert not hasattr(original, "verified")
    assert not hasattr(original.schedules[0], "verified")


def test_parses_array_and_returns_requested_order_while_validating_extra_records():
    raw = _raw([_record("ETHUSDT"), _record("BTCUSDT")])
    parsed = parse_bracket_response(raw, symbols=("BTCUSDT", "ETHUSDT"))
    assert tuple(item.symbol for item in parsed.schedules) == (
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    )


def test_unit_notional_coefficient_is_allowed_but_non_unit_is_rejected():
    parsed = parse_bracket_response(_raw(_record(coefficient=1.0)), symbols=("BTCUSDT",))
    assert parsed.schedules[0].notional_coefficient == Decimal("1.0")
    with pytest.raises(ValueError, match="notionalCoef"):
        parse_bracket_response(_raw(_record(coefficient=1.25)), symbols=("BTCUSDT",))


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda r: r["brackets"][0].update(notionalFloor=1), "zero notional"),
        (lambda r: r["brackets"][1].update(bracket=3), "consecutive"),
        (lambda r: r["brackets"][1].update(notionalFloor=11000), "contiguous"),
        (lambda r: r["brackets"][1].update(cum=11), "continuity"),
        (lambda r: r["brackets"][0].update(maintMarginRatio=-0.1), "between 0 and 1"),
        (lambda r: r["brackets"][0].update(cum=1), "first cum"),
    ],
)
def test_rejects_malformed_brackets(mutation, message):
    record = _record()
    mutation(record)
    with pytest.raises(ValueError, match=message):
        parse_bracket_response(_raw(record), symbols=("BTCUSDT",))

def test_cumulative_boundary_continuity_uses_exact_decimal_arithmetic():
    raw = b'{"symbol":"BTCUSDT","brackets":[' \
        b'{"bracket":1,"initialLeverage":100,"notionalCap":1000000000000000000000000000000,"notionalFloor":0,"maintMarginRatio":0.1234567890123456789012345678,"cum":0},' \
        b'{"bracket":2,"initialLeverage":50,"notionalCap":2000000000000000000000000000000,"notionalFloor":1000000000000000000000000000000,"maintMarginRatio":0.1234567890123456789012345678,"cum":1}]}'
    with pytest.raises(ValueError, match="continuity"):
        parse_bracket_response(raw, symbols=("BTCUSDT",))


def test_continuity_check_is_independent_of_callers_decimal_context():
    raw = b'{"symbol":"BTCUSDT","brackets":[' \
        b'{"bracket":1,"initialLeverage":100,"notionalCap":1000000000000000000000000000000,"notionalFloor":0,"maintMarginRatio":0.1234567890123456789012345678,"cum":0},' \
        b'{"bracket":2,"initialLeverage":50,"notionalCap":2000000000000000000000000000000,"notionalFloor":1000000000000000000000000000000,"maintMarginRatio":0.2469135780246913578024691356,"cum":123456789012345678901234567800}]}'
    with localcontext() as context:
        context.prec = 6
        parsed = parse_bracket_response(raw, symbols=("BTCUSDT",))
    assert parsed.schedules[0].bands[1].cumulative_maintenance_usdt == Decimal(
        "123456789012345678901234567800"
    )


def test_rejects_duplicate_symbols_and_missing_requested_symbol():
    with pytest.raises(ValueError, match="duplicate symbol"):
        parse_bracket_response(_raw([_record(), _record("BTC/USDT:USDT")]), symbols=("BTCUSDT",))
    with pytest.raises(ValueError, match="missing"):
        parse_bracket_response(_raw(_record()), symbols=("ETHUSDT",))


def test_rejects_duplicate_json_keys_and_credential_like_fields():
    duplicate = b'{"symbol":"BTCUSDT","symbol":"ETHUSDT","brackets":[]}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_bracket_response(duplicate, symbols=("BTCUSDT",))
    for key in ("apiKey", "BINANCE_API_SECRET", "X-MBX-APIKEY", "requestSignature"):
        record = _record()
        record[key] = "redacted"
        with pytest.raises(ValueError, match="credential"):
            parse_bracket_response(_raw(record), symbols=("BTCUSDT",))


def test_rejects_non_numeric_strings_bool_and_non_finite_numbers():
    record = _record()
    record["brackets"][0]["initialLeverage"] = "125"
    with pytest.raises(ValueError, match="JSON number"):
        parse_bracket_response(_raw(record), symbols=("BTCUSDT",))
    record = _record()
    record["brackets"][0]["cum"] = True
    with pytest.raises(ValueError, match="JSON number"):
        parse_bracket_response(_raw(record), symbols=("BTCUSDT",))
    with pytest.raises(ValueError, match="non-finite"):
        parse_bracket_response(b'{"symbol":"BTCUSDT","notionalCoef":NaN,"brackets":[]}', symbols=("BTCUSDT",))


def test_rejects_invalid_root_and_oversized_bytes():
    with pytest.raises(ValueError, match="at least one symbol"):
        parse_bracket_response(b"[]", symbols=("BTCUSDT",))
    with pytest.raises(ValueError, match="8 MiB"):
        parse_bracket_response(b" " * (MAX_RESPONSE_BYTES + 1), symbols=("BTCUSDT",))


def test_extra_unrequested_symbol_is_still_validated():
    extra = _record("ETHUSDT", coefficient=1.25)
    with pytest.raises(ValueError, match="notionalCoef"):
        parse_bracket_response(_raw([_record(), extra]), symbols=("BTCUSDT",))


@pytest.mark.parametrize("field", ["bracket", "initialLeverage", "notionalCap", "notionalFloor", "maintMarginRatio", "cum"])
def test_bool_is_not_accepted_for_any_numeric_bracket_field(field):
    record = _record()
    record["brackets"][0][field] = True
    with pytest.raises(ValueError, match="JSON number"):
        parse_bracket_response(_raw(record), symbols=("BTCUSDT",))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_rejected_globally(token):
    raw = _raw(_record())[:-1] + b',"ignored":' + token.encode() + b"}"
    with pytest.raises(ValueError, match="non-finite"):
        parse_bracket_response(raw, symbols=("BTCUSDT",))


def test_invalid_utf8_deep_json_and_raw_type_are_rejected():
    with pytest.raises(ValueError, match="UTF-8"):
        parse_bracket_response(b"\xff", symbols=("BTCUSDT",))
    with pytest.raises(ValueError, match=r"response\[0\] must be an object"):
        parse_bracket_response(b"[" * 2000 + b"]" * 2000, symbols=("BTCUSDT",))
    with pytest.raises(TypeError, match="bytes"):
        parse_bracket_response("{}", symbols=("BTCUSDT",))


def test_rejects_duplicate_requested_aliases_and_non_unit_coefficient_string():
    with pytest.raises(ValueError, match="duplicate requested"):
        parse_bracket_response(_raw(_record()), symbols=("BTCUSDT", "BTC/USDT:USDT"))
    record = _record(coefficient="1")
    with pytest.raises(ValueError, match="JSON number"):
        parse_bracket_response(_raw(record), symbols=("BTCUSDT",))
