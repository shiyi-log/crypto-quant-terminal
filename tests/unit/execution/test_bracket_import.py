import hashlib
import json
from decimal import Decimal, localcontext

import pytest

from core.execution.account_risk_preflight import (
    RiskConfig, TierBand, TierSchedule, canonical_sha256, validate_risk_config,
)
from core.execution.bracket_import import (
    import_bracket_artifact, main, verify_bracket_artifact,
)
from core.execution.tier_snapshot import load_tier_snapshot


IMPORTED = "2026-10-23T01:00:00Z"


def _raw(symbol="BTCUSDT", *, coefficient=None):
    data = {"symbol": symbol, "brackets": [{
        "bracket": 1, "initialLeverage": 125, "notionalCap": 50000,
        "notionalFloor": 0, "maintMarginRatio": 0.004, "cum": 0,
    }]}
    if coefficient is not None:
        data["notionalCoef"] = coefficient
    return json.dumps(data, separators=(",", ":")).encode()


def _metadata(raw):
    return {
        "account_scope": "synthetic-account-1",
        "request_id": "synthetic-request-1",
        "endpoint": "/fapi/v1/leverageBracket", "http_method": "GET",
        "security_type": "USER_DATA", "environment": "testnet",
        "signed_at_utc": "2026-10-23T00:00:00Z",
        "effective_at_utc": "2026-10-23T00:00:00Z",
        "fetched_at_utc": "2026-10-23T00:00:01Z",
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "notional_cap_inclusive": False,
        "boundary_evidence_sha256": "a" * 64,
        "raw_artifact_ref": "raw_response.json",
        "source": "synthetic-testnet-source",
    }


def _import(raw=None, metadata=None):
    raw = _raw() if raw is None else raw
    return import_bracket_artifact(
        raw, metadata=_metadata(raw) if metadata is None else metadata,
        symbols=("BTCUSDT",), imported_at_utc=IMPORTED,
    )


