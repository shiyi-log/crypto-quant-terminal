"""真实不可变归档配合内存 SQL 边界；不连接数据库或交易所。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import localcontext

import pytest

from demo_acceptance_service import DemoAcceptanceService
from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_round_trip_costs import reconcile_round_trip_costs
from tests.test_demo_archive_providers import MemorySQL
from tests.test_demo_acceptance import valid_round
from tests.test_demo_round_trip_costs import evidence as cost_evidence

SCOPE = {
    "environment": "binance_demo",
    "account_id": "demo-account",
    "stage_id": "stage-1000",
    "strategy_version_id": "strategy-v1",
    "run_id": "demo-acceptance-001",
}
IDENTITY = {key: str(index + 1) * 64 for index, key in enumerate(HASHES)}
START = datetime(2025, 12, 31, 16, tzinfo=timezone.utc)
END = START + timedelta(days=30)
NOW = int(END.timestamp() * 1000)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def semantic(record, sources, *, purpose, binding):
    return {
        "verified": True,
        "purpose": purpose,
        "scope": binding["scope"],
        "identity_hashes": binding["identity_hashes"],
        "record_sha256": record["record_sha256"],
        "source_record_hashes": {ref: row["record_sha256"] for ref, row in sources.items()},
        "window_start_at": binding["window_start_at"],
        "window_end_at": binding["window_end_at"],
    }


def setup(*, validator=semantic, change=None, omit_day=None, costs=False,
          change_bundle_source=False, change_projection_source=False):
    archive = DemoEvidenceStore(MemorySQL(), SCOPE["account_id"], SCOPE["stage_id"])
    raw = {}
    for kind in ("account_snapshot", "normal_orders", "conditional_orders", "fills", "cashflows"):
        raw[kind] = archive.append(kind, SCOPE, {"identity_hashes": IDENTITY, "data": []}, {})[
            "ref"
        ]
    source = valid_round()
    if change:
        change(source)

    def projection(kind, purpose, value, refs=None, **extra):
        refs = refs or raw
        body = {
            "schema": "demo-acceptance-projection-v1",
            "purpose": purpose,
            "identity_hashes": IDENTITY,
            "source_refs": refs,
            "source_record_hashes": {
                ref: archive.read(ref)["record_sha256"] for ref in refs.values()
            },
            "value": value,
            **extra,
        }
        return archive.append(kind, SCOPE, body, {})["ref"]

    boundaries = []
    for index in range(31):
        at = START + timedelta(days=index)
        if index == 0:
            value = deepcopy(source["opening_account_state"])
        elif index == 30:
            value = deepcopy(source["closing_account_state"])
        else:
            value = {
                **SCOPE,
                "as_of": iso(at),
                "net_equity_usdt": source["daily_records"][index]["opening_equity_usdt"],
                "flat": True,
                "no_open_orders": True,
                "no_conditional_orders": True,
                "reconciled": True,
            }
        value.update(
            method="replayed_boundary_projection", state_at=iso(at), valuation_policy_hash="a" * 64
        )
        value['stage_id'] = SCOPE['stage_id']
        boundaries.append(projection("boundary", "boundary", value))
    daily = []
    for index, row in enumerate(source["daily_records"]):
        if index == omit_day:
            continue
        at = START + timedelta(days=index)
        value = {**row, "window_start_at": iso(at), "window_end_at": iso(at + timedelta(days=1))}
        value['stage_id'] = SCOPE['stage_id']
        daily_sources = {
            **raw,
            "opening_boundary": boundaries[index],
            "closing_boundary": boundaries[index + 1],
        }
        if change_projection_source and index == 0:
            daily_sources["fills"] = archive.append(
                "fills", SCOPE, {"identity_hashes": IDENTITY, "data": ["other"]}, {}
            )["ref"]
        daily.append(
            projection(
                "equity_reconciliation",
                "daily",
                value,
                daily_sources,
            )
        )
    risk = projection(
        "equity_reconciliation",
        "risk",
        {
            **SCOPE,
            "complete": source["risk_evidence_complete"],
            "window_start_at": iso(START),
            "window_end_at": iso(END),
            "hard_failures": source["confirmed_hard_risk_failures"],
        },
    )
    completions = []
    if costs:
        evidence = cost_evidence()
        evidence.update(
            SCOPE, **IDENTITY, start_ms=int(START.timestamp() * 1000), end_ms=NOW, as_of_ms=NOW
        )
        evidence["opening"].update(SCOPE, timestamp_ms=int(START.timestamp() * 1000))
        evidence["coverage"].update(SCOPE, start_ms=int(START.timestamp() * 1000), end_ms=NOW)
        base = deepcopy(evidence)
        evidence.update(fills=[], orders=[], income_rows=[])
        for index in range(100):
            offset = int(START.timestamp() * 1000) + index * 100_000
            for field in ("fills", "orders", "income_rows"):
                for original in base[field]:
                    row = deepcopy(original)
                    row.update(SCOPE)
                    for key in (
                        "exchange_trade_id",
                        "exchange_order_id",
                        "client_id",
                        "transaction_id",
                    ):
                        if row.get(key) is not None:
                            row[key] = str(row[key]) + "-" + str(index)
                    for key in ("timestamp_ms", "updated_ms"):
                        if key in row:
                            row[key] += offset
                    if row.get("realized_pnl") == "10":
                        row["realized_pnl"] = "15.7"
                    if row.get("income_type") == "REALIZED_PNL":
                        row["amount"] = "15.7"
                    evidence[field].append(row)
        cost_ref = projection("equity_reconciliation", "cost", {}, cost_evidence=evidence)
        for trip in reconcile_round_trip_costs(evidence).to_dict()["trips"]:
            completions.append(archive.append_completion(SCOPE, trip, [cost_ref])["ref"])
    manifest = {
        "schema": "demo-acceptance-manifest-v1",
        "identity_hashes": IDENTITY,
        "round_id": "demo-acceptance-001",
        "start_date_bjt": "2026-01-01",
        "projection_source_refs": raw,
        "boundary_refs": boundaries,
        "daily_refs": daily,
        "risk_ref": risk,
        "completion_refs": completions,
    }
    manifest["projection_record_hashes"] = {
        ref: archive.read(ref)["record_sha256"] for ref in [*boundaries, *daily, risk, *completions]
    }
    manifest_ref = archive.append("account_snapshot", SCOPE, manifest, {})["ref"]
    bundle_refs = dict(raw)
    if change_bundle_source:
        bundle_refs["fills"] = archive.append(
            "fills", SCOPE, {"identity_hashes": IDENTITY, "data": ["other"]}, {}
        )["ref"]
    bundle = archive.append_acceptance_bundle(SCOPE, IDENTITY, bundle_refs)["ref"]
    service = DemoAcceptanceService(
        archive,
        scope=SCOPE,
        identity_hashes=IDENTITY,
        round_id="demo-acceptance-001",
        start_date_bjt="2026-01-01",
        bundle_selector=lambda **kw: {"bundle_ref": bundle, "manifest_ref": manifest_ref},
        semantic_validator=validator,
        clock_ms=lambda: NOW,
    )
    return service, archive, source, manifest, bundle


def test_actual_archive_bundle_and_fully_bound_semantics_rebuilds_evaluator_inputs():
    service, archive, _, _, _ = setup(costs=True)
    before = deepcopy(archive.store.rows)
    report = service.evaluate()
    assert report["status"] == "passed"
    assert report["criteria"]["net_profit_usdt"] == "1500"
    assert report["criteria"]["complete_round_trip_count"] == 100
    assert report["window_end_at"] == "2026-01-30T16:00:00Z"
    assert report["acceptance_eligible"] is False
    assert archive.store.rows == before


def test_manifest_raw_sources_must_match_selected_typed_bundle():
    service, _, _, _, _ = setup(change_bundle_source=True)
    report = service.evaluate()
    assert report["status"] == "insufficient_evidence"
    assert "acceptance_manifest_sources_invalid" in report["reasons"]


def test_projection_typed_sources_must_match_selected_bundle_refs():
    service, _, _, _, _ = setup(change_projection_source=True)
    report = service.evaluate()
    assert report["status"] == "insufficient_evidence"
    assert "projection_typed_sources_mismatch" in report["reasons"]


@pytest.mark.parametrize(
    "validator", [None, lambda *a, **kw: True, lambda *a, **kw: {"verified": True}]
)
def test_archive_existence_or_unbound_boolean_does_not_supply_semantic_truth(validator):
    service, _, _, _, _ = setup(validator=validator)
    report = service.evaluate()
    assert report["status"] == "insufficient_evidence"
    assert report["data_quality"] != "complete"
    assert report["cost_completeness"] != "complete"


def test_confirmed_exact_boundary_failure_survives_missing_day_and_costs():
    service, _, _, _, _ = setup(
        change=lambda value: value["closing_account_state"].update(flat=False), omit_day=4
    )
    report = service.evaluate()
    assert report["status"] == "failed"
    assert "closing_account_state_flat_violated" in report["reasons"]


def test_confirmed_loss_uses_private_precision_and_survives_unrelated_gap():
    def change(value):
        value["daily_records"][0]["closing_equity_usdt"] = "949.9999999999999999999999999999"
        value["daily_records"][1]["opening_equity_usdt"] = "949.9999999999999999999999999999"

    service, _, _, _, _ = setup(change=change, omit_day=4)
    with localcontext() as context:
        context.prec = 6
        report = service.evaluate()
    assert report["status"] == "failed"
    assert "daily_loss_exceeded" in report["reasons"]


def test_unverified_failure_cannot_be_promoted_by_archived_confirmed_flag():
    def change(value):
        value["confirmed_hard_risk_failures"] = [{"confirmed": True, "code": "daily_loss_exceeded"}]

    service, _, _, _, _ = setup(validator=None, change=change)
    assert service.evaluate()["status"] == "insufficient_evidence"


def test_missing_calendar_day_is_not_excused_as_halted_day():
    service, _, _, _, _ = setup(omit_day=7)
    report = service.evaluate()
    assert report["status"] == "insufficient_evidence"
    assert "calendar_day_missing" in report["reasons"]
    assert report["criteria"]["missing_dates_bjt"] == ["2026-01-08"]


def test_tampered_chain_and_selector_exception_return_fixed_error_without_secret():
    service, archive, _, _, _ = setup()
    archive.store.rows[0]["sha256"] = "0" * 64
    report = service.evaluate()
    assert report["status"] == "insufficient_evidence"
    assert report["reason"] == "archive_record_unverified"
    service, _, _, _, _ = setup()
    service.bundle_selector = lambda **kw: (_ for _ in ()).throw(
        ValueError("postgresql://synthetic:secret@x/db")
    )
    report = service.evaluate()
    assert report["reason"] == "acceptance_selector_failed"
    assert "secret" not in str(report)


def test_service_has_no_caller_evidence_or_approval_argument():
    service, _, _, _, _ = setup()
    with pytest.raises(TypeError):
        service.evaluate({"confirmed_hard_risk_failures": []})
    with pytest.raises(TypeError):
        service.evaluate(approved=True)


def test_missing_cost_evidence_never_turns_an_unmet_performance_target_into_confirmed_failure():
    service, _, _, _, _ = setup()
    evidence = valid_round()
    evidence['closed_round_trips'] = evidence['closed_round_trips'][:99]
    report = service._report(evidence, ['completion_history_incomplete'])
    assert report['status'] == 'insufficient_evidence'
    assert 'complete_round_trips_below_target' in report['reasons']


def test_verified_hard_risk_failure_remains_failed_when_other_evidence_is_missing():
    service, _, _, _, _ = setup()
    evidence = valid_round()
    evidence['daily_records'][0]['closing_equity_usdt'] = '949.9999999999999999999999999999'
    evidence['daily_records'][1]['opening_equity_usdt'] = '949.9999999999999999999999999999'
    evidence['daily_records'][4]['complete'] = False
    report = service._report(evidence, ['daily_evidence_incomplete'])
    assert report['status'] == 'failed'
    assert 'daily_loss_exceeded' in report['reasons']


@pytest.mark.parametrize("field", ["account_id", "stage_id", "run_id"])
def test_unsafe_frozen_identity_rejected_before_archive_read(field):
    archive = DemoEvidenceStore(MemorySQL(), SCOPE["account_id"], SCOPE["stage_id"])
    with pytest.raises(ValueError):
        DemoAcceptanceService(
            archive,
            scope={**SCOPE, field: "postgresql://synthetic:secret@db/x"},
            identity_hashes=IDENTITY,
            round_id="round",
            start_date_bjt="2026-01-01",
        )
