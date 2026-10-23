"""Synthetic cross-layer contracts for account-tier symbol aliases.

All snapshots below are synthetic review fixtures.  Their ``verified`` flags
exercise an already-reviewed input boundary and do not authenticate any real
account, raw response, credential or exchange connection.
"""

from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from core.execution.account_risk_preflight import (
    RiskConfig,
    TierBand,
    TierProvenance,
    TierSchedule,
    canonical_sha256,
    tier_schedule_snapshot_hash,
    validate_risk_config,
)
from core.execution.symbols import normalize_symbol
from core.execution.tier_snapshot import (
    TierSnapshot,
    load_tier_snapshot,
    tier_snapshot_content_hash,
    tier_snapshot_hash,
)


AS_OF = "2026-10-23T00:00:00Z"
ACCOUNT_SCOPE = "synthetic-contract-account"


def _schedule(symbol: str, leverage: str = "100") -> TierSchedule:
    return TierSchedule(
        symbol=symbol,
        bands=(
            TierBand(
                initial_leverage=Decimal(leverage),
                notional_floor_usdt=Decimal("0"),
                notional_cap_usdt=Decimal("1000000"),
                maintenance_margin_ratio=Decimal("0.004"),
                cumulative_maintenance_usdt=Decimal("0"),
            ),
        ),
        source="synthetic-contract-fixture",
        effective_at_utc=AS_OF,
        fetched_at_utc=AS_OF,
        account_scope=ACCOUNT_SCOPE,
        evidence_sha256="a" * 64,
        verified=True,
        snapshot_kind="account_api_snapshot",
        notional_cap_inclusive=False,
        provenance=TierProvenance(
            endpoint="/fapi/v1/leverageBracket",
            http_method="GET",
            security_type="USER_DATA",
            request_id="synthetic-contract-request",
            signed_at_utc=AS_OF,
            response_sha256="b" * 64,
            environment="testnet",
        ),
    )


def _two_band_schedule(
    symbol: str = "BTCUSDT",
    *,
    second_mmr: str = "0.0100000000000000000000000000001",
    second_cum: str = "0.6",
) -> TierSchedule:
    """Build a high-precision two-band schedule for exact-boundary tests."""

    original = _schedule(symbol)
    return replace(
        original,
        bands=(
            TierBand(
                initial_leverage=Decimal("100"),
                notional_floor_usdt=Decimal("0"),
                notional_cap_usdt=Decimal("100"),
                maintenance_margin_ratio=Decimal("0.004"),
                cumulative_maintenance_usdt=Decimal("0"),
            ),
            TierBand(
                initial_leverage=Decimal("75"),
                notional_floor_usdt=Decimal("100"),
                notional_cap_usdt=Decimal("1000000"),
                maintenance_margin_ratio=Decimal(second_mmr),
                cumulative_maintenance_usdt=Decimal(second_cum),
            ),
        ),
    )


def _single_band_cushion_schedule() -> TierSchedule:
    original = _schedule("BTCUSDT")
    return replace(
        original,
        bands=(
            TierBand(
                initial_leverage=Decimal("400"),
                notional_floor_usdt=Decimal("0"),
                notional_cap_usdt=Decimal("1000000"),
                maintenance_margin_ratio=Decimal("0.004"),
                cumulative_maintenance_usdt=Decimal("0"),
            ),
        ),
    )


def _validate_single_schedule(schedule: TierSchedule, leverage: str):
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
    config = replace(
        _config(frozen_hash, ("BTCUSDT",)),
        max_leverage=Decimal("400"),
        n_max_usdt=Decimal("100"),
        per_symbol_caps_usdt=(("BTCUSDT", Decimal("100")),),
        per_symbol_leverages=(("BTCUSDT", Decimal(leverage)),),
    )
    return validate_risk_config(config, (schedule,), as_of_utc=AS_OF)


def _config(
    snapshot_hash: str,
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
) -> RiskConfig:
    return RiskConfig(
        initial_equity_usdt=Decimal("10000"),
        max_leverage=Decimal("100"),
        margin_mode="isolated",
        n_max_usdt=Decimal("200"),
        per_symbol_caps_usdt=tuple((symbol, Decimal("100")) for symbol in symbols),
        per_symbol_leverages=tuple((symbol, Decimal("75")) for symbol in symbols),
        account_p99_limit_pct=Decimal("0.05"),
        account_scope=ACCOUNT_SCOPE,
        account_loss_authority_scenario_id="A_long",
        tier_snapshot_hash=snapshot_hash,
    )


