"""Verify an older provenance-free table upgrades without losing its rows."""
import os
import unittest
import uuid

from data_store import DataStore
from runtime_config import load_environment

load_environment()


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class PriceCandidateSchemaMigrationTests(unittest.TestCase):
    def test_legacy_table_upgrades_and_preserves_existing_candidate(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        if conninfo_to_dict(dsn).get("dbname") != "quant_test":
            raise RuntimeError("migration tests may only write quant_test")
        schema = "price_upgrade_" + uuid.uuid4().hex
        store = DataStore(dsn=dsn, schema=schema)
        try:
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
                with store._connection() as conn:
                    row = conn.execute("SELECT event_id,observation,source_id FROM "
                                       "orderflow_price_candidates").fetchone()
                    self.assertEqual(row, {"event_id": "legacy", "observation": {"legacy": True},
                                           "source_id": None})
                    conn.execute("UPDATE orderflow_price_candidates SET source_id='collector' "
                                 "WHERE event_id='legacy'")
                    conn.execute("UPDATE orderflow_price_candidates SET source_id=NULL "
                                 "WHERE event_id='legacy'")
                store.close()
                store = DataStore(dsn=dsn, schema=schema)
        finally:
            store.close()
            with psycopg.connect(dsn) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
