"""Export a credential-free account-risk preflight report from a JSON bundle.

The input is an explicit, bounded evidence bundle.  This command never
contacts an exchange, reads environment files, or creates a research gate
event.  Missing account or execution evidence is reported as
``not_estimable`` by the authoritative preflight function.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Mapping

from core.execution.account_risk_preflight import (
    ExecutionEvidence,
    GapP99,
    PreflightReport,
    QuantileResult,
    RiskConfig,
    ScenarioEvidence,
    TierBand,
    TierProvenance,
    TierSchedule,
    evaluate_preflight,
    preflight_report_to_mapping,
)


MAX_INPUT_BYTES = 16 * 1024 * 1024
OUTPUT_REPORT_NAME = "report.json"
OUTPUT_MANIFEST_NAME = "manifest.json"


def _object_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("input JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_floats(value: object) -> None:
    if isinstance(value, float):
        raise ValueError("JSON floating-point numbers are not accepted; use decimal strings")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, list):
        for item in value:
            _reject_floats(item)


def _parse_json(raw: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input must be valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("input JSON must be an object")
    _reject_floats(value)
    return value


def _fields(value: object, *, allowed: set[str], required: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) - allowed:
        raise ValueError(f"{label} contains unknown fields")
    if required - set(value):
        raise ValueError(f"{label} is missing required fields")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _nullable_decimal(value: object, label: str) -> Decimal | None:
    return None if value is None else _decimal(value, label)


def _sha(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _nullable_sha(value: object, label: str) -> str | None:
    return None if value is None else _sha(value, label)


def _int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _pair_list(value: object, label: str) -> tuple[tuple[str, Decimal], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of [symbol, decimal] pairs")
    result: list[tuple[str, Decimal]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"{label} must contain only [symbol, decimal] pairs")
        result.append((_text(item[0], f"{label}.symbol"), _decimal(item[1], f"{label}.value")))
    return tuple(result)


def _config(raw: object) -> RiskConfig:
    value = _fields(
        raw,
        allowed={
            "initial_equity_usdt", "max_leverage", "margin_mode", "n_max_usdt",
            "per_symbol_caps_usdt", "per_symbol_leverages", "account_p99_limit_pct",
            "required_n_gap", "required_account_windows", "account_scope",
            "tier_snapshot_max_age_hours", "account_loss_authority_scenario_id",
            "tier_snapshot_hash",
        },
        required={
            "initial_equity_usdt", "max_leverage", "margin_mode", "n_max_usdt",
            "per_symbol_caps_usdt", "per_symbol_leverages", "account_p99_limit_pct",
            "required_n_gap", "required_account_windows", "account_scope",
            "tier_snapshot_max_age_hours", "account_loss_authority_scenario_id",
            "tier_snapshot_hash",
        },
        label="config",
    )
    account_scope = value["account_scope"]
    authority = value["account_loss_authority_scenario_id"]
    tier_hash = value["tier_snapshot_hash"]
    return RiskConfig(
        initial_equity_usdt=_decimal(value["initial_equity_usdt"], "config.initial_equity_usdt"),
        max_leverage=_decimal(value["max_leverage"], "config.max_leverage"),
        margin_mode=_text(value["margin_mode"], "config.margin_mode"),  # type: ignore[arg-type]
        n_max_usdt=_decimal(value["n_max_usdt"], "config.n_max_usdt"),
        per_symbol_caps_usdt=_pair_list(value["per_symbol_caps_usdt"], "config.per_symbol_caps_usdt"),
        per_symbol_leverages=_pair_list(value["per_symbol_leverages"], "config.per_symbol_leverages"),
        account_p99_limit_pct=_decimal(value["account_p99_limit_pct"], "config.account_p99_limit_pct"),
        required_n_gap=_int(value["required_n_gap"], "config.required_n_gap"),
        required_account_windows=_int(value["required_account_windows"], "config.required_account_windows"),
        account_scope=None if account_scope is None else _text(account_scope, "config.account_scope"),
        tier_snapshot_max_age_hours=_decimal(value["tier_snapshot_max_age_hours"], "config.tier_snapshot_max_age_hours"),
        account_loss_authority_scenario_id=None if authority is None else _text(authority, "config.account_loss_authority_scenario_id"),
        tier_snapshot_hash=None if tier_hash is None else _sha(tier_hash, "config.tier_snapshot_hash"),
    )


def _tier_band(raw: object, label: str) -> TierBand:
    value = _fields(
        raw,
        allowed={"initial_leverage", "notional_floor_usdt", "notional_cap_usdt", "maintenance_margin_ratio", "cumulative_maintenance_usdt"},
        required={"initial_leverage", "notional_floor_usdt", "notional_cap_usdt", "maintenance_margin_ratio", "cumulative_maintenance_usdt"},
        label=label,
    )
    return TierBand(
        initial_leverage=_decimal(value["initial_leverage"], f"{label}.initial_leverage"),
        notional_floor_usdt=_decimal(value["notional_floor_usdt"], f"{label}.notional_floor_usdt"),
        notional_cap_usdt=_decimal(value["notional_cap_usdt"], f"{label}.notional_cap_usdt"),
        maintenance_margin_ratio=_decimal(value["maintenance_margin_ratio"], f"{label}.maintenance_margin_ratio"),
        cumulative_maintenance_usdt=_decimal(value["cumulative_maintenance_usdt"], f"{label}.cumulative_maintenance_usdt"),
    )


def _provenance(raw: object, label: str) -> TierProvenance | None:
    if raw is None:
        return None
    value = _fields(
        raw,
        allowed={"endpoint", "http_method", "security_type", "request_id", "signed_at_utc", "response_sha256", "environment"},
        required={"endpoint", "http_method", "security_type", "request_id", "signed_at_utc", "response_sha256", "environment"},
        label=label,
    )
    return TierProvenance(
        endpoint=_text(value["endpoint"], f"{label}.endpoint"),
        http_method=_text(value["http_method"], f"{label}.http_method"),
        security_type=_text(value["security_type"], f"{label}.security_type"),
        request_id=_text(value["request_id"], f"{label}.request_id"),
        signed_at_utc=_text(value["signed_at_utc"], f"{label}.signed_at_utc"),
        response_sha256=_sha(value["response_sha256"], f"{label}.response_sha256"),
        environment=_text(value["environment"], f"{label}.environment"),  # type: ignore[arg-type]
    )


def _schedule(raw: object, index: int) -> TierSchedule:
    label = f"tier_schedules[{index}]"
    value = _fields(
        raw,
        allowed={"symbol", "bands", "source", "effective_at_utc", "fetched_at_utc", "account_scope", "evidence_sha256", "verified", "snapshot_kind", "notional_cap_inclusive", "provenance"},
        required={"symbol", "bands", "source", "effective_at_utc", "fetched_at_utc", "account_scope", "evidence_sha256", "verified", "snapshot_kind", "notional_cap_inclusive", "provenance"},
        label=label,
    )
    bands = value["bands"]
    if not isinstance(bands, list) or not bands:
        raise ValueError(f"{label}.bands must be a non-empty array")
    return TierSchedule(
        symbol=_text(value["symbol"], f"{label}.symbol"),
        bands=tuple(_tier_band(item, f"{label}.bands[{band_index}]") for band_index, item in enumerate(bands)),
        source=_text(value["source"], f"{label}.source"),
        effective_at_utc=_text(value["effective_at_utc"], f"{label}.effective_at_utc"),
        fetched_at_utc=_text(value["fetched_at_utc"], f"{label}.fetched_at_utc"),
        account_scope=_text(value["account_scope"], f"{label}.account_scope"),
        evidence_sha256=_sha(value["evidence_sha256"], f"{label}.evidence_sha256"),
        verified=_bool(value["verified"], f"{label}.verified"),
        snapshot_kind=_text(value["snapshot_kind"], f"{label}.snapshot_kind"),  # type: ignore[arg-type]
        notional_cap_inclusive=_bool(value["notional_cap_inclusive"], f"{label}.notional_cap_inclusive"),
        provenance=_provenance(value["provenance"], f"{label}.provenance"),
    )


def _quantile(raw: object, label: str) -> QuantileResult:
    value = _fields(
        raw,
        allowed={"status", "p99", "maximum", "sample_size", "required_sample_size", "excluded_count", "rank"},
        required={"status", "p99", "maximum", "sample_size", "required_sample_size", "excluded_count", "rank"},
        label=label,
    )
    status = _text(value["status"], f"{label}.status")
    if status not in ("pass", "fail", "not_estimable"):
        raise ValueError(f"{label}.status is invalid")
    return QuantileResult(
        status=status,  # type: ignore[arg-type]
        p99=_nullable_decimal(value["p99"], f"{label}.p99"),
        maximum=_nullable_decimal(value["maximum"], f"{label}.maximum"),
        sample_size=_int(value["sample_size"], f"{label}.sample_size"),
        required_sample_size=_int(value["required_sample_size"], f"{label}.required_sample_size"),
        excluded_count=_int(value["excluded_count"], f"{label}.excluded_count"),
        rank=None if value["rank"] is None else _int(value["rank"], f"{label}.rank"),
    )


def _gaps(raw: object) -> tuple[GapP99, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("gap_p99s must be an array or null")
    result: list[GapP99] = []
    for index, item in enumerate(raw):
        label = f"gap_p99s[{index}]"
        value = _fields(item, allowed={"symbol", "side", "trade", "mark"}, required={"symbol", "side", "trade", "mark"}, label=label)
        side = _text(value["side"], f"{label}.side")
        if side not in ("long", "short"):
            raise ValueError(f"{label}.side is invalid")
        result.append(GapP99(
            symbol=_text(value["symbol"], f"{label}.symbol"),
            side=side,  # type: ignore[arg-type]
            trade=_quantile(value["trade"], f"{label}.trade"),
            mark=_quantile(value["mark"], f"{label}.mark"),
        ))
    return tuple(result)


def _decimal_tuple(value: object, label: str) -> tuple[Decimal, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array or null")
    return tuple(_decimal(item, f"{label}[{index}]") for index, item in enumerate(value))


def _required_decimal_tuple(value: object, label: str) -> tuple[Decimal, ...]:
    parsed = _decimal_tuple(value, label)
    if parsed is None:
        raise ValueError(f"{label} must be an array")
    return parsed


def _hash_pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of [name, sha256] pairs")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"{label} must contain only [name, sha256] pairs")
        result.append((_text(item[0], f"{label}[{index}].name"), _sha(item[1], f"{label}[{index}].sha256")))
    return tuple(result)


def _scenario(raw: object, index: int) -> ScenarioEvidence:
    label = f"execution_evidence.scenario_evidence[{index}]"
    value = _fields(
        raw,
        allowed={"scenario_id", "config_hash", "evidence_sha256", "window_data_sha256", "account_window_losses_usdt", "uncovered_deficits_usdt", "model_sha256", "tier_snapshot_hash", "assumption_hashes", "status", "execution_path", "slippage_multiple", "delay_ms", "liquidation_slippage_ticks"},
        required={"scenario_id", "config_hash", "evidence_sha256", "window_data_sha256", "account_window_losses_usdt", "uncovered_deficits_usdt", "model_sha256", "tier_snapshot_hash", "assumption_hashes", "status", "execution_path", "slippage_multiple", "delay_ms", "liquidation_slippage_ticks"},
        label=label,
    )
    status = _text(value["status"], f"{label}.status")
    if status not in ("pass", "fail"):
        raise ValueError(f"{label}.status is invalid")
    return ScenarioEvidence(
        scenario_id=_text(value["scenario_id"], f"{label}.scenario_id"),
        config_hash=_sha(value["config_hash"], f"{label}.config_hash"),
        evidence_sha256=_sha(value["evidence_sha256"], f"{label}.evidence_sha256"),
        window_data_sha256=_nullable_sha(value["window_data_sha256"], f"{label}.window_data_sha256"),
        account_window_losses_usdt=_required_decimal_tuple(value["account_window_losses_usdt"], f"{label}.account_window_losses_usdt"),
        uncovered_deficits_usdt=_required_decimal_tuple(value["uncovered_deficits_usdt"], f"{label}.uncovered_deficits_usdt"),
        model_sha256=_nullable_sha(value["model_sha256"], f"{label}.model_sha256"),
        tier_snapshot_hash=_nullable_sha(value["tier_snapshot_hash"], f"{label}.tier_snapshot_hash"),
        assumption_hashes=_hash_pairs(value["assumption_hashes"], f"{label}.assumption_hashes"),
        status=status,  # type: ignore[arg-type]
        execution_path=None if value["execution_path"] is None else _text(value["execution_path"], f"{label}.execution_path"),
        slippage_multiple=_nullable_decimal(value["slippage_multiple"], f"{label}.slippage_multiple"),
        delay_ms=None if value["delay_ms"] is None else _int(value["delay_ms"], f"{label}.delay_ms"),
        liquidation_slippage_ticks=None if value["liquidation_slippage_ticks"] is None else _int(value["liquidation_slippage_ticks"], f"{label}.liquidation_slippage_ticks"),
    )


def _execution(raw: object) -> ExecutionEvidence | None:
    if raw is None:
        return None
    value = _fields(
        raw,
        allowed={"model_sha256", "window_data_sha256", "assumption_hashes", "account_window_losses_usdt", "uncovered_deficits_usdt", "tier_snapshot_hash", "scenario_evidence"},
        required={"model_sha256", "window_data_sha256", "assumption_hashes", "account_window_losses_usdt", "uncovered_deficits_usdt", "tier_snapshot_hash", "scenario_evidence"},
        label="execution_evidence",
    )
    scenarios = value["scenario_evidence"]
    if not isinstance(scenarios, list):
        raise ValueError("execution_evidence.scenario_evidence must be an array")
    return ExecutionEvidence(
        model_sha256=_nullable_sha(value["model_sha256"], "execution_evidence.model_sha256"),
        window_data_sha256=_nullable_sha(value["window_data_sha256"], "execution_evidence.window_data_sha256"),
        assumption_hashes=_hash_pairs(value["assumption_hashes"], "execution_evidence.assumption_hashes"),
        account_window_losses_usdt=_decimal_tuple(value["account_window_losses_usdt"], "execution_evidence.account_window_losses_usdt"),
        uncovered_deficits_usdt=_decimal_tuple(value["uncovered_deficits_usdt"], "execution_evidence.uncovered_deficits_usdt"),
        tier_snapshot_hash=_nullable_sha(value["tier_snapshot_hash"], "execution_evidence.tier_snapshot_hash"),
        scenario_evidence=tuple(_scenario(item, index) for index, item in enumerate(scenarios)),
    )


def parse_bundle(raw: bytes) -> tuple[RiskConfig, tuple[TierSchedule, ...], tuple[GapP99, ...] | None, ExecutionEvidence | None, str, str]:
    value = _fields(
        _parse_json(raw),
        allowed={"config", "tier_schedules", "gap_p99s", "execution_evidence", "as_of_utc"},
        required={"config", "tier_schedules", "gap_p99s", "execution_evidence", "as_of_utc"},
        label="bundle",
    )
    schedules = value["tier_schedules"]
    if not isinstance(schedules, list):
        raise ValueError("tier_schedules must be an array")
    as_of = _text(value["as_of_utc"], "as_of_utc")
    return (
        _config(value["config"]),
        tuple(_schedule(item, index) for index, item in enumerate(schedules)),
        _gaps(value["gap_p99s"]),
        _execution(value["execution_evidence"]),
        as_of,
        hashlib.sha256(raw).hexdigest(),
    )


def _read_bounded(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError("cannot read input bundle") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds the documented size limit")
    return raw


def _write_private(output: Path, report: PreflightReport, input_sha256: str) -> dict[str, object]:
    report_mapping = preflight_report_to_mapping(report)
    report_bytes = (json.dumps(report_mapping, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "account-risk-preflight-export-v1",
        "status": report.status,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "input_bundle_sha256": input_sha256,
        "strategy_signal_or_return_accessed": False,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        output.chmod(0o700)
    except FileExistsError as exc:
        raise ValueError("output directory already exists") from exc
    except OSError as exc:
        raise ValueError("cannot create private output directory") from exc
    for name, data in ((OUTPUT_REPORT_NAME, report_bytes), (OUTPUT_MANIFEST_NAME, manifest_bytes)):
        path = output / name
        try:
            with path.open("xb") as stream:
                path.chmod(0o600)
                stream.write(data)
        except OSError as exc:
            raise ValueError("cannot write private preflight artifact") from exc
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="bounded JSON evidence bundle")
    parser.add_argument("--output", type=Path, required=True, help="new private output directory")
    args = parser.parse_args(argv)
    try:
        raw = _read_bounded(args.input)
        config, schedules, gaps, execution, as_of, input_sha256 = parse_bundle(raw)
        report = evaluate_preflight(config, schedules, gaps, execution, as_of_utc=as_of)
        manifest = _write_private(args.output, report, input_sha256)
    except (OSError, TypeError, ValueError, KeyError, ArithmeticError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": manifest["status"], "report_sha256": manifest["report_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
