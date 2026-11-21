from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database" / "demo_migrations" / "0008_research_two_stage.sql"
HOLDOUT_MIGRATION = ROOT / "database" / "demo_migrations" / "0009_research_holdout_claims.sql"
TABLES = (
    "refactor_research_inferences_v2",
    "refactor_research_validation_results_v2",
    "refactor_research_selections_v2",
    "refactor_research_candidate_results_v2",
)


def test_research_v2_migration_is_append_only_and_has_replica_guards():
    source = MIGRATION.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "drop table" not in lowered
    assert "drop column" not in lowered
    assert "create function prevent_refactor_research_v2_mutation" in lowered

    for table in TABLES:
        assert re.search(rf"create table\s+{table}\b", lowered)
        trigger = f"{table}_immutable"
        assert re.search(rf"create trigger\s+{trigger}\b", lowered)
        assert re.search(rf"alter table\s+{table}\s+enable always trigger\s+{trigger}\b", lowered)

    assert lowered.count("before update or delete or truncate") >= len(TABLES)


def test_holdout_v2_claim_is_unique_per_account_and_inference_and_append_only():
    source = HOLDOUT_MIGRATION.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "create table refactor_research_holdout_claims_v2" in lowered
    assert re.search(
        r"primary key\s*\(\s*account_id\s*,\s*inference_id\s*\)",
        lowered,
    )
    assert re.search(
        r"unique\s*\(\s*account_id\s*,\s*inference_sha256\s*\)",
        lowered,
    )
    for digest in (
        "family_sha256", "inference_sha256", "selection_sha256",
        "candidate_sha256", "dataset_sha256", "strategy_sha256",
        "validation_plan_sha256", "selection_policy_sha256",
        "holdout_policy_sha256",
    ):
        assert digest in lowered
    assert "before update or delete or truncate" in lowered
    assert "enable always trigger refactor_research_holdout_claims_v2_immutable" in lowered
    assert "execution_ticket" not in lowered
    assert "registration_order_verified" not in lowered
