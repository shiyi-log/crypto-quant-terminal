"""Real ledger/adapter/coordinator integration against isolated quant_test.

All exchange requests use an in-process stub. Only a random test schema is
written; these tests never use credentials or contact Binance.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid

import ccxt

from binance_demo_adapter import BinanceDemoAdapter
from data_store import DataStore
from demo_execution_service import DemoExecutionService, DemoRunnerConfig
from demo_shared_account import SharedDemoAccount
from demo_store import DemoPairOwnershipError, DemoStore
from tests.test_demo_contract_integration import ExchangeStub, NOW, PaperStub
from tests.test_demo_store import fill, intent, observation

BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


class MultiExchangeStub(ExchangeStub):
    def _info(self):
        info = super()._info()
        eth = copy.deepcopy(info["symbols"][0])
        eth["symbol"] = "ETHUSDT"
        info["symbols"].append(eth)
        return info

    def add_fill(self, client_id, qty, **kwargs):
        super().add_fill(client_id, qty, **kwargs)
        self.fills[-1]["symbol"] = self.orders[client_id]["symbol"]

    def request(self, path, api, method, params, *, config=None):
        if path == "positionRisk":
            self.calls.append((path, api, method, copy.deepcopy(params)))
            return [{"symbol": symbol, "positionAmt": str(sum((
                Decimal(fill["qty"]) * (1 if fill["side"] == "BUY" else -1)
                for fill in self.fills if fill["symbol"] == symbol), Decimal(0))),
                "positionSide": "BOTH", "entryPrice": "60000.1", "leverage": "100",
                "marginType": "isolated"} for symbol in ("BTCUSDT", "ETHUSDT")]
        if path == "order" and method == "POST":
            self.calls.append((path, api, method, copy.deepcopy(params)))
            client = params["newClientOrderId"]
            self.orders[client] = {"symbol": params["symbol"], "orderId": len(self.orders) + 100,
                "clientOrderId": client, "side": params["side"], "type": params["type"],
                "status": "NEW", "origQty": params["quantity"], "executedQty": "0",
                "price": params.get("price", "0"), "avgPrice": "0", "time": self.now,
                "updateTime": self.now, "reduceOnly": params["reduceOnly"] == "true"}
            if self.timeout_once:
                self.timeout_once = False
                raise ccxt.RequestTimeout("offline ambiguous order")
            if params["type"] == "MARKET":
                self.add_fill(client, params["quantity"], maker=False)
            return copy.deepcopy(self.orders[client])
        if path == "userTrades":
            # The real endpoint is scoped by symbol, including cursor pages.
            original = self.fills
            self.fills = [row for row in original if row["symbol"] == params["symbol"]]
            try:
                return super().request(path, api, method, params, config=config)
            finally:
                self.fills = original
        return super().request(path, api, method, params, config=config)


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class SharedAccountIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        keys = ("host", "port", "dbname")
        if test.get("dbname") != "quant_test" or tuple(test.get(k, "") for k in keys) == tuple(
                production.get(k, "") for k in keys):
            raise RuntimeError("requires independent quant_test database")
        cls.schema = "quant_shared_demo_" + uuid.uuid4().hex
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
        with self.data._connection() as conn:
            conn.execute("TRUNCATE demo_fills,demo_orders,demo_positions,demo_account,demo_signal_comparisons")
        self.clock = NOW
        self.exchange = MultiExchangeStub()
        self.ledgers = {ns: DemoStore(self.data, ns) for ns in ("s35", "s07")}
        self.shared = SharedDemoAccount(self.ledgers["s35"])
        self.papers, self.services = {}, {}
        for ns in self.ledgers:
            paper = PaperStub()
            paper.row["strategy_id"] = ns
            paper.row["execution"]["execution_universe"] = [BTC, ETH]
            self.papers[ns] = paper
            self.services[ns] = self.new_service(ns)
            self.shared.register(self.services[ns])

    def new_service(self, ns):
        adapter = BinanceDemoAdapter(client=self.exchange, api_key="offline-placeholder-key",
            api_secret="offline-placeholder-secret", allow_trading=True)
        return DemoExecutionService(adapter, self.ledgers[ns], self.papers[ns],
            DemoRunnerConfig(ns, virtual_account_key=ns, trading_enabled=True), clock_ms=lambda: self.clock)

    def start(self):
        for service in self.services.values():
            state = service.start()
            self.assertIsNone(state.get("last_error"), state)

    def signal(self, ns, *, pair=BTC, side="buy", kind="entry"):
        self.clock += 1000
        self.exchange.now = self.clock
        self.papers[ns].signal(self.clock, side=side, kind=kind)
        self.papers[ns].rows[-1]["payload"]["pair"] = pair
        return self.services[ns].cycle()

    def posts(self):
        return [params for path, _, method, params in self.exchange.calls if path == "order" and method == "POST"]

    def test_same_pair_same_and_opposite_entries_are_denied_before_post(self):
        self.start()
        self.signal("s35")
        for side in ("buy", "sell"):
            state = self.signal("s07", side=side)
            self.assertIsNone(state.get("last_error"), state)
            denial = self.ledgers["s07"].list_comparisons(stage="entry_denied", limit=1)[0]
            self.assertEqual(denial["payload"]["reason"], "shared_pair_busy")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.ledgers["s07"].runner_orders(), [])

    def test_different_pairs_submit_and_real_fills_keep_strategy_and_namespace(self):
        self.start()
        self.signal("s35")
        state = self.signal("s07", pair=ETH, side="sell")
        self.assertEqual(state["ownership_conflicts"], [])
        self.assertEqual(len(self.posts()), 2)
        for ns in self.services:
            row = self.ledgers[ns].runner_orders()[0]
            self.exchange.add_fill(row["client_id"], row["quantity"])
            self.services[ns].cycle()
            actual_fill = self.ledgers[ns].runner_fills()[0]
            self.assertEqual(actual_fill["strategy_id"], ns)
            self.assertEqual(actual_fill["virtual_account_key"], ns)
        self.assertEqual(self.services["s07"].cycle()["ownership_conflicts"], [])
        self.assertNotEqual(self.posts()[0]["newClientOrderId"], self.posts()[1]["newClientOrderId"])

    def test_partial_entry_is_canceled_before_actual_quantity_exit_and_pair_release(self):
        self.start()
        self.signal("s35")
        entry = self.ledgers["s35"].runner_orders()[0]
        self.exchange.add_fill(entry["client_id"], ".003")
        self.papers["s35"].latest_price = "50000"
        state = self.services["s35"].cycle()
        self.assertIsNone(state.get("last_error"), state)
        self.assertEqual(self.exchange.orders[entry["client_id"]]["status"], "CANCELED")
        self.assertEqual(Decimal(self.posts()[1]["quantity"]), Decimal("0.003"))
        self.assertEqual(self.posts()[1]["reduceOnly"], "true")
        # A successful market response still needs its actual fill ledger.
        self.assertEqual(self.shared.entry_denial("s07", BTC), "shared_pair_busy")
        self.services["s35"].cycle()
        self.assertIsNone(self.shared.entry_denial("s07", BTC))
        self.signal("s07", side="sell")
        self.assertEqual(len(self.posts()), 3)

    def test_recovery_keeps_binding_and_unknown_post_is_not_retried(self):
        self.start()
        binding = self.ledgers["s35"].get_binding()
        self.exchange.timeout_once = True
        self.signal("s35")
        self.assertEqual(self.ledgers["s35"].runner_orders()[0]["status"], "unknown")
        recovered = self.new_service("s35")
        self.shared = SharedDemoAccount(self.ledgers["s35"])
        self.shared.register(recovered)
        self.shared.register(self.services["s07"])
        state = recovered.start(entries_enabled=False)
        self.assertIsNone(state.get("last_error"), state)
        recovered.cycle()
        self.assertFalse(state["entries_enabled"])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.ledgers["s35"].get_binding(), binding)

    def test_account_mismatch_does_not_allow_owned_exit_to_reduce_foreign_exposure(self):
        self.start()
        self.signal("s35")
        entry = self.ledgers["s35"].runner_orders()[0]
        self.exchange.add_fill(entry["client_id"], entry["quantity"])
        self.exchange.fills.append({"symbol": "BTCUSDT", "side": "BUY", "qty": ".002",
            "orderId": 999, "id": 999, "price": "60000", "time": self.clock,
            "commission": "0.01", "commissionAsset": "USDT", "realizedPnl": "0", "maker": True})
        self.papers["s35"].latest_price = "50000"
        state = self.services["s35"].cycle()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(state["risk_alert"], "shared_pair_ownership_unverified")

    def test_atomic_pair_reservation_race_has_one_winner(self):
        barrier = threading.Barrier(2)
        def reserve(ns):
            barrier.wait()
            try:
                self.ledgers[ns].reserve_order(intent(client_id=ns + "-intent", strategy_id=ns),
                                               exclusive_pair=True)
                return "reserved"
            except DemoPairOwnershipError:
                return "busy"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("s35", "s07")))
        self.assertEqual(sorted(results), ["busy", "reserved"])
        self.assertEqual(len(self.ledgers["s35"].shared_account_ownership()["orders"]), 1)

    def test_nonterminal_and_terminal_missing_fills_do_not_release_database_reservation(self):
        self.ledgers["s35"].reserve_order(intent(client_id="s35-intent"), exclusive_pair=True)
        for status in ("intent", "submitting", "unknown", "open", "partially_filled", "canceled"):
            with self.subTest(status=status):
                with self.data._connection() as conn:
                    conn.execute("UPDATE demo_orders SET status=%s,filled_quantity=%s", (status, "1"))
                with self.assertRaises(DemoPairOwnershipError):
                    self.ledgers["s07"].reserve_order(intent(client_id="s07-intent"), exclusive_pair=True)
        self.assertEqual(len(self.ledgers["s07"].runner_orders()), 0)

    def test_reconciled_flat_terminal_reservation_can_be_acquired_by_new_namespace(self):
        self.ledgers["s35"].reserve_order(intent(client_id="s35-intent"), exclusive_pair=True)
        self.ledgers["s35"].abandon_unclaimed_order("s35-intent", "explicit_cancel")
        self.ledgers["s07"].reserve_order(intent(client_id="s07-intent"), exclusive_pair=True)
        self.assertEqual(self.shared.status()["pair_owners"][BTC], ["s07"])

    def test_database_gate_does_not_net_opposing_old_namespaces(self):
        for ns, side, number in (("s35", "buy", 100), ("s07", "sell", 101)):
            ledger, client = self.ledgers[ns], ns + "-old"
            ledger.reserve_order(intent(client_id=client, signal_id=client, side=side, quantity="1"))
            ledger.record_order_observation(client, observation("FILLED", "1", clientOrderId=client,
                side=side.upper(), orderId=number, origQty="1"))
            ledger.record_fill(client, fill(exchange_order_id=str(number), trade=str(number), side=side))
        new = DemoStore(self.data, "third")
        with self.assertRaises(DemoPairOwnershipError):
            new.reserve_order(intent(client_id="third-intent"), exclusive_pair=True)
        self.assertIn("shared_pair_multiple_owners:" + BTC, self.shared.ownership_conflicts([], []))

    def test_different_pairs_and_unknown_persisted_namespace_survive_coordinator_rebuild(self):
        self.ledgers["s35"].reserve_order(intent(client_id="s35-intent"), exclusive_pair=True)
        self.ledgers["s07"].reserve_order(intent(client_id="s07-intent", pair=ETH), exclusive_pair=True)
        rebuilt = SharedDemoAccount(self.ledgers["s35"])
        rebuilt.register(SimpleNamespace(config=SimpleNamespace(virtual_account_key="s35"), store=self.ledgers["s35"]))
        self.assertEqual(rebuilt.entry_denial("s35", ETH), "shared_pair_busy")
        self.assertIn("unregistered_demo_owner:" + ETH, rebuilt.ownership_conflicts([], []))
        self.assertEqual(rebuilt.status()["pair_owners"], {BTC: ["s35"], ETH: ["s07"]})

    def test_burst_hold_starts_at_actual_fill_and_exits_without_candle_availability(self):
        paper = self.papers["s07"]
        paper.row["definition"]["signal_source"] = "database_live_closed_candle_forward_burst"
        paper.row["execution"]["max_hold_ms"] = 900000
        self.start()
        self.signal("s07")
        entry = self.ledgers["s07"].runner_orders()[0]
        self.clock += 10000
        self.exchange.now = self.clock
        self.exchange.add_fill(entry["client_id"], entry["quantity"])
        fill_ms = self.clock
        self.services["s07"].cycle()
        paper.closed_candle = lambda *args, **kwargs: None
        self.clock = fill_ms + 899999
        self.exchange.now = self.clock
        self.services["s07"].cycle()
        self.assertEqual(len(self.posts()), 1)
        self.clock += 1
        self.exchange.now = self.clock
        state = self.services["s07"].cycle()
        self.assertIsNone(state.get("last_error"), state)
        self.assertEqual(len(self.posts()), 2)
        exit_order = next(row for row in self.ledgers["s07"].runner_orders() if row["reduce_only"])
        self.assertEqual(exit_order["intent_payload"]["exit_reason"], "burst_max_hold_from_actual_fill")
        self.assertEqual(self.ledgers["s35"].runner_orders(), [])

    def test_burst_unfilled_intent_has_no_time_exit_and_other_strategies_ignore_max_hold(self):
        self.papers["s07"].row["definition"]["signal_source"] = "database_live_closed_candle_forward_burst"
        self.papers["s07"].row["execution"]["max_hold_ms"] = 900000
        self.papers["s35"].row["execution"]["max_hold_ms"] = 900000
        self.start()
        self.signal("s07", pair=ETH)
        self.signal("s35")
        entry = self.ledgers["s35"].runner_orders()[0]
        self.exchange.add_fill(entry["client_id"], entry["quantity"])
        self.clock += 1000000
        self.exchange.now = self.clock
        self.services["s07"].cycle()
        self.services["s35"].cycle()
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(all(not row["reduce_only"] for ledger in self.ledgers.values() for row in ledger.runner_orders()))

    def test_burst_provenance_is_a_database_signal_and_actual_stop_mode_is_explicit(self):
        paper = self.papers["s07"]
        paper.row["definition"].update(signal_source="database_live_closed_candle_forward_burst", paper_mirror=False)
        paper.row["execution"]["stop_policy"] = "close_confirmed"
        self.papers["s35"].row["execution"]["stop_policy"] = "intrabar_ohlc"
        self.start()
        self.signal("s07")
        binding = self.ledgers["s07"].get_binding()
        entry = self.ledgers["s07"].runner_orders()[0]
        evidence = entry["intent_payload"]
        self.assertIn("strategy_signal", evidence)
        self.assertNotIn("paper_signal", evidence)
        self.assertEqual(evidence["reference_source"], "database_live_closed_candle_forward_burst")
        self.assertLessEqual(evidence["reference_timestamp_ms"], self.clock)
        self.assertFalse(evidence["live_executable_slippage_verified"])
        self.assertEqual(self.ledgers["s07"].list_comparisons(stage="paper_event"), [])
        signal = self.ledgers["s07"].list_comparisons(stage="strategy_signal")[0]["payload"]
        self.assertEqual(signal["source_type"], "database_live_closed_candle_forward_burst")
        self.assertFalse(signal["paper_mirror"])
        self.assertNotIn("paper_event", signal)
        self.assertNotIn("paper_initially_flat", binding)
        self.assertEqual(self.ledgers["s07"].runner_fills(), [])
        self.signal("s35", pair=ETH)
        mirrored = self.ledgers["s35"].runner_orders()[0]["intent_payload"]
        self.assertEqual(mirrored["frozen_source_stop_policy"], "intrabar_ohlc")
        self.assertEqual(mirrored["actual_stop_execution_mode"], "close_confirmed_closed_candle_poll_reduce_only_market")
        self.assertIn("paper_signal", mirrored)
        self.assertEqual(mirrored["reference_source"], "paper_closed_candle_from_live_market")
        self.assertEqual(self.services["s07"].status()["signal_source"], "database_live_closed_candle_forward_burst")

    def test_burst_rejects_a_future_candle_even_if_mislabelled_closed(self):
        paper = self.papers["s07"]
        paper.row["definition"].update(signal_source="database_live_closed_candle_forward_burst", paper_mirror=False)
        self.start()
        original = paper.closed_candle
        def future(pair, timeframe, candle_ms=None):
            candle = original(pair, timeframe, candle_ms)
            candle["timestamp"] = self.clock
            return candle
        paper.closed_candle = future
        self.signal("s07")
        self.assertEqual(self.posts(), [])
        denied = self.ledgers["s07"].list_comparisons(stage="entry_denied")[0]["payload"]
        self.assertEqual(denied["reason"], "strategy_candle_not_closed_at_execution")


if __name__ == "__main__":
    unittest.main()
