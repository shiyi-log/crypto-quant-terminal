"""Real fee-capture modules; fake HTTP and an isolated quant_test schema only.

No credential file, production database connection, or real HTTP transport is
used. Persisted claims and fills are checked across fresh adapter/store objects.
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
import uuid
from collections import Counter
from decimal import Decimal

import ccxt

from binance_demo_adapter import ALLOWED_PAIRS, PAIR_SYMBOLS, BinanceDemoAdapter
from data_store import DataStore
from demo_store import DemoStore
from tests.test_demo_contract_integration import ExchangeStub


class FeeCaptureExchangeStub(ExchangeStub):
    """Exchange-shaped facts with automatic fills, never a network request."""

    markets = {
        "BTCUSDT": ("60000", "59999.9", "0.1", "0.0001"),
        "ETHUSDT": ("3000", "2999.99", "0.01", "0.001"),
        "TRXUSDT": ("0.25", "0.2499", "0.0001", "1"),
    }

    def __init__(self):
        super().__init__()
        self.maker_attempts = Counter()
        self.unfilled_first_maker = False
        self.hidden_client_ids = set()
        self.hide_timeout_order = False
        self.hidden_fill_order_ids = set()

    def _info(self):
        symbols = []
        for symbol, (_, _, tick, step) in self.markets.items():
            symbols.append({"symbol": symbol, "contractType": "PERPETUAL",
                "quoteAsset": "USDT", "marginAsset": "USDT", "status": "TRADING",
                "filters": [{"filterType": kind, "minQty": step,
                    "maxQty": "100000", "stepSize": step}
                    for kind in ("LOT_SIZE", "MARKET_LOT_SIZE")] + [
                    {"filterType": "PRICE_FILTER", "minPrice": tick,
                     "maxPrice": "1000000", "tickSize": tick},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]})
        return {"symbols": symbols}

    def add_fill(self, client_id, qty, *, commission=None, asset="USDT", maker=True):
        order = self.orders[client_id]
        quantity = Decimal(qty)
        price = (order["price"] if maker else self.markets[order["symbol"]][0])
        order["executedQty"] = str(Decimal(order["executedQty"]) + quantity)
        order["avgPrice"] = price
        order["updateTime"] = self.now
        order["status"] = ("FILLED" if Decimal(order["executedQty"]) == Decimal(order["origQty"])
                           else "PARTIALLY_FILLED")
        rate = Decimal("0.0002" if maker else "0.0004")
        self.fills.append({"symbol": order["symbol"], "id": len(self.fills) + 1,
            "orderId": order["orderId"], "side": order["side"], "qty": str(quantity),
            "price": price, "commission": commission or str(quantity * Decimal(price) * rate),
            "commissionAsset": asset, "maker": maker, "time": self.now, "realizedPnl": "0"})

    def request(self, path, api, method, params, *, config=None):
        self.calls.append((path, api, method, copy.deepcopy(params)))
        symbol = params.get("symbol")
        if path == "exchangeInfo":
            return self._info()
        if path == "ticker/bookTicker":
            ask, bid, _, _ = self.markets[symbol]
            return {"symbol": symbol, "bidPrice": bid, "askPrice": ask,
                    "bidQty": "1000", "askQty": "1000", "time": self.now}
        if path == "premiumIndex":
            return {"symbol": symbol, "markPrice": self.markets[symbol][0], "time": self.now}
        if path == "commissionRate":
            return {"symbol": symbol, "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0004"}
        if path == "positionSide/dual":
            return {"dualSidePosition": self.hedged}
        if path == "account":
            return {"totalWalletBalance": "10000", "totalMarginBalance": "10000",
                    "availableBalance": "10000", "assets": [], "positions": []}
        if path == "symbolConfig":
            return [{"symbol": item, "leverage": 100, "marginType": "ISOLATED"}
                    for item in self.markets if symbol is None or item == symbol]
        if path == "positionRisk":
            return [{"symbol": item, "positionAmt": str(sum(
                (Decimal(fill["qty"]) * (1 if fill["side"] == "BUY" else -1)
                 for fill in self.fills if fill["symbol"] == item), Decimal(0))),
                "positionSide": "BOTH", "entryPrice": self.markets[item][0]}
                for item in self.markets]
        if path == "userTrades":
            rows = [fill for fill in self.fills if fill["symbol"] == symbol
                    and fill["orderId"] not in self.hidden_fill_order_ids]
            if params.get("orderId") is not None:
                rows = [fill for fill in rows if fill["orderId"] == int(params["orderId"])]
            if params.get("fromId") is not None:
                rows = [fill for fill in rows if fill["id"] >= int(params["fromId"])]
            if params.get("startTime") is not None:
                rows = [fill for fill in rows if fill["time"] >= int(params["startTime"])]
            return copy.deepcopy(rows[:params.get("limit", 1000)])
        if path in ("allOrders", "openOrders"):
            rows = [order for order in self.orders.values()
                    if (symbol is None or order["symbol"] == symbol)
                    and order["clientOrderId"] not in self.hidden_client_ids]
            if path == "openOrders":
                rows = [order for order in rows if order["status"] in ("NEW", "PARTIALLY_FILLED")]
            return copy.deepcopy(rows[:params.get("limit", 1000)])
        if path == "order" and method == "POST":
            client_id = params["newClientOrderId"]
            if client_id in self.orders:
                raise AssertionError("an existing durable client ID was POSTed again")
            order = {"symbol": symbol, "orderId": len(self.orders) + 100,
                "clientOrderId": client_id, "side": params["side"], "type": params["type"],
                "timeInForce": params.get("timeInForce", "GTC"), "status": "NEW",
                "origQty": params["quantity"], "executedQty": "0",
                "price": params.get("price", "0"), "avgPrice": "0",
                "time": self.now, "updateTime": self.now,
                "reduceOnly": params["reduceOnly"] == "true"}
            self.orders[client_id] = order
            if params.get("timeInForce") == "GTX":
                self.maker_attempts[symbol] += 1
                order["_fill_due"] = (None if self.unfilled_first_maker
                    and self.maker_attempts[symbol] == 1 else self.now + 1000)
            else:
                self.add_fill(client_id, params["quantity"], maker=False)
            if self.timeout_once:
                self.timeout_once = False
                # Simulate an accepted/fill-before-response timeout.  Recovery
                # must discover this terminal order and submit only its exit.
                if params.get("timeInForce") == "GTX" and not self.unfilled_first_maker:
                    self.add_fill(client_id, params["quantity"], maker=True)
                if self.hide_timeout_order:
                    self.hidden_client_ids.add(client_id)
                raise ccxt.RequestTimeout("offline response lost after exchange acceptance")
            return copy.deepcopy(order)
        if path == "order":
            client_id = params.get("origClientOrderId")
            if client_id not in self.orders or client_id in self.hidden_client_ids:
                raise ccxt.OrderNotFound("offline absent order")
            order = self.orders[client_id]
            due = order.get("_fill_due")
            if order["status"] == "NEW" and due is not None and self.now >= due:
                self.add_fill(client_id, order["origQty"], maker=True)
            if method == "DELETE" and order["status"] in ("NEW", "PARTIALLY_FILLED"):
                order["status"] = "CANCELED"
                order["updateTime"] = self.now
            return copy.deepcopy(order)
        raise AssertionError(f"unexpected fake endpoint: {path}/{method}")


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test PostgreSQL required")
class FeeCaptureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        def location(info):
            return tuple(info.get(key, "") for key in ("host", "port", "dbname"))
        if test.get("dbname") != "quant_test" or location(test) == location(production):
            raise RuntimeError("requires an independent quant_test database")
        cls.schema = "quant_fee_capture_" + uuid.uuid4().hex
        cls.root = tempfile.TemporaryDirectory()
        cls.data = DataStore(root=cls.root.name, dsn=os.environ["QUANT_TEST_DATABASE_URL"],
                             schema=cls.schema).initialize()
        DemoStore(cls.data).ensure_schema()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.data.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.root.cleanup()

    def setUp(self):
        with self.data._connection() as connection:
            connection.execute("TRUNCATE demo_fills,demo_orders,demo_positions,"
                               "demo_account,demo_signal_comparisons")
        self.client = FeeCaptureExchangeStub()
        self.ledger = DemoStore(self.data, "fee_capture_" + uuid.uuid4().hex)

    def adapter(self):
        return BinanceDemoAdapter(client=self.client, api_key="offline-placeholder-key",
                                  api_secret="offline-placeholder-secret", allow_trading=True)

    def pause(self, seconds):
        self.client.now += int(seconds * 1000)

    def run_capture(self, *, pairs=ALLOWED_PAIRS, recovery_only=False):
        from tools.run_binance_demo_fee_capture import run_fee_capture
        return run_fee_capture(self.adapter(), self.ledger, pairs=pairs,
            clock_ms=lambda: self.client.now, sleep=self.pause, maker_ttl_seconds=30,
            total_timeout_seconds=60, reconciliation_seconds=3, recovery_only=recovery_only)

    def posts(self):
        return [params for path, _, method, params in self.client.calls
                if path == "order" and method == "POST"]

    def assert_durable_single_claims(self):
        rows = self.ledger.runner_orders()
        self.assertEqual(len(self.posts()), len(rows))
        self.assertTrue(all(row["submission_attempts"] == 1 for row in rows), rows)
        self.assertEqual(len({row["client_id"] for row in rows}), len(rows))
        with self.data._connection() as connection:
            invalid = connection.execute("SELECT COUNT(*) AS n FROM demo_orders "
                "WHERE virtual_account_key=%s AND (submission_attempts<>1 OR submission_claimed_ms IS NULL)",
                (self.ledger.virtual_account_key,)).fetchone()["n"]
        self.assertEqual(invalid, 0)

    def test_three_pairs_actual_maker_and_taker_facts_match_persisted_fills(self):
        report = self.run_capture()
        self.assertTrue(report["success"], report)
        self.assertTrue(report["position_flat"])
        self.assertTrue(report["open_orders_zero"])
        self.assertTrue(report["fill_reconciliation_complete"])
        self.assertEqual(len(self.posts()), 12)
        fills = self.ledger.runner_fills()
        self.assertEqual(len(fills), 12)
        self.assertEqual(Counter((fill["pair"], fill["is_maker"]) for fill in fills),
            Counter({(pair, True): 1 for pair in ALLOWED_PAIRS}) +
            Counter({(pair, False): 3 for pair in ALLOWED_PAIRS}))
        for fill in fills:
            rate = Decimal(fill["commission"]) / (Decimal(fill["price"]) * Decimal(fill["quantity"]))
            self.assertEqual(rate, Decimal("0.0002" if fill["is_maker"] else "0.0004"))
            self.assertEqual(fill["commission_asset"], "USDT")
        self.assertEqual(len(report["fills"]), len(fills))
        self.assertEqual(len(report["samples"]), len(fills))
        self.assertTrue(all(report["maker_verified_by_pair"].values()))
        self.assertEqual({post["symbol"] for post in self.posts()}, set(PAIR_SYMBOLS.values()))
        self.assertEqual(sum(post.get("timeInForce") == "GTX" for post in self.posts()), 3)
        self.assertEqual(sum(post.get("timeInForce") == "IOC" for post in self.posts()), 3)
        self.assertTrue(all(post["reduceOnly"] == "true" for post in self.posts()
                            if post["type"] == "MARKET"))
        self.assert_durable_single_claims()

    def test_maker_rollover_cancels_before_claiming_a_distinct_client_id(self):
        self.client.unfilled_first_maker = True
        report = self.run_capture(pairs=(ALLOWED_PAIRS[0],))
        self.assertTrue(report["success"], report)
        makers = [post for post in self.posts() if post.get("timeInForce") == "GTX"]
        self.assertEqual(len(makers), 2)
        self.assertNotEqual(makers[0]["newClientOrderId"], makers[1]["newClientOrderId"])
        first = self.client.orders[makers[0]["newClientOrderId"]]
        second = self.client.orders[makers[1]["newClientOrderId"]]
        self.assertEqual(first["status"], "CANCELED")
        self.assertEqual(first["executedQty"], "0")
        self.assertEqual(second["status"], "FILLED")
        self.assertGreaterEqual(second["time"], first["updateTime"])
        self.assertEqual(len(self.ledger.runner_fills()), 4)
        self.assert_durable_single_claims()

    def test_completed_session_reopens_from_database_without_any_new_order_post(self):
        first = self.run_capture(pairs=(ALLOWED_PAIRS[0],))
        self.assertTrue(first["success"], first)
        count = len(self.posts())
        before = copy.deepcopy(self.ledger.runner_orders())
        self.ledger = DemoStore(self.data, self.ledger.virtual_account_key)
        second = self.run_capture(pairs=(ALLOWED_PAIRS[0],))
        self.assertTrue(second["success"], second)
        self.assertEqual(len(self.posts()), count)
        self.assertEqual(len(self.ledger.runner_orders()), len(before))
        self.assertEqual(len(self.ledger.runner_fills()), len(first["fills"]))
        self.assert_durable_single_claims()

    def test_unknown_post_recovery_queries_original_id_and_never_reposts_it(self):
        self.client.timeout_once = self.client.hide_timeout_order = True
        first = self.run_capture(pairs=(ALLOWED_PAIRS[0],))
        self.assertTrue(first["recovery_required"], first)
        self.assertEqual(len(self.posts()), 1)
        original_id = self.posts()[0]["newClientOrderId"]
        # Keep the accepted order unqueryable: recovery must stop safely rather
        # than guessing its state or posting a replacement.
        self.ledger = DemoStore(self.data, self.ledger.virtual_account_key)
        second = self.run_capture(pairs=(ALLOWED_PAIRS[0],), recovery_only=True)
        self.assertTrue(second["recovery_required"], second)
        self.assertFalse(second["position_flat"], second)
        self.assertTrue(second["open_orders_zero"], second)
        self.assertEqual(sum(post["newClientOrderId"] == original_id for post in self.posts()), 1)
        self.assertEqual(len(self.ledger.runner_fills()), 0)
        self.assert_durable_single_claims()
