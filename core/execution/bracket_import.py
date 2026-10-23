"""Import offline leverage-bracket bytes into a pending, non-executable artifact.

This module binds exact response bytes to parsed bands and a small allowlist of
claimed, non-secret request metadata. It does not contact Binance, inspect keys,
verify a request signature or authenticate a response. The output deliberately
has no execution snapshot hash and cannot be loaded by ``load_tier_snapshot``.
Independent provenance review and the complete account-risk preflight remain
separate prerequisites.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .account_risk_preflight import canonical_sha256
from .bracket_response import MAX_RESPONSE_BYTES, ParsedBracketResponse, parse_bracket_response


SCHEMA_VERSION = "binance-bracket-import-v1"
PARSER_CONTRACT = "binance-leverage-bracket-strict-v1"
MAX_METADATA_BYTES = 16 * 1024
_METADATA_FIELDS = frozenset({
    "account_scope", "request_id", "endpoint", "http_method", "security_type", "environment",
    "signed_at_utc", "fetched_at_utc", "effective_at_utc", "response_sha256",
    "notional_cap_inclusive", "boundary_evidence_sha256", "raw_artifact_ref", "source",
})


def _token(value: Any, field: str) -> str:
    # Opaque aliases only: do not copy headers, query strings or URLs to evidence.
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ValueError(f"{field} must be a non-secret opaque identifier of at most 128 characters")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _artifact_ref(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError("metadata.raw_artifact_ref must be a non-empty relative artifact name")
    if re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
        raise ValueError("metadata.raw_artifact_ref must be a safe relative artifact name")
    lowered = value.casefold().replace("_", "").replace("-", "")
    if any(fragment in lowered for fragment in ("apikey", "secret", "privatekey", "mnemonic", "seedphrase")):
        raise ValueError("metadata.raw_artifact_ref must not contain credential-like names")
    return value


def _utc(value: Any, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value or value != value.strip() or "T" not in value:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must have a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), parsed


def _decimal_text(value: Decimal) -> str:
    # Decimal.normalize() uses the caller's precision and could round evidence.
    # The parser bounds extreme exponents before these exact strings are made.
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _execution_symbol(symbol: str) -> str:
    """Return the repository's compact Binance symbol for preflight joins."""
    suffix = "/USDT:USDT"
    if not symbol.endswith(suffix):
        raise ValueError("parsed symbol is not a canonical USDT-M symbol")
    return symbol[:-len(suffix)] + "USDT"


@dataclass(frozen=True, slots=True)
class BracketImportMetadata:
    """Caller claims, not proof of account ownership or exchange origin."""

    account_scope: str
    request_id: str
    endpoint: str
    http_method: str
    security_type: str
    environment: str
    signed_at_utc: str
    fetched_at_utc: str
    effective_at_utc: str
    response_sha256: str
    notional_cap_inclusive: bool
    boundary_evidence_sha256: str
    raw_artifact_ref: str
    source: str


def _metadata(raw: Mapping[str, object], *, imported_at: datetime) -> BracketImportMetadata:
    if not isinstance(raw, Mapping) or set(raw) != _METADATA_FIELDS:
        # Do not echo unknown field names/values, which could be credentials.
        raise ValueError("metadata must contain exactly the documented non-secret fields")
    if (raw["endpoint"], raw["http_method"], raw["security_type"]) != (
        "/fapi/v1/leverageBracket", "GET", "USER_DATA",
    ):
        raise ValueError("metadata must describe GET /fapi/v1/leverageBracket USER_DATA")
    if raw["environment"] not in ("production", "testnet", "demo"):
        raise ValueError("metadata.environment must be production, testnet or demo")
    if type(raw["notional_cap_inclusive"]) is not bool:
        raise ValueError("metadata.notional_cap_inclusive must be an explicit boolean")
    signed_text, signed = _utc(raw["signed_at_utc"], "metadata.signed_at_utc")
    fetched_text, fetched = _utc(raw["fetched_at_utc"], "metadata.fetched_at_utc")
    effective_text, effective = _utc(raw["effective_at_utc"], "metadata.effective_at_utc")
    if signed > fetched or effective > fetched or fetched > imported_at:
        raise ValueError("metadata timestamps must satisfy signed/effective <= fetched <= imported")
    return BracketImportMetadata(
        account_scope=_token(raw["account_scope"], "metadata.account_scope"),
        request_id=_token(raw["request_id"], "metadata.request_id"),
        endpoint=raw["endpoint"],
        http_method=raw["http_method"],
        security_type=raw["security_type"],
        environment=raw["environment"],
        signed_at_utc=signed_text,
        fetched_at_utc=fetched_text,
        effective_at_utc=effective_text,
        response_sha256=_sha(raw["response_sha256"], "metadata.response_sha256"),
        notional_cap_inclusive=raw["notional_cap_inclusive"],
        boundary_evidence_sha256=_sha(raw["boundary_evidence_sha256"], "metadata.boundary_evidence_sha256"),
        raw_artifact_ref=_artifact_ref(raw["raw_artifact_ref"]),
        source=_token(raw["source"], "metadata.source"),
    )


def _parsed_payload(parsed: ParsedBracketResponse) -> list[dict[str, object]]:
    return [{
        "symbol": schedule.symbol,
        "notional_coefficient": (
            None if schedule.notional_coefficient is None
            else _decimal_text(schedule.notional_coefficient)
        ),
        "bands": [{field: _decimal_text(value) for field, value in asdict(band).items()}
                  for band in schedule.bands],
    } for schedule in parsed.schedules]


