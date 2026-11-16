"""已登记结构漂移必须保留事实并拒绝隐式修复。"""
import os
import unittest
import uuid

from data_store import DataStore
from runtime_config import load_environment

load_environment()


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class PriceCandidateSchemaMigrationTests(unittest.TestCase):
    def test_missing_provenance_column_blocks_startup_without_rewriting_candidate(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        if conninfo_to_dict(dsn).get("dbname") != "quant_test":
            raise RuntimeError("migration tests may only write quant_test")
        schema = "price_upgrade_" + uuid.uuid4().hex
        store = DataStore(dsn=dsn, schema=schema)
        try:
            store.migrate_schema()
            with store._connection() as conn:
                conn.execute("INSERT INTO orderflow_price_candidates "
                    "(event_id,method_version,data_fingerprint,exchange,market,pair,timeframe,"
                    "timestamp,observed_at,observation) VALUES "
                    "('legacy','legacy-v1','frozen-input','binance','futures',"
                    "'BTC/USDT:USDT','5m',1000,2000,%s)",
                    (store._json({"legacy": True}),))
                conn.execute("ALTER TABLE orderflow_price_candidates DROP COLUMN source_id")
            store.close()
            store = DataStore(dsn=dsn, schema=schema)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "结构检查失败"):
                    store.initialize()
                with self.assertRaisesRegex(RuntimeError, "shape"):
                    store.migrate_schema()
                # 直接只读核对失败前后的事实，不能靠普通连接补回缺列。
                with psycopg.connect(dsn) as conn:
                    conn.execute(sql.SQL("SET search_path TO {}, pg_catalog")
                                 .format(sql.Identifier(schema)))
                    row = conn.execute("SELECT event_id,observation FROM "
                                       "orderflow_price_candidates").fetchone()
                    self.assertEqual(row, ("legacy", {"legacy": True}))
                    column = conn.execute("SELECT 1 FROM information_schema.columns "
                                          "WHERE table_schema=%s AND table_name=%s "
                                          "AND column_name='source_id'",
                                          (schema, "orderflow_price_candidates")).fetchone()
                    self.assertIsNone(column)
        finally:
            store.close()
            with psycopg.connect(dsn) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
