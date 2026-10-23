"""Frozen account-specific Binance maintenance-tier snapshots.

The dated table in :mod:`liquidation_model` is useful for diagnostics, but it
cannot be used to execute a research account.  This module is the small
runtime adapter for a preflight-approved ``account_api_snapshot``.  It keeps
the snapshot hash and account scope attached to every tier lookup so fills and
liquidation evidence can be audited back to the same frozen input.  The
``verified`` and USER_DATA provenance fields are an externally reviewed/manual
attestation at this layer: hashes prove payload consistency, but this module
does not verify Binance HMAC signatures or authenticate raw response bytes.
Callers must not set ``verified=True`` merely because a mapping claims it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Mapping

from .account_risk_preflight import (
    TierBand,
    TierProvenance,
    TierSchedule,
    tier_schedule_snapshot_hash,
)
from .symbols import normalize_symbol
from .tier_math import exact_margin_offset


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _sha(value: Any, field: str) -> str:
    value = _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _band(raw: Any) -> TierBand:
    if isinstance(raw, TierBand):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("tier bands must be TierBand records or mappings")
    upper = raw.get("notional_cap_usdt")
    if upper is None:
        raise ValueError("notional_cap_usdt must be explicitly supplied and finite")
    return TierBand(
        initial_leverage=_decimal(raw.get("initial_leverage", raw.get("max_leverage")), "initial_leverage"),
        notional_floor_usdt=_decimal(raw.get("notional_floor_usdt", raw.get("notional_lower_usdt", 0)), "notional_floor_usdt"),
        notional_cap_usdt=_decimal(upper, "notional_cap_usdt"),
        maintenance_margin_ratio=_decimal(raw.get("maintenance_margin_ratio", raw.get("maintenance_margin_rate", raw.get("mm_rate"))), "maintenance_margin_ratio"),
        cumulative_maintenance_usdt=_decimal(raw.get("cumulative_maintenance_usdt", raw.get("cum", 0)), "cumulative_maintenance_usdt"),
    )


def _provenance(raw: Any) -> TierProvenance | None:
    if raw is None:
        return None
    if isinstance(raw, TierProvenance):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("tier provenance must be a TierProvenance record or mapping")
    return TierProvenance(
        endpoint=_text(raw.get("endpoint"), "provenance.endpoint"),
        http_method=_text(raw.get("http_method"), "provenance.http_method"),
        security_type=_text(raw.get("security_type"), "provenance.security_type"),
        request_id=_text(raw.get("request_id"), "provenance.request_id"),
        signed_at_utc=_text(raw.get("signed_at_utc"), "provenance.signed_at_utc"),
        response_sha256=_sha(raw.get("response_sha256"), "provenance.response_sha256"),
        environment=_text(raw.get("environment"), "provenance.environment"),
    )


def _parse_utc(value: str, field: str) -> datetime:
    text = _text(value, field)
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    return result.astimezone(timezone.utc)


def _schedule(raw: Any) -> TierSchedule:
    if isinstance(raw, TierSchedule):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("tier schedules must be TierSchedule records or mappings")
    bands = raw.get("bands")
    if not isinstance(bands, (tuple, list)) or not bands:
        raise ValueError("tier schedule bands must be a non-empty sequence")
    cap_inclusive = raw.get("notional_cap_inclusive")
    if type(cap_inclusive) is not bool:
        raise ValueError("tier schedule requires an explicit notional_cap_inclusive boolean")
    snapshot_kind = raw.get("snapshot_kind", "account_api_snapshot")
    if snapshot_kind != "account_api_snapshot":
        raise ValueError("static diagnostic tier tables cannot execute orders")
    if raw.get("verified") is not True:
        raise ValueError("tier schedule must be verified before execution")
    return TierSchedule(
        symbol=normalize_symbol(raw.get("symbol")),
        bands=tuple(_band(item) for item in bands),
        source=_text(raw.get("source"), "schedule.source"),
        effective_at_utc=_text(raw.get("effective_at_utc"), "schedule.effective_at_utc"),
        fetched_at_utc=_text(raw.get("fetched_at_utc"), "schedule.fetched_at_utc"),
        account_scope=_text(raw.get("account_scope"), "schedule.account_scope"),
        evidence_sha256=_sha(raw.get("evidence_sha256"), "schedule.evidence_sha256"),
        verified=True,
        snapshot_kind="account_api_snapshot",
        notional_cap_inclusive=cap_inclusive,
        provenance=_provenance(raw.get("provenance")),
    )


@dataclass(frozen=True, slots=True)
class TierSnapshot:
    snapshot_hash: str
    account_scope: str
    as_of_utc: str
    schedules: tuple[TierSchedule, ...]

    def __post_init__(self) -> None:
        _sha(self.snapshot_hash, "snapshot_hash")
        _text(self.account_scope, "account_scope")
        _parse_utc(self.as_of_utc, "as_of_utc")
        if not self.schedules:
            raise ValueError("tier snapshot requires at least one schedule")
        if not isinstance(self.schedules, tuple):
            raise ValueError("tier snapshot schedules must be immutable")
        if any(not isinstance(item, TierSchedule) for item in self.schedules):
            raise ValueError("tier snapshot schedules must contain TierSchedule records")
        symbols = [normalize_symbol(item.symbol) for item in self.schedules]
        if len(symbols) != len(set(symbols)):
            raise ValueError("tier snapshot contains duplicate symbols")
        for item in self.schedules:
            if not isinstance(item, TierSchedule):
                raise ValueError("tier snapshot schedules must contain TierSchedule records")
            if (
                item.account_scope != self.account_scope
                or item.verified is not True
                or item.snapshot_kind != "account_api_snapshot"
                or type(item.notional_cap_inclusive) is not bool
                or not isinstance(item.bands, tuple)
                or not item.bands
            ):
                raise ValueError("tier schedule is not a complete immutable account snapshot")
            for field, value in (
                ("symbol", item.symbol),
                ("source", item.source),
                ("effective_at_utc", item.effective_at_utc),
                ("fetched_at_utc", item.fetched_at_utc),
                ("account_scope", item.account_scope),
            ):
                _text(value, f"schedule.{field}")
            _sha(item.evidence_sha256, "schedule.evidence_sha256")
            effective = _parse_utc(item.effective_at_utc, "schedule.effective_at_utc")
            fetched = _parse_utc(item.fetched_at_utc, "schedule.fetched_at_utc")
            as_of = _parse_utc(self.as_of_utc, "as_of_utc")
            if effective > fetched or fetched > as_of:
                raise ValueError("tier schedule timestamps are inconsistent with snapshot as-of")
            provenance = item.provenance
            if (
                not isinstance(provenance, TierProvenance)
                or provenance.endpoint != "/fapi/v1/leverageBracket"
                or provenance.http_method != "GET"
                or provenance.security_type != "USER_DATA"
                or provenance.environment not in ("production", "testnet", "demo")
            ):
                raise ValueError("tier schedule requires signed USER_DATA provenance")
            _text(provenance.request_id, "provenance.request_id")
            _sha(provenance.response_sha256, "provenance.response_sha256")
            signed_at = _parse_utc(provenance.signed_at_utc, "provenance.signed_at_utc")
            if signed_at > fetched:
                raise ValueError("provenance signed_at_utc is after fetched_at_utc")
            if any(not isinstance(band, TierBand) for band in item.bands):
                raise ValueError("tier bands must be immutable TierBand records")
            bands = sorted(item.bands, key=lambda band: band.notional_floor_usdt)
            # The exchange brackets are an ordered partition of notional
            # space.  Do not silently sort a supplied snapshot here: the
            # serialized order is part of the evidence hash, and accepting a
            # reversed sequence would let one loader choose a different tier
            # from another implementation that iterates the original tuple.
            if tuple(item.bands) != tuple(bands):
                raise ValueError("tier bands must be in ascending notional-floor order")
            if bands[0].notional_floor_usdt != 0:
                raise ValueError("tier bands must start at zero notional")
            for index, band in enumerate(bands):
                values = (
                    band.initial_leverage,
                    band.notional_floor_usdt,
                    band.notional_cap_usdt,
                    band.maintenance_margin_ratio,
                    band.cumulative_maintenance_usdt,
                )
                if any(not isinstance(value, Decimal) or not value.is_finite()
                       for value in values):
                    raise ValueError("tier bands require finite Decimal fields")
                if (
                    band.initial_leverage <= 0
                    or band.notional_floor_usdt < 0
                    or band.notional_cap_usdt <= band.notional_floor_usdt
                    or not 0 < band.maintenance_margin_ratio < 1
                    or band.cumulative_maintenance_usdt < 0
                ):
                    raise ValueError("tier band contains invalid maintenance-margin values")
                if index and bands[index - 1].notional_cap_usdt != band.notional_floor_usdt:
                    raise ValueError("tier bands must be contiguous and non-overlapping")
                if exact_margin_offset(
                    band.notional_floor_usdt,
                    band.maintenance_margin_ratio,
                    band.cumulative_maintenance_usdt,
                ) < 0:
                    raise ValueError("tier maintenance margin cannot be negative")
                if index and (
                    exact_margin_offset(
                        bands[index - 1].notional_cap_usdt,
                        bands[index - 1].maintenance_margin_ratio,
                        bands[index - 1].cumulative_maintenance_usdt,
                    )
                    != exact_margin_offset(
                        band.notional_floor_usdt,
                        band.maintenance_margin_ratio,
                        band.cumulative_maintenance_usdt,
                    )
                ):
                    raise ValueError("tier maintenance schedule must be continuous")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(normalize_symbol(item.symbol) for item in self.schedules))

    def get_schedule(self, symbol: Any) -> TierSchedule:
        wanted = normalize_symbol(symbol)
        for schedule in self.schedules:
            if normalize_symbol(schedule.symbol) == wanted:
                return schedule
        raise KeyError(f"no frozen tier schedule for {wanted}")

    def get_tier(self, symbol: Any, notional: Decimal) -> TierBand:
        schedule = self.get_schedule(symbol)
        value = _decimal(notional, "notional")
        if value < 0:
            raise ValueError("notional must be non-negative")
        # __post_init__ rejects unsorted schedules.  Keep this defensive check
        # at the lookup boundary as well so a future mutable adapter cannot
        # reintroduce order-dependent tier selection.
        bands = tuple(schedule.bands)
        if bands != tuple(sorted(bands, key=lambda band: band.notional_floor_usdt)):
            raise ValueError("tier bands must be in ascending notional-floor order")
        for band in bands:
            lower = band.notional_floor_usdt
            upper = band.notional_cap_usdt
            in_band = lower <= value <= upper if schedule.notional_cap_inclusive else lower <= value < upper
            if in_band:
                return band
        raise ValueError(f"notional {value} is outside frozen schedule for {normalize_symbol(symbol)}")


def tier_snapshot_content_hash(snapshot: TierSnapshot) -> str:
    """Hash normalized snapshot content, excluding its declared hash field."""
    if not isinstance(snapshot, TierSnapshot):
        raise ValueError("snapshot must be a validated TierSnapshot")
    return tier_schedule_snapshot_hash(
        snapshot.account_scope,
        snapshot.as_of_utc,
        snapshot.schedules,
    )


def tier_snapshot_hash(raw: Any) -> str:
    """Return the declared hash for a raw snapshot before it is frozen.

    Test and ingestion callers may need to calculate the hash before adding
    ``snapshot_hash`` to a serialized mapping.  The mapping is normalized by
    the same parser used by :func:`load_tier_snapshot`; no unvalidated fields
    are included in the result.
    """
    if isinstance(raw, TierSnapshot):
        return tier_snapshot_content_hash(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("tier_snapshot must be a TierSnapshot or mapping")
    raw_schedules = raw.get("schedules")
    if isinstance(raw_schedules, Mapping):
        raw_schedules = [dict(value, symbol=key) if isinstance(value, Mapping) else value
                         for key, value in raw_schedules.items()]
    if not isinstance(raw_schedules, (tuple, list)):
        raise ValueError("tier_snapshot.schedules must be a sequence or mapping")
    snapshot = TierSnapshot(
        snapshot_hash="0" * 64,
        account_scope=_text(raw.get("account_scope"), "account_scope"),
        as_of_utc=_text(raw.get("as_of_utc"), "as_of_utc"),
        schedules=tuple(_schedule(item) for item in raw_schedules),
    )
    return tier_snapshot_content_hash(snapshot)


def load_tier_snapshot(raw: Any) -> TierSnapshot:
    """Parse an execution snapshot and fail closed on any malformed field."""
    if isinstance(raw, TierSnapshot):
        snapshot = raw
    elif not isinstance(raw, Mapping):
        raise ValueError("tier_snapshot must be a TierSnapshot or mapping")
    else:
        raw_schedules = raw.get("schedules")
        if isinstance(raw_schedules, Mapping):
            raw_schedules = [dict(value, symbol=key) if isinstance(value, Mapping) else value
                             for key, value in raw_schedules.items()]
        if not isinstance(raw_schedules, (tuple, list)):
            raise ValueError("tier_snapshot.schedules must be a sequence or mapping")
        snapshot = TierSnapshot(
            snapshot_hash=_sha(raw.get("snapshot_hash"), "snapshot_hash"),
            account_scope=_text(raw.get("account_scope"), "account_scope"),
            as_of_utc=_text(raw.get("as_of_utc"), "as_of_utc"),
            schedules=tuple(_schedule(item) for item in raw_schedules),
        )
    # Every schedule must carry the same account scope and a provenance record
    # for the signed USER_DATA response.  Preflight remains the authoritative
    # validator for freshness and cumulative-band continuity; the execution
    # adapter rejects an obviously unproven snapshot before any order is made.
    actual_hash = tier_snapshot_content_hash(snapshot)
    if actual_hash != snapshot.snapshot_hash:
        raise ValueError("tier snapshot content does not match snapshot_hash")
    return snapshot
