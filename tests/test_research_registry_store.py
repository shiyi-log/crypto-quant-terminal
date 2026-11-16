"""研究族持久登记、失败留痕和留出消费；只写独立 quant_test 随机schema。"""

from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import os
from unittest.mock import patch
import uuid

import pytest

from data_store import DataStore
from runtime_config import load_environment
from tests.test_research_protocol import family_spec, candidate, DAY

load_environment()


def api():
    try:
        return importlib.import_module("research_registry_store")
    except ModuleNotFoundError:
        pytest.fail("durable research registry implementation missing")


@pytest.fixture(scope="module")
def data():
    dsn = os.getenv("QUANT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("independent quant_test required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    config = conninfo_to_dict(dsn)
    assert config.get("dbname") == "quant_test"
    production = os.getenv("QUANT_DATABASE_URL")
    if production:
        live = conninfo_to_dict(production)
        keys = ("host", "port", "dbname")
        assert tuple(config.get(k, "") for k in keys) != tuple(live.get(k, "") for k in keys)
    schema = "research_registry_test_" + uuid.uuid4().hex
    value = DataStore(dsn=dsn, schema=schema, max_connections=6)
    try:
        value.migrate_schema()
        api().ResearchRegistryStore(value, "setup").ensure_schema()
        yield value
    finally:
        value.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def version(version_id, sha256, **changes):
    return {
        "version_id": version_id,
        "sha256": sha256,
        "source_uri": "archive:" + version_id,
        "source_sha256": sha256,
        "captured_ms": 0,
        "quality_ref": "quality:" + version_id,
        "definition": {},
        **changes,
    }


@pytest.fixture
def registry(data):
    value = api().ResearchRegistryStore(data, "research-" + uuid.uuid4().hex)
    spec = family_spec()
    value.register_version(
        "dataset",
        version(
            "dataset-v1",
            "b" * 64,
            source_uri="dataset:1",
            definition={"start_ms": 0, "end_ms": 365 * DAY},
        ),
    )
    value.register_version("strategy", version("strategy-v1", "e" * 64))
    for factor in spec["factor_sets"]:
        value.register_version("factor", version(factor["id"], factor["sha256"]))
    for exit_set in spec["exit_sets"]:
        value.register_version("exit", version(exit_set["id"], exit_set["sha256"]))
    value.register_family(spec, dataset_version_id="dataset-v1", strategy_version_id="strategy-v1")
    return value


def frozen(registry):
    return registry.get_family(family_spec()["family_id"])


def configs(registry):
    values = frozen(registry)["frozen"]["configurations"]
    base = next(row for row in values if row["cost_scenario"] == "base")
    stress = next(
        row
        for row in values
        if row["cost_scenario"] == "stress"
        and all(
            row[key] == base[key]
            for key in ("symbol", "timeframe", "factor_set", "exit_set", "seed")
        )
    )
    return base["config_id"], stress["config_id"]


def claim(registry, **changes):
    base, _ = configs(registry)
    values = {
        "family_id": family_spec()["family_id"],
        "claim_id": "claim-1",
        "base_config_id": base,
        "base_attempt_id": "final-base",
        "stress_attempt_id": "final-stress",
        "producer_id": "worker-1",
        "claimed_ms": 365 * DAY + 1,
        **changes,
    }
    return registry.claim_holdout(**values)


def test_family_uses_real_pure_freeze_and_resolves_all_registered_versions(registry):
    stored = frozen(registry)
    assert stored["frozen"]["configuration_count"] == 12
    assert stored["dataset_version_id"] == "dataset-v1"
    assert stored["strategy_version_id"] == "strategy-v1"
    assert stored["source_authenticity_verified"] is False
    from research_protocol import freeze_experiment_family

    assert stored["family_sha256"] == freeze_experiment_family(family_spec()).sha256
    again = registry.register_family(
        family_spec(), dataset_version_id="dataset-v1", strategy_version_id="strategy-v1"
    )
    assert again == stored
    changed = family_spec()
    changed["seeds"] = [8]
    with pytest.raises(ValueError, match="immutable"):
        registry.register_family(
            changed, dataset_version_id="dataset-v1", strategy_version_id="strategy-v1"
        )


def test_version_exact_retry_conflict_and_missing_family_reference(registry):
    first = registry.register_version("factor", version("new-factor", "f" * 64))
    assert registry.register_version("factor", version("new-factor", "f" * 64)) == first
    with pytest.raises(ValueError, match="immutable"):
        registry.register_version("factor", version("new-factor", "0" * 64))
    spec = family_spec()
    spec["family_id"] = "family-missing"
    spec["factor_sets"][0]["id"] = "not-registered"
    with pytest.raises(ValueError, match="version"):
        registry.register_family(
            spec, dataset_version_id="dataset-v1", strategy_version_id="strategy-v1"
        )
    assert registry.get_family("family-missing") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"source_uri": "postgresql://alice:secret@host/db"},
        {"version_id": "user@secret"},
        {"source_uri": "https://x/a?token=secret"},
        {"captured_ms": True},
        {"definition": {"amount": float("nan")}},
    ],
)
def test_unsafe_or_inexact_version_facts_rejected_before_write(registry, changes):
    value = version("unsafe-factor", "f" * 64)
    value.update(changes)
    with pytest.raises(ValueError):
        registry.register_version("factor", value)


