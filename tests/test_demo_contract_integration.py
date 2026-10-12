"""Real Demo modules together; only an isolated test DB and fake HTTP client.

The placeholder credentials never reach a network. No live configuration or
private credential file is opened by this test.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import os
import tempfile
import unittest
import uuid

import ccxt

from binance_demo_adapter import BinanceDemoAdapter
from data_store import DataStore
from demo_execution_service import DemoExecutionService, DemoRunnerConfig
from demo_store import DemoStore, binance_fill, binance_order
from runtime_config import load_environment

load_environment()
BTC = "BTC/USDT:USDT"
SYMBOL = "BTCUSDT"
NOW = 1_800_000_000_000


class ExchangeStub:
    id = "binance"
    isSandboxModeEnabled = False
    verbose = False

    def __init__(self):
        self.options = {"enableDemoTrading": True, "maxRetriesOnFailure": 0}
        self.urls = {"api": {name: f"https://demo-fapi.binance.com/fapi/v{version}"
                             for name, version in (("fapiPublic", 1), ("fapiPrivate", 1),
                                                   ("fapiPrivateV3", 3))}}
        self.now = NOW
        self.orders, self.fills, self.calls = {}, [], []
        self.timeout_once = False
        self.visible_fill_limit = None
        self.balance_error = False
        self.server_trade_page_size = None
        self.hedged = False
        self.actual_leverage = 100

    def _info(self):
        return {"symbols": [{"symbol": SYMBOL, "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT", "status": "TRADING", "filters": [
                {"filterType": kind, "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"}
                for kind in ("LOT_SIZE", "MARKET_LOT_SIZE")
            ] + [{"filterType": "PRICE_FILTER", "minPrice": "0.10", "maxPrice": "1000000",
                  "tickSize": "0.10"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}

    def add_fill(self, client_id, qty, *, commission="0.01", asset="USDT", maker=True):
        order = self.orders[client_id]
        quantity = Decimal(qty)
        order["executedQty"] = str(Decimal(order["executedQty"]) + quantity)
        order["avgPrice"] = order["price"] if order["type"] == "LIMIT" else "58000"
        order["updateTime"] = self.now
        if Decimal(order["executedQty"]) == Decimal(order["origQty"]):
            order["status"] = "FILLED"
        elif order["status"] != "CANCELED":
            order["status"] = "PARTIALLY_FILLED"
        self.fills.append({"symbol": SYMBOL, "id": len(self.fills) + 1,
            "orderId": order["orderId"], "side": order["side"], "qty": str(quantity),
            "price": order["avgPrice"], "commission": commission, "commissionAsset": asset,
            "maker": maker, "time": self.now, "realizedPnl": "0"})

    def request(self, path, api, method, params, *, config=None):
        self.calls.append((path, api, method, copy.deepcopy(params)))
        if path == "exchangeInfo":
            return self._info()
        if path == "positionSide/dual":
            return {"dualSidePosition": self.hedged}
        if path == "symbolConfig":
            return [{"symbol": symbol, "leverage": self.actual_leverage, "marginType": "ISOLATED"}
                    for symbol in (SYMBOL, "ETHUSDT", "TRXUSDT")]
        if path == "account":
            if self.balance_error:
                raise ccxt.NetworkError("offline balance endpoint unavailable")
            return {"totalWalletBalance": "10000", "totalMarginBalance": "10000",
                    "availableBalance": "10000", "assets": [], "positions": []}
        if path == "positionRisk":
            signed = sum((Decimal(fill["qty"]) * (1 if fill["side"] == "BUY" else -1)
                          for fill in self.fills), Decimal(0))
            return [{"symbol": SYMBOL, "positionAmt": str(signed), "positionSide": "BOTH",
                     "entryPrice": "60000.1", "leverage": "100", "marginType": "isolated"}]
        if path == "userTrades":
            rows = self.fills
            if self.visible_fill_limit is not None:
                rows = rows[:self.visible_fill_limit]
            if params.get("orderId") is not None:
                rows = [row for row in rows if row["orderId"] == int(params["orderId"])]
            if params.get("fromId") is not None:
                rows = [row for row in rows if row["id"] >= int(params["fromId"])]
            if params.get("startTime") is not None:
                rows = [row for row in rows if row["time"] >= int(params["startTime"])]
            if self.server_trade_page_size is not None:
                rows = rows[:self.server_trade_page_size]
            return copy.deepcopy(rows[:params.get("limit", 1000)])
        if path in ("allOrders", "openOrders"):
            rows = list(self.orders.values())
            if path == "openOrders":
                rows = [row for row in rows if row["status"] in ("NEW", "PARTIALLY_FILLED")]
            return copy.deepcopy(rows[:params.get("limit", 1000)])
        if path == "order" and method == "POST":
            client_id = params["newClientOrderId"]
            self.orders[client_id] = {"symbol": SYMBOL, "orderId": len(self.orders) + 100,
                "clientOrderId": client_id, "side": params["side"], "type": params["type"],
                "status": "NEW", "origQty": params["quantity"], "executedQty": "0",
                "price": params.get("price", "0"), "avgPrice": "0",
                "time": self.now, "updateTime": self.now, "reduceOnly": params["reduceOnly"] == "true"}
            if self.timeout_once:
                self.timeout_once = False
                raise ccxt.RequestTimeout("offline transport outcome unknown")
            if params["type"] == "MARKET":
                self.add_fill(client_id, params["quantity"], maker=False)
            return copy.deepcopy(self.orders[client_id])
        if path == "order":
            client_id = params.get("origClientOrderId")
            if client_id not in self.orders:
                raise ccxt.OrderNotFound("offline absent order")
            if method == "DELETE":
                self.orders[client_id]["status"] = "CANCELED"
                self.orders[client_id]["updateTime"] = self.now
            return copy.deepcopy(self.orders[client_id])
        raise AssertionError(f"unexpected offline endpoint {path}")


class PaperStub:
    def __init__(self):
        self.rows = []
        self.latest_price = "60000.1"
        self.signal_price = "60000.1"
        self.latest_candle_ms = NOW
        self.row = {"strategy_id": "offline-paper", "rule_hash": "rule", "execution_hash": "execution",
            "definition": {"strategy_id": "offline-paper", "params": {"risk_stop_atr_mult": 1.5}},
            "execution": {"execution_enabled": True, "risk_sizing_mode": "entry_atr",
                "execution_universe": [BTC], "timeframe": "15m", "fee_rate_per_side": .0002,
                "max_loss_per_trade_usdt": 50, "leverage": 100}, "state": {"positions": {}}}

    def account(self, strategy_id):
        return copy.deepcopy(self.row)

    def latest_sequence(self, strategy_id):
        return max((row["sequence_id"] for row in self.rows), default=0)

    def events(self, strategy_id, after_sequence, limit=1000):
        return copy.deepcopy([row for row in self.rows if row["sequence_id"] > after_sequence][:limit])

    def closed_candle(self, pair, timeframe, candle_ms=None):
        return {"timestamp": candle_ms if candle_ms is not None else self.latest_candle_ms,
                "close": self.signal_price if candle_ms is not None else self.latest_price,
                "extras": {"closed": True}}

    def signal(self, now, *, kind="entry", side="buy"):
        seq = len(self.rows) + 1
        event = {"sequence_id": seq, "event_id": "offline-signal-" + str(seq),
            "rule_hash": "rule", "data_fingerprint": "offline-input-" + str(seq),
            "event_type": "order_submitted", "observed_at_ms": now, "candle_ms": NOW - 900_000,
            "payload": {"pair": BTC, "kind": kind, "buy_sell": side,
                        "signal_candle_ms": NOW - 900_000, "size_multiplier": 1,
                        "evidence": {"entry_atr": 200}}}
        self.rows.append(event)


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "isolated test PostgreSQL required")
class DemoContractIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        def location(info):
            return tuple(info.get(key, "") for key in ("host", "port", "dbname"))
        if test.get("dbname") != "quant_test" or location(test) == location(production):
            raise RuntimeError("requires independent quant_test database")
        cls.schema = "quant_demo_contract_" + uuid.uuid4().hex
        cls.root = tempfile.TemporaryDirectory()
        cls.data = DataStore(root=cls.root.name, dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema).initialize()
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
        self.clock = NOW
        self.client, self.paper = ExchangeStub(), PaperStub()
        self.ledger = DemoStore(self.data, "offline-" + uuid.uuid4().hex)
        self.config = DemoRunnerConfig("offline-paper", virtual_account_key=self.ledger.virtual_account_key,
                                       trading_enabled=True)
        self.service = self.new_service()

    def new_service(self):
        adapter = BinanceDemoAdapter(client=self.client, api_key="offline-placeholder-key",
                                     api_secret="offline-placeholder-secret", allow_trading=True)
        return DemoExecutionService(adapter, self.ledger, self.paper, self.config, clock_ms=lambda: self.clock)

    def tick(self, elapsed=1000):
        self.clock += elapsed
        self.client.now = self.clock
        return self.service.cycle()

    def enter(self, *, side="buy"):
        self.assertNotEqual(self.service.start()["status"], "reconciliation_error")
        self.clock += 1000
        self.client.now = self.clock
        self.paper.signal(self.clock, side=side)
        result = self.service.cycle()
        self.assertIsNone(result["last_error"], result)
        rows = self.ledger.runner_orders()
        self.assertEqual(len(rows), 1, result)
        self.assertEqual(rows[0]["status"], "open", rows[0])
        return rows[0]

    def order_post_count(self):
        return sum(path == "order" and method == "POST"
                   for path, _, method, _ in self.client.calls)

    def signal_during_next_event_read(self, elapsed=663):
        """Model an event committed after cycle time, before its source read."""
        original_events = self.paper.events

        def events(strategy_id, after_sequence, limit=1000):
            self.paper.events = original_events
            self.clock += elapsed
            self.client.now = self.clock
            self.paper.signal(self.clock)
            return original_events(strategy_id, after_sequence, limit)

        self.paper.events = events

    def test_signal_arriving_during_cycle_uses_processing_time_and_submits_once(self):
        self.service.start()
        self.clock += 1000
        cycle_started_ms = self.clock
        self.signal_during_next_event_read()

        result = self.service.cycle()

        self.assertIsNone(result["last_error"], result)
        self.assertEqual(self.order_post_count(), 1, result)
        self.assertEqual(len(self.ledger.runner_orders()), 1)
        order = self.ledger.runner_orders()[0]
        self.assertEqual(order["created_ms"], cycle_started_ms + 663)
        self.assertEqual(order["intent_payload"]["expires_ms"],
                         cycle_started_ms + 663 + self.config.entry_order_ttl_ms)
        self.assertFalse(self.ledger.list_comparisons(stage="entry_denied"))
        evidence = self.ledger.list_comparisons(stage="paper_event")
        self.assertEqual(evidence[0]["observed_ms"], cycle_started_ms + 663)
        self.assertEqual(evidence[0]["payload"]["paper_observed_ms"], cycle_started_ms + 663)
        self.assertEqual(result["last_sequence_id"], 1)
        checkpoint = self.ledger.list_comparisons(stage="runner_checkpoint", limit=1)[0]
        self.assertEqual(checkpoint["observed_ms"], cycle_started_ms + 663)
        self.tick()
        self.assertEqual(self.order_post_count(), 1)

    def test_first_start_processes_arriving_signal_without_changing_forward_boundary(self):
        boundary_ms = self.clock
        self.signal_during_next_event_read()

        result = self.service.start()

        self.assertIsNone(result["last_error"], result)
        self.assertEqual(result["forward_boundary_ms"], boundary_ms)
        self.assertEqual(self.ledger.get_binding()["forward_boundary_ms"], boundary_ms)
        self.assertEqual(self.order_post_count(), 1, result)
        evidence = self.ledger.list_comparisons(stage="paper_event")
        self.assertEqual(evidence[0]["observed_ms"], boundary_ms + 663)
        self.assertFalse(self.ledger.list_comparisons(stage="entry_denied"))

    def test_each_event_rechecks_clock_when_second_signal_exceeds_lag_limit(self):
        self.service.start()
        self.clock += 1000
        observed_ms = self.clock
        self.paper.signal(observed_ms)
        self.paper.rows[0]["event_type"] = "order_cancelled"
        self.paper.signal(observed_ms)
        original_events = self.paper.events

        def events(strategy_id, after_sequence, limit=1000):
            self.paper.events = original_events
            rows = original_events(strategy_id, after_sequence, limit)
            yield rows[0]
            self.clock += self.config.max_signal_lag_ms + 1
            self.client.now = self.clock
            yield rows[1]

        self.paper.events = events
        result = self.service.cycle()

        self.assertIsNone(result["last_error"], result)
        self.assertEqual(self.order_post_count(), 0, result)
        self.assertFalse(self.ledger.runner_orders())
        denied = self.ledger.list_comparisons(stage="entry_denied")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["signal_id"], "offline-signal-2")
        self.assertEqual(denied[0]["payload"]["reason"], "paper_signal_stale")
        self.assertEqual(denied[0]["observed_ms"], observed_ms + self.config.max_signal_lag_ms + 1)
        evidence = {row["signal_id"]: row for row in self.ledger.list_comparisons(stage="paper_event")}
        self.assertEqual(evidence["offline-signal-1"]["observed_ms"], observed_ms)
        self.assertEqual(evidence["offline-signal-2"]["observed_ms"], self.clock)
        self.assertEqual(result["last_sequence_id"], 2)
        checkpoint = self.ledger.list_comparisons(stage="runner_checkpoint", limit=1)[0]
        self.assertEqual(checkpoint["observed_ms"], self.clock)

    def test_genuinely_future_signal_stays_rejected_without_an_order_post(self):
        self.service.start()
        self.clock += 1000
        self.paper.signal(self.clock + 663)

        result = self.service.cycle()

        self.assertIsNone(result["last_error"], result)
        self.assertEqual(self.order_post_count(), 0)
        self.assertFalse(self.ledger.runner_orders())
        denied = self.ledger.list_comparisons(stage="entry_denied")
        self.assertEqual(denied[0]["payload"]["reason"], "paper_signal_stale")
        self.assertEqual(denied[0]["observed_ms"], self.clock)
        self.assertEqual(result["last_sequence_id"], 1)

    def test_explicit_cycle_time_remains_authoritative_for_future_and_old_wall_clock_signals(self):
        self.service.start()
        explicit_ms = NOW + 1000
        self.clock = NOW + 200_000
        self.client.now = self.clock
        self.paper.signal(explicit_ms + 663)

        denied_result = self.service.cycle(now_ms=explicit_ms)

        self.assertIsNone(denied_result["last_error"], denied_result)
        self.assertEqual(self.order_post_count(), 0)
        denied = self.ledger.list_comparisons(stage="entry_denied")
        self.assertEqual(denied[0]["payload"]["reason"], "paper_signal_stale")
        self.assertEqual(denied[0]["observed_ms"], explicit_ms)
        explicit_ms += 2000
        self.paper.signal(explicit_ms)

        accepted_result = self.service.cycle(now_ms=explicit_ms)

        self.assertIsNone(accepted_result["last_error"], accepted_result)
        self.assertEqual(accepted_result["last_cycle_ms"], explicit_ms)
        self.assertEqual(self.order_post_count(), 1, accepted_result)
        evidence = {row["signal_id"]: row for row in self.ledger.list_comparisons(stage="paper_event")}
        self.assertEqual(evidence["offline-signal-2"]["observed_ms"], explicit_ms)
        checkpoint = self.ledger.list_comparisons(stage="runner_checkpoint", limit=1)[0]
        self.assertEqual(checkpoint["observed_ms"], explicit_ms)

    def test_denied_signals_are_checkpointed_and_restart_replays_no_order_submission(self):
        self.service.start()
        self.clock += 1000
        self.paper.signal(self.clock - self.config.max_signal_lag_ms - 1)
        self.paper.signal(self.clock + 663)

        result = self.service.cycle()

        self.assertIsNone(result["last_error"], result)
        self.assertEqual(result["last_sequence_id"], 2)
        self.assertEqual(self.order_post_count(), 0)
        self.assertEqual(len(self.ledger.list_comparisons(stage="entry_denied")), 2)
        checkpoint = self.ledger.list_comparisons(stage="runner_checkpoint", limit=1)[0]
        self.assertEqual(checkpoint["payload"]["last_sequence_id"], 2)
        # The formerly future signal is now admissible; it must not be replayed.
        self.clock += 1663
        self.client.now = self.clock
        self.service = self.new_service()
        restarted = self.service.start()
        self.assertIsNone(restarted["last_error"], restarted)
        self.assertEqual(restarted["last_sequence_id"], 2)
        self.assertEqual(self.order_post_count(), 0)
        self.paper.signal(self.clock)
        accepted = self.service.cycle()
        self.assertIsNone(accepted["last_error"], accepted)
        self.assertEqual(accepted["last_sequence_id"], 3)
        self.assertEqual(self.order_post_count(), 1)
        self.service = self.new_service()
        self.service.start()
        self.tick()
        self.assertEqual(self.order_post_count(), 1)
        self.assertEqual(len(self.ledger.runner_orders()), 1)

    def test_real_modules_round_quantity_store_binance_format_and_frozen_risk(self):
        row = self.enter()
        self.assertEqual(Decimal(row["quantity"]) % Decimal(".001"), 0)
        self.assertLessEqual(Decimal(row["quantity"]) * Decimal(row["price"]), 1000)
        self.assertEqual(len(row["client_id"]), 35)
        self.assertEqual(row["intent_payload"]["entry_atr"], "200")
        original = self.client.orders[row["client_id"]]
        exported = binance_order(row)
        for name in ("orderId", "clientOrderId", "symbol", "side", "status", "origQty"):
            self.assertEqual(str(exported[name]), str(original[name]))

    def test_binance_export_preserves_exchange_time_separate_from_local_intent_clock(self):
        row = self.enter()
        self.client.orders[row["client_id"]]["time"] = self.clock + 75
        self.tick()
        exported = binance_order(self.ledger.get_order(row["client_id"]))
        self.assertEqual(exported["time"], self.client.orders[row["client_id"]]["time"])

    def test_partial_expiry_and_late_actual_fee_are_not_lost_or_duplicated(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], ".008", commission=None, asset=None)
        self.tick()
        self.tick(31_000)
        canceled = self.ledger.get_order(row["client_id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(Decimal(canceled["filled_quantity"]), Decimal(".008"))
        fills = self.ledger.list_fills(row["client_id"])
        self.assertEqual(len(fills), 1)
        self.assertIsNone(fills[0]["commission"])
        self.client.fills[0].update(commission="0.123456789", commissionAsset="USDT")
        self.tick()
        fills = self.ledger.list_fills(row["client_id"])
        self.assertEqual(len(fills), 1)
        self.assertEqual(Decimal(fills[0]["commission"]), Decimal("0.123456789"))
        self.assertEqual(Decimal(self.ledger.net_fill_positions()[BTC]), Decimal(".008"))
        self.assertEqual(binance_fill(fills[0])["commissionAsset"], "USDT")

    def test_unknown_timeout_restart_reconciles_without_another_submission(self):
        self.service.start()
        self.clock += 1000
        self.client.now = self.clock
        self.paper.signal(self.clock)
        self.client.timeout_once = True
        self.service.cycle()
        self.assertEqual(self.ledger.runner_orders()[0]["status"], "unknown")
        self.service = self.new_service()
        self.service.start()
        self.tick()
        self.assertEqual(self.ledger.runner_orders()[0]["status"], "open")
        self.assertEqual(sum(path == "order" and verb == "POST" for path, _, verb, _ in self.client.calls), 1)

    def test_closed_foreign_order_is_not_an_open_ownership_conflict(self):
        self.client.orders["foreign-old"] = {"symbol": SYMBOL, "orderId": 30,
            "clientOrderId": "foreign-old", "side": "BUY", "type": "LIMIT", "status": "FILLED",
            "origQty": ".01", "executedQty": ".01", "avgPrice": "60000.1",
            "price": "60000.1", "time": NOW - 1000, "updateTime": NOW - 1000}
        row = self.enter()
        self.assertEqual(row["status"], "open")

    def test_short_partial_risk_exit_is_reduce_only_and_stays_protected_after_restart(self):
        row = self.enter(side="sell")
        self.client.add_fill(row["client_id"], ".008")
        self.tick(31_000)
        self.service = self.new_service()
        self.service.start()
        self.paper.latest_price = "61000"
        result = self.tick()
        self.assertIsNone(result["last_error"], result)
        exits = [item for item in self.ledger.runner_orders() if item["reduce_only"]]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["side"], "buy")
        self.assertEqual(Decimal(exits[0]["quantity"]), Decimal(".008"))
        self.tick()
        self.assertEqual(Decimal(self.ledger.net_fill_positions().get(BTC, "0")), 0)

    def test_below_exchange_minimum_does_not_claim_unknown_order(self):
        self.config = DemoRunnerConfig("offline-paper", virtual_account_key=self.ledger.virtual_account_key,
                                       trading_enabled=True, max_notional_usdt="1")
        self.service = self.new_service()
        self.service.start()
        self.clock += 1000
        self.paper.signal(self.clock)
        result = self.service.cycle()
        self.assertIsNone(result["last_error"], result)
        self.assertFalse(self.ledger.runner_orders())
        denied = self.ledger.list_comparisons(stage="entry_denied")
        self.assertTrue(denied)

    def test_late_partial_fill_can_get_another_protective_exit_on_same_closed_bar(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], ".008")
        self.client.add_fill(row["client_id"], ".008")
        self.client.visible_fill_limit = 1
        self.paper.latest_price = "58000"
        self.tick()
        self.client.visible_fill_limit = None
        self.tick()
        exits = [item for item in self.ledger.runner_orders() if item["reduce_only"]]
        self.assertEqual(len(exits), 2, self.service.status())
        self.tick()
        self.assertEqual(Decimal(self.ledger.net_fill_positions().get(BTC, "0")), 0)

    def test_balance_failure_does_not_suppress_owned_reduce_only_risk_exit(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], ".008")
        self.tick(31_000)
        self.client.balance_error = True
        self.paper.latest_price = "58000"
        self.tick()
        exits = [item for item in self.ledger.runner_orders() if item["reduce_only"]]
        self.assertEqual(len(exits), 1, self.service.status())

    def test_more_than_one_thousand_actual_fills_are_paginated_without_truncation(self):
        self.paper.row["execution"]["max_loss_per_trade_usdt"] = 1000
        self.config = DemoRunnerConfig("offline-paper", virtual_account_key=self.ledger.virtual_account_key,
                                       trading_enabled=True, max_notional_usdt="100000")
        self.service = self.new_service()
        row = self.enter()
        for _ in range(1000):
            self.client.add_fill(row["client_id"], ".001")
        self.client.add_fill(row["client_id"], str(Decimal(row["quantity"]) - Decimal(1)))
        result = self.tick()
        self.assertEqual(len(self.ledger.runner_fills()), 1001, result)
        self.assertEqual(Decimal(self.ledger.net_fill_positions()[BTC]), Decimal(row["quantity"]))
        self.assertFalse(result["ownership_conflicts"], result)

    def test_short_trade_page_is_followed_when_confirmed_execution_is_larger(self):
        row = self.enter()
        for _ in range(5):
            self.client.add_fill(row["client_id"], ".002")
        self.client.add_fill(row["client_id"], str(Decimal(row["quantity"]) - Decimal(".010")))
        self.client.server_trade_page_size = 2
        result = self.tick()
        self.assertEqual(len(self.ledger.runner_fills()), 6, result)
        self.assertEqual(Decimal(self.ledger.net_fill_positions()[BTC]), Decimal(row["quantity"]))

    def test_real_frozen_paper_account_and_event_source_are_read_only(self):
        from demo_execution_service import PaperEventSource
        from strategy_factorial import load_factorial_specs
        from strategy_simulation import StrategySimulation
        definition = next(row for row in load_factorial_specs()
                          if row["canonical_strategy_id"] == "s03_tsmom_vol_target"
                          and row["max_loss_per_trade_usdt"] == 50
                          and row["params"]["risk_stop_atr_mult"] == 1.5)
        simulation = StrategySimulation(self.data, clock_ms=lambda: self.clock)
        simulation.ensure_accounts([definition], NOW)
        source = PaperEventSource(self.data, clock_ms=lambda: self.clock)
        account = source.account(definition["strategy_id"])
        self.data.upsert_candles("binance", "futures", BTC, "15m", "futures", [{
            "timestamp": NOW - 900_000, "open": 60000.1, "high": 60001,
            "low": 59999, "close": 60000.1, "volume": 1, "extras": {"closed": True}},
            {"timestamp": NOW, "open": 60000.1, "high": 60001, "low": 59999,
             "close": 60000.1, "volume": 1, "extras": {"closed": False}}])
        self.assertEqual(source.closed_candle(BTC, "15m")["timestamp"], NOW - 900_000)
        self.config = DemoRunnerConfig(definition["strategy_id"],
                                       virtual_account_key=self.ledger.virtual_account_key,
                                       trading_enabled=True)
        self.paper = source
        self.service = self.new_service()
        self.service.start()
        self.clock += 1000
        self.client.now = self.clock
        with self.data._connection() as conn:
            simulation._append(conn, [{"event_id": "offline-actual-paper-event",
                "strategy_id": definition["strategy_id"], "event_type": "order_submitted",
                "candle_ms": NOW - 900_000, "observed_at_ms": self.clock,
                "rule_hash": account["rule_hash"], "data_fingerprint": "offline-actual-data",
                "payload": {"pair": BTC, "kind": "entry", "buy_sell": "buy",
                    "signal_candle_ms": NOW - 900_000, "size_multiplier": 1,
                    "evidence": {"entry_atr": 200}}}])
        result = self.service.cycle()
        self.assertIsNone(result["last_error"], result)
        orders = self.ledger.runner_orders()
        self.assertTrue(orders, {"status": result, "denied": self.ledger.list_comparisons(stage="entry_denied")})
        order = orders[0]
        self.assertEqual(order["rule_hash"], account["rule_hash"])
        self.assertEqual(order["execution_hash"], account["execution_hash"])
        self.assertEqual(source.account(definition["strategy_id"])["state"], account["state"])

    def test_flat_hedge_mode_refuses_new_orders_before_an_exchange_rejection(self):
        self.client.hedged = True
        self.service.start()
        self.clock += 1000
        self.paper.signal(self.clock)
        result = self.service.cycle()
        self.assertIn("demo_hedge_mode_unsupported", result["ownership_conflicts"])
        self.assertFalse(self.ledger.runner_orders())

    def test_actual_demo_leverage_is_recorded_and_never_changed_to_paper_setting(self):
        self.client.actual_leverage = 20
        self.enter()
        status = self.service.status()
        self.assertEqual(status["actual_symbol_config"][0]["leverage"], 20)
        self.assertEqual(status["paper_leverage"], 100)
        self.assertFalse(any(path in ("leverage", "marginType", "positionSide/dual") and method == "POST"
                             for path, _, method, _ in self.client.calls))

    def test_known_fee_amount_with_late_asset_is_reconciled_after_order_terminal(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], row["quantity"], commission="0.123456789", asset=None)
        self.tick()
        self.assertIsNone(self.ledger.runner_fills()[0]["commission_asset"])
        self.client.fills[0]["commissionAsset"] = "USDT"
        self.tick()
        fill = self.ledger.runner_fills()[0]
        self.assertEqual(fill["commission_asset"], "USDT")
        self.assertEqual(Decimal(fill["commission"]), Decimal("0.123456789"))

    def test_original_signal_reference_is_distinct_from_quantized_submitted_limit(self):
        self.paper.signal_price = "60000.19"
        row = self.enter()
        evidence = row["intent_payload"]
        self.assertEqual(evidence["signal_reference_price"], "60000.19")
        self.assertEqual(evidence["submitted_limit_price"], "60000.10")
        self.assertEqual(evidence["reference_price"], evidence["submitted_limit_price"])
        self.assertEqual(evidence["reference_timestamp_ms"], NOW)
        self.assertEqual(evidence["reference_source"], "paper_closed_candle_from_live_market")

    def test_risk_exit_freezes_triggering_candle_reference_without_limit_price(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], ".008")
        self.tick(31_000)
        self.paper.latest_price = "58000.19"
        self.paper.latest_candle_ms = NOW - 900_000
        self.tick()
        exits = [order for order in self.ledger.runner_orders() if order["reduce_only"]]
        self.assertEqual(len(exits), 1)
        evidence = exits[0]["intent_payload"]
        self.assertEqual(evidence["signal_reference_price"], "58000.19")
        self.assertEqual(evidence["reference_timestamp_ms"], NOW)
        self.assertIsNone(evidence["submitted_limit_price"])
        self.assertIsNone(evidence["reference_price"])

    def test_paper_exit_freezes_original_closed_candle_reference(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], row["quantity"])
        self.tick()
        self.paper.signal_price = "60000.19"
        self.paper.signal(self.clock, kind="exit", side="sell")
        self.tick()
        exits = [order for order in self.ledger.runner_orders() if order["reduce_only"]]
        self.assertEqual(len(exits), 1)
        evidence = exits[0]["intent_payload"]
        self.assertEqual(evidence["signal_reference_price"], "60000.19")
        self.assertEqual(evidence["reference_timestamp_ms"], NOW)
        self.assertIsNone(evidence["submitted_limit_price"])

    def test_missing_paper_exit_reference_stays_unknown_and_does_not_gate_exit(self):
        row = self.enter()
        self.client.add_fill(row["client_id"], row["quantity"])
        self.tick()
        self.paper.closed_candle = lambda *args, **kwargs: None
        self.paper.signal(self.clock, kind="exit", side="sell")
        self.tick()
        exits = [order for order in self.ledger.runner_orders() if order["reduce_only"]]
        self.assertEqual(len(exits), 1)
        evidence = exits[0]["intent_payload"]
        self.assertIsNone(evidence["signal_reference_price"])
        self.assertIsNone(evidence["reference_source"])
        self.assertIsNone(evidence["reference_timestamp_ms"])

    def test_local_expired_unsubmitted_intent_does_not_enter_nonfill_population(self):
        self.service.start()
        event = {"event_id": "unsubmitted-local", "data_fingerprint": "local-fingerprint",
                 "candle_ms": NOW - 900_000}
        intent = self.service._base_intent(event, BTC, "buy", "entry", Decimal(".001"),
            Decimal("60000.1"), self.clock, raw={"expires_ms": self.clock})
        self.ledger.reserve_order(intent)
        self.tick()
        self.assertEqual(self.ledger.get_order(intent["client_id"])["status"], "expired")
        status, metrics = self.service.status(), self.ledger.metrics()
        self.assertEqual(status["terminal_entry_count"], metrics["terminal_entry_count"])
        self.assertEqual(status["unfilled_entry_count"], metrics["unfilled_entry_count"])
        self.assertEqual(status["terminal_entry_count"], 0)
        self.assertIsNone(status["unfilled_entry_ratio"])
        self.assertFalse(status["seconds_hypothesis_tested"])


if __name__ == "__main__":
    unittest.main()
