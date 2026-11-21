from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database" / "demo_migrations" / "0008_research_two_stage.sql"
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
