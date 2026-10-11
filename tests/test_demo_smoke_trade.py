import copy
import unittest
from decimal import Decimal

from binance_demo_adapter import DemoSubmissionUnknown, DemoSafetyError
from demo_smoke_trade import run_smoke_trade, _common_step


class Clock:
    def __init__(self):
        self.value = 1_000_000

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += int(seconds * 1000)


class Ledger:
    environment = "binance_demo"
    endpoint_host = "demo-fapi.binance.com"
    virtual_account_key = "manual_smoke_offline_test"

    def __init__(self):
        self.orders = {}
        self.fills = {}
        self.binding = None
        self.comparisons = []
        self.fail_claim = False
        self.projection_failures = 0

    def runner_orders(self):
        return copy.deepcopy(list(self.orders.values()))

    def runner_fills(self):
        return copy.deepcopy(list(self.fills.values()))

    def bind_account(self, binding):
        if self.binding and self.binding != binding:
            raise ValueError("binding differs")
        self.binding = copy.deepcopy(binding)

    def reserve_order(self, intent):
        client = intent["client_id"]
        if client in self.orders:
            old = self.orders[client]
            for field in ("quantity", "price", "signal_ms", "intent_payload"):
                if str(old[field]) != str(intent[field]):
                    raise ValueError("intent differs")
            return copy.deepcopy(old), False
        row = {**copy.deepcopy(intent), "status": "intent", "submission_attempts": 0,
               "exchange_order_id": None, "filled_quantity": "0"}
        self.orders[client] = row
        return copy.deepcopy(row), True

    def claim_submission(self, client, _time):
        if self.fail_claim:
            raise RuntimeError("secret=never-print-this")
        row = self.orders[client]
        if row["submission_attempts"]:
            return False
        row.update(submission_attempts=1, status="submitting")
        return True

    def record_order_observation(self, client, order):
        if self.projection_failures:
            self.projection_failures -= 1
            raise RuntimeError("signed-url=never-print-this")
        status = {"NEW": "open", "PARTIALLY_FILLED": "partially_filled"}.get(
            order["status"], order["status"].lower())
        self.orders[client].update(status=status, exchange_order_id=order.get("order_id"),
                                   filled_quantity=order["filled_qty"], raw=order["raw"])

    def record_fill(self, client, fill):
        self.fills[fill["fill_id"]] = {**copy.deepcopy(fill), "client_id": client,
            "exchange_trade_id": fill["fill_id"], "exchange_order_id": fill["order_id"],
            "quantity": fill["qty"], "timestamp_ms": fill["ts"]}

    def mark_submission_unknown(self, client, _reason, _time):
        self.orders[client]["status"] = "unknown"

    def append_reconciliation(self, *_args):
        return None

    def append_account_snapshot(self, *_args):
        return None

    def append_position_snapshot(self, *_args):
        return None

    def append_comparison(self, payload, stage, _time):
        self.comparisons.append((stage, copy.deepcopy(payload)))