def test_validation_registered_before_execution_and_failure_survives_restart(registry):
    base, _ = configs(registry)
    first = registry.register_attempt(
        family_spec()["family_id"],
        "attempt-1",
        base,
        producer_id="worker-1",
        registered_ms=365 * DAY + 1,
    )
    assert first["status"] == "registered"
    assert (
        registry.register_attempt(
            family_spec()["family_id"],
            "attempt-1",
            base,
            producer_id="worker-1",
            registered_ms=365 * DAY + 1,
        )
        == first
    )
    registry.record_attempt_event(
        "attempt-1", "started", event_id="started-1", observed_ms=365 * DAY + 2
    )
    registry.record_attempt_event(
        "attempt-1",
        "failed",
        event_id="failed-1",
        observed_ms=365 * DAY + 3,
        error_code="WorkerTimeout",
    )
    assert (
        registry.record_attempt_event(
            "attempt-1",
            "failed",
            event_id="failed-1",
            observed_ms=365 * DAY + 3,
            error_code="WorkerTimeout",
        )["event_type"]
        == "failed"
    )
    with pytest.raises(ValueError, match="conflict"):
        registry.record_attempt_event(
            "attempt-1",
            "failed",
            event_id="failed-1",
            observed_ms=365 * DAY + 4,
            error_code="DifferentFailure",
        )
    restart = api().ResearchRegistryStore(registry.store, registry.account_id)
    history = restart.attempts(family_spec()["family_id"])
    assert len(history) == 1 and history[0]["status"] == "failed"
    assert [row["event_type"] for row in history[0]["events"]] == ["started", "failed"]
    with pytest.raises(ValueError, match="terminal"):
        registry.record_attempt_event(
            "attempt-1", "started", event_id="started-again", observed_ms=365 * DAY + 4
        )


def test_changed_attempt_identity_or_early_time_cannot_replace_registration(registry):
    base, stress = configs(registry)
    registry.register_attempt(
        family_spec()["family_id"],
        "attempt-1",
        base,
        producer_id="worker-1",
        registered_ms=365 * DAY,
    )
    with pytest.raises(ValueError, match="immutable"):
        registry.register_attempt(
            family_spec()["family_id"],
            "attempt-1",
            stress,
            producer_id="worker-1",
            registered_ms=365 * DAY,
        )
    with pytest.raises(ValueError, match="available"):
        registry.register_attempt(
            family_spec()["family_id"],
            "early",
            base,
            producer_id="worker-1",
            registered_ms=365 * DAY - 1,
        )


def test_holdout_claim_is_committed_once_and_exact_retry_has_no_execution_ticket(registry):
    first = claim(registry)
    assert first["execution_ticket"] is True
    assert first["committed"] is True
    assert first["state"] == "consumed"
    assert len(registry.attempts(family_spec()["family_id"])) == 2
    again = claim(registry)
    assert again["execution_ticket"] is False
    assert again["claim"] == first["claim"]
    restart = api().ResearchRegistryStore(registry.store, registry.account_id)
    assert claim(restart)["execution_ticket"] is False
    with pytest.raises(ValueError, match="consumed"):
        claim(
            restart,
            claim_id="claim-2",
            base_attempt_id="second-base",
            stress_attempt_id="second-stress",
        )
    with pytest.raises(ValueError, match="consumed"):
        claim(restart, producer_id="different-worker")


def test_two_competing_holdout_claims_produce_one_execution_ticket(registry):
    def take(index):
        try:
            return claim(
                registry,
                claim_id="claim-" + str(index),
                base_attempt_id="base-" + str(index),
                stress_attempt_id="stress-" + str(index),
            )
        except ValueError as exc:
            assert "consumed" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, (1, 2)))
    assert sum(row is not None and row["execution_ticket"] for row in results) == 1
    assert len(registry.attempts(family_spec()["family_id"])) == 2


