from __future__ import annotations

import json
import stat

import pytest

from core.execution.account_risk_preflight import evaluate_preflight, preflight_report_to_mapping
from tools.export_account_risk_preflight import main, parse_bundle


def _bundle(*, config_extra: dict[str, object] | None = None, initial_equity: object = "10000") -> bytes:
    config: dict[str, object] = {
        "initial_equity_usdt": initial_equity,
        "max_leverage": "100",
        "margin_mode": "isolated",
        "n_max_usdt": "25000",
        "per_symbol_caps_usdt": [["BTCUSDT", "25000"], ["ETHUSDT", "25000"], ["TRXUSDT", "25000"]],
        "per_symbol_leverages": [["BTCUSDT", "100"], ["ETHUSDT", "75"], ["TRXUSDT", "25"]],
        "account_p99_limit_pct": "0.05",
        "required_n_gap": 100,
        "required_account_windows": 100,
        "account_scope": "synthetic-research-boundary",
        "tier_snapshot_max_age_hours": "24",
        "account_loss_authority_scenario_id": "A_long",
        "tier_snapshot_hash": None,
    }
    if config_extra:
        config.update(config_extra)
    return (json.dumps({
        "config": config,
        "tier_schedules": [],
        "gap_p99s": None,
        "execution_evidence": None,
        "as_of_utc": "2026-10-24T00:00:00Z",
    }, separators=(",", ":")) + "\n").encode()


def test_no_evidence_exports_not_estimable_without_path_or_secret_echo(tmp_path, capsys):
    input_path = tmp_path / "bundle.json"
    output_path = tmp_path / "private-preflight"
    input_path.write_bytes(_bundle())

    assert main(["--input", str(input_path), "--output", str(output_path)]) == 0
    stdout = capsys.readouterr().out
    assert "not_estimable" in stdout
    assert str(input_path) not in stdout
    assert str(output_path) not in stdout

    report = json.loads((output_path / "report.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_path / "manifest.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == "account-risk-preflight-v1"
    assert report["status"] == "not_estimable"
    assert manifest["status"] == "not_estimable"
    assert manifest["strategy_signal_or_return_accessed"] is False
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((output_path / "report.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output_path / "manifest.json").stat().st_mode) == 0o600


def test_unknown_fields_and_json_floats_are_rejected_without_echoing_values():
    raw = _bundle(config_extra={"api_secret": "do-not-echo"})
    with pytest.raises(ValueError, match="unknown fields") as error:
        parse_bundle(raw)
    assert "do-not-echo" not in str(error.value)

    with pytest.raises(ValueError, match="floating-point"):
        parse_bundle(_bundle(initial_equity=10000.0))


def test_existing_output_directory_is_never_overwritten(tmp_path):
    input_path = tmp_path / "bundle.json"
    output_path = tmp_path / "private-preflight"
    input_path.write_bytes(_bundle())
    output_path.mkdir()
    sentinel = output_path / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--input", str(input_path), "--output", str(output_path)])
    assert error.value.code == 2
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_report_serializer_preserves_decimal_strings_and_rejects_wrong_type():
    config = {
        "initial_equity_usdt": "10000",
        "max_leverage": "100",
        "margin_mode": "isolated",
        "n_max_usdt": "25000",
        "per_symbol_caps_usdt": (("BTCUSDT", "25000"),),
        "per_symbol_leverages": (("BTCUSDT", "100"),),
        "account_p99_limit_pct": "0.05",
        "account_scope": "synthetic-research-boundary",
        "account_loss_authority_scenario_id": "A_long",
    }
    from decimal import Decimal
    from core.execution.account_risk_preflight import RiskConfig

    risk_config = RiskConfig(
        initial_equity_usdt=Decimal(config["initial_equity_usdt"]),
        max_leverage=Decimal(config["max_leverage"]),
        margin_mode="isolated",
        n_max_usdt=Decimal(config["n_max_usdt"]),
        per_symbol_caps_usdt=(("BTCUSDT", Decimal("25000")),),
        per_symbol_leverages=(("BTCUSDT", Decimal("100")),),
        account_p99_limit_pct=Decimal(config["account_p99_limit_pct"]),
        account_scope=config["account_scope"],
        account_loss_authority_scenario_id=config["account_loss_authority_scenario_id"],
    )
    report = evaluate_preflight(risk_config, (), None, None, as_of_utc="2026-10-24T00:00:00Z")
    mapping = preflight_report_to_mapping(report)
    assert mapping["status"] == "not_estimable"
    assert isinstance(mapping["config_hash"], str)
    assert all(isinstance(item, str) for item in mapping["missing_assumptions"])
    with pytest.raises(TypeError):
        preflight_report_to_mapping(object())
