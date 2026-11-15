"""Research facts are immutable even when a caller issues direct PostgreSQL SQL."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest

from data_store import DataStore
import research_ledger as ledger

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "database/schema.sql").read_text(
    encoding="utf-8")


@pytest.fixture
def store():
    """Every test owns only a fresh schema in an explicitly isolated test database."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get("QUANT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires isolated PostgreSQL test DSN")
    test = conninfo_to_dict(dsn)
    production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
    def location(config):
        return tuple(config.get(key, "") for key in ("host", "port", "dbname"))
    name = test.get("dbname", "").lower()
    if not ("test" in name or name == "quant_ci") or (
        production and location(test) == location(production)
    ):
        raise RuntimeError("ledger integration tests require an isolated test database")
    schema = "test_research_ledger_" + uuid.uuid4().hex[:20]
    value = DataStore(dsn=dsn, schema=schema, max_connections=2)
    try:
        value.initialize()
        yield value
    finally:
        value.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def original_fact(store):
    """Keep a hand-written payload and producer source, independent of mutation SQL."""
    event_id = ledger._record_event(store, "research_test", {"net_bp": -4}, "fixture/source")
    with store._connection() as conn:
        row = conn.execute("SELECT * FROM research_ledger_events WHERE event_id=%s",
                           (event_id,)).fetchone()
    return event_id, row


@pytest.mark.parametrize("statement", [
    "UPDATE research_ledger_events SET payload=jsonb_build_object('net_bp',99)",
    "UPDATE research_ledger_events SET event_id='replacement-id'",
    "UPDATE research_ledger_events SET event_type='replacement-type'",
    "UPDATE research_ledger_events SET source_id='replacement-source'",
    "UPDATE research_ledger_events SET sequence_id=sequence_id+100",
    "UPDATE research_ledger_events SET created_at='2020-01-01'",
    "DELETE FROM research_ledger_events",
    "TRUNCATE research_ledger_events",
    "TRUNCATE research_ledger_events RESTART IDENTITY CASCADE",
])
def test_direct_mutation_is_rejected_and_original_evidence_survives(store, statement):
    # Break caught: database accepts a direct mutation that bypasses ledger.py.
    import psycopg

    event_id, before = original_fact(store)
    with pytest.raises(psycopg.Error) as failure:
        with store._connection() as conn:
            conn.execute(statement)
    assert failure.value.sqlstate == "55000"
    with store._connection() as conn:
        after = conn.execute("SELECT * FROM research_ledger_events").fetchall()
    assert after == [before]
    assert after[0]["event_id"] == event_id
    # A rejected attempt must not poison later normal inserts or change old facts.
    new_id = ledger._record_event(store, "research_test", {"net_bp": 2}, "fixture/new")
    assert new_id != event_id


def test_reapplying_schema_keeps_existing_event_ids_sequences_and_protection(store):
    # Break caught: initialization loses protection or rewrites historical sequence IDs.
    import psycopg

    _, before = original_fact(store)
    with store._connection() as conn:
        conn.execute(SCHEMA_SQL)
    with store._connection() as conn:
        assert conn.execute("SELECT * FROM research_ledger_events").fetchall() == [before]
    with pytest.raises(psycopg.Error):
        with store._connection() as conn:
            conn.execute("DELETE FROM research_ledger_events")


def test_forward_retries_remain_idempotent_and_conflicts_preserve_prior_facts(store):
    # Break caught: new triggers block legitimate ON CONFLICT DO NOTHING insertion.
    event = {"run_id": "run-1", "event_type": "snapshot",
             "event_id": "producer-stable-7", "equity": 10000}
    first = ledger.append_forward_paper_events(store, "run-1", [event])
    assert ledger.append_forward_paper_events(store, "run-1", [event]) == first
    with pytest.raises(ValueError, match="immutable"):
        ledger.append_forward_paper_events(store, "run-1", [{**event, "equity": 99999}])
    with store._connection() as conn:
        rows = conn.execute("SELECT event_id,event_type,payload,source_id"
                            " FROM research_ledger_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_id"] == first[0]
    assert rows[0]["event_type"] == "forward_paper_snapshot"
    assert rows[0]["source_id"] == "forward-paper/run-1"
    assert rows[0]["payload"] == event


def test_legacy_sequence_constraints_are_added_without_renumbering_facts(store):
    # Break caught: an older ADD COLUMN schema accepts ambiguous or missing cursors.
    import psycopg

    with store._connection() as conn:
        conn.execute("DROP TABLE research_ledger_events")
        conn.execute("""CREATE TABLE research_ledger_events (
            sequence_id BIGSERIAL, event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
            payload JSONB NOT NULL, source_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("ALTER TABLE research_ledger_events ALTER COLUMN sequence_id DROP NOT NULL")
        conn.execute("""INSERT INTO research_ledger_events
            (sequence_id,event_id,event_type,payload) VALUES(7,'legacy-fact','fixture','{}')""")
        conn.execute("SELECT setval(pg_get_serial_sequence('research_ledger_events','sequence_id'),7)")
        conn.execute(SCHEMA_SQL)
    with store._connection() as conn:
        row = conn.execute("SELECT sequence_id,event_id FROM research_ledger_events").fetchone()
    assert row == {"sequence_id": 7, "event_id": "legacy-fact"}
    for sequence, exception in [(7, psycopg.errors.UniqueViolation),
                                (None, psycopg.errors.NotNullViolation)]:
        with pytest.raises(exception):
            with store._connection() as conn:
                conn.execute("""INSERT INTO research_ledger_events
                    (sequence_id,event_id,event_type,payload) VALUES(%s,'new-fact','fixture','{}')""",
                             (sequence,))
    added_id = ledger._record_event(store, "fixture", {"next": True})
    with store._connection() as conn:
        added = conn.execute("SELECT sequence_id FROM research_ledger_events WHERE event_id=%s",
                             (added_id,)).fetchone()
    assert added["sequence_id"] == 8