def test_failure_unknown_and_restart_never_reset_holdout_consumption(registry):
    claim(registry)
    registry.record_holdout_outcome(
        "claim-1",
        "unknown",
        event_id="unknown-1",
        observed_ms=365 * DAY + 2,
        error_code="WorkerDisconnected",
    )
    assert claim(registry)["state"] == "unknown"
    assert claim(registry)["execution_ticket"] is False
    registry.record_holdout_outcome(
        "claim-1",
        "failed",
        event_id="failure-1",
        observed_ms=365 * DAY + 3,
        error_code="StatJobFailed",
    )
    assert claim(registry)["state"] == "failed"
    with pytest.raises(ValueError, match="consumed"):
        claim(registry, claim_id="claim-new")


def test_claim_rollback_cannot_leave_ticket_or_half_registered_attempts(registry):
    with patch.object(
        type(registry), "_insert_attempt", side_effect=RuntimeError("injected rollback")
    ):
        with pytest.raises(RuntimeError):
            claim(registry)
    assert registry.get_holdout(family_spec()["family_id"]) is None
    assert registry.attempts(family_spec()["family_id"]) == []
    assert claim(registry)["execution_ticket"] is True


def add_artifacts(registry):
    ids = []
    for kind in ("base", "stress"):
        registry.record_attempt_event(
            "final-" + kind,
            "completed",
            event_id="final-" + kind + ":done",
            observed_ms=365 * DAY + 2,
        )
        artifact = {
            "artifact_id": kind + "-artifact",
            "attempt_id": "final-" + kind,
            "uri": "artifact:" + kind,
            "sha256": ("a" if kind == "base" else "b") * 64,
            "media_type": "application/json",
            "created_ms": 365 * DAY + 3,
        }
        registry.register_artifact(artifact)
        ids.append(artifact["artifact_id"])
    return ids


def test_final_candidate_requires_claimed_base_stress_artifacts_and_returns_verdict_only(registry):
    receipt = claim(registry)
    add_artifacts(registry)
    from research_protocol import freeze_experiment_family

    facts = candidate(freeze_experiment_family(family_spec()))
    result = registry.record_candidate_result(
        "claim-1",
        facts,
        base_artifact_id="base-artifact",
        stress_artifact_id="stress-artifact",
        observed_ms=365 * DAY + 4,
    )
    assert result["verdict"]["status"] == "research_pass"
    assert result["production_authorized"] is False
    assert result["acceptance_eligible"] is False
    assert result["source_authenticity_verified"] is False
    assert result["claim_id"] == receipt["claim"]["claim_id"]
    assert claim(registry)["state"] == "completed"
    assert (
        registry.record_candidate_result(
            "claim-1",
            facts,
            base_artifact_id="base-artifact",
            stress_artifact_id="stress-artifact",
            observed_ms=365 * DAY + 4,
        )
        == result
    )
    changed = copy.deepcopy(facts)
    changed["base"]["net_profit"] = "11"
    with pytest.raises(ValueError, match="immutable"):
        registry.record_candidate_result(
            "claim-1",
            changed,
            base_artifact_id="base-artifact",
            stress_artifact_id="stress-artifact",
            observed_ms=365 * DAY + 4,
        )


@pytest.mark.parametrize("mutation", ["identity", "config", "stress", "missing-artifact"])
def test_final_result_cannot_borrow_other_experiment_or_unstored_artifact(registry, mutation):
    claim(registry)
    add_artifacts(registry)
    from research_protocol import freeze_experiment_family

    facts = candidate(freeze_experiment_family(family_spec()))
    base_artifact = "base-artifact"
    if mutation == "identity":
        facts["identities"]["cost_hash"] = "0" * 64
    elif mutation == "config":
        facts["config_id"] = "0" * 64
    elif mutation == "stress":
        facts["stress"]["config_id"] = "0" * 64
    else:
        base_artifact = "not-stored"
    with pytest.raises(ValueError, match="identity|artifact|configuration"):
        registry.record_candidate_result(
            "claim-1",
            facts,
            base_artifact_id=base_artifact,
            stress_artifact_id="stress-artifact",
            observed_ms=365 * DAY + 4,
        )
    assert claim(registry)["state"] == "consumed"