def _mapping(
    schedules: tuple[TierSchedule, ...],
    *,
    declared_hash: str | None = None,
    schedules_by_symbol: bool = False,
) -> dict[str, object]:
    records = [asdict(item) for item in schedules]
    if schedules_by_symbol:
        records = {
            item.pop("symbol"): item
            for item in records
        }
    return {
        "snapshot_hash": declared_hash or "0" * 64,
        "account_scope": ACCOUNT_SCOPE,
        "as_of_utc": AS_OF,
        "schedules": records,
    }


@pytest.mark.parametrize("by_symbol", [False, True])
def test_compact_typed_schedules_mapping_loader_and_preflight_share_hash(by_symbol):
    compact = (_schedule("BTCUSDT"), _schedule("ETHUSDT", "75"))
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, compact)
    raw = _mapping(compact, declared_hash=frozen_hash, schedules_by_symbol=by_symbol)

    assert tier_snapshot_hash(raw) == frozen_hash
    snapshot = load_tier_snapshot(raw)
    assert tuple(item.symbol for item in snapshot.schedules) == (
        "BTC/USDT:USDT", "ETH/USDT:USDT",
    )
    assert tier_snapshot_content_hash(snapshot) == frozen_hash
    assert tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, snapshot.schedules) == frozen_hash

    config = _config(frozen_hash)
    original_keys = config.symbols
    result = validate_risk_config(config, snapshot.schedules, as_of_utc=AS_OF)
    assert result.status == "pass", (result.reasons, result.missing_assumptions)
    assert result.tier_checks == (("BTCUSDT", "pass"), ("ETHUSDT", "pass"))
    assert config.symbols == original_keys == ("BTCUSDT", "ETHUSDT")


def test_schedule_hash_normalizes_aliases_without_mutating_typed_records():
    compact = (_schedule("BTCUSDT"), _schedule("ETHUSDT", "75"))
    aliases = (
        replace(compact[0], symbol="BTC/USDT:USDT"),
        replace(compact[1], symbol="ETH/USDT"),
    )
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, compact)
    assert tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, aliases) == frozen_hash
    assert tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, tuple(reversed(aliases))) == frozen_hash
    assert tuple(item.symbol for item in compact) == ("BTCUSDT", "ETHUSDT")
    typed_snapshot = TierSnapshot(frozen_hash, ACCOUNT_SCOPE, AS_OF, compact)
    assert tier_snapshot_content_hash(typed_snapshot) == frozen_hash
    assert load_tier_snapshot(typed_snapshot) is typed_snapshot


def test_canonical_decimal_hash_is_exact_and_context_independent():
    first = Decimal("0.004000000000000000000000000001")
    second = Decimal("0.004000000000000000000000000002")
    with localcontext() as context:
        context.prec = 6
        first_hash = canonical_sha256({"margin_ratio": first})
        second_hash = canonical_sha256({"margin_ratio": second})
        assert first_hash != second_hash
        assert canonical_sha256(Decimal("1.00")) == canonical_sha256(Decimal("1"))
        assert canonical_sha256(Decimal("0.000")) == canonical_sha256(Decimal("-0"))
        assert canonical_sha256(Decimal("1E+3")) == canonical_sha256(Decimal("1000"))
    assert canonical_sha256({"margin_ratio": first}) == first_hash
    assert canonical_sha256({"margin_ratio": second}) == second_hash

    original = _schedule("BTCUSDT")
    first_schedule = replace(
        original,
        bands=(replace(original.bands[0], maintenance_margin_ratio=first),),
    )
    second_schedule = replace(
        original,
        bands=(replace(original.bands[0], maintenance_margin_ratio=second),),
    )
    with localcontext() as context:
        context.prec = 6
        first_snapshot_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (first_schedule,))
        second_snapshot_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (second_schedule,))
    assert first_snapshot_hash != second_snapshot_hash
    assert tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (first_schedule,)) == first_snapshot_hash
    assert tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (second_schedule,)) == second_snapshot_hash


@pytest.mark.parametrize("precision", [28, 80])
def test_snapshot_and_preflight_reject_exact_tiny_tier_discontinuity(precision):
    schedule = _two_band_schedule(
        second_mmr="0.01000000000000000000000000000001",
        second_cum="0.6",
    )
    prior, current = schedule.bands
    exact_difference = (
        Fraction(current.notional_floor_usdt) * Fraction(current.maintenance_margin_ratio)
        - Fraction(current.cumulative_maintenance_usdt)
        - Fraction(prior.notional_cap_usdt) * Fraction(prior.maintenance_margin_ratio)
    )
    assert exact_difference == Fraction(Decimal("1e-30"))
    with localcontext() as context:
        context.prec = precision
        frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
        with pytest.raises(ValueError, match="must be continuous"):
            TierSnapshot(frozen_hash, ACCOUNT_SCOPE, AS_OF, (schedule,))
        with pytest.raises(ValueError, match="must be continuous"):
            load_tier_snapshot(_mapping((schedule,), declared_hash=frozen_hash))
        result = _validate_single_schedule(schedule, "75")
        assert result.status == "fail"
        assert result.tier_checks == (("BTCUSDT", "maintenance_margin_discontinuity"),)


