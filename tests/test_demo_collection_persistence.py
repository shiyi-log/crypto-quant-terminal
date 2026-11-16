"""冻结采集运行的真实提交与重启语义；只写 quant_test 的自有随机 schema。"""

from contextlib import contextmanager
import os
import uuid

import pytest

from data_store import DataStore
from demo_account_collector import CollectionError
from demo_collection_run import DemoCollectionRun, FrozenCollectionBinding
from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_store import DemoStore
from runtime_config import load_environment
from tests.test_demo_account_collector import row
from tests.test_demo_collection_run import EvidenceAdapter

load_environment()

HASH_VALUES = dict(zip(HASHES, (str(i) * 64 for i in range(1, 6))))
MANIFEST_SCHEMA = "demo-collection-manifest-v1"


@pytest.fixture(scope="module")
def data():
    """建库守卫在任何 DDL 前执行；结束时仅删除本 fixture 的随机 schema。"""
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
        other = conninfo_to_dict(production)
        keys = ("host", "port", "dbname")
        assert tuple(config.get(k, "") for k in keys) != tuple(other.get(k, "") for k in keys)
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "quant_test"
    schema = "collection_persistence_test_" + uuid.uuid4().hex
    store = DataStore(dsn=dsn, schema=schema, max_connections=2)
    try:
        store.migrate_schema()
        DemoStore(store).ensure_schema()
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            assert conn.execute("SELECT current_database()").fetchone()[0] == "quant_test"
            conn.execute("SET LOCAL lock_timeout TO '5s'")
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def binding(account_id):
    return FrozenCollectionBinding(
        {
            "environment": "binance_demo",
            "account_id": account_id,
            "stage_id": "stage-1000",
            "strategy_version_id": "strategy-v1",
            "run_id": "run-1",
        },
        HASH_VALUES,
    )


def own_archive(data):
    account = "collection-account-" + uuid.uuid4().hex
    return DemoEvidenceStore(data, account, "stage-1000"), binding(account)


@contextmanager
def restarted_archive(data, original):
    """关闭旧连接池，使用新 DataStore 和归档实例读取同一已提交事实。"""
    data.close()
    restarted = DataStore(dsn=data.dsn, schema=data.schema, max_connections=2)
    try:
        yield DemoEvidenceStore(restarted, original.account_id, original.stage_id)
    finally:
        restarted.close()


def committed_rows(archive):
    """独立读 SQL 行，比较重放前后的持久化内容，而非仅比较内存收据。"""
    with archive.store._connection() as conn:
        return conn.execute(
            "SELECT * FROM demo_evidence_archive WHERE environment=%s AND account_id=%s "
            "ORDER BY account_sequence",
            (archive.environment, archive.account_id),
        ).fetchall()


class NoNetwork:
    def __init__(self):
        self.attempts = []

    def __getattr__(self, name):
        self.attempts.append(name)
        raise AssertionError("持久重放不允许再次读取交易所")


def run_for(adapter, archive, frozen, collection_id):
    return DemoCollectionRun(
        adapter, archive, frozen, collection_id=collection_id, clock_ms=lambda: 60_000_000
    )


def test_capture_survives_new_connection_pool_with_exact_sources_and_no_new_writes(data):
    """若重启重新采集、丢失运行绑定或提交回执，本测试会失败。"""
    archive, frozen = own_archive(data)
    result = run_for(EvidenceAdapter(), archive, frozen, "capture-durable").capture_account()
    before = committed_rows(archive)
    assert len(before) == 16
    assert result["scope"] == frozen.scope
    assert result["capture_complete"] is True
    assert result["transport_evidence_complete"] is True
    assert result["as_of_ms"] is None
    assert result["reconciled"] is False
    assert result["boundary_verified"] is False
    assert result["acceptance_eligible"] is False
    assert all(item["body"]["scope"] == frozen.scope for item in before)
    assert all(item["body"]["payload"]["identity_hashes"] == HASH_VALUES for item in before)

    with restarted_archive(data, archive) as reopened:
        facts = reopened.records()
        end = reopened.read(result["manifest_ref"])["payload"]
        raw = [item for item in facts if "collection_component_index" in item["payload"]]
        assert [item["payload"]["capture_component"] for item in raw] == [
            "clock_before", "account_snapshot", "positions", "mode",
            "normal_orders", "conditional_orders", "clock_after",
        ]
        receipt_fields = ("ref", "sha256", "record_sha256", "sequence", "committed", "kind", "scope")
        assert end["components"] == [{key: item[key] for key in receipt_fields} for item in raw]
        assert end["start_record_sha256"] == facts[0]["record_sha256"]
        positions = next(item for item in raw if item["kind"] == "positions")
        account = next(item for item in raw if item["payload"]["capture_component"] == "account_snapshot")
        assert positions["request_evidence"] == {}
        assert positions["payload"]["data"]["source_ref"] == account["ref"]
        assert positions["payload"]["data"]["source_record_sha256"] == account["record_sha256"]
        forbidden = NoNetwork()
        assert run_for(forbidden, reopened, frozen, "capture-durable").capture_account() == result
        assert forbidden.attempts == []
        assert committed_rows(reopened) == before


