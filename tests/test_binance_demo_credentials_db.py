"""Real encrypted DB/API integration; writes are restricted to quant_test."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from binance_demo_credentials import DemoCredentialStore
from data_store import DataStore
from demo_api import DemoApi, DemoApiError
from demo_store import DemoStore
from runtime_config import load_environment

load_environment()


@unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "isolated quant_test required")
class DemoCredentialsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        config = conninfo_to_dict(dsn)
        if config.get("dbname") != "quant_test":
            raise RuntimeError("Demo credential tests only write independent quant_test")
        production = os.environ.get("QUANT_DATABASE_URL")
        if production:
            live = conninfo_to_dict(production)
            if tuple(config.get(k, "") for k in ("host", "port", "dbname")) == \
                    tuple(live.get(k, "") for k in ("host", "port", "dbname")):
                raise RuntimeError("test DB must differ from production")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.schema = "demo_creds_test_" + uuid.uuid4().hex
        cls.data = DataStore(dsn=dsn, schema=cls.schema)
        cls.ledger = DemoStore(cls.data)
        cls.ledger.ensure_schema()
        cls.credentials = DemoCredentialStore(cls.data,
            master_key_path=Path(cls.temporary.name) / "demo_master.key")
        cls.credentials.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.data.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.temporary.cleanup()

    def setUp(self):
        with self.data._connection() as conn:
            conn.execute("TRUNCATE binance_demo_credentials,demo_orders,demo_fills,demo_account,demo_positions,demo_signal_comparisons")
        controller = Mock()
        controller.status.return_value = {"running": False, "entriesEnabled": False,
                                          "status": "not_started"}
        self.api = DemoApi(self.data, self.credentials, self.ledger, controller,
                           account_provider=lambda: [])

    def test_real_database_has_ciphertext_and_no_api_key_or_secret_plaintext(self):
        key, secret = "synthetic-demo-key-" + "A" * 48, "synthetic-demo-secret-" + "B" * 48
        status = self.credentials.save_credentials(key, secret, actor="test")
        with self.data._connection() as conn:
            row = conn.execute("SELECT * FROM binance_demo_credentials").fetchone()
        self.assertNotIn(key, json.dumps(row, default=str))
        self.assertNotIn(secret, json.dumps(row, default=str))
        self.assertTrue(status["decryptionAvailable"])
        self.assertEqual(self.credentials.load_credentials().api_key, key)
        self.credentials.ensure_schema()
        self.assertEqual(self.credentials.load_credentials().secret, secret)

    def test_real_store_api_orders_fills_and_snapshots_preserve_binance_fields(self):
        self.ledger.reserve_order({"strategy_id": "frozen", "signal_id": "closed-900000",
            "intent_key": "entry", "client_id": "qd-" + "1" * 32, "rule_hash": "rule",
            "execution_hash": "exec", "input_fingerprint": "input", "pair": "BTC/USDT:USDT",
            "side": "buy", "type": "limit", "quantity": "0.001", "price": "60000",
            "time_in_force": "GTX", "signal_ms": 900000})
        client = "qd-" + "1" * 32
        self.ledger.record_order_observation(client, {"raw": {"symbol": "BTCUSDT", "orderId": 123,
            "clientOrderId": client, "side": "BUY", "type": "LIMIT", "status": "FILLED",
            "origQty": "0.00100000", "executedQty": "0.00100000", "avgPrice": "60000.1000",
            "price": "60000.1000", "time": 900001, "updateTime": 900002}})
        self.ledger.record_fill(client, {"exchange_order_id": "123", "exchange_trade_id": "456",
            "price": "60000.1", "quantity": "0.001", "commission": "0.01200002",
            "commission_asset": "USDT", "is_maker": True, "timestamp_ms": 900002,
            "exchange_updated_ms": 900002, "raw": {"symbol": "BTCUSDT", "id": 456,
                "orderId": 123, "price": "60000.1000", "qty": "0.00100000", "buyer": True,
                "maker": True, "commission": "0.01200002", "commissionAsset": "USDT", "time": 900002}})
        orders = self.api.get("/orders")["items"]
        fills = self.api.get("/fills")["items"]
        self.assertEqual(orders[0]["origQty"], "0.00100000")
        self.assertEqual(orders[0]["time"], 900001)
        self.assertEqual(fills[0]["commissionAsset"], "USDT")
        self.assertEqual(fills[0]["commission"], "0.012000020000000000")
        balance = {"raw": {"assets": [{"asset": "USDT", "walletBalance": "100.00000000"}]}}
        positions = [{"raw": {"symbol": "BTCUSDT", "positionAmt": "0.00100000"}}]
        self.ledger.append_account_snapshot({"balance": balance}, observed_ms=1000000)
        self.ledger.append_position_snapshot({"positions": positions}, observed_ms=1000000)
        self.assertEqual(self.api.get("/account")["items"][0]["walletBalance"], "100.00000000")
        self.assertEqual(self.api.get("/positions")["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(self.api.get("/positions")["items"][0]["positionAmt"], "0.00100000")
        self.assertTrue(self.api.get("")["runner"]["accountLocked"])

    def test_real_restart_projection_locks_credentials_while_unknown_submission_exists(self):
        self.credentials.save_credentials("synthetic-demo-key-" + "A" * 48,
                                          "synthetic-demo-secret-" + "B" * 48, actor="test")
        self.ledger.reserve_order({"strategy_id": "frozen", "signal_id": "closed-900000",
            "intent_key": "entry", "client_id": "qd-" + "1" * 32, "rule_hash": "rule",
            "execution_hash": "exec", "input_fingerprint": "input", "pair": "BTC/USDT:USDT",
            "side": "buy", "type": "limit", "quantity": "0.001", "price": "60000", "signal_ms": 900000})
        self.ledger.claim_submission("qd-" + "1" * 32, 900002)
        self.ledger.mark_submission_unknown("qd-" + "1" * 32, "timeout", 900003)
        with self.assertRaisesRegex(DemoApiError, "pause_and_reconcile"):
            self.api.post("/credentials", {"apiKey": "synthetic-new-demo-key-" + "C" * 48,
                "secret": "synthetic-new-demo-secret-" + "D" * 48}, actor="test")


if __name__ == "__main__":
    unittest.main()
