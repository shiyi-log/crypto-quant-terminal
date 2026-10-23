"""Pure, evidence-gated account risk preflight primitives.

This module deliberately does not import strategy, signal, backtest, or return
code.  It calculates deterministic gap summaries and linear stress envelopes;
an account preflight cannot pass unless frozen execution assumptions and
synchronized account-window loss evidence are supplied.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from fractions import Fraction
from typing import Literal, Mapping, Sequence

from .symbols import normalize_symbol, normalized_schedule_payload
from .tier_math import exact_margin_offset

Status = Literal["pass", "fail", "not_estimable"]
Side = Literal["long", "short"]
Path = Literal["trade", "mark"]

REQUIRED_EXECUTION_EVIDENCE = (
    "maintenance_margin_bracket_formula",
    "synchronized_trade_mark_windows",
    "mark_price_liquidation_trigger",
    "trade_price_forced_fill",
    "fees_slippage_and_1000ms_delay",
    "symbol_tick_sizes_and_liquidation_fee",
    "settled_funding_cashflows",
    "bankruptcy_insurance_adl_and_deficit_rules",
)

# A single account-window loss series is not enough evidence for the frozen
# pressure set.  Every scenario must be run against the same config and carry
# its own immutable evidence hash.  Keeping these names here makes the gate
# auditable and prevents a caller from accidentally treating a preliminary
# gap calculation as a final account-level pass.
REQUIRED_SCENARIO_EVIDENCE = (
    "A_long",
    "A_short",
    "B",
    "C",
    "joint_marginal_p99",
    "synthetic_max_envelope",
    "A_long_trade_only",
    "A_long_mark_only",
    "A_short_trade_only",
    "A_short_mark_only",
    "B_trade_only",
    "B_mark_only",
    "C_trade_only",
    "C_mark_only",
    "A_long_liquidation_1tick",
    "A_long_liquidation_5tick",
    "A_short_liquidation_1tick",
    "A_short_liquidation_5tick",
    "B_liquidation_1tick",
    "B_liquidation_5tick",
    "C_liquidation_1tick",
    "C_liquidation_5tick",
)

# These scenarios are synchronized account-window loss distributions and
# therefore receive the user's 5% P99 hard gate.  The one-/five-tick
# liquidation variants are separate executions of the same A/B/C account
# windows; they are part of the frozen execution pressure set and cannot be
# treated as disclosure-only evidence.  Marginal/synthetic packages remain
# disclosures because they are not synchronized account-window distributions.
BASE_ACCOUNT_LOSS_P99_SCENARIOS = frozenset({"A_long", "A_short", "B", "C"})
ACCOUNT_LOSS_P99_SCENARIOS = BASE_ACCOUNT_LOSS_P99_SCENARIOS | frozenset(
    {
        "A_long",
        "A_short",
        "B",
        "C",
        "A_long_liquidation_1tick",
        "A_long_liquidation_5tick",
        "A_short_liquidation_1tick",
        "A_short_liquidation_5tick",
        "B_liquidation_1tick",
        "B_liquidation_5tick",
        "C_liquidation_1tick",
        "C_liquidation_5tick",
    }
)
ACCOUNT_LOSS_AUTHORITY_ORDER = ("A_long", "A_short", "B", "C")


def _scenario_expected_stress(scenario_id: str) -> tuple[str, Decimal, int, int]:
    """Return path, slippage multiple, delay, and liquidation ticks.

    The values are part of the frozen pressure-set contract.  They are
    carried as structured fields on :class:`ScenarioEvidence` so a caller
    cannot claim a generic ``pass`` hash without showing which execution
    stress was actually run.
    """
    # Do not infer a frozen pressure contract from a suffix alone.  For
    # example, accepting ``research_liquidation_1tick`` merely because it
    # ends in ``_liquidation_1tick`` would let an extra, unregistered scenario
    # enter the evidence set without an explicit review decision.
    if scenario_id not in REQUIRED_SCENARIO_EVIDENCE:
        raise ValueError(f"unknown frozen scenario id: {scenario_id}")
    if scenario_id in BASE_ACCOUNT_LOSS_P99_SCENARIOS:
        return ("trade_mark", Decimal("2"), 1000, 0)
    if scenario_id.endswith("_trade_only"):
        return ("trade_only", Decimal("2"), 1000, 0)
    if scenario_id.endswith("_mark_only"):
        return ("mark_only", Decimal("2"), 1000, 0)
    if scenario_id == "joint_marginal_p99":
        return ("joint_marginal", Decimal("2"), 1000, 0)
    if scenario_id == "synthetic_max_envelope":
        return ("synthetic_max", Decimal("2"), 1000, 0)
    for ticks in (1, 5):
        suffix = f"_liquidation_{ticks}tick"
        if scenario_id.endswith(suffix):
            return ("trade_mark", Decimal("2"), 1000, ticks)
    raise ValueError(f"unknown frozen scenario id: {scenario_id}")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite Decimal values are not canonical")
    if value == 0:
        return "0"
    # Decimal.normalize() applies the active context precision and can round
    # evidence before hashing.  Formatting the original Decimal is exact and
    # trimming only fractional zeros keeps equivalent numeric spellings
    # canonical without changing significant digits.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float values are not canonical")
        raise TypeError("floats are not accepted in canonical evidence; use Decimal")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_sha256(value: object) -> str:
    """Return SHA-256 of stable UTF-8 canonical JSON; floats are rejected."""
    payload = json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def tier_schedule_snapshot_hash(
    account_scope: str,
    as_of_utc: str,
    schedules: Sequence["TierSchedule"],
) -> str:
    """Hash the complete typed schedule set used by one frozen preflight.

    The execution adapter uses the same normalized content shape for a
    ``TierSnapshot``.  Keeping this helper here avoids accepting a set of
    individually valid schedule hashes that came from different snapshots.
    The hash proves content identity only; it does not authenticate the
    exchange response or its USER_DATA signature.
    """
    if not isinstance(account_scope, str) or not account_scope.strip():
        raise ValueError("account_scope must be a non-empty string")
    if not isinstance(as_of_utc, str) or not as_of_utc.strip():
        raise ValueError("as_of_utc must be a non-empty string")
    if not isinstance(schedules, tuple) or any(
        not isinstance(item, TierSchedule) for item in schedules
    ):
        raise ValueError("schedules must be an immutable tuple of TierSchedule records")
    return canonical_sha256(
        normalized_schedule_payload(account_scope, as_of_utc, schedules)
    )


@dataclass(frozen=True, slots=True)
class RiskConfig:
    initial_equity_usdt: Decimal
    max_leverage: Decimal
    margin_mode: Literal["isolated", "cross"]
    n_max_usdt: Decimal
    per_symbol_caps_usdt: tuple[tuple[str, Decimal], ...]
    per_symbol_leverages: tuple[tuple[str, Decimal], ...]
    account_p99_limit_pct: Decimal
    required_n_gap: int = 100
    required_account_windows: int = 100
    account_scope: str | None = None
    tier_snapshot_max_age_hours: Decimal = Decimal("24")
    account_loss_authority_scenario_id: str | None = None
    tier_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "per_symbol_caps_usdt",
            tuple(sorted(self.per_symbol_caps_usdt, key=lambda item: item[0])),
        )
        object.__setattr__(
            self,
            "per_symbol_leverages",
            tuple(sorted(self.per_symbol_leverages, key=lambda item: item[0])),
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(symbol for symbol, _ in self.per_symbol_caps_usdt))

    @property
    def leverage_by_symbol(self) -> dict[str, Decimal]:
        return dict(self.per_symbol_leverages)

    @property
    def config_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class TierBand:
    """One exchange bracket from a frozen account/symbol snapshot.

    ``initial_leverage`` and the four notional/margin fields mirror the
    exchange's bracket response.  They are intentionally explicit rather than
    inferred from :mod:`liquidation_model`, whose dated static table is only a
    diagnostic aid.
    """

    initial_leverage: Decimal
    notional_floor_usdt: Decimal
    notional_cap_usdt: Decimal
    maintenance_margin_ratio: Decimal
    cumulative_maintenance_usdt: Decimal

    @property
    def notional_lower_usdt(self) -> Decimal:
        return self.notional_floor_usdt

    @property
    def notional_upper_usdt(self) -> Decimal:
        return self.notional_cap_usdt

    @property
    def maintenance_margin_rate(self) -> Decimal:
        return self.maintenance_margin_ratio

    @property
    def max_leverage(self) -> Decimal:
        return self.initial_leverage


@dataclass(frozen=True, slots=True)
class TierProvenance:
    """Externally reviewed request/response metadata, not API authentication.

    This record cannot authenticate a USER_DATA response.  Its metadata and
    hashes bind the supplied payload only; verification of response origin
    and the relationship between raw response bytes and parsed bands remains
    an external/manual prerequisite.
    """

    endpoint: str
    http_method: str
    security_type: str
    request_id: str
    signed_at_utc: str
    response_sha256: str
    environment: Literal["production", "testnet", "demo"]


@dataclass(frozen=True, slots=True)
class TierSchedule:
    """Immutable bracket data with an external/manual review attestation.

    ``verified=True`` means a reviewer approved the source evidence; it must
    not be inferred solely from caller-provided USER_DATA labels or hashes.
    """

    symbol: str
    bands: tuple[TierBand, ...]
    source: str
    effective_at_utc: str
    fetched_at_utc: str
    account_scope: str
    evidence_sha256: str
    verified: bool
    snapshot_kind: Literal["account_api_snapshot", "static_diagnostic"]
    notional_cap_inclusive: bool
    provenance: TierProvenance | None = None


@dataclass(frozen=True, slots=True)
class ConfigValidation:
    status: Status
    reasons: tuple[str, ...]
    missing_assumptions: tuple[str, ...]
    tier_checks: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class GapObservation:
    """A candidate-free adjacent-open gap observation, as a signed fraction."""

    gap_fraction: Decimal
    prior_open_time_ms: int
    next_open_time_ms: int
    valid: bool = True
    has_valid_next_bar: bool = True

    @property
    def is_valid_adjacency(self) -> bool:
        return (
            self.valid
            and self.has_valid_next_bar
            and self.next_open_time_ms - self.prior_open_time_ms == 900_000
        )


@dataclass(frozen=True, slots=True)
class QuantileResult:
    status: Status
    p99: Decimal | None
    maximum: Decimal | None
    sample_size: int
    required_sample_size: int
    excluded_count: int = 0
    rank: int | None = None


def _quantile_result_issue(result: object, label: str = "quantile") -> str | None:
    """Return a deterministic validation error for a quantile record.

    A hand-constructed ``QuantileResult(status='pass', sample_size=1)`` must
    never be enough to enter an A/B/C envelope.  The result therefore carries
    its own sample, rank, and finite-value proof and is checked before any
    adverse rate is consumed.
    """
    if not isinstance(result, QuantileResult):
        return f"{label} must be a QuantileResult"
    if result.status not in ("pass", "fail", "not_estimable"):
        return f"{label} has invalid status"
    if not isinstance(result.required_sample_size, int) or result.required_sample_size < 1:
        return f"{label} required_sample_size must be a positive integer"
    if not isinstance(result.sample_size, int) or result.sample_size < 0:
        return f"{label} sample_size must be a non-negative integer"
    if not isinstance(result.excluded_count, int) or result.excluded_count < 0:
        return f"{label} excluded_count must be a non-negative integer"
    if result.p99 is not None and not _decimal_is_positive(result.p99):
        return f"{label} p99 must be a finite positive Decimal when present"
    if result.maximum is not None and not _decimal_is_positive(result.maximum):
        return f"{label} maximum must be a finite positive Decimal when present"
    if result.p99 is not None and result.maximum is not None and result.p99 > result.maximum:
        return f"{label} p99 cannot exceed maximum"
    if result.status == "pass":
        if result.sample_size < result.required_sample_size:
            return f"{label} pass status has too few samples"
        expected_rank = math.ceil(0.99 * result.sample_size)
        if result.rank != expected_rank:
            return f"{label} rank does not match nearest-rank P99"
        if result.p99 is None or result.maximum is None:
            return f"{label} pass status requires p99 and maximum"
    elif result.status == "not_estimable" and result.sample_size >= result.required_sample_size:
        # A caller cannot downgrade a complete sample to not_estimable while
        # leaving a seemingly usable P99 value for envelope construction.
        if result.p99 is not None or result.maximum is not None:
            return f"{label} not_estimable record cannot carry a complete P99"
    return None


def _gap_p99_issue(item: object, required_sample_size: int | None = None) -> str | None:
    if not isinstance(item, GapP99):
        return "gap P99 entries must be GapP99 records"
    if not isinstance(item.symbol, str) or not item.symbol or item.symbol != item.symbol.strip():
        return "gap P99 symbol must be non-empty and trimmed"
    if item.side not in ("long", "short"):
        return f"gap P99 side for {item.symbol} is invalid"
    trade_issue = _quantile_result_issue(item.trade, f"{item.symbol} {item.side} trade P99")
    if trade_issue:
        return trade_issue
    mark_issue = _quantile_result_issue(item.mark, f"{item.symbol} {item.side} mark P99")
    if mark_issue:
        return mark_issue
    if required_sample_size is not None and any(
        result.required_sample_size != required_sample_size
        or result.sample_size < required_sample_size
        for result in (item.trade, item.mark)
    ):
        return f"{item.symbol} {item.side} P99 sample does not meet RiskConfig.required_n_gap"
    return None


@dataclass(frozen=True, slots=True)
class GapP99:
    symbol: str
    side: Side
    trade: QuantileResult
    mark: QuantileResult

    @property
    def status(self) -> Status:
        if not isinstance(self.trade, QuantileResult) or not isinstance(self.mark, QuantileResult):
            return "not_estimable"
        if self.trade.status == "fail" or self.mark.status == "fail":
            return "fail"
        if _quantile_result_issue(self.trade) or _quantile_result_issue(self.mark):
            return "not_estimable"
        if self.trade.status != "pass" or self.mark.status != "pass":
            return "not_estimable"
        return "pass"

    @property
    def adverse_rate(self) -> Decimal | None:
        if self.status != "pass" or self.trade.p99 is None or self.mark.p99 is None:
            return None
        return max(self.trade.p99, self.mark.p99)


@dataclass(frozen=True, slots=True)
class Exposure:
    symbol: str
    side: Side
    notional_usdt: Decimal
    adverse_rate: Decimal

    @property
    def linear_loss_usdt(self) -> Decimal:
        return self.notional_usdt * self.adverse_rate


@dataclass(frozen=True, slots=True)
class RiskEnvelope:
    envelope_id: str
    status: Status
    exposures: tuple[Exposure, ...]
    total_notional_usdt: Decimal
    linear_loss_usdt: Decimal | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Hashes and account-window losses produced by a separately frozen runner.

    Hash presence makes inputs auditable; it does not certify that an exchange
    model is correct. The caller must only supply these after independent
    evidence review.
    """

    model_sha256: str | None
    window_data_sha256: str | None
    assumption_hashes: tuple[tuple[str, str], ...]
    account_window_losses_usdt: tuple[Decimal, ...] | None
    uncovered_deficits_usdt: tuple[Decimal, ...] | None
    tier_snapshot_hash: str | None = None
    scenario_evidence: tuple["ScenarioEvidence", ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioEvidence:
    """Evidence for one frozen account-risk scenario.

    ``config_hash`` binds the result to the exact risk configuration.  A
    status of ``fail`` is a hard risk failure; a missing or malformed record
    remains ``not_estimable`` instead of being inferred from another scenario.
    """

    scenario_id: str
    config_hash: str
    evidence_sha256: str
    window_data_sha256: str | None
    account_window_losses_usdt: tuple[Decimal, ...]
    uncovered_deficits_usdt: tuple[Decimal, ...]
    model_sha256: str | None = None
    tier_snapshot_hash: str | None = None
    assumption_hashes: tuple[tuple[str, str], ...] = ()
    status: Literal["pass", "fail"] = "pass"
    execution_path: str | None = None
    slippage_multiple: Decimal | None = None
    delay_ms: int | None = None
    liquidation_slippage_ticks: int | None = None


def _scenario_payload(item: ScenarioEvidence) -> dict[str, object]:
    return {
        "scenario_id": item.scenario_id,
        "config_hash": item.config_hash,
        "model_sha256": item.model_sha256,
        "tier_snapshot_hash": item.tier_snapshot_hash,
        "assumption_hashes": item.assumption_hashes,
        "window_data_sha256": item.window_data_sha256,
        "account_window_losses_usdt": item.account_window_losses_usdt,
        "uncovered_deficits_usdt": item.uncovered_deficits_usdt,
        "status": item.status,
        "execution_path": item.execution_path,
        "slippage_multiple": item.slippage_multiple,
        "delay_ms": item.delay_ms,
        "liquidation_slippage_ticks": item.liquidation_slippage_ticks,
    }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    status: Status
    reasons: tuple[str, ...]
    missing_assumptions: tuple[str, ...]
    config_hash: str
    tier_snapshot_hash: str | None
    tier_evidence_hashes: tuple[tuple[str, str], ...]
    gap_evidence_hash: str | None
    execution_model_hash: str | None
    window_data_hash: str | None
    account_window_p99_loss_usdt: Decimal | None
    account_window_max_loss_usdt: Decimal | None
    account_window_sample_size: int
    envelopes: tuple[RiskEnvelope, ...]


PREFLIGHT_REPORT_SCHEMA_VERSION = "account-risk-preflight-v1"


def preflight_report_to_mapping(report: PreflightReport) -> dict[str, object]:
    """Serialize a report without losing Decimal precision or evidence order.

    The mapping is deliberately limited to report fields.  It contains no
    input paths, raw account metadata, request parameters, or credentials, so
    it can be written to a private evidence directory by a separate CLI.
    """
    if not isinstance(report, PreflightReport):
        raise TypeError("report must be a PreflightReport")

    def decimal_or_none(value: Decimal | None) -> str | None:
        return None if value is None else _decimal_text(value)

    def exposure_mapping(item: Exposure) -> dict[str, object]:
        return {
            "symbol": item.symbol,
            "side": item.side,
            "notional_usdt": _decimal_text(item.notional_usdt),
            "adverse_rate": _decimal_text(item.adverse_rate),
            "linear_loss_usdt": _decimal_text(item.linear_loss_usdt),
        }

    def envelope_mapping(item: RiskEnvelope) -> dict[str, object]:
        return {
            "envelope_id": item.envelope_id,
            "status": item.status,
            "exposures": [exposure_mapping(exposure) for exposure in item.exposures],
            "total_notional_usdt": _decimal_text(item.total_notional_usdt),
            "linear_loss_usdt": decimal_or_none(item.linear_loss_usdt),
            "reasons": list(item.reasons),
        }

    return {
        "schema_version": PREFLIGHT_REPORT_SCHEMA_VERSION,
        "status": report.status,
        "reasons": list(report.reasons),
        "missing_assumptions": list(report.missing_assumptions),
        "config_hash": report.config_hash,
        "tier_snapshot_hash": report.tier_snapshot_hash,
        "tier_evidence_hashes": [list(item) for item in report.tier_evidence_hashes],
        "gap_evidence_hash": report.gap_evidence_hash,
        "execution_model_hash": report.execution_model_hash,
        "window_data_hash": report.window_data_hash,
        "account_window_p99_loss_usdt": decimal_or_none(report.account_window_p99_loss_usdt),
        "account_window_max_loss_usdt": decimal_or_none(report.account_window_max_loss_usdt),
        "account_window_sample_size": report.account_window_sample_size,
        "envelopes": [envelope_mapping(envelope) for envelope in report.envelopes],
    }


def _safe_config_hash(config: object) -> str:
    """Return a stable report key even when a caller supplies malformed config."""
    try:
        if isinstance(config, RiskConfig):
            return config.config_hash
    except (TypeError, ValueError, AttributeError):
        pass
    return canonical_sha256({"invalid_config_type": type(config).__name__})


def _not_estimable_report(
    config: object,
    *,
    reasons: Sequence[str] = (),
    missing: Sequence[str] = (),
) -> PreflightReport:
    return PreflightReport(
        status="not_estimable",
        reasons=tuple(reasons),
        missing_assumptions=tuple(sorted(set(missing))),
        config_hash=_safe_config_hash(config),
        tier_snapshot_hash=None,
        tier_evidence_hashes=(),
        gap_evidence_hash=None,
        execution_model_hash=None,
        window_data_hash=None,
        account_window_p99_loss_usdt=None,
        account_window_max_loss_usdt=None,
        account_window_sample_size=0,
        envelopes=(),
    )


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _decimal_is_finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _decimal_is_positive(value: object) -> bool:
    return _decimal_is_finite(value) and value > 0


def _decimal_is_nonnegative(value: object) -> bool:
    return _decimal_is_finite(value) and value >= 0


def _text_is_nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _computed_tier_snapshot_hash(
    config: RiskConfig,
    tier_schedules: Sequence[TierSchedule],
    as_of_utc: str | None,
) -> str | None:
    if (
        not _text_is_nonblank(config.account_scope)
        or _parse_utc(as_of_utc) is None
        or not isinstance(tier_schedules, (tuple, list))
        or not tier_schedules
        or any(not isinstance(item, TierSchedule) for item in tier_schedules)
    ):
        return None
    try:
        return tier_schedule_snapshot_hash(
            config.account_scope,
            as_of_utc,
            tuple(tier_schedules),
        )
    except (TypeError, ValueError):
        return None


def _parse_utc(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_risk_config(
    config: RiskConfig,
    tier_schedules: Sequence[TierSchedule],
    *,
    as_of_utc: str | None = None,
) -> ConfigValidation:
    """Validate frozen numeric bounds and an explicit, fresh bracket snapshot.

    ``as_of_utc`` is required for a freshness decision. Omitting it keeps the
    result ``not_estimable`` rather than silently treating an old exchange
    response as current.
    """
    if not isinstance(config, RiskConfig):
        return ConfigValidation(
            status="not_estimable",
            reasons=(),
            missing_assumptions=("typed RiskConfig record",),
            tier_checks=(),
        )

    failures: list[str] = []
    missing: list[str] = []
    tier_checks: list[tuple[str, str]] = []

    numeric_positive = (
        ("initial_equity_usdt", config.initial_equity_usdt),
        ("max_leverage", config.max_leverage),
        ("n_max_usdt", config.n_max_usdt),
        ("account_p99_limit_pct", config.account_p99_limit_pct),
    )
    for name, value in numeric_positive:
        if not _decimal_is_positive(value):
            failures.append(f"{name} must be finite and positive")
    if config.margin_mode not in ("isolated", "cross"):
        failures.append("margin_mode must be isolated or cross")
    if type(config.required_n_gap) is not int or config.required_n_gap < 100:
        failures.append("required_n_gap must be an integer >= 100")
    if type(config.required_account_windows) is not int or config.required_account_windows < 100:
        failures.append("required_account_windows must be an integer >= 100")
    if config.account_loss_authority_scenario_id is None:
        missing.append("pre-registered account-loss authority scenario from A_long/A_short/B/C")
    elif (
        not _text_is_nonblank(config.account_loss_authority_scenario_id)
        or config.account_loss_authority_scenario_id not in BASE_ACCOUNT_LOSS_P99_SCENARIOS
    ):
        failures.append("account_loss_authority_scenario_id must be one of A_long/A_short/B/C")
    if _decimal_is_finite(config.account_p99_limit_pct) and config.account_p99_limit_pct > 1:
        failures.append("account_p99_limit_pct is a fraction and must be <= 1")
    if not _decimal_is_positive(config.tier_snapshot_max_age_hours):
        failures.append("tier_snapshot_max_age_hours must be finite and positive")
    if not _is_sha256(config.tier_snapshot_hash):
        missing.append("frozen aggregate tier-snapshot SHA-256")
    if not isinstance(config.account_scope, str) or not config.account_scope.strip():
        missing.append("explicit account_scope for the frozen bracket snapshot")
    elif config.account_scope != config.account_scope.strip():
        failures.append("account_scope must not have surrounding whitespace")

    as_of = _parse_utc(as_of_utc) if as_of_utc is not None else None
    if as_of is None:
        missing.append("UTC as-of time for bracket snapshot freshness")

    if not isinstance(tier_schedules, (tuple, list)):
        missing.append("typed tier schedule collection for every symbol")
        tier_schedules = ()

    caps: dict[str, Decimal] = {}
    cap_symbols_canonical: dict[str, str] = {}
    for symbol, cap in config.per_symbol_caps_usdt:
        if not symbol or symbol != symbol.strip():
            failures.append("symbol names must be non-empty and have no surrounding whitespace")
        if symbol in caps:
            failures.append(f"duplicate per-symbol cap: {symbol}")
        try:
            canonical_symbol = normalize_symbol(symbol)
        except ValueError:
            failures.append(f"per-symbol cap has an invalid USDT-M symbol: {symbol}")
        else:
            if canonical_symbol in cap_symbols_canonical:
                failures.append(
                    "duplicate per-symbol cap after symbol normalization: "
                    f"{canonical_symbol}"
                )
            cap_symbols_canonical[canonical_symbol] = symbol
        caps[symbol] = cap
        if not _decimal_is_positive(cap):
            failures.append(f"per-symbol cap for {symbol} must be finite and positive")
    if not caps:
        failures.append("at least one per-symbol cap is required")
    if all(_decimal_is_positive(cap) for cap in caps.values()) and _decimal_is_finite(config.n_max_usdt):
        # Keep this configuration gate independent of the caller's Decimal
        # context.  A cap that is smaller than N_max by a tiny amount must not
        # round up and pass at low precision.
        total_caps = sum((Fraction(cap) for cap in caps.values()), Fraction(0))
        if total_caps < Fraction(config.n_max_usdt):
            failures.append("sum of per-symbol caps must be >= n_max_usdt")

    leverages: dict[str, Decimal] = {}
    leverage_symbols_canonical: dict[str, str] = {}
    for symbol, leverage in config.per_symbol_leverages:
        if not symbol or symbol != symbol.strip():
            failures.append("per-symbol leverage names must be non-empty and have no surrounding whitespace")
        if symbol in leverages:
            failures.append(f"duplicate per-symbol leverage: {symbol}")
        try:
            canonical_symbol = normalize_symbol(symbol)
        except ValueError:
            failures.append(f"per-symbol leverage has an invalid USDT-M symbol: {symbol}")
        else:
            if canonical_symbol in leverage_symbols_canonical:
                failures.append(
                    "duplicate per-symbol leverage after symbol normalization: "
                    f"{canonical_symbol}"
                )
            leverage_symbols_canonical[canonical_symbol] = symbol
        leverages[symbol] = leverage
        if not _decimal_is_positive(leverage):
            failures.append(f"per-symbol leverage for {symbol} must be finite and positive")
        elif _decimal_is_finite(config.max_leverage) and leverage > config.max_leverage:
            failures.append(
                f"{symbol} actual leverage {leverage}x exceeds user maximum {config.max_leverage}x"
            )
    for symbol in sorted(caps):
        if symbol not in leverages:
            missing.append(f"actual leverage for {symbol}")
    for symbol in sorted(leverages):
        if symbol not in caps:
            failures.append(f"per-symbol leverage has no matching cap: {symbol}")

    schedules: dict[str, TierSchedule] = {}
    for schedule in tier_schedules:
        if not isinstance(schedule, TierSchedule):
            missing.append("typed TierSchedule records for every symbol")
            continue
        if not _text_is_nonblank(schedule.symbol):
            failures.append("tier schedule symbols must be non-empty and have no surrounding whitespace")
            continue
        try:
            canonical_schedule_symbol = normalize_symbol(schedule.symbol)
        except ValueError:
            failures.append(f"tier schedule symbol is not a supported USDT-M market: {schedule.symbol}")
            continue
        if canonical_schedule_symbol in schedules:
            failures.append(f"duplicate tier schedule for {canonical_schedule_symbol}")
            continue
        schedules[canonical_schedule_symbol] = schedule

    computed_snapshot_hash = _computed_tier_snapshot_hash(
        config,
        tier_schedules,
        as_of_utc,
    )
    if computed_snapshot_hash is None:
        missing.append("complete typed tier snapshot content for the frozen as-of time")
    elif (
        _is_sha256(config.tier_snapshot_hash)
        and config.tier_snapshot_hash != computed_snapshot_hash
    ):
        missing.append("supplied tier schedules do not match the frozen RiskConfig tier_snapshot_hash")

    for symbol in sorted(caps):
        try:
            canonical_config_symbol = normalize_symbol(symbol)
        except ValueError:
            canonical_config_symbol = None
        schedule = schedules.get(canonical_config_symbol) if canonical_config_symbol else None
        if schedule is None:
            missing.append(f"verified maintenance-margin tier schedule for {symbol}")
            tier_checks.append((symbol, "missing"))
            continue
        if schedule.verified is not True:
            missing.append(f"verified maintenance-margin tier schedule for {symbol}")
            tier_checks.append((symbol, "unverified"))
            continue
        if (
            not _text_is_nonblank(schedule.source)
            or not _text_is_nonblank(schedule.effective_at_utc)
            or not _text_is_nonblank(schedule.fetched_at_utc)
            or not _text_is_nonblank(schedule.account_scope)
            or not _is_sha256(schedule.evidence_sha256)
        ):
            missing.append(
                f"source, effective/fetched dates, account scope, and SHA-256 evidence for {symbol} tier schedule"
            )
            tier_checks.append((symbol, "incomplete_evidence"))
            continue
        if schedule.snapshot_kind != "account_api_snapshot":
            missing.append(f"live account API bracket snapshot for {symbol}; static tables are diagnostic only")
            tier_checks.append((symbol, "not_account_snapshot"))
            continue
        effective = _parse_utc(schedule.effective_at_utc)
        fetched = _parse_utc(schedule.fetched_at_utc)
        if effective is None or fetched is None:
            missing.append(f"UTC effective_at_utc and fetched_at_utc for {symbol} tier schedule")
            tier_checks.append((symbol, "invalid_timestamps"))
            continue
        provenance = schedule.provenance
        if not isinstance(provenance, TierProvenance):
            missing.append(f"typed API provenance for {symbol} tier schedule")
            tier_checks.append((symbol, "missing_provenance"))
            continue
        if (
            provenance.endpoint != "/fapi/v1/leverageBracket"
            or provenance.http_method != "GET"
            or provenance.security_type != "USER_DATA"
            or not _text_is_nonblank(provenance.request_id)
            or not _is_sha256(provenance.response_sha256)
            or provenance.environment not in ("production", "testnet", "demo")
        ):
            missing.append(f"valid leverage-bracket API provenance for {symbol} tier schedule")
            tier_checks.append((symbol, "invalid_provenance"))
            continue
        signed_at = _parse_utc(provenance.signed_at_utc)
        if signed_at is None:
            missing.append(f"UTC signed_at_utc for {symbol} tier schedule provenance")
            tier_checks.append((symbol, "invalid_signed_at"))
            continue
        if signed_at > fetched:
            failures.append(f"{symbol} API request was signed after its response was fetched")
            tier_checks.append((symbol, "signed_after_fetch"))
            continue
        if as_of is None:
            tier_checks.append((symbol, "freshness_unknown"))
            continue
        if fetched > as_of:
            failures.append(f"{symbol} bracket snapshot fetched in the future relative to as-of")
            tier_checks.append((symbol, "future_snapshot"))
            continue
        if effective > as_of:
            failures.append(f"{symbol} bracket effective date is in the future relative to as-of")
            tier_checks.append((symbol, "future_effective_date"))
            continue
        if effective > fetched:
            failures.append(f"{symbol} bracket effective date is later than fetched date")
            tier_checks.append((symbol, "inconsistent_snapshot_times"))
            continue
        age = as_of - fetched
        max_age_seconds = config.tier_snapshot_max_age_hours * 3600 if _decimal_is_positive(config.tier_snapshot_max_age_hours) else None
        if max_age_seconds is None:
            continue
        if Decimal(str(age.total_seconds())) > max_age_seconds:
            missing.append(f"fresh bracket snapshot for {symbol}; fetched {schedule.fetched_at_utc}")
            tier_checks.append((symbol, "stale"))
            continue
        if config.account_scope is not None and schedule.account_scope != config.account_scope:
            missing.append(f"bracket account scope matching {config.account_scope} for {symbol}")
            tier_checks.append((symbol, "account_scope_mismatch"))
            continue
        if not isinstance(schedule.notional_cap_inclusive, bool):
            missing.append(f"explicit notional cap boundary convention for {symbol}")
            tier_checks.append((symbol, "unknown_boundary_convention"))
            continue
        bands = schedule.bands
        if not isinstance(bands, tuple) or not bands or any(
            not isinstance(item, TierBand) for item in bands
        ):
            missing.append(f"complete typed maintenance-margin bands for {symbol}")
            tier_checks.append((symbol, "incomplete_bands"))
            continue
        if any(
            not all(
                _decimal_is_finite(value)
                for value in (
                    item.initial_leverage,
                    item.notional_floor_usdt,
                    item.notional_cap_usdt,
                    item.maintenance_margin_ratio,
                    item.cumulative_maintenance_usdt,
                )
            )
            for item in bands
        ):
            missing.append(f"finite explicit bracket fields including cumulative maintenance for {symbol}")
            tier_checks.append((symbol, "incomplete_band_fields"))
            continue
        if any(
            item.initial_leverage <= 0
            or item.notional_floor_usdt < 0
            or item.notional_cap_usdt <= item.notional_floor_usdt
            or not 0 < item.maintenance_margin_ratio < 1
            or item.cumulative_maintenance_usdt < 0
            for item in bands
        ):
            failures.append(f"invalid maintenance-margin band for {symbol}")
            tier_checks.append((symbol, "invalid_band"))
            continue
        ordered_bands = sorted(bands, key=lambda item: item.notional_floor_usdt)
        if bands != tuple(ordered_bands):
            failures.append(f"tier bands for {symbol} must be in ascending notional-floor order")
            tier_checks.append((symbol, "unsorted_bands"))
            continue
        if ordered_bands[0].notional_floor_usdt != 0 or any(
            prior.notional_cap_usdt != current.notional_floor_usdt
            for prior, current in zip(ordered_bands, ordered_bands[1:])
        ):
            missing.append(f"continuous, non-overlapping bracket coverage from zero for {symbol}")
            tier_checks.append((symbol, "non_contiguous_bands"))
            continue
        # Binance-style maintenance amount (``cum``) is a subtraction offset:
        # maintenance margin = notional * maintMarginRatio - cum.  Do not turn
        # it into an additive buffer.  A complete multi-tier snapshot must
        # also preserve the continuous maintenance schedule at each boundary.
        if any(
            exact_margin_offset(
                item.notional_floor_usdt,
                item.maintenance_margin_ratio,
                item.cumulative_maintenance_usdt,
            ) < 0
            for item in ordered_bands
        ):
            failures.append(f"maintenance margin is negative at a bracket floor for {symbol}")
            tier_checks.append((symbol, "negative_maintenance_margin"))
            continue
        if any(
            exact_margin_offset(
                prior.notional_cap_usdt,
                prior.maintenance_margin_ratio,
                prior.cumulative_maintenance_usdt,
            )
            != exact_margin_offset(
                current.notional_floor_usdt,
                current.maintenance_margin_ratio,
                current.cumulative_maintenance_usdt,
            )
            for prior, current in zip(ordered_bands, ordered_bands[1:])
        ):
            failures.append(f"maintenance-margin offsets are discontinuous across tiers for {symbol}")
            tier_checks.append((symbol, "maintenance_margin_discontinuity"))
            continue
        cap = caps[symbol]
        if not _decimal_is_positive(cap):
            continue
        matching_bands = [
            item
            for item in bands
            if (
                item.notional_floor_usdt < cap
                if schedule.notional_cap_inclusive
                else item.notional_floor_usdt <= cap
            )
            and (
                cap <= item.notional_cap_usdt
                if schedule.notional_cap_inclusive
                else cap < item.notional_cap_usdt
            )
        ]
        if len(matching_bands) == 0:
            missing.append(f"tier coverage through the frozen cap for {symbol}")
            tier_checks.append((symbol, "uncovered"))
            continue
        if len(matching_bands) > 1:
            missing.append(f"unambiguous exchange bracket at frozen cap for {symbol}")
            tier_checks.append((symbol, "ambiguous_bracket"))
            continue
        band = matching_bands[0]
        if (
            not band.maintenance_margin_ratio.is_finite()
            or not band.initial_leverage.is_finite()
            or not band.cumulative_maintenance_usdt.is_finite()
            or band.maintenance_margin_ratio <= 0
            or band.maintenance_margin_ratio >= 1
            or band.initial_leverage <= 0
            or band.cumulative_maintenance_usdt < 0
            or not band.notional_floor_usdt.is_finite()
            or not band.notional_cap_usdt.is_finite()
            or band.notional_floor_usdt < 0
            or band.notional_cap_usdt <= band.notional_floor_usdt
        ):
            failures.append(f"invalid maintenance-margin band for {symbol}")
            tier_checks.append((symbol, "invalid_band"))
            continue
        actual_leverage = leverages.get(symbol)
        if not _decimal_is_positive(actual_leverage):
            tier_checks.append((symbol, "missing_actual_leverage"))
            continue
        if actual_leverage > band.initial_leverage:
            failures.append(
                f"{symbol} actual leverage {actual_leverage}x exceeds tier maximum "
                f"{band.initial_leverage}x at cap {cap} USDT"
            )
            tier_checks.append((symbol, "leverage_exceeds_tier"))
        elif Fraction(cap) / Fraction(actual_leverage) <= max(
            Fraction(0),
            exact_margin_offset(
                cap,
                band.maintenance_margin_ratio,
                band.cumulative_maintenance_usdt,
            ),
        ):
            failures.append(f"{symbol} leverage leaves no initial-margin cushion over maintenance margin")
            tier_checks.append((symbol, "maintenance_margin_incompatible"))
        else:
            tier_checks.append((symbol, "pass"))

    status: Status = "fail" if failures else "not_estimable" if missing else "pass"
    return ConfigValidation(
        status=status,
        reasons=tuple(failures),
        missing_assumptions=tuple(sorted(set(missing))),
        tier_checks=tuple(tier_checks),
    )


def nearest_rank_p99(
    adverse_magnitudes: Sequence[Decimal],
    required_sample_size: int = 100,
) -> QuantileResult:
    """Calculate nearest-rank P99 from strictly positive adverse magnitudes."""
    if required_sample_size < 1:
        raise ValueError("required_sample_size must be positive")
    values: list[Decimal] = []
    excluded = 0
    malformed = False
    for value in adverse_magnitudes:
        if not _decimal_is_finite(value):
            excluded += 1
            malformed = True
        elif value < 0:
            excluded += 1
            malformed = True
        elif value == 0:
            excluded += 1
        else:
            values.append(value)
    values.sort()
    count = len(values)
    if count < required_sample_size or malformed:
        return QuantileResult(
            status="not_estimable",
            p99=None,
            maximum=values[-1] if values else None,
            sample_size=count,
            required_sample_size=required_sample_size,
            excluded_count=excluded,
        )
    rank = math.ceil(0.99 * count)
    return QuantileResult(
        status="pass",
        p99=values[rank - 1],
        maximum=values[-1],
        sample_size=count,
        required_sample_size=required_sample_size,
        excluded_count=excluded,
        rank=rank,
    )


def gap_p99_from_observations(
    observations: Sequence[GapObservation],
    side: Side,
    required_sample_size: int = 100,
) -> QuantileResult:
    """Use only valid consecutive 15-minute bars; never substitute a pressure bar."""
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    magnitudes: list[Decimal] = []
    excluded = 0
    for observation in observations:
        if (
            not isinstance(observation, GapObservation)
            or not observation.is_valid_adjacency
            or not _decimal_is_finite(observation.gap_fraction)
        ):
            excluded += 1
            continue
        adverse = (
            max(-observation.gap_fraction, Decimal("0"))
            if side == "long"
            else max(observation.gap_fraction, Decimal("0"))
        )
        if adverse > 0:
            magnitudes.append(adverse)
        else:
            excluded += 1
    result = nearest_rank_p99(magnitudes, required_sample_size)
    return QuantileResult(
        status=result.status,
        p99=result.p99,
        maximum=result.maximum,
        sample_size=result.sample_size,
        required_sample_size=result.required_sample_size,
        excluded_count=excluded,
        rank=result.rank,
    )


def _caps(config: RiskConfig) -> dict[str, Decimal]:
    return dict(config.per_symbol_caps_usdt)


def _gap_index(gap_p99s: Sequence[GapP99]) -> dict[tuple[str, Side], GapP99]:
    index: dict[tuple[str, Side], GapP99] = {}
    for item in gap_p99s:
        if isinstance(item, GapP99):
            index[(item.symbol, item.side)] = item
    return index


def _make_envelope(
    envelope_id: str,
    allocations: Sequence[tuple[str, Side, Decimal]],
    gap_index: Mapping[tuple[str, Side], GapP99],
    required_sample_size: int,
    reason: str | None = None,
) -> RiskEnvelope:
    exposures: list[Exposure] = []
    missing = []
    for symbol, side, notional in allocations:
        risk = gap_index.get((symbol, side))
        issue = (
            _gap_p99_issue(risk, required_sample_size)
            if risk is not None
            else "gap P99 evidence is missing"
        )
        if issue:
            missing.append(f"{symbol} {side}: {issue}")
            continue
        if risk.adverse_rate is None:
            missing.append(f"valid trade and mark P99 for {symbol} {side}")
            continue
        exposures.append(Exposure(symbol, side, notional, risk.adverse_rate))
    if reason or missing:
        return RiskEnvelope(
            envelope_id=envelope_id,
            status="not_estimable",
            exposures=tuple(exposures),
            total_notional_usdt=sum((item.notional_usdt for item in exposures), Decimal("0")),
            linear_loss_usdt=None,
            reasons=tuple(([reason] if reason else []) + missing),
        )
    return RiskEnvelope(
        envelope_id=envelope_id,
        status="pass",
        exposures=tuple(exposures),
        total_notional_usdt=sum((item.notional_usdt for item in exposures), Decimal("0")),
        linear_loss_usdt=sum((item.linear_loss_usdt for item in exposures), Decimal("0")),
    )


def build_envelopes(
    config: RiskConfig,
    gap_p99s: Sequence[GapP99],
) -> tuple[RiskEnvelope, ...]:
    """Build A (both same-side), B (alternating), and C (concentrated worst-case).

    A returns two independently evaluated envelopes, ``A_long`` and
    ``A_short``. B is not estimable when the alternating construction cannot
    allocate exactly half of N_max to each side. C greedily fills the highest
    per-symbol adverse P99, respecting each symbol's absolute cap.
    """
    caps = _caps(config)
    symbols = sorted(caps)
    indexed = _gap_index(gap_p99s)
    if any(not isinstance(item, GapP99) for item in gap_p99s):
        duplicate_reason = "gap P99 entries must be GapP99 records"
    elif len(indexed) != len(gap_p99s):
        duplicate_reason = "duplicate symbol/side P99 entries"
    else:
        duplicate_reason = None

    def waterfill(symbol_subset: Sequence[str], target: Decimal) -> dict[str, Decimal] | None:
        """Equal-notional water filling under deterministic symbol caps."""
        active = list(symbol_subset)
        remaining = target
        output: dict[str, Decimal] = {symbol: Decimal("0") for symbol in active}
        if target < 0:
            return None
        while active and remaining > 0:
            level = remaining / Decimal(len(active))
            capped = [symbol for symbol in active if caps[symbol] <= level]
            if not capped:
                for symbol in active:
                    output[symbol] = level
                remaining = Decimal("0")
                break
            for symbol in capped:
                output[symbol] = caps[symbol]
                remaining -= caps[symbol]
                active.remove(symbol)
        if remaining > 0:
            return None
        return output

    def allocate_same_side(side: Side) -> tuple[list[tuple[str, Side, Decimal]], str | None]:
        allocation = waterfill(symbols, config.n_max_usdt)
        if allocation is None:
            return [], "per-symbol caps cannot fill n_max_usdt"
        return [
            (symbol, side, amount)
            for symbol, amount in allocation.items()
            if amount > 0
        ], None

    a_long_alloc, a_long_reason = allocate_same_side("long")
    a_short_alloc, a_short_reason = allocate_same_side("short")
    a_reason = duplicate_reason or a_long_reason or a_short_reason
    a_long = _make_envelope("A_long", a_long_alloc, indexed, config.required_n_gap, a_reason)
    a_short = _make_envelope("A_short", a_short_alloc, indexed, config.required_n_gap, a_reason)

    half = config.n_max_usdt / Decimal("2")
    long_symbols = symbols[::2]
    short_symbols = symbols[1::2]
    b_long = waterfill(long_symbols, half)
    b_short = waterfill(short_symbols, half)
    b_alloc: list[tuple[str, Side, Decimal]] = []
    if b_long is not None:
        b_alloc.extend((symbol, "long", amount) for symbol, amount in b_long.items() if amount > 0)
    if b_short is not None:
        b_alloc.extend((symbol, "short", amount) for symbol, amount in b_short.items() if amount > 0)
    b_reason = duplicate_reason
    if b_long is None or b_short is None:
        b_reason = b_reason or "alternating symbols cannot allocate n_max_usdt/2 to each side"
    b_envelope = _make_envelope("B", b_alloc, indexed, config.required_n_gap, b_reason)

    c_alloc: list[tuple[str, Side, Decimal]] = []
    c_reason = duplicate_reason
    candidates: list[tuple[Decimal, str, Side]] = []
    for symbol in symbols:
        long_risk = indexed.get((symbol, "long"))
        short_risk = indexed.get((symbol, "short"))
        if long_risk is not None:
            issue = _gap_p99_issue(long_risk, config.required_n_gap)
            if issue:
                c_reason = c_reason or issue
        if short_risk is not None:
            issue = _gap_p99_issue(short_risk, config.required_n_gap)
            if issue:
                c_reason = c_reason or issue
        if long_risk is None or long_risk.adverse_rate is None:
            c_reason = c_reason or f"valid trade and mark P99 for {symbol} long is missing"
        if short_risk is None or short_risk.adverse_rate is None:
            c_reason = c_reason or f"valid trade and mark P99 for {symbol} short is missing"
        if long_risk is None or short_risk is None:
            continue
        if (
            _gap_p99_issue(long_risk, config.required_n_gap)
            or _gap_p99_issue(short_risk, config.required_n_gap)
            or long_risk.adverse_rate is None
            or short_risk.adverse_rate is None
        ):
            continue
        if long_risk.adverse_rate >= short_risk.adverse_rate:
            side: Side = "long"
            rate = long_risk.adverse_rate
        else:
            side = "short"
            rate = short_risk.adverse_rate
        candidates.append((rate, symbol, side))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    remaining = config.n_max_usdt
    used_symbols: set[str] = set()
    for _, symbol, side in candidates:
        if symbol in used_symbols or remaining <= 0:
            continue
        amount = min(caps[symbol], remaining)
        if amount > 0:
            c_alloc.append((symbol, side, amount))
            used_symbols.add(symbol)
            remaining -= amount
    if remaining > 0:
        c_reason = c_reason or "available P99 evidence and caps cannot fill n_max_usdt"
    c_envelope = _make_envelope("C", c_alloc, indexed, config.required_n_gap, c_reason)
    return (a_long, a_short, b_envelope, c_envelope)


def _valid_hash_or_missing(value: str | None, label: str, missing: list[str]) -> None:
    if value is None or not _is_sha256(value):
        missing.append(label)


def _account_p99_loss_limit(config: RiskConfig) -> Fraction:
    """Return the validated equity-percentage limit without context rounding."""
    return Fraction(config.initial_equity_usdt) * Fraction(config.account_p99_limit_pct)


def _validate_scenario_evidence(
    config: RiskConfig,
    evidence: ExecutionEvidence,
    *,
    reasons: list[str],
    missing: list[str],
) -> None:
    """Require independent evidence for every frozen scenario.

    The top-level account-window series is retained for the report, but it is
    never sufficient by itself: each envelope, marginal/synthetic package,
    and one-/five-tick liquidation stress must be bound to this config.
    """
    seen: set[str] = set()
    by_id: dict[str, ScenarioEvidence] = {}
    loss_limit = _account_p99_loss_limit(config)
    if not isinstance(evidence.scenario_evidence, tuple):
        missing.append("scenario_evidence must be an immutable tuple")
        return
    for item in evidence.scenario_evidence:
        if not isinstance(item, ScenarioEvidence):
            reasons.append("scenario evidence entries must be ScenarioEvidence records")
            continue
        if (
            not isinstance(item.scenario_id, str)
            or not item.scenario_id
            or item.scenario_id != item.scenario_id.strip()
        ):
            reasons.append("scenario evidence ids must be non-empty and have no surrounding whitespace")
            continue
        if item.scenario_id in seen:
            reasons.append(f"duplicate scenario evidence: {item.scenario_id}")
            continue
        seen.add(item.scenario_id)
        by_id[item.scenario_id] = item
        if item.config_hash != config.config_hash:
            reasons.append(f"scenario {item.scenario_id} is bound to a different risk config")
        # A malformed top-level hash is already reported as missing below.
        # Do not turn every dependent scenario into a hard ``fail`` merely
        # because it cannot be compared; the result remains not_estimable
        # until the top-level binding is supplied.
        if _is_sha256(evidence.model_sha256) and item.model_sha256 != evidence.model_sha256:
            reasons.append(f"scenario {item.scenario_id} is bound to a different execution model")
        if _is_sha256(evidence.tier_snapshot_hash) and item.tier_snapshot_hash != evidence.tier_snapshot_hash:
            reasons.append(f"scenario {item.scenario_id} is bound to a different tier snapshot")
        if isinstance(evidence.assumption_hashes, tuple) and item.assumption_hashes != evidence.assumption_hashes:
            reasons.append(f"scenario {item.scenario_id} is bound to different execution assumptions")
        _valid_hash_or_missing(
            item.model_sha256,
            f"execution model SHA-256: {item.scenario_id}",
            missing,
        )
        _valid_hash_or_missing(
            item.tier_snapshot_hash,
            f"tier snapshot SHA-256: {item.scenario_id}",
            missing,
        )
        if not isinstance(item.assumption_hashes, tuple):
            missing.append(f"immutable execution assumption hashes: {item.scenario_id}")
        elif any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not _is_sha256(pair[1])
            for pair in item.assumption_hashes
        ):
            missing.append(f"valid execution assumption hashes: {item.scenario_id}")
        _valid_hash_or_missing(
            item.window_data_sha256,
            f"synchronized window-data SHA-256: {item.scenario_id}",
            missing,
        )
        if (
            _is_sha256(evidence.window_data_sha256)
            and _is_sha256(item.window_data_sha256)
            and item.window_data_sha256 != evidence.window_data_sha256
        ):
            missing.append(
                f"scenario {item.scenario_id} does not use the shared synchronized account windows"
            )
        _valid_hash_or_missing(item.evidence_sha256, f"scenario evidence SHA-256: {item.scenario_id}", missing)
        try:
            expected_path, expected_slippage, expected_delay, expected_ticks = _scenario_expected_stress(item.scenario_id)
        except ValueError:
            reasons.append(f"scenario {item.scenario_id} is not in the frozen pressure set")
            expected_path = expected_slippage = expected_delay = expected_ticks = None
        if expected_path is not None:
            if item.execution_path != expected_path:
                reasons.append(f"scenario {item.scenario_id} has incorrect execution path metadata")
            if item.slippage_multiple != expected_slippage:
                reasons.append(f"scenario {item.scenario_id} must bind the frozen 2x slippage stress")
            if item.delay_ms != expected_delay:
                reasons.append(f"scenario {item.scenario_id} must bind the frozen 1000ms delay stress")
            if item.liquidation_slippage_ticks != expected_ticks:
                reasons.append(
                    f"scenario {item.scenario_id} must bind the frozen {expected_ticks} tick liquidation stress"
                )
        if _is_sha256(item.evidence_sha256):
            try:
                if item.evidence_sha256 != canonical_sha256(_scenario_payload(item)):
                    reasons.append(f"scenario {item.scenario_id} evidence hash does not bind its payload")
            except (TypeError, ValueError):
                reasons.append(f"scenario {item.scenario_id} payload cannot be canonically hashed")
        if item.status not in ("pass", "fail"):
            reasons.append(f"scenario {item.scenario_id} has invalid status")
        elif item.status == "fail":
            reasons.append(f"scenario {item.scenario_id} failed its frozen risk checks")
        losses = item.account_window_losses_usdt
        deficits = item.uncovered_deficits_usdt
        if not isinstance(losses, tuple) or not losses:
            missing.append(f"account-window losses for scenario {item.scenario_id}")
        elif len(losses) < config.required_account_windows:
            missing.append(
                f"at least {config.required_account_windows} account windows for scenario {item.scenario_id}; found {len(losses)}"
            )
        elif any(not _decimal_is_nonnegative(value) for value in losses):
            reasons.append(f"scenario {item.scenario_id} losses must be finite non-negative Decimal values")
        if not isinstance(deficits, tuple):
            missing.append(f"uncovered-deficit observations for scenario {item.scenario_id}")
        elif not isinstance(losses, tuple) or len(deficits) != len(losses):
            missing.append(
                f"deficit observations must match loss-window count for scenario {item.scenario_id}"
            )
        elif len(deficits) < config.required_account_windows:
            missing.append(
                f"at least {config.required_account_windows} deficit observations for scenario {item.scenario_id}"
            )
        elif any(not _decimal_is_nonnegative(value) for value in deficits):
            reasons.append(f"scenario {item.scenario_id} deficits must be finite non-negative Decimal values")
        elif any(value > 0 for value in deficits):
            reasons.append(f"scenario {item.scenario_id} observed an uncovered deficit")
        if (
            isinstance(losses, tuple)
            and len(losses) >= config.required_account_windows
            and all(_decimal_is_nonnegative(value) for value in losses)
        ):
            ordered = sorted(losses)
            p99 = ordered[math.ceil(0.99 * len(ordered)) - 1]
            maximum = ordered[-1]
            if item.scenario_id in ACCOUNT_LOSS_P99_SCENARIOS and Fraction(p99) > loss_limit:
                reasons.append(f"scenario {item.scenario_id} account-loss P99 exceeds the frozen equity limit")
            if maximum > config.initial_equity_usdt:
                reasons.append(f"scenario {item.scenario_id} loss exceeds initial equity")
    for scenario_id in REQUIRED_SCENARIO_EVIDENCE:
        if scenario_id not in by_id:
            missing.append(f"independent evidence for frozen scenario {scenario_id}")


def _validate_account_loss_authority(
    config: RiskConfig,
    evidence: ExecutionEvidence,
    *,
    reasons: list[str],
    missing: list[str],
) -> None:
    """Bind top-level account windows to the deterministic worst A/B/C result.

    A/B/C are distinct risk constructions and their loss series must remain
    distinct.  The top-level report has one explicit authority scenario: the
    valid account-loss scenario with the largest nearest-rank P99, with ties
    resolved by ``ACCOUNT_LOSS_AUTHORITY_ORDER``.  This prevents a caller from
    selecting a convenient scenario while preserving each scenario's own
    evidence and gate.
    """
    authority = config.account_loss_authority_scenario_id
    if authority not in BASE_ACCOUNT_LOSS_P99_SCENARIOS:
        return
    if not isinstance(evidence.scenario_evidence, tuple):
        return
    by_id = {
        item.scenario_id: item
        for item in evidence.scenario_evidence
        if isinstance(item, ScenarioEvidence)
        and item.scenario_id in BASE_ACCOUNT_LOSS_P99_SCENARIOS
    }

    valid: dict[str, tuple[ScenarioEvidence, Decimal]] = {}
    for scenario_id in ACCOUNT_LOSS_AUTHORITY_ORDER:
        scenario = by_id.get(scenario_id)
        if scenario is None:
            continue
        losses = scenario.account_window_losses_usdt
        deficits = scenario.uncovered_deficits_usdt
        if (
            not isinstance(losses, tuple)
            or len(losses) < config.required_account_windows
            or not isinstance(deficits, tuple)
            or len(deficits) != len(losses)
            or any(not _decimal_is_nonnegative(value) for value in losses)
            or any(not _decimal_is_nonnegative(value) for value in deficits)
        ):
            continue
        ordered = sorted(losses)
        rank = math.ceil(0.99 * len(ordered))
        valid[scenario_id] = (scenario, ordered[rank - 1])

    if not valid:
        missing.append("valid account-loss authority scenario among A_long/A_short/B/C")
        return
    selected_id = max(
        valid,
        key=lambda scenario_id: (valid[scenario_id][1], -ACCOUNT_LOSS_AUTHORITY_ORDER.index(scenario_id)),
    )
    if authority != selected_id:
        reasons.append(
            f"account-loss authority {authority} is not deterministic worst-P99 scenario {selected_id}"
        )
    scenario = valid[selected_id][0]
    top_losses = evidence.account_window_losses_usdt
    scenario_losses = scenario.account_window_losses_usdt
    if not isinstance(top_losses, tuple) or not isinstance(scenario_losses, tuple):
        missing.append(f"top-level account-loss series for authority scenario {selected_id}")
    elif top_losses != scenario_losses:
        reasons.append(
            f"top-level account-loss series differs from authority scenario {selected_id}"
        )
    top_deficits = evidence.uncovered_deficits_usdt
    scenario_deficits = scenario.uncovered_deficits_usdt
    if not isinstance(top_deficits, tuple) or not isinstance(scenario_deficits, tuple):
        missing.append(f"top-level account-deficit series for authority scenario {selected_id}")
    elif top_deficits != scenario_deficits:
        reasons.append(
            f"top-level account-deficit series differs from authority scenario {selected_id}"
        )


def evaluate_preflight(
    config: RiskConfig,
    tier_schedules: Sequence[TierSchedule],
    gap_p99s: Sequence[GapP99] | None,
    execution_evidence: ExecutionEvidence | None,
    *,
    as_of_utc: str | None,
) -> PreflightReport:
    """Evaluate the evidence gate; gap-only or config-only evidence cannot pass."""
    if not isinstance(config, RiskConfig):
        return _not_estimable_report(config, missing=("typed RiskConfig record",))

    # ``validate_risk_config`` deliberately accepts malformed input so it can
    # return a structured ``not_estimable`` result.  Keep a separately
    # normalized view for report hashing: report construction must remain
    # fail-closed even when a caller passes ``None`` or non-TierSchedule
    # entries in the collection.
    typed_tier_schedules = (
        tuple(item for item in tier_schedules if isinstance(item, TierSchedule))
        if isinstance(tier_schedules, (tuple, list))
        else ()
    )
    try:
        validation = validate_risk_config(config, tier_schedules, as_of_utc=as_of_utc)
        config_hash = config.config_hash
    except (AttributeError, TypeError, ValueError, KeyError, ArithmeticError):
        return _not_estimable_report(config, missing=("valid RiskConfig fields",))
    if validation.status == "fail":
        # Do not let malformed frozen bounds reach scenario arithmetic.  In
        # particular, initial_equity/account_p99_limit are multiplied below
        # and the authority id is used in set membership; invalid field types
        # must produce a report instead of leaking TypeError from those paths.
        return PreflightReport(
            status="fail",
            reasons=validation.reasons,
            missing_assumptions=validation.missing_assumptions,
            config_hash=config_hash,
            tier_snapshot_hash=(
                config.tier_snapshot_hash
                if _is_sha256(config.tier_snapshot_hash)
                else None
            ),
            tier_evidence_hashes=tuple(
                (schedule.symbol, schedule.evidence_sha256)
                for schedule in sorted(typed_tier_schedules, key=lambda item: str(item.symbol))
                if _is_sha256(schedule.evidence_sha256)
            ),
            gap_evidence_hash=None,
            execution_model_hash=None,
            window_data_hash=None,
            account_window_p99_loss_usdt=None,
            account_window_max_loss_usdt=None,
            account_window_sample_size=0,
            envelopes=(),
        )
    reasons = list(validation.reasons)
    missing = list(validation.missing_assumptions)
    envelopes: tuple[RiskEnvelope, ...] = ()
    gap_hash: str | None = None
    if gap_p99s is None:
        missing.append("trade and mark gap P99 evidence for every symbol and direction")
    elif not isinstance(gap_p99s, (tuple, list)):
        missing.append("gap P99 evidence must be a sequence of typed records")
    else:
        try:
            gap_hash = canonical_sha256(gap_p99s)
        except (TypeError, ValueError):
            missing.append("canonical SHA-256 for typed gap P99 evidence")
        else:
            try:
                envelopes = build_envelopes(config, gap_p99s)
            except (AttributeError, TypeError, ValueError, KeyError, ArithmeticError):
                missing.append("deterministic A/B/C risk envelopes")
                envelopes = ()
            for envelope in envelopes:
                if envelope.status != "pass":
                    missing.extend(f"{envelope.envelope_id}: {item}" for item in envelope.reasons)

    model_hash = None
    window_hash = None
    account_p99 = None
    account_max = None
    sample_size = 0
    if execution_evidence is None:
        missing.extend(
            [
                "frozen execution model SHA-256",
                "synchronized account-window loss series and SHA-256",
                "uncovered-deficit observations",
                *REQUIRED_EXECUTION_EVIDENCE,
            ]
        )
    elif not isinstance(execution_evidence, ExecutionEvidence):
        missing.append("typed ExecutionEvidence record")
    else:
        model_hash = execution_evidence.model_sha256
        window_hash = execution_evidence.window_data_sha256
        _valid_hash_or_missing(model_hash, "frozen execution model SHA-256", missing)
        _valid_hash_or_missing(window_hash, "synchronized account-window loss series and SHA-256", missing)
        execution_tier_hash = execution_evidence.tier_snapshot_hash
        _valid_hash_or_missing(execution_tier_hash, "execution tier-snapshot SHA-256", missing)
        actual_tier_hash = _computed_tier_snapshot_hash(config, tier_schedules, as_of_utc)
        if (
            actual_tier_hash is not None
            and _is_sha256(execution_tier_hash)
            and execution_tier_hash != actual_tier_hash
        ):
            reasons.append("execution tier snapshot does not match the supplied typed schedule set")
        if (
            _is_sha256(config.tier_snapshot_hash)
            and _is_sha256(execution_tier_hash)
            and execution_tier_hash != config.tier_snapshot_hash
        ):
            reasons.append("execution tier snapshot differs from the frozen RiskConfig tier snapshot")
        if not isinstance(execution_evidence.assumption_hashes, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            for item in execution_evidence.assumption_hashes
        ):
            missing.append("execution assumption hashes must be an immutable tuple of name/hash pairs")
            assumption_hashes = {}
        else:
            keys = [item[0] for item in execution_evidence.assumption_hashes]
            if len(set(keys)) != len(keys):
                reasons.append("duplicate execution assumption evidence keys")
            assumption_hashes = {name: value for name, value in execution_evidence.assumption_hashes}
            if set(keys) != set(REQUIRED_EXECUTION_EVIDENCE):
                missing.append("execution assumption evidence must exactly match the frozen required set")
        for assumption in REQUIRED_EXECUTION_EVIDENCE:
            value = assumption_hashes.get(assumption)
            _valid_hash_or_missing(value, assumption, missing)
        losses = execution_evidence.account_window_losses_usdt
        if losses is None:
            missing.append("synchronized account-window losses")
        elif not isinstance(losses, tuple):
            missing.append("synchronized account-window losses must be an immutable tuple")
        else:
            sample_size = len(losses)
            if sample_size < config.required_account_windows:
                missing.append(
                    f"at least {config.required_account_windows} account windows; found {sample_size}"
                )
            if any(not _decimal_is_nonnegative(value) for value in losses):
                reasons.append("account-window losses must be finite non-negative Decimal values")
            elif losses:
                ordered_losses = sorted(losses)
                rank = math.ceil(0.99 * len(ordered_losses))
                account_p99 = ordered_losses[rank - 1]
                account_max = max(ordered_losses)
        deficits = execution_evidence.uncovered_deficits_usdt
        if deficits is None:
            missing.append("uncovered-deficit observations under frozen bankruptcy/insurance/ADL rules")
        elif not isinstance(deficits, tuple):
            missing.append("uncovered-deficit observations must be an immutable tuple")
        elif losses is None or not isinstance(losses, tuple) or len(deficits) != len(losses):
            missing.append("one uncovered-deficit observation for every account-loss window")
        elif len(deficits) < config.required_account_windows:
            missing.append(
                f"at least {config.required_account_windows} uncovered-deficit observations; found {len(deficits)}"
            )
        elif any(not _decimal_is_nonnegative(value) for value in deficits):
            reasons.append("uncovered deficits must be finite non-negative Decimal values")
        elif any(value > 0 for value in deficits):
            reasons.append("uncovered deficit observed; account risk preflight fails")
        _validate_scenario_evidence(
            config,
            execution_evidence,
            reasons=reasons,
            missing=missing,
        )
        _validate_account_loss_authority(
            config,
            execution_evidence,
            reasons=reasons,
            missing=missing,
        )

    if account_p99 is not None and Fraction(account_p99) > _account_p99_loss_limit(config):
        reasons.append("single-window account-loss P99 exceeds the frozen equity percentage limit")
    if account_max is not None and account_max > config.initial_equity_usdt:
        reasons.append("a single-window loss exceeds initial equity")

    if reasons:
        status: Status = "fail"
    elif missing or validation.status != "pass":
        status = "not_estimable"
    else:
        status = "pass"
    tier_hashes = tuple(
        (schedule.symbol, schedule.evidence_sha256)
        for schedule in sorted(typed_tier_schedules, key=lambda item: str(item.symbol))
        if _is_sha256(schedule.evidence_sha256)
    )
    return PreflightReport(
        status=status,
        reasons=tuple(reasons),
        missing_assumptions=tuple(sorted(set(missing))),
        config_hash=config_hash,
        tier_snapshot_hash=_computed_tier_snapshot_hash(config, tier_schedules, as_of_utc),
        tier_evidence_hashes=tier_hashes,
        gap_evidence_hash=gap_hash,
        execution_model_hash=model_hash,
        window_data_hash=window_hash,
        account_window_p99_loss_usdt=account_p99,
        account_window_max_loss_usdt=account_max,
        account_window_sample_size=sample_size,
        envelopes=envelopes,
    )