def test_income_parent_and_short_leaves_survive_restart_without_recount_or_network(data):
    """满页父窗口留存但不重复计入净流；重放只能使用原始五份请求。"""
    archive, frozen = own_archive(data)
    adapter = EvidenceAdapter([row(1, 1, "0.1234567890123456789"), row(2, 2, "2"), row(3, 4, "3")])
    result = run_for(adapter, archive, frozen, "income-durable").collect_income(
        since_ms=1, until_ms=4, limit=2
    )
    assert result["complete"] is True
    assert result["settlement_finality_verified"] is False
    assert [item["tranId"] for item in result["rows"]] == [1, 2, 3]
    assert result["rows"][0]["income"] == "0.1234567890123456789"
    assert [(item["start_ms"], item["end_ms"]) for item in result["income_scans"]] == [
        (1, 1), (2, 2), (3, 4)
    ]
    before = committed_rows(archive)
    assert len(before) == 12
    with restarted_archive(data, archive) as reopened:
        sources = [item for item in reopened.records() if item["kind"] == "cashflows"]
        assert [(item["payload"]["start_ms"], item["payload"]["end_ms"],
                 item["payload"]["window_saturated"]) for item in sources] == [
            (1, 4, True), (1, 2, True), (1, 1, False), (2, 2, False), (3, 4, False)
        ]
        forbidden = NoNetwork()
        replay = run_for(forbidden, reopened, frozen, "income-durable").collect_income(
            since_ms=1, until_ms=4, limit=2
        )
        assert replay == result
        assert forbidden.attempts == []
        assert committed_rows(reopened) == before


@contextmanager
def reject_component_commit(data, collection_id):
    """只在自有 schema 增加临时延迟约束；原文提交后清单事务在 COMMIT 才失败。"""
    from psycopg import sql

    function = "test_collection_reject_component_commit"
    trigger = "test_collection_component_commit_failure"
    with data._connection() as conn:
        conn.execute(
            sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected collection commit failure' "
                    "USING ERRCODE='P0001'; END; $$").format(sql.Identifier(function))
        )
        conn.execute(
            sql.SQL("CREATE CONSTRAINT TRIGGER {} AFTER INSERT ON demo_evidence_archive "
                    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                    "WHEN (NEW.body->'payload'->>'schema'={} "
                    "AND NEW.body->'payload'->>'event'='component' "
                    "AND NEW.body->'payload'->>'collection_id'={}) "
                    "EXECUTE FUNCTION {}()").format(
                sql.Identifier(trigger), sql.Literal(MANIFEST_SCHEMA),
                sql.Literal(collection_id), sql.Identifier(function),
            )
        )
    try:
        yield
    finally:
        with data._connection() as conn:
            conn.execute(sql.SQL("DROP TRIGGER {} ON demo_evidence_archive").format(sql.Identifier(trigger)))
            conn.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(function)))


def test_component_commit_failure_keeps_raw_and_restart_never_reissues_request(data):
    """真实提交失败必须回滚清单项，不能回滚前一事务原文或重启后重新请求。"""
    archive, frozen = own_archive(data)
    collection_id = "capture-commit-failure"
    with reject_component_commit(data, collection_id):
        with pytest.raises(CollectionError, match="^collection_manifest_commit_failed$"):
            run_for(EvidenceAdapter(), archive, frozen, collection_id).capture_account()
        stored = committed_rows(archive)
        assert len(stored) == 2
        assert [item["account_sequence"] for item in stored] == [1, 2]
        assert [item["body"]["payload"].get("event") for item in stored] == ["start", None]
        assert stored[1]["kind"] == "clock"
        assert stored[1]["body"]["payload"]["capture_component"] == "clock_before"
        assert stored[1]["body"]["payload"]["collection_id"] == collection_id
    with restarted_archive(data, archive) as reopened:
        assert len(reopened.records()) == 2
        forbidden = NoNetwork()
        with pytest.raises(CollectionError, match="^collection_incomplete_requires_new_id$"):
            run_for(forbidden, reopened, frozen, collection_id).capture_account()
        assert forbidden.attempts == []
        assert committed_rows(reopened) == stored


def test_archive_immutability_survives_restart_and_all_failed_mutations_roll_back(data):
    """持久清单不能通过改删或联合截断清除；每次拒绝都必须来自不可变守卫。"""
    import psycopg

    archive, frozen = own_archive(data)
    run_for(EvidenceAdapter(), archive, frozen, "capture-immutable").capture_account()
    before = committed_rows(archive)
    with restarted_archive(data, archive) as reopened:
        with reopened.store._connection() as conn:
            enabled = conn.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgrelid='demo_evidence_archive'::regclass "
                "AND tgname='demo_evidence_archive_immutable'"
            ).fetchone()
            assert enabled["tgenabled"] == "A"
        for query in (
            "UPDATE demo_evidence_archive SET body=body WHERE account_id=%s",
            "DELETE FROM demo_evidence_archive WHERE account_id=%s",
            "TRUNCATE demo_evidence_archive, demo_completion_records",
        ):
            with pytest.raises(psycopg.Error) as caught:
                with reopened.store._connection() as conn:
                    conn.execute(query, (archive.account_id,) if "%s" in query else None)
            assert caught.value.sqlstate == "55000"
        assert committed_rows(reopened) == before


@pytest.mark.parametrize("change", ["run", "hash"])
def test_same_collection_id_cannot_change_frozen_run_or_hash_after_restart(data, change):
    """持久 ID 冲突须在网络前拒绝，不能借重启或新绑定创建第二份采集。"""
    archive, frozen = own_archive(data)
    run_for(EvidenceAdapter(), archive, frozen, "capture-binding").capture_account()
    before = committed_rows(archive)
    scope, hashes = frozen.scope, frozen.identity_hashes
    if change == "run":
        scope["run_id"] = "run-2"
    else:
        hashes["code_hash"] = "f" * 64
    changed = FrozenCollectionBinding(scope, hashes)
    with restarted_archive(data, archive) as reopened:
        forbidden = NoNetwork()
        with pytest.raises(CollectionError, match="^collection_identity_conflict$"):
            run_for(forbidden, reopened, changed, "capture-binding").capture_account()
        assert forbidden.attempts == []
        assert committed_rows(reopened) == before
