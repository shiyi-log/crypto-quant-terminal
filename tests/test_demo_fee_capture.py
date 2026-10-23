"""Offline fee-capture safety tests; no credentials, database or venue calls."""
from copy import deepcopy
from decimal import Decimal
import unittest

from binance_demo_adapter import DemoSafetyError, DemoSubmissionUnknown
from demo_fee_capture import expected_risk_gate_target_binding


PAIR = "BTC/USDT:USDT"
SYMBOLS = {PAIR: "BTCUSDT", "ETH/USDT:USDT": "ETHUSDT", "TRX/USDT:USDT": "TRXUSDT"}
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


class Clock:
    def __init__(self):
        self.value = 1_000_000
        self.monotonic_value = 0.0

    def now(self):
        return self.value

    def mono(self):
        return self.monotonic_value

    def sleep(self, seconds):
        self.value += int(seconds * 1000)
        self.monotonic_value += seconds


class Ledger:
    environment = "binance_demo"
    endpoint_host = "demo-fapi.binance.com"

    def __init__(self):
        self.virtual_account_key = "fee_capture_offline"
        self.orders, self.fills, self.comparisons = {}, {}, []
        self.binding = None
        self.fail_binding = self.fail_reserve = self.fail_claim = False
        self.fail_fill = False
        self.fail_projection_count = 0

    def get_binding(self):
        return deepcopy(self.binding)

    def bind_account(self, value):
        if self.fail_binding:
            raise RuntimeError("secret=never-serialize-this")
        if self.binding is not None and self.binding != value:
            raise ValueError("immutable binding mismatch")
        self.binding = deepcopy(value)
        return deepcopy(value)

    def reserve_order(self, value):
        if self.fail_reserve:
            raise RuntimeError("secret=never-serialize-this")
        client = value["client_id"]
        if client in self.orders:
            old = self.orders[client]
            for field in ("quantity", "price", "signal_ms", "intent_key", "intent_payload"):
                if old[field] != value[field]:
                    raise ValueError("immutable intent mismatch")
            return deepcopy(old), False
        row = {**deepcopy(value), "status": "intent", "submission_attempts": 0,
               "filled_quantity": "0", "exchange_order_id": None, "exchange_updated_ms": -1,
               "raw": {}, "audit_observations": []}
        self.orders[client] = row
        return deepcopy(row), True

    def claim_submission(self, client, stamp):
        if self.fail_claim:
            raise RuntimeError("secret=never-serialize-this")
        row = self.orders[client]
        if row["submission_attempts"] or row["status"] != "intent":
            return False
        row.update(status="submitting", submission_attempts=1, submission_claimed_ms=stamp)
        return True

    def record_order_observation(self, client, value):
        if self.fail_projection_count:
            self.fail_projection_count -= 1
            raise RuntimeError("secret=never-serialize-this")
        row = self.orders[client]
        status = {"NEW": "open", "PARTIALLY_FILLED": "partially_filled",
                  "EXPIRED_IN_MATCH": "expired"}.get(value["status"], value["status"].lower())
        row.update(status=status, raw=deepcopy(value.get("raw", {})),
                   filled_quantity=value.get("filled_qty", "0"),
                   exchange_order_id=value.get("order_id"),
                   exchange_updated_ms=value.get("updated_ms", -1))
        return deepcopy(row)

    def mark_submission_unknown(self, client, *_args):
        if self.orders[client]["status"] == "submitting":
            self.orders[client]["status"] = "unknown"
        return deepcopy(self.orders[client])

    def record_fill(self, client, value):
        if self.fail_fill:
            raise RuntimeError("secret=never-serialize-this")
        key = (value["pair"], value["order_id"], value["fill_id"])
        row = {**deepcopy(value), "client_id": client,
               "exchange_trade_id": value["fill_id"], "exchange_order_id": value["order_id"],
               "quantity": value["qty"], "timestamp_ms": value["ts"]}
        old = self.fills.get(key)
        if old is not None:
            for field in ("price", "quantity", "timestamp_ms", "client_id"):
                if old[field] != row[field]:
                    raise ValueError("immutable fill mismatch")
        self.fills[key] = row
        return deepcopy(row)

    def runner_orders(self):
        return deepcopy(list(self.orders.values()))

    def runner_fills(self):
        return deepcopy(list(self.fills.values()))

    def append_position_snapshot(self, *_args):
        return None

    def append_account_snapshot(self, *_args):
        return None

    def append_reconciliation(self, *_args):
        return None

    def append_comparison(self, value, stage, *_args):
        self.comparisons.append((stage, deepcopy(value)))
        return None

    def list_comparisons(self, *, stage=None, signal_id=None, limit=1000):
        result = [{"stage": item_stage, "payload": deepcopy(value),
                   "signal_id": value.get("signal_id"),
                   "observed_ms": value.get("observed_ms", 0)}
                  for item_stage, value in self.comparisons
                  if (stage is None or item_stage == stage)
                  and (signal_id is None or value.get("signal_id") == signal_id)]
        if stage in {None, "binding"} and self.binding is not None:
            result.append({"stage": "binding", "payload": deepcopy(self.binding),
                           "observed_ms": self.binding.get("observed_ms", 0)})
        return result[-limit:][::-1]

    def abandon_unclaimed_order(self, client, *_args):
        row = self.orders[client]
        if row["submission_attempts"] == 0 and row["status"] == "intent":
            row["status"] = "expired"
            return True
        return False