@pytest.mark.parametrize("precision", [28, 80])
def test_snapshot_and_preflight_accept_exact_high_precision_continuity(precision):
    schedule = _two_band_schedule(
        second_mmr="0.01000000000000000000000000000001",
        second_cum="0.600000000000000000000000000001",
    )
    with localcontext() as context:
        context.prec = precision
        frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
        snapshot = TierSnapshot(frozen_hash, ACCOUNT_SCOPE, AS_OF, (schedule,))
        assert tier_snapshot_content_hash(snapshot) == frozen_hash
        loaded = load_tier_snapshot(_mapping((schedule,), declared_hash=frozen_hash))
        assert tier_snapshot_content_hash(loaded) == frozen_hash
        result = _validate_single_schedule(schedule, "75")
        assert result.status == "pass", (result.reasons, result.missing_assumptions)
        assert result.tier_checks == (("BTCUSDT", "pass"),)


@pytest.mark.parametrize("precision", [28, 80])
def test_snapshot_and_preflight_reject_exact_tiny_negative_floor_margin(precision):
    schedule = _two_band_schedule(
        second_mmr="0.00999999999999999999999999999999",
        second_cum="1",
    )
    second = schedule.bands[1]
    exact_margin = (
        Fraction(second.notional_floor_usdt) * Fraction(second.maintenance_margin_ratio)
        - Fraction(second.cumulative_maintenance_usdt)
    )
    assert exact_margin == Fraction(Decimal("-1e-30"))
    with localcontext() as context:
        context.prec = precision
        frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
        with pytest.raises(ValueError, match="cannot be negative"):
            TierSnapshot(frozen_hash, ACCOUNT_SCOPE, AS_OF, (schedule,))
        with pytest.raises(ValueError, match="cannot be negative"):
            load_tier_snapshot(_mapping((schedule,), declared_hash=frozen_hash))
        result = _validate_single_schedule(schedule, "75")
        assert result.status == "fail"
        assert result.tier_checks == (("BTCUSDT", "negative_maintenance_margin"),)


@pytest.mark.parametrize("precision", [28, 80])
@pytest.mark.parametrize("mmr,leverage,expected", [
    ("0.00999999999999999999999999999999", "100", "pass"),
    ("0.01000000000000000000000000000001", "100", "fail"),
    ("0.01", "99.9999999999999999999999999999", "pass"),
    ("0.01", "100.0000000000000000000000000001", "fail"),
    ("0.01", "100", "fail"),
])
def test_preflight_initial_margin_cushion_is_exact_at_both_sides(
    precision, mmr, leverage, expected,
):
    original = _single_band_cushion_schedule()
    schedule = replace(
        original,
        bands=(replace(original.bands[0], maintenance_margin_ratio=Decimal(mmr)),),
    )
    margin_difference = (
        Fraction(100) / Fraction(Decimal(leverage))
        - Fraction(100) * Fraction(Decimal(mmr))
    )
    assert (margin_difference > 0) is (expected == "pass")
    if leverage == "100" and mmr != "0.01":
        assert abs(margin_difference) == Fraction(Decimal("1e-30"))
    with localcontext() as context:
        context.prec = precision
        frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
        # Cushion is a selected leverage/config constraint, not a property of
        # an otherwise coherent snapshot. The snapshot must remain loadable.
        assert TierSnapshot(frozen_hash, ACCOUNT_SCOPE, AS_OF, (schedule,))
        result = _validate_single_schedule(schedule, leverage)
        assert result.status == expected, (result.reasons, result.missing_assumptions)
        expected_check = "pass" if expected == "pass" else "maintenance_margin_incompatible"
        assert result.tier_checks == (("BTCUSDT", expected_check),)


@pytest.mark.parametrize("precision", [28, 80])
@pytest.mark.parametrize("cap,expected", [
    ("99.9999999999999999999999999999", "fail"),
    ("100", "pass"),
])
def test_preflight_cap_sum_is_exact_at_n_max_boundary(precision, cap, expected):
    schedule = _schedule("BTCUSDT")
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
    config = replace(
        _config(frozen_hash, ("BTCUSDT",)),
        n_max_usdt=Decimal("100"),
        per_symbol_caps_usdt=(("BTCUSDT", Decimal(cap)),),
    )
    with localcontext() as context:
        context.prec = precision
        result = validate_risk_config(config, (schedule,), as_of_utc=AS_OF)
        assert result.status == expected, (result.reasons, result.missing_assumptions)
        if expected == "fail":
            assert "sum of per-symbol caps must be >= n_max_usdt" in result.reasons


