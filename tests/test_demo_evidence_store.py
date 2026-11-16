"""账户证据原文、账户哈希链和成本完成身份只在隔离 quant_test 中验证。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import importlib
import importlib.util
import os
import threading
from unittest.mock import Mock
import uuid

import pytest

from data_store import DataStore
from demo_store import DemoStore
from runtime_config import load_environment
from tests.test_demo_round_trip_costs import evidence

load_environment()


def cls():
    assert importlib.util.find_spec("demo_evidence_store"), "immutable evidence archive missing"
    return importlib.import_module("demo_evidence_store").DemoEvidenceStore


def scope(account="account-a", stage="stage-1000"):
    return {"environment": "binance_demo", "account_id": account, "stage_id": stage}


def request(**changes):
    return {
        "path": "income",
        "api": "fapiPrivate",
        "method": "GET",
        "status": "ok",
        "request_params": {"startTime": 1, "endTime": 1000, "limit": 1000},
        "response_sha256": "a" * 64,
        "response_hash_basis": "raw_http_response_bytes",
        "started_at_utc": "2026-11-16T06:00:00Z",
        "received_at_utc": "2026-11-16T06:00:01Z",
        "latency_ms": 1.25,
        **changes,
    }


@pytest.fixture(scope="module")
def data():
    dsn = os.getenv("QUANT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("isolated quant_test required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    config = conninfo_to_dict(dsn)
    assert config.get("dbname") == "quant_test"
    prod = os.getenv("QUANT_DATABASE_URL")
    if prod:
        other = conninfo_to_dict(prod)
        keys = ("host", "port", "dbname")
        assert tuple(config.get(k, "") for k in keys) != tuple(other.get(k, "") for k in keys)
    schema = "evidence_test_" + uuid.uuid4().hex
    store = DataStore(dsn=dsn, schema=schema, max_connections=6)
    try:
        store.migrate_schema()
        DemoStore(store).ensure_schema()
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def archive(data):
    return cls()(data, "account-" + uuid.uuid4().hex, "stage-1000")


def own(archive):
    return scope(archive.account_id, archive.stage_id)


def test_invalid_identity_never_opens_connection():
    store = Mock()
    with pytest.raises(ValueError):
        cls()(store, "account", "stage", environment="live")
    store._connection.assert_not_called()


@pytest.mark.parametrize(
    "kind,payload,metadata",
    [
        ("unknown", {}, {}),
        ("cashflows", {"income": 0.1}, {}),
        ("cashflows", {"income": Decimal("NaN")}, {}),
        ("cashflows", {}, request(method="POST")),
        ("cashflows", {}, request(path="leverage")),
        ("cashflows", {}, request(api="fapiPrivateV2")),
        ("cashflows", {}, request(response_sha256="bad")),
        ("cashflows", {}, request(response_hash_basis="verified_truth")),
        ("cashflows", {}, request(request_params={"signature": "secret"})),
        ("cashflows", {}, request(authorization="Bearer secret")),
    ],
)
def test_invalid_payload_or_transport_evidence_never_connects(kind, payload, metadata):
    data = Mock()
    store = cls()(data, "a", "s")
    with pytest.raises(ValueError):
        store.append(kind, scope("a", "s"), payload, metadata)
    data._connection.assert_not_called()


def test_append_preserves_exact_amounts_scrubs_secrets_and_commits_before_receipt(archive):
    payload = {
        "income": Decimal("0.12345678901234567890123456789012345"),
        "secret": "never-store",
        "nested": {"apiKey": "bad", "quantity": "0.0020"},
        "notice": "https://x.invalid/path?signature=secret",
    }
    receipt = archive.append("cashflows", own(archive), payload, request(), stable_key="capture-1")
    assert receipt["committed"] is True
    assert receipt["sequence"] == 1
    stored = archive.read(receipt["ref"])
    assert stored["payload"]["income"] == "0.12345678901234567890123456789012345"
    assert stored["payload"]["nested"] == {"quantity": "0.0020"}
    assert "never-store" not in str(stored) and "signature=secret" not in str(stored)
    assert stored["request_evidence"]["latency_ms"] == "1.25"
    assert stored["sha256"] == receipt["sha256"]
    stored["payload"]["income"] = "changed"
    assert archive.read(receipt["ref"])["payload"]["income"].startswith("0.123456")


def test_idempotent_capture_does_not_append_and_changed_facts_conflict(archive):
    first = archive.append("positions", own(archive), {"items": []}, {}, stable_key="position-1")
    assert (
        archive.append("positions", own(archive), {"items": []}, {}, stable_key="position-1")
        == first
    )
    with pytest.raises(ValueError, match="immutable"):
        archive.append(
            "positions", own(archive), {"items": [{"quantity": "1"}]}, {}, stable_key="position-1"
        )
    assert len(archive.records()) == 1


def test_account_chain_is_serialized_across_stages_and_instances(data):
    account = "account-" + uuid.uuid4().hex
    one, two = cls()(data, account, "stage-1"), cls()(data, account, "stage-2")

    def write(item):
        i, store = item
        return store.append(
            "clock", own(store), {"server_time": i}, {}, stable_key="clock-" + str(i)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(write, enumerate((one, two))))
    assert sorted(row["sequence"] for row in receipts) == [1, 2]
    rows = one.records(all_stages=True)
    assert rows[0]["previous_hash"] == "0" * 64
    assert rows[1]["previous_hash"] == rows[0]["record_sha256"]
    with pytest.raises(ValueError, match="scope"):
        one.read(receipts[1]["ref"])


def test_same_capture_key_concurrent_retry_commits_once(archive):
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(
            pool.map(
                lambda _: archive.append(
                    "positions", own(archive), {"items": []}, {}, stable_key="one"
                ),
                range(3),
            )
        )
    assert receipts[0] == receipts[1] == receipts[2]
    assert len(archive.records()) == 1


def test_scope_mismatch_and_foreign_ref_are_rejected(archive, data):
    with pytest.raises(ValueError, match="scope"):
        archive.append("fills", scope("other"), {}, {})
    other = cls()(data, "other-" + uuid.uuid4().hex, archive.stage_id)
    ref = other.append("fills", own(other), {}, {})["ref"]
    with pytest.raises(ValueError, match="scope"):
        archive.read(ref)


def test_archive_and_completion_history_cannot_update_delete_or_truncate(archive):
    import psycopg

    archive.append("positions", own(archive), {"items": []}, {})
    for table in ("demo_evidence_archive", "demo_completion_records"):
        for verb in ("UPDATE", "DELETE", "TRUNCATE"):
            query = (
                f"UPDATE {table} SET account_id=account_id"
                if verb == "UPDATE"
                else f"DELETE FROM {table}"
                if verb == "DELETE"
                else f"TRUNCATE {table}"
            )
            with pytest.raises(psycopg.Error), archive.store._connection() as conn:
                conn.execute(query)


def test_read_detects_hash_or_chain_damage_even_if_database_owner_disables_trigger(archive):
    first = archive.append("clock", own(archive), {"server_time": 1}, {})
    archive.append("clock", own(archive), {"server_time": 2}, {})
    with archive.store._connection() as conn:
        conn.execute(
            "ALTER TABLE demo_evidence_archive DISABLE TRIGGER demo_evidence_archive_immutable"
        )
        conn.execute(
            "UPDATE demo_evidence_archive SET previous_hash=%s WHERE account_id=%s AND account_sequence=2",
            ("f" * 64, archive.account_id),
        )
        conn.execute(
            "ALTER TABLE demo_evidence_archive ENABLE ALWAYS TRIGGER demo_evidence_archive_immutable"
        )
    with pytest.raises(ValueError, match="integrity"):
        archive.read(first["ref"])


def cost_bundle(archive, **changes):
    from demo_round_trip_costs import reconcile_round_trip_costs

    source = evidence()
    source["account_id"] = archive.account_id
    source["stage_id"] = archive.stage_id
    for key in ("opening", "coverage"):
        source[key]["account_id"] = archive.account_id
        source[key]["stage_id"] = archive.stage_id
    for key in ("fills", "orders", "income_rows"):
        for row in source[key]:
            row["account_id"] = archive.account_id
            row["stage_id"] = archive.stage_id
    source.update(changes)
    receipt = archive.append("equity_reconciliation", own(archive), {"cost_evidence": source}, {})
    trip = reconcile_round_trip_costs(source).to_dict()["trips"][0]
    return receipt, trip, source


def test_completion_recomputed_from_archived_facts_emits_once(archive):
    receipt, trip, _ = cost_bundle(archive)
    first = archive.append_completion(own(archive), trip, [receipt["ref"]])
    assert first["new_event"] is True
    assert first["correction_required"] is False
    assert first["risk_event"]["net_pnl_usdt"] == "9.3"
    repeat = archive.append_completion(own(archive), trip, [receipt["ref"]])
    assert repeat["new_event"] is False and repeat["risk_event"] is None
    assert archive.previous_completions() == {trip["trade_id"]: trip["proof_sha256"]}
    assert len(archive.completion_records()) == 1


def test_cost_correction_is_a_new_audit_record_and_never_second_loss_event(archive):
    receipt, trip, source = cost_bundle(archive)
    archive.append_completion(own(archive), trip, [receipt["ref"]])
    source["fills"][0]["commission"] = "0.2"
    source["income_rows"][0]["amount"] = "-0.2"
    new_receipt = archive.append(
        "equity_reconciliation", own(archive), {"cost_evidence": source}, {}
    )
    from demo_round_trip_costs import reconcile_round_trip_costs

    revised = reconcile_round_trip_costs(source).to_dict()["trips"][0]
    correction = archive.append_completion(own(archive), revised, [new_receipt["ref"]])
    assert correction["correction_required"] is True
    assert correction["new_event"] is False and correction["risk_event"] is None
    assert archive.previous_completions() == {trip["trade_id"]: trip["proof_sha256"]}
    assert len(archive.completion_records()) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"complete": False},
        {"net_pnl_usdt": "10000"},
        {"proof_sha256": "f" * 64},
        {"account_id": "other"},
        {"risk_event": {"event_id": "fake"}},
    ],
)
def test_caller_cannot_forge_complete_cost_proof(archive, change):
    receipt, trip, _ = cost_bundle(archive)
    trip.update(change)
    with pytest.raises(ValueError):
        archive.append_completion(own(archive), trip, [receipt["ref"]])
    assert archive.completion_records() == []


def test_completion_requires_typed_immutable_reconciliation_source(archive):
    receipt = archive.append("positions", own(archive), {"items": []}, {})
    with pytest.raises(ValueError):
        archive.append_completion(own(archive), {"trade_id": "t"}, [receipt["ref"]])


def test_acceptance_bundle_requires_all_typed_same_scope_sources_and_hashes(archive):
    hashes = {
        name: str(i) * 64
        for i, name in enumerate(
            ("code_hash", "strategy_hash", "execution_hash", "risk_policy_hash", "cost_model_hash"),
            1,
        )
    }
    refs = {}
    binding = {**own(archive), "strategy_version_id": "strategy-v1", "run_id": "run-1"}
    for kind in ("account_snapshot", "normal_orders", "conditional_orders", "fills", "cashflows"):
        refs[kind] = archive.append(kind, binding, {"identity_hashes": hashes, "items": []}, {})[
            "ref"
        ]
    bundle = archive.append_acceptance_bundle(binding, hashes, refs, stable_key="acceptance-1")
    body = archive.read(bundle["ref"])["payload"]
    assert body["status"] == "evidence_bundle_recorded" and body["acceptance_eligible"] is False
    assert body["source_refs"] == refs
    missing = dict(refs)
    missing.pop("cashflows")
    with pytest.raises(ValueError):
        archive.append_acceptance_bundle(binding, hashes, missing)
    swapped = dict(refs)
    swapped["cashflows"] = refs["fills"]
    with pytest.raises(ValueError):
        archive.append_acceptance_bundle(binding, hashes, swapped)
    drift = dict(hashes)
    drift["cost_model_hash"] = "f" * 64
    with pytest.raises(ValueError):
        archive.append_acceptance_bundle(binding, drift, refs)
    with pytest.raises(ValueError, match="scope"):
        archive.append_acceptance_bundle(own(archive), hashes, refs)


def test_request_evidence_kind_and_path_must_match_before_connection():
    data = Mock()
    store = cls()(data, "a", "s")
    with pytest.raises(ValueError, match="kind"):
        store.append("positions", scope("a", "s"), [], request())
    data._connection.assert_not_called()


def test_commit_failure_cannot_return_successful_receipt():
    data = Mock()

    @contextmanager
    def failed_commit():
        yield Mock()
        raise RuntimeError("synthetic commit failed")

    data._connection.side_effect = failed_commit
    store = cls()(data, "a", "s")
    store._chain = Mock(return_value=[])
    store._append = Mock(return_value=({}, True))
    store._receipt = Mock()
    with pytest.raises(RuntimeError, match="commit failed"):
        store.append("positions", scope("a", "s"), [], {})
    store._receipt.assert_not_called()


def test_real_previous_aware_retry_and_correction_outputs_remain_compatible(archive):
    from demo_round_trip_costs import reconcile_round_trip_costs

    receipt, trip, source = cost_bundle(archive)
    archive.append_completion(own(archive), trip, [receipt["ref"]])
    previous = archive.previous_completions()
    retry = reconcile_round_trip_costs(source, previous_completions=previous).to_dict()["trips"][0]
    assert retry["already_recorded"] is True and retry["risk_event"] is None
    outcome = archive.append_completion(own(archive), retry, [receipt["ref"]])
    assert outcome["new_event"] is False
    source["fills"][0]["commission"] = "0.2"
    source["income_rows"][0]["amount"] = "-0.2"
    changed = archive.append("equity_reconciliation", own(archive), {"cost_evidence": source}, {})
    revised = reconcile_round_trip_costs(source, previous_completions=previous).to_dict()["trips"][
        0
    ]
    assert revised["complete"] is False and revised["risk_event"] is None
    first = archive.append_completion(own(archive), revised, [changed["ref"]])
    retry = archive.append_completion(own(archive), revised, [changed["ref"]])
    assert first["ref"] == retry["ref"]
    assert first["correction_required"] is True and retry["new_event"] is False
    assert len(archive.completion_records()) == 2


def test_acceptance_rejects_cross_run_even_with_identical_hashes(archive):
    hashes = {
        name: str(i) * 64
        for i, name in enumerate(
            ("code_hash", "strategy_hash", "execution_hash", "risk_policy_hash", "cost_model_hash"),
            1,
        )
    }
    target = {**own(archive), "run_id": "run-1", "strategy_version_id": "v1"}
    refs = {}
    for kind in ("account_snapshot", "normal_orders", "conditional_orders", "fills", "cashflows"):
        source = dict(target)
        if kind == "cashflows":
            source["run_id"] = "run-2"
        refs[kind] = archive.append(kind, source, {"identity_hashes": hashes}, {})["ref"]
    with pytest.raises(ValueError, match="scope"):
        archive.append_acceptance_bundle(target, hashes, refs)


def test_typed_records_cannot_bypass_completion_validation():
    data = Mock()
    store = cls()(data, "a", "s")
    for kind in ("completion", "acceptance"):
        with pytest.raises(ValueError):
            store.append(kind, scope("a", "s"), {}, {})
    data._connection.assert_not_called()


@pytest.mark.parametrize(
    "secret",
    [
        "postgresql://alice:synthetic-secret@invalid/db",
        "https://user:synthetic-secret@invalid/path",
    ],
)
def test_connection_strings_in_keys_or_identity_are_rejected_before_connection(secret):
    data = Mock()
    with pytest.raises(ValueError):
        cls()(data, secret, "stage")
    store = cls()(data, "a", "s")
    with pytest.raises(ValueError):
        store.append("account_snapshot", scope("a", "s"), {secret: "opaque"}, {})
    with pytest.raises(ValueError):
        store.append("positions", scope("a", "s"), [], {}, stable_key=secret)
    data._connection.assert_not_called()


def test_acceptance_requires_full_run_binding_before_connection():
    data = Mock()
    store = cls()(data, "a", "s")
    hashes = {
        name: "a" * 64
        for name in (
            "code_hash",
            "strategy_hash",
            "execution_hash",
            "risk_policy_hash",
            "cost_model_hash",
        )
    }
    refs = {
        kind: "evidence:" + "a" * 64
        for kind in (
            "account_snapshot",
            "normal_orders",
            "conditional_orders",
            "fills",
            "cashflows",
        )
    }
    with pytest.raises(ValueError, match="scope"):
        store.append_acceptance_bundle(scope("a", "s"), hashes, refs)
    data._connection.assert_not_called()


@pytest.mark.parametrize("scheme", ["postgresql+psycopg", "mongodb", "redis"])
def test_custom_scheme_connection_string_values_are_scrubbed(scheme):
    module = importlib.import_module("demo_evidence_store")
    secret = f"{scheme}://alice:synthetic-secret@invalid/db"
    cleaned = module._sanitize({"notice": secret})
    assert "synthetic-secret" not in str(cleaned)
    assert "://" not in cleaned["notice"]


def test_completion_reader_holds_account_lock_across_both_snapshots(archive):
    receipt, trip, _ = cost_bundle(archive)
    original_chain = archive._chain
    writer_started = threading.Event()
    writer_done = threading.Event()
    original = cls()(archive.store, archive.account_id, archive.stage_id)

    def write():
        writer_started.set()
        outcome = original.append_completion(own(archive), trip, [receipt["ref"]])
        writer_done.set()
        return outcome

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def pause_after_archive_read(conn):
            rows = original_chain(conn)
            with archive.store._connection() as contender:
                lock = contender.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS held",
                    (f"demo-evidence:{archive.environment}:{archive.account_id}",),
                ).fetchone()
                assert lock["held"] is False, "completion reader must own account snapshot lock"
            futures.append(pool.submit(write))
            assert writer_started.wait(2)
            assert not writer_done.wait(0.3), "completion writer committed between reader snapshots"
            return rows

        archive._chain = pause_after_archive_read
        try:
            assert archive.previous_completions() == {}
        finally:
            archive._chain = original_chain
        assert futures[0].result(timeout=10)["new_event"] is True
    assert archive.previous_completions() == {trip["trade_id"]: trip["proof_sha256"]}
