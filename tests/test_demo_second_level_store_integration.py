"""Real PostgreSQL contract test with an entirely offline exchange adapter."""
import os
import unittest
import uuid

from data_store import DataStore
from demo_second_level_runner import (
    SecondLevelConfig, recover_second_level_session, run_second_level_session,
)
from demo_store import DemoStore
from runtime_config import load_environment
from tests.test_demo_second_level_runner import Adapter, Clock, Source

load_environment()


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class SecondLevelStoreIntegrationTests(unittest.TestCase):
    def test_native_rows_and_missing_fill_recovery_never_repost(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        if conninfo_to_dict(dsn).get("dbname") != "quant_test":
            raise RuntimeError("integration test may only write quant_test")
        schema = "second_contract_" + uuid.uuid4().hex
        data = DataStore(dsn=dsn, schema=schema)
        clock = Clock()
        adapter = Adapter(clock)
        config = SecondLevelConfig("contract", max_signals=2,
                                   max_duration_seconds=100, reconciliation_seconds=3)
        ledgers = {arm: DemoStore(data, "second_level_contract_" + arm)
                   for arm in ("maker", "taker")}
        try:
            ledgers["maker"].ensure_schema()
            report = run_second_level_session(
                adapter, ledgers, Source(clock), config=config, clock_ms=clock.now,
                sleep=clock.sleep, monotonic=clock.mono,
            )
            self.assertTrue(report["success"], report["errors"])
            self.assertTrue(report["position_flat"])
            self.assertTrue(report["open_orders_zero"])
            self.assertEqual(report["observed_arms"], ["maker", "taker"])
            self.assertEqual(len(adapter.posts), 4)
            with data._connection() as conn:
                orders = conn.execute("SELECT submission_attempts,raw FROM demo_orders").fetchall()
                fills = conn.execute("SELECT raw,commission,commission_asset,is_maker FROM demo_fills").fetchall()
                comparisons = conn.execute("SELECT payload FROM demo_signal_comparisons").fetchall()
                self.assertEqual(len(orders), 4)
                self.assertTrue(all(row["submission_attempts"] == 1 for row in orders))
                self.assertTrue(all("orderId" in row["raw"] for row in orders))
                self.assertEqual(len(fills), 4)
                self.assertTrue(all(row["commission"] is not None and row["commission_asset"] == "USDT"
                                    and "quoteQty" in row["raw"] for row in fills))
                self.assertEqual(sum(row["is_maker"] is True for row in fills), 1)
                self.assertIn("local_http_started_ms", str(comparisons))
                # A crash may leave terminal/flat orders before their fills
                # were committed. Recovery must GET these facts, never POST.
                conn.execute("DELETE FROM demo_fills WHERE virtual_account_key=%s",
                             ("second_level_contract_taker",))
            recovered = recover_second_level_session(
                adapter, ledgers, config=config, clock_ms=clock.now,
                sleep=clock.sleep, monotonic=clock.mono,
            )
            self.assertTrue(recovered["success"], recovered["errors"])
            self.assertEqual(len(adapter.posts), 4)
            self.assertEqual(len(ledgers["taker"].runner_fills()), 2)
            self.assertTrue(all(cycle["fill_reconciliation_complete"]
                                for cycle in recovered["cycles"]))
        finally:
            data.close()
            with psycopg.connect(dsn) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