def test_preflight_matches_compact_schedule_to_canonical_config_and_keeps_config_keys():
    compact = (_schedule("BTCUSDT"), _schedule("ETHUSDT", "75"))
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, compact)
    keys = ("BTC/USDT:USDT", "ETH/USDT:USDT")
    config = _config(frozen_hash, keys)
    result = validate_risk_config(config, compact, as_of_utc=AS_OF)
    assert result.status == "pass", (result.reasons, result.missing_assumptions)
    assert result.tier_checks == tuple((symbol, "pass") for symbol in keys)
    assert config.symbols == keys


def test_alias_duplicate_schedules_are_rejected_by_hash_snapshot_and_preflight():
    btc = _schedule("BTCUSDT")
    duplicate_alias = replace(btc, symbol="BTC/USDT:USDT")
    duplicated = (btc, duplicate_alias)
    with pytest.raises(ValueError, match="duplicate tier schedule"):
        tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, duplicated)
    with pytest.raises(ValueError, match="duplicate symbols"):
        TierSnapshot("0" * 64, ACCOUNT_SCOPE, AS_OF, duplicated)
    with pytest.raises(ValueError, match="duplicate symbols"):
        tier_snapshot_hash(_mapping(duplicated))
    with pytest.raises(ValueError, match="duplicate symbols"):
        load_tier_snapshot(_mapping(duplicated))

    # A frozen hash from the valid schedule must not make the alias duplicate
    # collection acceptable at the preflight boundary.
    valid_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (btc,))
    config = replace(_config(valid_hash, ("BTCUSDT",)), n_max_usdt=Decimal("100"))
    result = validate_risk_config(config, duplicated, as_of_utc=AS_OF)
    assert result.status == "fail"
    assert "duplicate tier schedule for BTC/USDT:USDT" in result.reasons


@pytest.mark.parametrize("field,label", [
    ("per_symbol_caps_usdt", "cap"),
    ("per_symbol_leverages", "leverage"),
])
def test_preflight_rejects_alias_duplicates_inside_config_symbol_lists(field, label):
    schedules = (_schedule("BTCUSDT"), _schedule("ETHUSDT", "75"))
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, schedules)
    values = (("BTCUSDT", Decimal("100")), ("BTC/USDT:USDT", Decimal("100")))
    config = replace(_config(frozen_hash), **{field: values})
    result = validate_risk_config(config, schedules, as_of_utc=AS_OF)
    assert result.status == "fail"
    assert (
        f"duplicate per-symbol {label} after symbol normalization: BTC/USDT:USDT"
        in result.reasons
    )


def test_preflight_keeps_exact_config_cap_and_leverage_key_matching():
    schedule = _schedule("BTCUSDT")
    frozen_hash = tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (schedule,))
    config = replace(
        _config(frozen_hash, ("BTCUSDT",)),
        n_max_usdt=Decimal("100"),
        per_symbol_leverages=(("BTC/USDT:USDT", Decimal("75")),),
    )
    result = validate_risk_config(config, (schedule,), as_of_utc=AS_OF)
    assert result.status == "fail"
    assert "actual leverage for BTCUSDT" in result.missing_assumptions
    assert "per-symbol leverage has no matching cap: BTC/USDT:USDT" in result.reasons
    assert config.per_symbol_caps_usdt == (("BTCUSDT", Decimal("100")),)
    assert config.per_symbol_leverages == (("BTC/USDT:USDT", Decimal("75")),)


@pytest.mark.parametrize("alias", ["BTCUSDT", "BTC/USDT", "BTC/USDT:USDT", "BTC-USDT"])
def test_shared_symbol_normalization_supports_existing_usdt_m_aliases(alias):
    assert normalize_symbol(alias) == "BTC/USDT:USDT"


@pytest.mark.parametrize("symbol", [
    None, 123, "", " BTCUSDT", "BTCUSDT ", "BTC/USDC", "BTC/USDT:USDT:USDT",
    "BTC USDT", "BTC?USDT", "BTC_USDT", "BTC@USDT", "BTC/USDT?key=x",
])
def test_shared_symbol_normalization_rejects_invalid_market_identity(symbol):
    with pytest.raises(ValueError):
        normalize_symbol(symbol)