class Adapter:
    allow_trading = True

    def __init__(self):
        self.orders = {}
        self.trades = {}
        self.position = Decimal(0)
        self.post_calls = []
        self.cancel_calls = 0
        self.partial = False
        self.unknown_entry = False
        self.absent_entry = False
        self.unknown_exit = False
        self.absent_exit = False
        self.stale_position = 0
        self.fills_failures = 0
        self.fee_unknown = False
        self.hedged = False
        self.foreign_open = False
        self.partial_exit = False
        self.release_partial_exit = False

    def fetch_position_mode(self):
        return {"hedged": self.hedged}

    def fetch_positions(self):
        quantity = self.position
        if quantity and self.stale_position:
            self.stale_position -= 1
            quantity = Decimal(0)
        return [] if not quantity else [{"pair": "ETH/USDT:USDT", "symbol": "ETHUSDT",
                "position_side": "BOTH", "signed_qty": str(quantity), "raw": {}}]

    def fetch_open_orders(self):
        if self.foreign_open:
            return [{"client_id": "foreign"}]
        return [copy.deepcopy(order) for order in self.orders.values()
                if order["status"] in {"NEW", "PARTIALLY_FILLED"}]

    def fetch_balance(self):
        return {"available_balance": "5000", "raw": {"canTrade": True}}

    def fetch_exchange_info(self):
        result = []
        for symbol, price, minimum in (("BTCUSDT", "100000", "100"),
                                       ("ETHUSDT", "2000", "20"), ("TRXUSDT", "0.25", "5")):
            result.append({"symbol": symbol, "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.002", "maxQty": "1000", "stepSize": "0.002"},
                {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "999999", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": minimum}], "test_price": price})
        return {"symbols": result}

    def _call(self, path, api, method, params):
        assert (path, api, method) == ("premiumIndex", "fapiPublic", "GET")
        return {"symbol": params["symbol"], "markPrice": {"BTCUSDT": "100000", "ETHUSDT": "2000", "TRXUSDT": "0.25"}[params["symbol"]]}

    def prepare_limit_order(self, pair, side, qty, price):
        self.validate_order(pair, qty, price)
        return {"quantity": str(qty), "price": str(price)}

    def validate_order(self, pair, qty, price=None, *, market=False):
        qty = Decimal(str(qty))
        if qty <= 0 or qty % Decimal("0.002" if market else "0.001"):
            raise DemoSafetyError("invalid quantity")
        return str(qty), None if market else str(price)

    def authorize_owned_client_id(self, _client):
        pass

    def _order(self, pair, side, qty, price, client, filled, status, market=False):
        order_id = str(len(self.orders) + 1)
        raw = {"symbol": "ETHUSDT", "orderId": order_id, "clientOrderId": client,
               "side": side.upper(), "type": "MARKET" if market else "LIMIT",
               "status": status, "origQty": str(qty), "executedQty": str(filled),
               "price": str(price), "avgPrice": str(price), "updateTime": 1_000_000,
               "timeInForce": "GTC" if not market else "GTC", "reduceOnly": market}
        order = {"pair": pair, "side": side, "order_id": order_id, "client_id": client,
                 "qty": str(qty), "filled_qty": str(filled), "price": str(price),
                 "avg_price": str(price), "status": status, "updated_ms": 1_000_000,
                 "raw": raw}
        self.orders[client] = order
        if filled:
            self.position += filled if side == "buy" else -filled
            commission = None if self.fee_unknown else str(filled * price * Decimal("0.0004"))
            trade_raw = {"id": order_id, "orderId": order_id, "symbol": "ETHUSDT",
                "side": side.upper(), "price": str(price), "qty": str(filled),
                "quoteQty": str(price * filled), "commission": commission,
                "commissionAsset": "USDT", "maker": False, "realizedPnl": "0" if side == "buy" else "-0.01",
                "time": 1_000_000 + int(order_id)}
            self.trades[order_id] = [{"fill_id": order_id, "order_id": order_id,
                "pair": pair, "side": side, "price": str(price), "qty": str(filled),
                "commission": commission, "commission_asset": "USDT", "is_maker": False,
                "realized_pnl": trade_raw["realizedPnl"], "ts": trade_raw["time"], "raw": trade_raw}]
        return copy.deepcopy(order)

    def submit_limit_order(self, pair, side, qty, price, client, *, post_only):
        assert post_only is False
        self.post_calls.append(("entry", client))
        if self.absent_entry:
            raise DemoSubmissionUnknown("timeout")
        filled = Decimal("0.004") if self.partial else Decimal(str(qty))
        order = self._order(pair, side, Decimal(str(qty)), Decimal("2000"), client, filled,
                            "PARTIALLY_FILLED" if self.partial else "FILLED")
        if self.unknown_entry:
            raise DemoSubmissionUnknown("timeout")
        return order

    def submit_market_order(self, pair, side, qty, client, *, reduce_only):
        assert reduce_only is True
        self.post_calls.append(("exit", client))
        if self.absent_exit:
            raise DemoSubmissionUnknown("timeout")
        order = self._order(pair, side, Decimal(str(qty)), Decimal("1999"), client,
                            Decimal("0.004") if self.partial_exit else Decimal(str(qty)),
                            "PARTIALLY_FILLED" if self.partial_exit else "FILLED", market=True)
        if self.unknown_exit:
            raise DemoSubmissionUnknown("timeout")
        return order

    def fetch_order_by_client_id(self, pair, client):
        order = self.orders.get(client)
        if (order and order["side"] == "sell" and order["status"] == "PARTIALLY_FILLED"
                and self.release_partial_exit):
            remaining = Decimal(order["qty"]) - Decimal(order["filled_qty"])
            self.position -= remaining
            order["status"] = order["raw"]["status"] = "FILLED"
            order["filled_qty"] = order["raw"]["executedQty"] = order["qty"]
            new_fill = copy.deepcopy(self.trades[order["order_id"]][0])
            new_fill.update(fill_id="3", qty=str(remaining), ts=1_000_003,
                            commission=str(remaining * Decimal("1999") * Decimal("0.0004")))
            new_fill["raw"].update(id="3", qty=str(remaining), time=1_000_003,
                                   commission=new_fill["commission"])
            self.trades[order["order_id"]].append(new_fill)
        return copy.deepcopy(self.orders.get(client))

    def cancel_order(self, pair, *, client_order_id):
        self.cancel_calls += 1
        if client_order_id not in self.orders:
            return None
        self.orders[client_order_id]["status"] = "CANCELED"
        self.orders[client_order_id]["raw"]["status"] = "CANCELED"
        return copy.deepcopy(self.orders[client_order_id])

    def fetch_trades(self, pair, *, order_id):
        if self.fills_failures:
            self.fills_failures -= 1
            raise RuntimeError("apiKey=never-print-this")
        return copy.deepcopy(self.trades.get(str(order_id), []))


class SmokeTradeTests(unittest.TestCase):
    def setUp(self):
        self.adapter, self.ledger, self.clock = Adapter(), Ledger(), Clock()

    def run_trade(self):
        return run_smoke_trade(self.adapter, self.ledger, clock_ms=self.clock.now,
            sleep=self.clock.sleep, entry_wait_seconds=1, reconciliation_seconds=2)

    def test_minimal_open_close_actual_fee_and_native_records(self):
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["pair"], "ETH/USDT:USDT")
        self.assertEqual(result["plan"]["quantity"], "0.010")
        self.assertEqual(len(self.adapter.post_calls), 2)
        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(Decimal(result["commission_usdt"]), Decimal("0.015996"))
        self.assertEqual(Decimal(result["round_trips"][0]["actual_net_trading_pnl_usdt"]), Decimal("-0.025996"))
        self.assertIn("demo_policy_hash", self.ledger.binding)
        self.assertFalse(result["strategy_or_hft_evidence"])

    def test_partial_entry_cancelled_then_only_actual_long_closed(self):
        self.adapter.partial = True
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.adapter.cancel_calls, 1)
        exits = [order for order in self.ledger.orders.values() if order["reduce_only"]]
        self.assertEqual(Decimal(exits[0]["quantity"]), Decimal("0.004"))
        self.assertEqual(self.adapter.position, 0)

    def test_unknown_entry_and_exit_only_queried_never_posted_twice(self):
        self.adapter.unknown_entry = self.adapter.unknown_exit = True
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.adapter.post_calls), 2)
        rerun = self.run_trade()
        self.assertTrue(rerun["success"], rerun)
        self.assertEqual(len(self.adapter.post_calls), 2)

    def test_returned_order_validation_failure_is_unknown_then_reconciled(self):
        submit = self.adapter.submit_limit_order

        def mismatched_response(*args, **kwargs):
            response = submit(*args, **kwargs)
            response["raw"]["reduceOnly"] = True
            return response

        self.adapter.submit_limit_order = mismatched_response
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["errors"][0]["kind"], "submission_unknown")
        self.assertEqual(len(self.adapter.post_calls), 2)
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual({row["status"] for row in self.ledger.orders.values()}, {"filled"})

    def test_unknown_absent_entry_does_not_retry_or_claim_flat_success(self):
        self.adapter.absent_entry = True
        result = self.run_trade()
        self.assertFalse(result["success"])
        self.assertEqual(len(self.adapter.post_calls), 1)
        self.run_trade()
        self.assertEqual(len(self.adapter.post_calls), 1)

    def test_stale_position_after_filled_is_waited_for_and_closed(self):
        self.adapter.stale_position = 1
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(len(self.adapter.post_calls), 2)

    def test_telemetry_projection_failure_never_prevents_protective_exit(self):
        self.ledger.projection_failures = 1
        result = self.run_trade()
        self.assertFalse(result["persistence_complete"])
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(len(self.adapter.post_calls), 2)
        self.assertNotIn("never-print-this", json_text(result))

    def test_fill_fetch_failure_retries_only_after_exit_already_flat(self):
        self.adapter.fills_failures = 1
        result = self.run_trade()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.adapter.position, 0)
        self.assertNotIn("never-print-this", json_text(result))

    def test_durable_claim_failure_prevents_any_order_post(self):
        self.ledger.fail_claim = True
        result = self.run_trade()
        self.assertFalse(result["success"])
        self.assertEqual(self.adapter.post_calls, [])
        self.assertNotIn("never-print-this", json_text(result))

    def test_unknown_exit_not_reposted_and_exposure_reported(self):
        self.adapter.absent_exit = True
        result = self.run_trade()
        self.assertFalse(result["success"])
        self.assertFalse(result["position_flat"])
        self.assertEqual(len(self.adapter.post_calls), 2)

    def test_recovered_partial_exit_keeps_original_durable_quantity(self):
        self.adapter.partial_exit = True
        first = self.run_trade()
        self.assertFalse(first["success"])
        self.assertEqual(self.adapter.position, Decimal("0.006"))
        self.adapter.release_partial_exit = True
        second = self.run_trade()
        self.assertTrue(second["success"], second)
        self.assertEqual(len(self.adapter.post_calls), 2)
        exit_order = next(row for row in self.ledger.orders.values() if row["reduce_only"])
        self.assertEqual(Decimal(exit_order["quantity"]), Decimal("0.010"))
        self.run_trade()
        self.assertEqual(len(self.adapter.post_calls), 2)

    def test_missing_actual_fee_is_unknown_not_assumed(self):
        self.adapter.fee_unknown = True
        result = self.run_trade()
        self.assertFalse(result["success"])
        self.assertTrue(result["position_flat"])
        self.assertIsNone(result["commission_usdt"])
        self.assertIsNone(result["round_trips"][0]["actual_net_trading_pnl_usdt"])

    def test_one_way_flat_and_no_foreign_open_order_preconditions(self):
        for field, value in (("hedged", True), ("position", Decimal("0.01")), ("foreign_open", True)):
            with self.subTest(field=field):
                self.adapter = Adapter()
                setattr(self.adapter, field, value)
                result = self.run_trade()
                self.assertFalse(result["success"])
                self.assertEqual(self.adapter.post_calls, [])

    def test_default_namespace_or_excess_cap_rejected(self):
        self.ledger.virtual_account_key = "demo_default"
        self.assertFalse(self.run_trade()["success"])
        self.assertEqual(self.adapter.post_calls, [])

    def test_common_step_exact_lcm_of_non_dividing_decimal_steps(self):
        self.assertEqual(_common_step(Decimal("0.002"), Decimal("0.003")), Decimal("0.006"))


def json_text(value):
    import json
    return json.dumps(value)
