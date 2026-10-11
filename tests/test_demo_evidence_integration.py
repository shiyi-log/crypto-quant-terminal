"""Consistent, all-history evidence reads in an independent quant_test schema."""
from __future__ import annotations

from decimal import Decimal
import os
import tempfile
import unittest
from unittest.mock import patch
import uuid

from data_store import DataStore
from demo_evidence import DemoEvidenceError, read_demo_evidence
from demo_store import DemoStore
from runtime_config import load_environment

load_environment()
BTC = "BTC/USDT:USDT"


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class DemoEvidenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        live = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        def location(value):
            return tuple(value.get(key, "") for key in ("host", "port", "dbname"))
        if test.get("dbname") != "quant_test" or location(test) == location(live):
            raise RuntimeError("only an independent quant_test database is permitted")
        cls.schema = "demo_evidence_test_" + uuid.uuid4().hex
        cls.root = tempfile.TemporaryDirectory()
        cls.data = DataStore(root=cls.root.name, dsn=os.environ["QUANT_TEST_DATABASE_URL"],
                             schema=cls.schema).initialize()
        DemoStore(cls.data).ensure_schema()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.data.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.root.cleanup()

    def setUp(self):
        self.namespace = "evidence_" + uuid.uuid4().hex
        self.ledger = DemoStore(self.data, self.namespace)
        self.binding = {"strategy_id": "frozen-paper", "signal_id": "binding",
            "rule_hash": "frozen-rule", "execution_hash": "frozen-execution",
            "input_fingerprint": "frozen-input", "demo_policy_hash": "frozen-policy"}
        self.counter = 0

    def bind(self):
        self.ledger.bind_account(self.binding)

    def order(self, *, quantity="1", side="buy", reduce_only=False,
              status="NEW", filled="0", exchange=True):
        self.counter += 1
        client = "evidence_client_" + str(self.counter)
        intent = {**self.binding, "signal_id": "signal_" + str(self.counter),
            "client_id": client, "intent_key": "exit" if reduce_only else "entry",
            "pair": BTC, "side": side, "order_type": "limit", "quantity": quantity,
            "price": "100", "time_in_force": "GTC", "reduce_only": reduce_only,
            "signal_ms": 1000, "created_ms": 1000 + self.counter,
            "intent_payload": {"demo_policy_hash": self.binding["demo_policy_hash"],
                               "signal_reference_price": "100"}}
        row, _ = self.ledger.reserve_order(intent)
        if exchange:
            self.ledger.claim_submission(client, 1010)
            row = self.ledger.record_order_observation(client, {"raw": {
                "symbol": "BTCUSDT", "clientOrderId": client, "orderId": 100 + self.counter,
                "side": side.upper(), "type": "LIMIT", "status": status, "origQty": quantity,
                "executedQty": filled, "avgPrice": "100" if filled != "0" else "0",
                "price": "100", "time": 1010, "updateTime": 1020}})
        return row

    def fill(self, order, trade_id, *, quantity="1", fee=".02", asset="USDT",
             maker=True, quote="100", pnl="0", timestamp=2000):
        return self.ledger.record_fill(order["client_id"], {
            "exchange_order_id": order["exchange_order_id"], "exchange_trade_id": str(trade_id),
            "pair": BTC, "side": order["side"], "price": "100", "quantity": quantity,
            "commission": fee, "commission_asset": asset, "is_maker": maker,
            "realized_pnl": pnl, "timestamp_ms": timestamp,
            "raw": {"quoteQty": quote}})

    def read(self):
        return read_demo_evidence(self.data, self.namespace)

    def test_reader_is_direct_repeatable_read_read_only_without_setup_or_exchange(self):
        import psycopg
        self.bind()
        reader = DataStore(root=self.root.name, dsn=os.environ["QUANT_TEST_DATABASE_URL"],
                           schema=self.schema)
        statements, transaction_modes = [], []
        real_connect = psycopg.connect
        class ConnectionSpy:
            def __init__(self, connection):
                self.connection = connection
            def __enter__(self):
                self.connection.__enter__()
                return self
            def __exit__(self, *args):
                return self.connection.__exit__(*args)
            def transaction(self):
                return self.connection.transaction()
            def execute(self, query, *args, **kwargs):
                text = query.as_string(self.connection) if hasattr(query, "as_string") else str(query)
                statements.append(text)
                if text.lstrip().upper().startswith("SELECT"):
                    readonly = self.connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
                    isolation = self.connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
                    transaction_modes.append((readonly, isolation))
                return self.connection.execute(query, *args, **kwargs)
        def connect(*args, **kwargs):
            return ConnectionSpy(real_connect(*args, **kwargs))
        with patch("psycopg.connect", side_effect=connect), \
                patch.object(DataStore, "initialize", side_effect=AssertionError("no setup")), \
                patch.object(DataStore, "_connection", side_effect=AssertionError("no pool")), \
                patch.object(DemoStore, "ensure_schema", side_effect=AssertionError("no DDL")), \
                patch("binance_demo_credentials.DemoCredentialStore.load_credentials",
                      side_effect=AssertionError("no credentials")), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("no exchange")):
            report = read_demo_evidence(reader, self.namespace)
        self.assertEqual(report["questions"]["fees"]["state"], "no_actual_fills")
        self.assertIsNone(reader._pool)
        self.assertTrue(transaction_modes)
        self.assertTrue(all(mode == ("on", "repeatable read") for mode in transaction_modes))
        self.assertTrue(all(text.lstrip().upper().startswith(("SET ", "SELECT ")) for text in statements))
        self.assertFalse(any(" LIMIT " in text.upper() for text in statements))

    def test_all_history_exceeds_ui_limit_and_account_rate_quote_is_never_substituted(self):
        self.bind()
        row = self.order(quantity="101", status="FILLED", filled="101")
        for trade in range(101):
            self.fill(row, trade + 1)
        self.data.put_document("binance_demo_connection_check.json", {"commissionRates": [{
            "symbol": "BTCUSDT", "makerCommissionRate": "0.9", "takerCommissionRate": "0.8"}]})
        self.assertEqual(len(self.ledger.list_fills(limit=100)), 100)
        fees = self.read()["questions"]["fees"]
        self.assertEqual(fees["fillCount"], 101)
        self.assertEqual(fees["knownFeeFillCount"], 101)
        self.assertEqual(Decimal(fees["rates"][0]["effectiveFeeRate"]), Decimal(".0002"))
        self.assertEqual(Decimal(fees["rates"][0]["actualReportedQuoteUsdt"]), 10100)
        self.assertEqual(Decimal(fees["chargedByAsset"][0]["knownCommission"]), Decimal("2.02"))

    def test_native_reported_quote_denominator_and_maker_taker_entry_exit_groups(self):
        self.bind()
        entry = self.order(status="FILLED", filled="1")
        exit_order = self.order(side="sell", reduce_only=True, status="FILLED", filled="1")
        self.fill(entry, 1, fee=".02", quote="100.00000001")
        self.fill(exit_order, 2, fee=".05", maker=False, quote="100", pnl="10", timestamp=3000)
        report = self.read()
        rates = {(row["liquidity"], row["role"]): row for row in report["questions"]["fees"]["rates"]}
        self.assertEqual(rates[("maker", "entry")]["actualReportedQuoteUsdt"], "100.00000001")
        self.assertNotEqual(Decimal(rates[("maker", "entry")]["effectiveFeeRate"]), Decimal(".0002"))
        self.assertEqual(Decimal(rates[("taker", "exit")]["effectiveFeeRate"]), Decimal(".0005"))
        profit = report["questions"]["secondsProfit"]
        self.assertEqual(Decimal(profit["closedNetTradingPnlUsdt"]), Decimal("9.93"))
        self.assertFalse(profit["secondsExperimentConfigured"])
        self.assertFalse(profit["fundingIncluded"])

    def test_local_expiry_excluded_from_exchange_accepted_terminal_denominator(self):
        self.bind()
        local = self.order(exchange=False)
        self.ledger.abandon_unclaimed_order(local["client_id"], "entry_ttl", 4000)
        self.order(status="CANCELED", filled="0")
        partial = self.order(quantity="2", status="CANCELED", filled="1")
        self.fill(partial, 1)
        execution = self.read()["questions"]["execution"]
        self.assertEqual(execution["exchangeAcceptedEntryCount"], 2)
        self.assertEqual(execution["terminalAcceptedEntryCount"], 2)
        self.assertEqual(execution["acceptedTerminalNonfillRatio"], .5)
        self.assertEqual(execution["partialTerminalAcceptedEntryCount"], 1)
        self.assertEqual(execution["localTerminalUnconfirmedEntryCount"], 1)

    def test_empty_or_unknown_actual_fees_do_not_create_zero_cost_or_zero_net_evidence(self):
        self.bind()
        empty = self.read()
        self.assertEqual(empty["questions"]["fees"]["fillCount"], 0)
        self.assertFalse(empty["questions"]["fees"]["rates"])
        self.assertIsNone(empty["questions"]["secondsProfit"]["closedNetTradingPnlUsdt"])
        entry = self.order(status="FILLED", filled="1")
        exit_order = self.order(side="sell", reduce_only=True, status="FILLED", filled="1")
        self.fill(entry, 1, fee=None, asset=None)
        self.fill(exit_order, 2, pnl="10", timestamp=3000)
        report = self.read()
        self.assertEqual(report["questions"]["fees"]["unknownFeeFillCount"], 1)
        self.assertEqual(report["questions"]["secondsProfit"]["unknownNetRoundTripCount"], 1)
        self.assertIsNone(report["questions"]["secondsProfit"]["closedNetTradingPnlUsdt"])

    def test_order_policy_and_identity_must_match_immutable_binding(self):
        self.bind()
        row = self.order(status="CANCELED")
        with self.data._connection() as conn:
            conn.execute("UPDATE demo_orders SET rule_hash='different-rule' WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                         ("binance_demo", self.namespace, row["client_id"]))
        with self.assertRaisesRegex(DemoEvidenceError, "identity_mismatch"):
            self.read()

    def test_real_runner_rejection_classification_comes_from_persisted_signal_comparison(self):
        import ccxt
        from binance_demo_adapter import BinanceDemoAdapter
        from demo_execution_service import DemoExecutionService, DemoRunnerConfig
        from tests.test_demo_contract_integration import ExchangeStub, NOW, PaperStub
        class RejectingClient(ExchangeStub):
            def request(self, path, api, method, params, *, config=None):
                if path == "order" and method == "POST":
                    self.calls.append((path, api, method, dict(params)))
                    raise ccxt.InvalidOrder('binance {"code": -5022}')
                return super().request(path, api, method, params, config=config)
        paper, client, clock = PaperStub(), RejectingClient(), [NOW]
        adapter = BinanceDemoAdapter(client=client, api_key="offline-placeholder-key",
            api_secret="offline-placeholder-secret", allow_trading=True)
        service = DemoExecutionService(adapter, self.ledger, paper,
            DemoRunnerConfig("offline-paper", virtual_account_key=self.namespace, trading_enabled=True),
            clock_ms=lambda: clock[0])
        service.start()
        clock[0] += 1000
        paper.signal(clock[0])
        service.cycle()
        rejected = self.ledger.runner_orders()[0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertNotIn("error_kind", rejected["raw"])
        self.assertFalse(any(item.get("error_kind") for item in rejected["audit_observations"]))
        execution = self.read()["questions"]["execution"]
        self.assertEqual(execution["exchangeRejectedEntryCount"], 1)
        self.assertEqual(execution["exchangeAcceptedEntryCount"], 0)
        self.assertEqual(execution["localTerminalUnconfirmedEntryCount"], 0)
        self.assertIsNone(execution["acceptedTerminalNonfillRatio"])


if __name__ == "__main__":
    unittest.main()