class Adapter:
    """Scripted normalized exchange facts, with every simulated POST counted."""
    allow_trading = True

    def __init__(self, clock):
        self.clock = clock
        self.orders, self.trades = {}, {}
        self.positions = {}
        self.posts, self.cancels, self.lookups = [], [], []
        self.owned_client_ids = set()
        self.maker_behaviors = ["filled"]
        self.taker_behaviors = ["filled"]
        self.exit_behaviors = ["filled"]
        self.maker_index = self.taker_index = self.exit_index = 0
        self.hedged = self.foreign_position = self.foreign_open = False
        self.unknown_cancellation = False
        self.fee_missing = self.trade_quantity_gap = self.saturated_trades = False
        self.commission_asset = "USDT"
        self.events = []
        self.prices = {PAIR: Decimal("1000"), "ETH/USDT:USDT": Decimal("500"),
                       "TRX/USDT:USDT": Decimal("0.25")}

    @staticmethod
    def step(pair):
        return Decimal("1" if pair == "TRX/USDT:USDT" else "0.001")

    @staticmethod
    def tick(pair):
        return Decimal("0.00001" if pair == "TRX/USDT:USDT" else "0.1")

    def fetch_exchange_info(self):
        return {"rateLimits": [], "symbols": [
            {"symbol": symbol, "contractType": "PERPETUAL", "quoteAsset": "USDT",
             "marginAsset": "USDT", "status": "TRADING", "filters": [
                 {"filterType": "LOT_SIZE", "stepSize": str(self.step(pair)),
                  "minQty": str(self.step(pair)), "maxQty": "10000000"},
                 {"filterType": "MARKET_LOT_SIZE", "stepSize": str(self.step(pair)),
                  "minQty": str(self.step(pair)), "maxQty": "10000000"},
                 {"filterType": "PRICE_FILTER", "tickSize": str(self.tick(pair)),
                  "minPrice": str(self.tick(pair)), "maxPrice": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
            for pair, symbol in SYMBOLS.items()]}

    def validate_order(self, pair, qty, price=None, *, market=False):
        number = Decimal(str(qty))
        if pair not in SYMBOLS or number <= 0 or number % self.step(pair):
            raise DemoSafetyError("invalid quantity")
        if not market and number * Decimal(str(price)) < Decimal("5"):
            raise DemoSafetyError("invalid notional")
        return str(qty), None if price is None else str(price)

    def prepare_limit_order(self, pair, side, qty, price):
        self.validate_order(pair, qty, price)
        return {"quantity": str(qty), "price": str(price)}

    def fetch_book_ticker(self, pair):
        self.clock.sleep(0.01)
        bid, ask = self.prices[pair], self.prices[pair] + self.tick(pair)
        return {"pair": pair, "symbol": SYMBOLS[pair], "bid_price": str(bid),
                "ask_price": str(ask), "bid_qty": "1000", "ask_qty": "1000",
                "timestamp_ms": self.clock.now(), "raw": {"symbol": SYMBOLS[pair],
                    "bidPrice": str(bid), "askPrice": str(ask), "time": self.clock.now()}}

    def fetch_position_mode(self):
        return {"hedged": self.hedged}

    def fetch_balance(self):
        return {"available_balance": "5000", "raw": {"canTrade": True}}

    def fetch_commission_rate(self, pair):
        return {"symbol": SYMBOLS[pair], "makerCommissionRate": "0.00020000",
                "takerCommissionRate": "0.00040000"}

    def fetch_positions(self):
        if self.foreign_position:
            return [{"pair": "DOGEUSDT", "symbol": "DOGEUSDT", "position_side": "BOTH",
                     "signed_qty": "1"}]
        return [{"pair": pair, "symbol": SYMBOLS[pair], "position_side": "BOTH",
                 "signed_qty": str(qty)} for pair, qty in self.positions.items() if qty]

    def fetch_open_orders(self, pair=None):
        if self.foreign_open:
            return [{"pair": "DOGEUSDT", "client_id": "foreign", "raw": {"reduceOnly": False}}]
        return deepcopy([row for row in self.orders.values()
                         if row["status"] not in TERMINAL and (pair is None or row["pair"] == pair)])

    def authorize_owned_client_id(self, client):
        self.owned_client_ids.add(client)

    def _create(self, pair, side, qty, price, client, behavior, *, tif=None, market=False):
        if client in self.orders:
            raise AssertionError("duplicate simulated exchange POST")
        qty, price = Decimal(str(qty)), Decimal(str(price))
        if behavior == "unknown_absent":
            raise DemoSubmissionUnknown("signed-url=never-serialize-this")
        filled = ((qty / 2 // self.step(pair)) * self.step(pair) if behavior == "partial"
                  else Decimal(0) if behavior == "zero" else qty)
        status = "PARTIALLY_FILLED" if behavior == "partial" else "NEW" if behavior == "zero" else "FILLED"
        if tif == "IOC" and behavior in {"partial", "zero"}:
            status = "EXPIRED"
        number = str(len(self.orders) + 1)
        raw = {"symbol": SYMBOLS[pair], "orderId": number, "clientOrderId": client,
               "side": side.upper(), "type": "MARKET" if market else "LIMIT", "status": status,
               "origQty": str(qty), "executedQty": str(filled), "price": "0" if market else str(price),
               "avgPrice": str(price) if filled else "0", "time": self.clock.now(),
               "updateTime": self.clock.now(), "reduceOnly": market, "timeInForce": tif}
        row = {"pair": pair, "side": side, "type": raw["type"].lower(), "status": status,
               "order_id": number, "client_id": client, "qty": str(qty), "filled_qty": str(filled),
               "price": raw["price"], "avg_price": raw["avgPrice"], "created_ms": self.clock.now(),
               "updated_ms": self.clock.now(), "raw": raw}
        self.orders[client] = row
        self.trades[number] = []
        if filled:
            self.positions[pair] = self.positions.get(pair, Decimal(0)) + filled * (1 if side == "buy" else -1)
            maker = tif == "GTX"
            fee = None if self.fee_missing else str(filled * price * Decimal("0.0002" if maker else "0.0004"))
            fill_raw = {"id": number, "orderId": number, "symbol": SYMBOLS[pair], "side": side.upper(),
                        "qty": str(filled), "price": str(price), "quoteQty": str(filled * price),
                        "commission": fee, "commissionAsset": self.commission_asset,
                        "maker": maker, "time": self.clock.now(), "realizedPnl": "0"}
            self.trades[number] = [{"fill_id": number, "order_id": number, "pair": pair,
                "side": side, "qty": str(filled), "price": str(price), "ts": self.clock.now(),
                "is_maker": maker, "commission": fee, "commission_asset": self.commission_asset,
                "realized_pnl": "0", "raw": fill_raw}]
        if behavior == "unknown_filled":
            raise DemoSubmissionUnknown("signed-url=never-serialize-this")
        return deepcopy(row)

    @staticmethod
    def _behavior(values, index):
        return values[min(index, len(values) - 1)]

    def submit_limit_order(self, pair, side, qty, price, client, *, post_only=True, reduce_only=False):
        if post_only is not True or reduce_only is not False:
            raise AssertionError("maker capture must use native GTX entry")
        self.posts.append(("maker", client, pair, side, str(qty), str(price)))
        self.events.append(("maker_post", client))
        behavior = self._behavior(self.maker_behaviors, self.maker_index)
        self.maker_index += 1
        return self._create(pair, side, qty, price, client, behavior, tif="GTX")

    def submit_ioc_entry(self, pair, side, qty, price, client):
        self.posts.append(("taker", client, pair, side, str(qty), str(price)))
        self.events.append(("taker_post", client))
        behavior = self._behavior(self.taker_behaviors, self.taker_index)
        self.taker_index += 1
        return self._create(pair, side, qty, self.prices[pair], client, behavior, tif="IOC")

    def submit_market_order(self, pair, side, qty, client, *, reduce_only=True):
        if reduce_only is not True:
            raise AssertionError("protective exit must be reduce-only")
        self.posts.append(("exit", client, pair, side, str(qty), None))
        self.events.append(("exit_post", client))
        behavior = self._behavior(self.exit_behaviors, self.exit_index)
        self.exit_index += 1
        return self._create(pair, side, qty, self.prices[pair], client, behavior, market=True)

    def fetch_order_by_client_id(self, pair, client):
        self.lookups.append((pair, client))
        row = self.orders.get(client)
        return None if row is None else deepcopy(row)

    def cancel_order(self, pair, order_id=None, *, client_order_id):
        if client_order_id not in self.owned_client_ids:
            raise AssertionError("cannot cancel an unowned identity")
        self.cancels.append(client_order_id)
        self.events.append(("cancel", client_order_id))
        row = self.orders.get(client_order_id)
        if row is None:
            return None
        if self.unknown_cancellation:
            raise DemoSubmissionUnknown("signed-url=never-serialize-this")
        row["status"] = row["raw"]["status"] = "CANCELED"
        row["updated_ms"] = row["raw"]["updateTime"] = self.clock.now()
        return deepcopy(row)

    def fetch_trades(self, pair, *, order_id=None, limit=1000, **_kwargs):
        rows = deepcopy(self.trades.get(str(order_id), []))
        if rows and self.trade_quantity_gap:
            rows[0]["qty"] = rows[0]["raw"]["qty"] = str(Decimal(rows[0]["qty"]) / 2)
        if rows and self.saturated_trades:
            original = rows[0]
            rows = []
            for index in range(1000):
                row = deepcopy(original)
                row["fill_id"] = row["raw"]["id"] = str(index + 100000)
                row["qty"] = row["raw"]["qty"] = str(Decimal(original["qty"]) / 1000)
                rows.append(row)
        return rows


class FeeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ledger = Ledger()
        self.adapter = Adapter(self.clock)

    def run_capture(self, **kwargs):
        options = {"clock_ms": self.clock.now, "sleep": self.clock.sleep,
                   "pairs": (PAIR,), "maker_ttl_seconds": 30,
                   "total_timeout_seconds": 60, "reconciliation_seconds": 2,
                   "max_notional_usdt": "75"}
        options.update(kwargs)
        recovery_only = bool(options.get("recovery_only") or self.ledger.runner_orders())
        options["recovery_only"] = recovery_only
        if not recovery_only and "risk_gate_proof" not in kwargs:
            binding = expected_risk_gate_target_binding(
                "offline-test-account", pairs=options["pairs"],
                max_notional_usdt=options["max_notional_usdt"],
                maker_ttl_seconds=options["maker_ttl_seconds"],
                total_timeout_seconds=options["total_timeout_seconds"],
                reconciliation_seconds=options["reconciliation_seconds"])
            import research_ledger
            options["risk_gate_proof"] = {
                "research_id": "research-1", "event_id": "event-1",
                "risk_segment_status": "closed", "preflight_status": "pass",
                "boundary_hash": "boundary-1", "inputs_hash": "inputs-1",
                "target_binding": binding,
                "target_binding_hash": research_ledger.risk_gate_target_binding_hash(binding),
            }
        from demo_fee_capture import run_fee_capture
        return run_fee_capture(self.adapter, self.ledger, **options)

    def assert_protected_flat(self, report):
        self.assertTrue(report["position_flat"], report["errors"])
        self.assertTrue(report["open_orders_zero"], report["errors"])
        self.assertFalse(report["recovery_required"], report["errors"])
        self.assertEqual(self.adapter.fetch_positions(), [])
        self.assertEqual(self.adapter.fetch_open_orders(), [])

    def test_missing_gate_blocks_before_any_entry_post(self):
        report = self.run_capture(risk_gate_proof=None)
        self.assertFalse(report["success"])
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(self.ledger.runner_orders(), [])

    def test_full_capture_uses_native_gtx_ioc_and_reduce_only_market_exits(self):
        report = self.run_capture()
        self.assertTrue(report["success"], report["errors"])
        self.assert_protected_flat(report)
        self.assertTrue(report["fill_reconciliation_complete"])
        self.assertTrue(report["maker_verified_by_pair"][PAIR])
        self.assertEqual([post[0] for post in self.adapter.posts].count("maker"), 1)
        self.assertEqual([post[0] for post in self.adapter.posts].count("taker"), 1)
        self.assertEqual([post[0] for post in self.adapter.posts].count("exit"), 2)
        for row in self.ledger.runner_orders():
            if row["reduce_only"]:
                self.assertEqual(row["order_type"], "market")
                self.assertIs(row["raw"]["reduceOnly"], True)
            else:
                expected = "GTX" if row["raw"]["timeInForce"] == "GTX" else "IOC"
                self.assertEqual(row["time_in_force"], expected)
                self.assertIs(row["raw"]["reduceOnly"], False)
        for fill in self.ledger.runner_fills():
            rate = Decimal(fill["commission"]) / (Decimal(fill["price"]) * Decimal(fill["quantity"]))
            self.assertEqual(rate, Decimal("0.0002" if fill["is_maker"] else "0.0004"))

    def test_three_pairs_have_independent_actual_fee_samples(self):
        report = self.run_capture(pairs=tuple(SYMBOLS))
        self.assertTrue(report["success"], report["errors"])
        self.assert_protected_flat(report)
        self.assertEqual({fill["pair"] for fill in self.ledger.runner_fills()}, set(SYMBOLS))
        self.assertEqual(report["maker_verified_by_pair"], {pair: True for pair in SYMBOLS})
        for pair in SYMBOLS:
            roles = {fill["is_maker"] for fill in self.ledger.runner_fills() if fill["pair"] == pair}
            self.assertEqual(roles, {True, False})

    def test_unfilled_maker_terminal_cancel_precedes_new_identity_rollover(self):
        self.adapter.maker_behaviors = ["zero", "filled"]
        report = self.run_capture()
        self.assertTrue(report["success"], report["errors"])
        makers = [post for post in self.adapter.posts if post[0] == "maker"]
        self.assertEqual(len(makers), 2)
        self.assertNotEqual(makers[0][1], makers[1][1])
        self.assertEqual(self.ledger.orders[makers[0][1]]["status"], "canceled")
        self.assertEqual(self.ledger.orders[makers[0][1]]["filled_quantity"], "0")
        self.assertLess(self.adapter.events.index(("cancel", makers[0][1])),
                        self.adapter.events.index(("maker_post", makers[1][1])))
        self.assertEqual(len([fill for fill in self.ledger.runner_fills()
                              if fill["client_id"] == makers[0][1]]), 0)
        self.assert_protected_flat(report)

    def test_maker_partial_cancels_before_exiting_actual_quantity_and_does_not_rehang(self):
        self.adapter.maker_behaviors = ["partial", "filled"]
        report = self.run_capture()
        self.assert_protected_flat(report)
        maker_posts = [post for post in self.adapter.posts if post[0] == "maker"]
        self.assertEqual(len(maker_posts), 1)
        maker_id = maker_posts[0][1]
        entry = self.ledger.orders[maker_id]
        self.assertEqual(entry["status"], "canceled")
        filled = Decimal(entry["filled_quantity"])
        self.assertGreater(filled, 0)
        self.assertLess(filled, Decimal(entry["quantity"]))
        maker_exit = next(row for row in self.ledger.runner_orders()
                          if row["reduce_only"] and row["signal_id"] == entry["signal_id"])
        self.assertEqual(Decimal(maker_exit["quantity"]), filled)
        self.assertLess(self.adapter.events.index(("cancel", maker_id)),
                        self.adapter.events.index(("exit_post", maker_exit["client_id"])))
        self.assertTrue(report["maker_verified_by_pair"][PAIR])

    def test_unknown_entry_accepted_is_reconciled_and_never_reposted(self):
        self.adapter.taker_behaviors = ["unknown_filled"]
        report = self.run_capture()
        takers = [post for post in self.adapter.posts if post[0] == "taker"]
        self.assertEqual(len(takers), 1)
        self.assertIn((PAIR, takers[0][1]), self.adapter.lookups)
        self.assertTrue(report["position_flat"], report["errors"])
        self.assertTrue(report["open_orders_zero"], report["errors"])
        before = deepcopy(self.adapter.posts)
        self.run_capture()
        self.assertEqual(self.adapter.posts, before)

    def test_unknown_absent_entry_stays_unresolved_on_replay_and_cannot_rollover(self):
        self.adapter.taker_behaviors = ["unknown_absent"]
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertTrue(report["recovery_required"])
        self.assertEqual(len(self.adapter.posts), 1)
        old = deepcopy(self.adapter.posts)
        replay = self.run_capture()
        self.assertTrue(replay["recovery_required"])
        self.assertEqual(self.adapter.posts, old)
        self.assertEqual(self.ledger.runner_orders()[0]["submission_attempts"], 1)

    def test_unknown_cancellation_never_rehangs_or_claims_flat_completion(self):
        self.adapter.maker_behaviors = ["zero", "filled"]
        self.adapter.unknown_cancellation = True
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertTrue(report["recovery_required"])
        makers = [post for post in self.adapter.posts if post[0] == "maker"]
        self.assertEqual(len(makers), 1)
        self.assertNotIn(self.ledger.orders[makers[0][1]]["status"], {"filled", "canceled", "expired"})
        old = deepcopy(self.adapter.posts)
        self.run_capture()
        self.assertEqual(self.adapter.posts, old)

    def test_unknown_exit_accepted_replay_never_reposts_the_claimed_exit(self):
        self.adapter.exit_behaviors = ["unknown_filled"]
        report = self.run_capture()
        self.assertTrue(report["position_flat"], report["errors"])
        self.assertTrue(report["open_orders_zero"], report["errors"])
        self.assertEqual(len([post for post in self.adapter.posts if post[0] == "exit"]), 1)
        old = deepcopy(self.adapter.posts)
        self.run_capture()
        self.assertEqual(self.adapter.posts, old)

    def test_unknown_absent_exit_reports_risk_and_replay_remains_lookup_only(self):
        self.adapter.exit_behaviors = ["unknown_absent"]
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertTrue(report["recovery_required"])
        self.assertFalse(report["position_flat"])
        old = deepcopy(self.adapter.posts)
        self.run_capture()
        self.assertEqual(self.adapter.posts, old)

    def test_fresh_session_foreign_position_open_order_or_hedge_mode_blocks_all_posts(self):
        for flag in ("foreign_position", "foreign_open", "hedged"):
            with self.subTest(flag=flag):
                self.setUp()
                setattr(self.adapter, flag, True)
                report = self.run_capture()
                self.assertFalse(report["success"])
                self.assertEqual(self.adapter.posts, [])

    def test_completed_replay_reconciles_without_any_new_order(self):
        first = self.run_capture()
        self.assertTrue(first["success"], first["errors"])
        old_posts = deepcopy(self.adapter.posts)
        old_ids = set(self.ledger.orders)
        replay = self.run_capture()
        self.assertTrue(replay["success"], replay["errors"])
        self.assertEqual(self.adapter.posts, old_posts)
        self.assertEqual(set(self.ledger.orders), old_ids)
        self.assert_protected_flat(replay)

    def test_partial_reduce_only_exit_recovery_closes_only_confirmed_remainder(self):
        first = self.run_capture()
        self.assertTrue(first["success"], first["errors"])
        entry = next(row for row in self.ledger.runner_orders()
                     if row["intent_payload"].get("arm") == "taker" and not row["reduce_only"])
        old_exit = next(row for row in self.ledger.runner_orders()
                        if row["signal_id"] == entry["signal_id"] and row["reduce_only"])
        remaining = Decimal("0.003")
        exited = Decimal(entry["filled_quantity"]) - remaining
        ledger_exit = self.ledger.orders[old_exit["client_id"]]
        exchange_exit = self.adapter.orders[old_exit["client_id"]]
        ledger_exit["status"] = "canceled"
        ledger_exit["filled_quantity"] = str(exited)
        exchange_exit["status"] = "CANCELED"
        exchange_exit["filled_qty"] = str(exited)
        exchange_exit["raw"]["status"] = "CANCELED"
        exchange_exit["raw"]["executedQty"] = str(exited)
        exchange_fills = self.adapter.trades[old_exit["exchange_order_id"]]
        exchange_fills[0]["qty"] = exchange_fills[0]["raw"]["qty"] = str(exited)
        exchange_fills[0]["raw"]["quoteQty"] = str(exited * Decimal(exchange_fills[0]["price"]))
        exchange_fills[0]["commission"] = exchange_fills[0]["raw"]["commission"] = str(
            exited * Decimal(exchange_fills[0]["price"]) * Decimal("0.0004"))
        for fill in self.ledger.fills.values():
            if fill["client_id"] == old_exit["client_id"]:
                fill["quantity"] = str(exited)
                fill["qty"] = str(exited)
                fill["raw"]["qty"] = str(exited)
                fill["raw"]["quoteQty"] = str(exited * Decimal(fill["price"]))
                fill["commission"] = fill["raw"]["commission"] = str(
                    exited * Decimal(fill["price"]) * Decimal("0.0004"))
        self.adapter.positions[PAIR] = remaining
        old_posts = deepcopy(self.adapter.posts)

        replay = self.run_capture(recovery_only=True)

        self.assertTrue(replay["success"], replay["errors"])
        self.assert_protected_flat(replay)
        exits = [post for post in self.adapter.posts if post[0] == "exit"]
        self.assertEqual(len(exits), len([post for post in old_posts if post[0] == "exit"]) + 1)
        self.assertEqual(Decimal(exits[-1][4]), remaining)
        self.assertEqual(exits[-1][1], self.adapter.posts[-1][1])

        # Once the remainder is durably closed, a later recovery invocation is
        # reconciliation-only and must not create another protective identity.
        posts_after_recovery = deepcopy(self.adapter.posts)
        completed_replay = self.run_capture(recovery_only=True)
        self.assertTrue(completed_replay["success"], completed_replay["errors"])
        self.assert_protected_flat(completed_replay)
        self.assertEqual(self.adapter.posts, posts_after_recovery)

    def test_missing_actual_commission_does_not_verify_a_maker_rate(self):
        self.adapter.fee_missing = True
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertFalse(report["maker_verified_by_pair"][PAIR])
        self.assertTrue(report["position_flat"])
        self.assertFalse(any(fill.get("commission") is not None for fill in self.ledger.runner_fills()))

    def test_trade_quantity_gap_does_not_verify_fees_or_report_success(self):
        self.adapter.trade_quantity_gap = True
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertFalse(report["fill_reconciliation_complete"])
        self.assertFalse(report["maker_verified_by_pair"][PAIR])
        self.assertTrue(report["position_flat"])

    def test_saturated_trade_page_does_not_silently_verify_fees(self):
        self.adapter.saturated_trades = True
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertFalse(report["fill_reconciliation_complete"])
        self.assertFalse(report["maker_verified_by_pair"][PAIR])
        self.assertTrue(report["position_flat"])

    def test_non_usdt_commission_preserves_asset_and_cannot_verify_usdt_rate(self):
        self.adapter.commission_asset = "BNB"
        report = self.run_capture()
        self.assertFalse(report["maker_verified_by_pair"][PAIR])
        self.assertTrue(report["position_flat"])
        self.assertTrue(self.ledger.runner_fills())
        self.assertEqual({fill["commission_asset"] for fill in self.ledger.runner_fills()}, {"BNB"})
        self.assertTrue(all(sample["rate"] is None and sample["matches_quote"] is None
                            for sample in report["samples"]))

    def test_bound_session_policy_changes_are_rejected_before_another_post(self):
        report = self.run_capture()
        self.assertTrue(report["success"], report["errors"])
        old_posts, old_binding = deepcopy(self.adapter.posts), deepcopy(self.ledger.binding)
        for change in ({"max_notional_usdt": "70"}, {"maker_ttl_seconds": 60},
                       {"pairs": ("ETH/USDT:USDT",)}, {"total_timeout_seconds": 120}):
            with self.subTest(change=change):
                changed = self.run_capture(**change)
                self.assertFalse(changed["success"])
                self.assertEqual(self.adapter.posts, old_posts)
                self.assertEqual(self.ledger.binding, old_binding)

    def test_failed_binding_reserve_or_claim_persistence_never_permits_a_post(self):
        for flag in ("fail_binding", "fail_reserve", "fail_claim"):
            with self.subTest(flag=flag):
                self.setUp()
                setattr(self.ledger, flag, True)
                report = self.run_capture()
                self.assertFalse(report["success"])
                self.assertEqual(self.adapter.posts, [])
                self.assertNotIn("never-serialize-this", str(report))

    def test_resumed_unclaimed_entry_is_abandoned_without_first_post(self):
        self.ledger.fail_claim = True
        first = self.run_capture()
        self.assertFalse(first["success"])
        self.assertEqual(self.adapter.posts, [])
        self.ledger.fail_claim = False
        recovered = self.run_capture()
        self.assertFalse(recovered["success"])
        self.assertTrue(recovered["recovery_only"])
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual({row["status"] for row in self.ledger.orders.values()}, {"expired"})

    def test_fill_persistence_failure_keeps_protection_but_cannot_report_success(self):
        self.ledger.fail_fill = True
        report = self.run_capture()
        self.assertFalse(report["success"])
        self.assertFalse(report["persistence_complete"])
        self.assertTrue(report["position_flat"])
        self.assertTrue(report["open_orders_zero"])
        self.assertFalse(report["maker_verified_by_pair"][PAIR])
        self.assertEqual(self.adapter.fetch_positions(), [])
        self.assertNotIn("never-serialize-this", str(report))