@dataclass(frozen=True, slots=True)
class PendingBracketImport:
    metadata: BracketImportMetadata
    parsed: ParsedBracketResponse
    imported_at_utc: str

    def to_mapping(self) -> dict[str, object]:
        """Return fresh JSON-safe data; mutations cannot alter this record."""
        metadata = asdict(self.metadata)
        schedules = _parsed_payload(self.parsed)
        for item in schedules:
            item["execution_symbol"] = _execution_symbol(str(item["symbol"]))
        symbols = [item.symbol for item in self.parsed.schedules]
        binding = {
            "schema_version": SCHEMA_VERSION,
            "parser_contract": PARSER_CONTRACT,
            "response_sha256": self.parsed.response_sha256,
            "metadata_sha256": canonical_sha256(metadata),
            "parsed_payload_sha256": canonical_sha256(schedules),
            "selected_symbols": symbols,
            "execution_symbols": [_execution_symbol(symbol) for symbol in symbols],
            "imported_at_utc": self.imported_at_utc,
            "raw_artifact_ref": self.metadata.raw_artifact_ref,
        }
        binding_hash = canonical_sha256(binding)
        provenance = {key: metadata[key] for key in (
            "endpoint", "http_method", "security_type", "request_id", "signed_at_utc",
            "response_sha256", "environment",
        )}
        candidate = {
            "snapshot_hash": None,
            "account_scope": self.metadata.account_scope,
            "as_of_utc": self.metadata.fetched_at_utc,
            "schedules": [{
                # Pending artifacts expose both representations explicitly.
                # The eventual promotion step must normalize and hash once;
                # this record is not accepted by the execution loader.
                "symbol": item["execution_symbol"],
                "bands": item["bands"],
                "source": self.metadata.source,
                "effective_at_utc": self.metadata.effective_at_utc,
                "fetched_at_utc": self.metadata.fetched_at_utc,
                "account_scope": self.metadata.account_scope,
                "evidence_sha256": binding_hash,
                "verified": False,
                "snapshot_kind": "candidate_import",
                "notional_cap_inclusive": self.metadata.notional_cap_inclusive,
                "provenance": dict(provenance),
            } for item in schedules],
        }
        return {
            **binding,
            "binding_sha256": binding_hash,
            "status": "pending_manual_verification",
            "risk_feasibility_preflight": "not_estimable",
            "risk_segment_status": "open",
            "strategy_signal_or_return_accessed": False,
            "claimed_metadata": metadata,
            "parsed_schedules": schedules,
            "verification": {
                "raw_response_hash_matches_claim": True,
                "raw_to_parsed_binding": "reproducible_by_offline_reparse",
                "request_signature_verification": "not_run",
                "response_origin_verification": "not_run",
                "account_scope_verification": "not_run",
                "cap_boundary_verification": "not_run",
                "reviewer": None,
            },
            "pending_tier_snapshot": candidate,
            "pending_tier_snapshot_sha256": canonical_sha256(candidate),
        }


def import_bracket_artifact(
    raw: bytes,
    *,
    metadata: Mapping[str, object],
    symbols: tuple[str, ...],
    imported_at_utc: str | None = None,
) -> PendingBracketImport:
    """Bind exact bytes to pending bands; never approve an execution snapshot."""
    import_text, imported_at = _utc(
        datetime.now(timezone.utc).isoformat() if imported_at_utc is None else imported_at_utc,
        "imported_at_utc",
    )
    claims = _metadata(metadata, imported_at=imported_at)
    parsed = parse_bracket_response(raw, symbols=symbols)
    if claims.response_sha256 != parsed.response_sha256:
        raise ValueError("metadata.response_sha256 does not match exact response bytes")
    return PendingBracketImport(claims, parsed, import_text)


def verify_bracket_artifact(raw: bytes, manifest: Mapping[str, object]) -> PendingBracketImport:
    """Reparse and compare the complete pending manifest, without authentication."""
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a pending import mapping")
    symbols = manifest.get("selected_symbols")
    if not isinstance(symbols, list):
        raise ValueError("manifest.selected_symbols must be an array")
    recreated = import_bracket_artifact(
        raw,
        metadata=manifest.get("claimed_metadata"),
        symbols=tuple(symbols),
        imported_at_utc=manifest.get("imported_at_utc"),
    )
    if canonical_sha256(manifest) != canonical_sha256(recreated.to_mapping()):
        raise ValueError("manifest does not match raw bytes, parsed bands and pending metadata")
    return recreated


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input exceeds documented size limit")
    return raw


def _json_object(raw: bytes) -> Mapping[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate metadata JSON key")
            result[key] = value
        return result

    data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(data, Mapping):
        raise ValueError("metadata JSON must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new private artifact directory")
    args = parser.parse_args(argv)
    raw = _read_bounded(args.response, MAX_RESPONSE_BYTES)
    artifact = import_bracket_artifact(
        raw,
        metadata=_json_object(_read_bounded(args.metadata, MAX_METADATA_BYTES)),
        symbols=tuple(args.symbols),
    )
    manifest = artifact.to_mapping()
    if artifact.metadata.raw_artifact_ref != "raw_response.json":
        raise ValueError("CLI metadata.raw_artifact_ref must be raw_response.json")
    # Validate everything before creating output; existing evidence is never overwritten.
    verify_bracket_artifact(raw, manifest)
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in (
        ("raw_response.json", raw),
        ("manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")),
    ):
        with (args.output / name).open("xb") as stream:
            (args.output / name).chmod(0o600)
            stream.write(data)
    # No raw response, account scope, request ID or filesystem path on stdout.
    print(json.dumps({"status": manifest["status"], "binding_sha256": manifest["binding_sha256"],
                      "risk_feasibility_preflight": "not_estimable", "risk_segment_status": "open"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