def test_all_registry_facts_reject_update_delete_and_truncate(registry):
    import psycopg

    claim(registry)
    for table in (
        "refactor_research_versions",
        "refactor_research_families",
        "refactor_research_attempts",
        "refactor_research_attempt_events",
        "refactor_research_artifacts",
        "refactor_research_holdout_claims",
        "refactor_research_holdout_events",
        "refactor_research_candidate_results",
    ):
        for verb in ("UPDATE", "DELETE", "TRUNCATE"):
            query = (
                f"UPDATE {table} SET account_id=account_id"
                if verb == "UPDATE"
                else f"{verb} {table}"
            )
            if verb == "DELETE":
                query = "DELETE FROM " + table
            with pytest.raises(psycopg.Error), registry.store._connection() as conn:
                conn.execute(query)


def test_attempt_cannot_finish_before_started_and_terminal_is_irreversible(registry):
    base, _ = configs(registry)
    registry.register_attempt(
        family_spec()["family_id"],
        "attempt-1",
        base,
        producer_id="worker-1",
        registered_ms=365 * DAY,
    )
    with pytest.raises(ValueError, match="started"):
        registry.record_attempt_event(
            "attempt-1", "completed", event_id="done-early", observed_ms=365 * DAY + 1
        )
    registry.record_attempt_event(
        "attempt-1", "started", event_id="started", observed_ms=365 * DAY + 1
    )
    registry.record_attempt_event(
        "attempt-1", "completed", event_id="completed", observed_ms=365 * DAY + 2
    )
    with pytest.raises(ValueError, match="terminal"):
        registry.record_attempt_event(
            "attempt-1",
            "failed",
            event_id="late-fail",
            observed_ms=365 * DAY + 3,
            error_code="LateFailure",
        )


def test_candidate_result_cannot_follow_unknown_or_failed_holdout(registry):
    claim(registry)
    add_artifacts(registry)
    registry.record_holdout_outcome(
        "claim-1",
        "unknown",
        event_id="unknown-1",
        observed_ms=365 * DAY + 2,
        error_code="WorkerDisconnected",
    )
    from research_protocol import freeze_experiment_family

    facts = candidate(freeze_experiment_family(family_spec()))
    with pytest.raises(ValueError, match="terminal"):
        registry.record_candidate_result(
            "claim-1",
            facts,
            base_artifact_id="base-artifact",
            stress_artifact_id="stress-artifact",
            observed_ms=365 * DAY + 4,
        )


def test_candidate_result_requires_completed_claim_attempts(registry):
    claim(registry)
    add_artifacts(registry)
    from research_protocol import freeze_experiment_family
    facts = candidate(freeze_experiment_family(family_spec()))
    result=registry.record_candidate_result('claim-1',facts,base_artifact_id='base-artifact',
        stress_artifact_id='stress-artifact',observed_ms=365*DAY+5)
    assert result['verdict']['status']=='research_pass'
    assert [row['status'] for row in registry.attempts(family_spec()['family_id'])]==['completed','completed']


def test_failed_holdout_result_cannot_be_replaced_after_attempt_completion(registry):
    claim(registry)
    add_artifacts(registry)
    registry.record_holdout_outcome(
        "claim-1",
        "failed",
        event_id="failed-claim",
        observed_ms=365 * DAY + 3,
        error_code="WorkerFailed",
    )
    from research_protocol import freeze_experiment_family

    with pytest.raises(ValueError, match="terminal"):
        registry.record_candidate_result(
            "claim-1",
            candidate(freeze_experiment_family(family_spec())),
            base_artifact_id="base-artifact",
            stress_artifact_id="stress-artifact",
            observed_ms=365 * DAY + 4,
        )


def test_failed_claim_transaction_returns_no_ticket_and_leaves_no_records(registry):
    import psycopg

    with registry.store._connection() as conn:
        conn.execute(
            "CREATE FUNCTION test_research_registry_deferred_failure() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected commit failure'; END; $$"
        )
        conn.execute(
            "CREATE CONSTRAINT TRIGGER test_research_registry_deferred_failure "
            "AFTER INSERT ON refactor_research_holdout_claims DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION test_research_registry_deferred_failure()"
        )
    try:
        with pytest.raises(psycopg.Error, match="injected commit failure"):
            claim(registry)
    finally:
        with registry.store._connection() as conn:
            conn.execute(
                "DROP TRIGGER test_research_registry_deferred_failure ON refactor_research_holdout_claims"
            )
            conn.execute("DROP FUNCTION test_research_registry_deferred_failure()")
    assert registry.get_holdout(family_spec()["family_id"]) is None
    assert registry.attempts(family_spec()["family_id"]) == []
    assert claim(registry)["execution_ticket"] is True