def test_import_binds_exact_raw_bytes_metadata_and_decimal_bands_without_approval():
    raw = _raw()
    artifact = _import(raw)
    manifest = artifact.to_mapping()
    assert manifest["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["claimed_metadata"]["response_sha256"] == manifest["response_sha256"]
    assert manifest["metadata_sha256"] == canonical_sha256(manifest["claimed_metadata"])
    assert manifest["parsed_payload_sha256"] == canonical_sha256(manifest["parsed_schedules"])
    assert manifest["selected_symbols"] == ["BTC/USDT:USDT"]
    assert manifest["execution_symbols"] == ["BTCUSDT"]
    assert manifest["parsed_schedules"][0]["bands"][0]["maintenance_margin_ratio"] == "0.004"
    assert manifest["status"] == "pending_manual_verification"
    assert manifest["risk_segment_status"] == "open"
    assert manifest["risk_feasibility_preflight"] == "not_estimable"
    assert manifest["verification"]["request_signature_verification"] == "not_run"
    assert manifest["verification"]["response_origin_verification"] == "not_run"
    assert verify_bracket_artifact(raw, json.loads(json.dumps(manifest))) == artifact
    with pytest.raises(ValueError):
        load_tier_snapshot(manifest["pending_tier_snapshot"])
    # A caller cannot bypass the pending guard by just adding a content-shaped hash.
    candidate = manifest["pending_tier_snapshot"]
    candidate["snapshot_hash"] = "a" * 64
    with pytest.raises(ValueError, match="static diagnostic|verified"):
        load_tier_snapshot(candidate)


def test_import_is_immutable_and_does_not_retain_mutable_input_mappings():
    raw = _raw()
    metadata = _metadata(raw)
    artifact = _import(raw, metadata)
    before = artifact.to_mapping()
    metadata["account_scope"] = "different-account"
    changed = artifact.to_mapping()
    changed["parsed_schedules"][0]["bands"][0]["initial_leverage"] = "1"
    changed["pending_tier_snapshot"]["schedules"][0]["verified"] = True
    assert artifact.to_mapping() == before


def test_whitespace_or_numeric_change_in_raw_response_invalidates_claimed_hash():
    raw = _raw()
    for changed in (raw + b"\n", raw.replace(b"125", b"120")):
        with pytest.raises(ValueError, match="does not match exact response"):
            _import(changed, _metadata(raw))
    changed = raw + b"\n"
    first, second = _import(raw).to_mapping(), _import(changed).to_mapping()
    assert first["parsed_payload_sha256"] == second["parsed_payload_sha256"]
    assert first["binding_sha256"] != second["binding_sha256"]


@pytest.mark.parametrize("change", [
    lambda m: m["parsed_schedules"][0]["bands"][0].update(initial_leverage="120"),
    lambda m: m["pending_tier_snapshot"]["schedules"][0].update(verified=True),
    lambda m: m["claimed_metadata"].update(account_scope="other-account"),
    lambda m: m["verification"].update(response_origin_verification="pass"),
    lambda m: m.update(status="pass"),
    lambda m: m.update(response_sha256="0" * 64),
    lambda m: m.update(unexpected="extra"),
])
def test_reparse_verification_rejects_manifest_tampering(change):
    raw = _raw()
    manifest = _import(raw).to_mapping()
    change(manifest)
    with pytest.raises(ValueError):
        verify_bracket_artifact(raw, manifest)


def test_parser_rejections_propagate_and_missing_symbol_is_never_fabricated():
    with pytest.raises(ValueError, match="missing"):
        _import(_raw("ETHUSDT"))
    with pytest.raises(ValueError, match="notionalCoef"):
        _import(_raw(coefficient=1.2))


@pytest.mark.parametrize("field,value", [
    ("apiKey", "never-copy-me"), ("signature", "never-copy-me"), ("verified", True),
    ("endpoint", "/wrong"), ("http_method", "POST"), ("security_type", "NONE"),
    ("environment", "unknown"), ("notional_cap_inclusive", "false"),
    ("notional_cap_inclusive", 0), ("response_sha256", "A" * 64),
    ("boundary_evidence_sha256", "missing"), ("raw_artifact_ref", "../outside.json"),
    ("raw_artifact_ref", "source?apiKey=x"), ("raw_artifact_ref", "secret.json"),
    ("account_scope", "synthetic-account-1 "), ("request_id", "request?signature=x"),
    ("signed_at_utc", "2026-10-23T02:00:00Z"),
    ("signed_at_utc", "2026-10-23T00:00:00+08:00"),
    ("fetched_at_utc", "2026-10-23T02:00:00Z"),
    ("effective_at_utc", "2026-10-23T00:00:02Z"),
])
def test_metadata_is_strictly_allowlisted_and_times_and_claims_are_validated(field, value):
    raw = _raw()
    metadata = _metadata(raw)
    metadata[field] = value
    with pytest.raises(ValueError):
        _import(raw, metadata)


def test_metadata_does_not_echo_unknown_key_values_and_missing_fields_are_rejected():
    raw = _raw()
    metadata = _metadata(raw)
    metadata["unexpected-secret-value"] = "never-copy-me"
    with pytest.raises(ValueError) as error:
        _import(raw, metadata)
    assert "never-copy-me" not in str(error.value)
    assert "unexpected-secret-value" not in str(error.value)
    metadata = _metadata(raw)
    del metadata["notional_cap_inclusive"]
    with pytest.raises(ValueError):
        _import(raw, metadata)


def test_decimal_artifact_serialization_remains_exact_under_small_context_precision():
    raw = _raw().replace(b"0.004", b"0.004000000000000000000000000001")
    with localcontext() as context:
        context.prec = 6
        manifest = _import(raw).to_mapping()
    band = manifest["parsed_schedules"][0]["bands"][0]
    assert band["maintenance_margin_ratio"] == "0.004000000000000000000000000001"
    assert manifest["parsed_payload_sha256"] != _import().to_mapping()["parsed_payload_sha256"]


def test_pending_schedule_cannot_pass_preflight_by_being_wrapped_as_a_typed_record():
    artifact = _import()
    mapping = artifact.to_mapping()["pending_tier_snapshot"]["schedules"][0]
    tier = TierSchedule(
        symbol=mapping["symbol"],
        bands=tuple(TierBand(**{key: Decimal(value) for key, value in band.items()})
                    for band in mapping["bands"]),
        source=mapping["source"], effective_at_utc=mapping["effective_at_utc"],
        fetched_at_utc=mapping["fetched_at_utc"], account_scope=mapping["account_scope"],
        evidence_sha256=mapping["evidence_sha256"], verified=False,
        snapshot_kind=mapping["snapshot_kind"], notional_cap_inclusive=False,
    )
    config = RiskConfig(
        initial_equity_usdt=Decimal("10000"), max_leverage=Decimal("100"),
        margin_mode="isolated", n_max_usdt=Decimal("100"),
        per_symbol_caps_usdt=(("BTCUSDT", Decimal("100")),),
        per_symbol_leverages=(("BTCUSDT", Decimal("100")),),
        account_p99_limit_pct=Decimal("0.05"), account_scope=mapping["account_scope"],
    )
    validation = validate_risk_config(config, (tier,), as_of_utc=IMPORTED)
    assert validation.status == "not_estimable"
    assert ("BTCUSDT", "unverified") in validation.tier_checks


def test_cli_retains_exact_bytes_in_private_new_directory_and_cannot_overwrite(tmp_path, capsys):
    raw = _raw()
    response, metadata, output = (tmp_path / "source.json", tmp_path / "metadata.json", tmp_path / "bundle")
    response.write_bytes(raw)
    metadata.write_text(json.dumps(_metadata(raw)))
    args = ["--response", str(response), "--metadata", str(metadata), "--symbols", "BTCUSDT", "--output", str(output)]
    assert main(args) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert (output / "raw_response.json").read_bytes() == raw
    assert verify_bracket_artifact(raw, manifest).metadata.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o700
    assert all((output / name).stat().st_mode & 0o777 == 0o600 for name in ("manifest.json", "raw_response.json"))
    stdout = capsys.readouterr().out
    assert "synthetic-account-1" not in stdout
    assert "synthetic-request-1" not in stdout
    assert "pending_manual_verification" in stdout
    with pytest.raises(FileExistsError):
        main(args)
    assert (output / "raw_response.json").read_bytes() == raw


def test_cli_rejects_duplicate_metadata_and_raw_hash_mismatch_before_creating_output(tmp_path):
    raw = _raw()
    response, metadata, output = (tmp_path / "source.json", tmp_path / "metadata.json", tmp_path / "bundle")
    response.write_bytes(raw)
    text = json.dumps(_metadata(raw))
    metadata.write_text(text[:-1] + ', "account_scope": "duplicate"}')
    args = ["--response", str(response), "--metadata", str(metadata), "--symbols", "BTCUSDT", "--output", str(output)]
    with pytest.raises(ValueError, match="duplicate"):
        main(args)
    assert not output.exists()
    claims = _metadata(raw)
    claims["response_sha256"] = "0" * 64
    metadata.write_text(json.dumps(claims))
    with pytest.raises(ValueError, match="exact response"):
        main(args)
    assert not output.exists()
