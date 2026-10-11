"""Offline safety and attribution tests; no credentials or exchange calls."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import unittest

from binance_demo_adapter import DemoExchangeRejected, DemoSafetyError, DemoSubmissionUnknown
from demo_second_level_runner import (
    SecondLevelConfig, build_entry_plan, recover_second_level_session,
    run_second_level_session, _Session,
)
from demo_second_level_signals import POLICY_HASH, frozen_policy

PAIR = "BTC/USDT:USDT"


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

    def __init__(self, arm, session="offline"):
        self.virtual_account_key = "second_level_" + session + "_" + arm
        self.orders, self.fills, self.comparisons = {}, {}, []
        self.binding = None
        self.fail_claim_once = False
        self.fail_projection_once = False
        self.fail_result = False
        self.fail_fill = False

    def bind_account(self, value):
        if self.binding and self.binding != value:
            raise ValueError("binding mismatch")
        self.binding = deepcopy(value)

    def reserve_order(self, value):
        client = value["client_id"]
        if client in self.orders:
            old = self.orders[client]
            for field in ("quantity", "price", "intent_payload", "signal_ms", "created_ms"):
                if str(old[field]) != str(value[field]):
                    raise ValueError("immutable intent mismatch")
            return deepcopy(old), False
        row = {**deepcopy(value), "status": "intent", "submission_attempts": 0,
            "filled_quantity": "0", "exchange_order_id": None, "raw": {}}
        self.orders[client] = row
        return deepcopy(row), True

    def claim_submission(self, client, stamp):
        if self.fail_claim_once:
            self.fail_claim_once = False
            raise RuntimeError("secret=never-serialize-this")
        row = self.orders[client]
        if row["submission_attempts"] or row["status"] != "intent":
            return False
        row.update(status="submitting", submission_attempts=1, submission_claimed_ms=stamp)
        return True

    def record_order_observation(self, client, value):
        if self.fail_projection_once:
            self.fail_projection_once = False
            raise RuntimeError("secret=never-serialize-this")
        row = self.orders[client]
        status = {"NEW": "open", "PARTIALLY_FILLED": "partially_filled"}.get(value["status"], value["status"].lower())
        row.update(status=status, raw=deepcopy(value["raw"]), filled_quantity=value["filled_qty"],
                   exchange_order_id=value.get("order_id"), exchange_updated_ms=value.get("updated_ms", 0))

    def record_fill(self, client, value):
        if self.fail_fill:
            raise RuntimeError("secret=never-serialize-this")
        self.fills[value["fill_id"]] = {**deepcopy(value), "client_id": client,
            "exchange_trade_id": value["fill_id"], "exchange_order_id": value["order_id"],
            "quantity": value["qty"], "timestamp_ms": value["ts"]}

    def mark_submission_unknown(self, client, *_args):
        self.orders[client]["status"] = "unknown"

    def append_reconciliation(self, *_args):
        pass

    def append_account_snapshot(self, *_args):
        pass

    def append_position_snapshot(self, *_args):
        pass

    def append_comparison(self, value, stage, _stamp):
        if self.fail_result and stage == "second_level_session_result":
            raise RuntimeError("secret=never-serialize-this")
        self.comparisons.append((stage, deepcopy(value)))

    def abandon_unclaimed_order(self, client, *_args):
        row = self.orders[client]
        if row["submission_attempts"] == 0:
            row["status"] = "expired"
            return True
        return False

    def runner_orders(self):
        return deepcopy(list(self.orders.values()))

    def runner_fills(self):
        return deepcopy(list(self.fills.values()))


class Source:
    def __init__(self, clock, count=8):
        self.clock, self.count, self.index = clock, count, 0
        self.last_decision = {}

    def latest_signal(self, now):
        if self.index >= self.count:
            self.last_decision = {"reason": "below_frozen_threshold"}
            return None
        result = signal(now, self.index)
        self.index += 1
        self.last_decision = {"reason": "obi_entry_signal", "signal": result}
        return result

    @property
    def state(self):
        return {"eligible_signal_count": self.index}


def signal(stamp, index=0, side="buy"):
    return {"signal_id": "hft-offline-" + str(index), "pair": PAIR, "side": side,
        "input_fingerprint": hashlib.sha256(str(index).encode()).hexdigest(),
        "policy_hash": POLICY_HASH, "rule_hash": POLICY_HASH,
        "decision_ms": stamp, "source_event_ms": stamp - 100,
        "database_received_at_ms": stamp - 50, "original_received_at_ms": stamp - 80,
        "obi_5": 0.95 if side == "buy" else -0.95, "eligible_signal_index": index,
        "source": "real_collected_orderbooks", "hold_ms": 10000,
        **frozen_policy()["arm_schedule"][index % 8]}


class Adapter:
    allow_trading = True

    def __init__(self, clock):
        self.clock = clock
        self.orders, self.trades = {}, {}
        self.position = Decimal(0)
        self.posts, self.cancels, self.calls = [], [], []
        self.maker_unfilled = False
        self.entry_partial = False
        self.exit_partial_once = False
        self.unknown_entry = False
        self.absent_entry = False
        self.unknown_exit = False
        self.malformed_echo = False
        self.reject_entry = False
        self.stale_position_polls = 0
        self.quote_delay = 0.01
        self.quote_stale = False
        self.fee_unknown = False
        self.foreign_open = False
        self.hedged = False
        self.mark = Decimal("1000")
        self.minimum_notional = "5"
        self.step = "0.001"
        self.market_step = "0.001"
        self.market_min = "0.001"
        self.returned_unknown_exit_qty = None

    def fetch_exchange_info(self):
        return {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400}],
            "symbols": [{"symbol": "BTCUSDT", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": self.step, "minQty": self.step, "maxQty": "1000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": self.market_step, "minQty": self.market_min, "maxQty": "1000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
                {"filterType": "MIN_NOTIONAL", "notional": self.minimum_notional}]}]}

    def validate_order(self, pair, qty, price=None, *, market=False):
        assert pair == PAIR
        number = Decimal(str(qty))
        step = Decimal(self.market_step if market else self.step)
        if number <= 0 or number % step or (market and number < Decimal(self.market_min)):
            raise DemoSafetyError("invalid quantity")
        if not market and number * Decimal(str(price)) < Decimal(self.minimum_notional):
            raise DemoSafetyError("invalid notional")
        return str(qty), None if market else str(price)

    def prepare_limit_order(self, pair, side, qty, price):
        self.validate_order(pair, qty, price)
        return {"quantity": str(qty), "price": str(price)}

    def fetch_position_mode(self):
        return {"hedged": self.hedged}

    def fetch_balance(self):
        return {"raw": {"canTrade": True}, "available_balance": "5000"}

    def fetch_positions(self):
        self.calls.append((self.clock.mono(), 5))
        if self.position and self.stale_position_polls:
            self.stale_position_polls -= 1
            return []
        return [] if not self.position else [{"pair": PAIR, "symbol": "BTCUSDT",
            "position_side": "BOTH", "signed_qty": str(self.position)}]

    def fetch_open_orders(self, pair=None):
        self.calls.append((self.clock.mono(), 1 if pair else 40))
        if self.foreign_open:
            return [{"client_id": "foreign"}]
        return deepcopy([row for row in self.orders.values() if row["status"] in {"NEW", "PARTIALLY_FILLED"}])

    def fetch_book_ticker(self, pair):
        assert pair == PAIR
        self.clock.sleep(self.quote_delay)
        stamp = self.clock.now() - (10000 if self.quote_stale else 0)
        return {"pair": pair, "symbol": "BTCUSDT", "bid_price": str(self.mark),
            "ask_price": str(self.mark + Decimal("0.1")), "bid_qty": "10", "ask_qty": "10",
            "timestamp_ms": stamp, "raw": {"symbol": "BTCUSDT", "bidPrice": str(self.mark),
                "askPrice": str(self.mark + Decimal("0.1")), "time": stamp}}

    def authorize_owned_client_id(self, _client):
        pass

    def _order(self, pair, side, qty, price, client, filled, status, tif, market=False):
        number = str(len(self.orders) + 1)
        qty, price, filled = Decimal(str(qty)), Decimal(str(price)), Decimal(str(filled))
        raw = {"symbol": "BTCUSDT", "orderId": number, "clientOrderId": client,
            "side": side.upper(), "type": "MARKET" if market else "LIMIT", "status": status,
            "origQty": str(qty), "executedQty": str(filled), "price": "0" if market else str(price),
            "avgPrice": str(price), "updateTime": self.clock.now(), "time": self.clock.now(),
            "timeInForce": tif, "reduceOnly": market}
        row = {"pair": pair, "side": side, "order_id": number, "client_id": client,
            "qty": str(qty), "filled_qty": str(filled), "price": raw["price"],
            "avg_price": str(price), "status": status, "updated_ms": self.clock.now(), "raw": raw}
        self.orders[client] = row
        if filled:
            self.position += filled if side == "buy" else -filled
            maker = tif == "GTX"
            commission = None if self.fee_unknown else str(filled * price * Decimal("0.0002" if maker else "0.0004"))
            raw_fill = {"id": number, "orderId": number, "symbol": "BTCUSDT", "side": side.upper(),
                "qty": str(filled), "price": str(price), "quoteQty": str(filled * price),
                "time": self.clock.now(), "commission": commission, "commissionAsset": "USDT",
                "maker": maker, "realizedPnl": "-0.01" if market else "0"}
            self.trades[number] = [{"fill_id": number, "order_id": number, "pair": pair, "side": side,
                "qty": str(filled), "price": str(price), "ts": self.clock.now(), "is_maker": maker,
                "commission": commission, "commission_asset": "USDT", "realized_pnl": raw_fill["realizedPnl"], "raw": raw_fill}]
        return deepcopy(row)

    def _entry(self, pair, side, qty, price, client, tif):
        self.posts.append(("entry", client, str(qty), self.clock.mono()))
        assert self.position == 0, "overlapping entry"
        if self.reject_entry:
            raise DemoExchangeRejected("InvalidOrder", exchange_code=-5022)
        if self.absent_entry:
            raise DemoSubmissionUnknown("timeout")
        quantity = Decimal(str(qty))
        filled = Decimal("0.001") if self.entry_partial else quantity
        status = "EXPIRED" if self.entry_partial and tif == "IOC" else "PARTIALLY_FILLED" if self.entry_partial else "FILLED"
        if self.maker_unfilled and tif == "GTX":
            filled, status = Decimal(0), "NEW"
        result = self._order(pair, side, quantity, price, client, filled, status, tif)
        if self.unknown_entry:
            raise DemoSubmissionUnknown("timeout")
        if self.malformed_echo:
            result["raw"].pop("reduceOnly")
        return result

    def submit_limit_order(self, pair, side, qty, price, client, *, post_only):
        assert post_only is True
        return self._entry(pair, side, qty, price, client, "GTX")

    def submit_ioc_entry(self, pair, side, qty, price, client):
        return self._entry(pair, side, qty, price, client, "IOC")

    def submit_market_order(self, pair, side, qty, client, *, reduce_only):
        assert reduce_only is True
        self.posts.append(("exit", client, str(qty), self.clock.mono()))
        quantity = Decimal(str(qty))
        filled = Decimal("0.001") if self.exit_partial_once else quantity
        self.exit_partial_once = False
        result = self._order(pair, side, quantity, self.mark, client, filled,
            "EXPIRED" if filled < quantity else "FILLED", "GTC", market=True)
        if self.unknown_exit:
            self.returned_unknown_exit_qty = str(qty)
            raise DemoSubmissionUnknown("timeout")
        return result

    def fetch_order_by_client_id(self, pair, client):
        return deepcopy(self.orders.get(client))

    def cancel_order(self, pair, *, client_order_id):
        self.cancels.append((client_order_id, self.clock.mono()))
        row = self.orders.get(client_order_id)
        if row:
            row["status"] = row["raw"]["status"] = "CANCELED"
            row["updated_ms"] = row["raw"]["updateTime"] = self.clock.now()
        return deepcopy(row)

    def fetch_trades(self, pair, *, order_id):
        return deepcopy(self.trades.get(str(order_id), []))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.adapter = Adapter(self.clock)
        self.ledgers = {arm: Ledger(arm) for arm in ("maker", "taker")}
        self.config = SecondLevelConfig("offline", max_signals=1, max_duration_seconds=15, reconciliation_seconds=3)

    def run_session(self, source=None, config=None):
        return run_second_level_session(self.adapter, self.ledgers, source or Source(self.clock),
            config=config or self.config, clock_ms=self.clock.now, sleep=self.clock.sleep, monotonic=self.clock.mono)

    def recover(self):
        return recover_second_level_session(self.adapter, self.ledgers, config=self.config,
            clock_ms=self.clock.now, sleep=self.clock.sleep, monotonic=self.clock.mono)

    def seed(self, *, index=0, exit_intent=False, exit_claimed=False, unclaimed_entry=False):
        session = _Session(self.adapter, self.ledgers, None, self.config,
                           self.clock.now, self.clock.sleep, self.clock.mono)
        session.initialize()
        value = signal(self.clock.now(), index)
        arm = value["arm"]
        quote = self.adapter.fetch_book_ticker(PAIR)
        plan = build_entry_plan(self.adapter, PAIR, value["side"], arm, value.get("maker_distance_bp"), quote, session.metadata)
        entry = session.frozen_intent(arm, value, plan, "entry")
        self.ledgers[arm].reserve_order(entry)
        if unclaimed_entry:
            return arm, entry, None
        self.ledgers[arm].claim_submission(entry["client_id"], self.clock.now())
        method = self.adapter.submit_limit_order if arm == "maker" else self.adapter.submit_ioc_entry
        args = (PAIR, "buy", entry["quantity"], entry["price"], entry["client_id"])
        observation = method(*args, post_only=True) if arm == "maker" else method(*args)
        self.ledgers[arm].record_order_observation(entry["client_id"], observation)
        exit_row = None
        if exit_intent:
            exit_row = session.frozen_intent(arm, value, plan, "exit", entry["quantity"])
            self.ledgers[arm].reserve_order(exit_row)
            if exit_claimed:
                self.ledgers[arm].claim_submission(exit_row["client_id"], self.clock.now())
        return arm, entry, exit_row

    def test_real_fill_fees_and_native_round_trip(self):
        result = self.run_session()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.adapter.position, 0)
        cycle = result["cycles"][0]
        self.assertTrue(cycle["maker_is_mixed_execution"])
        self.assertEqual(cycle["round_trips"][0]["maker_fills"], 1)
        self.assertEqual(cycle["round_trips"][0]["taker_fills"], 1)
        self.assertLess(Decimal(cycle["round_trips"][0]["actual_net_trading_pnl_usdt"]), 0)
        self.assertIn("commissionAsset", cycle["fills"][0])
        self.assertEqual(cycle["input_fingerprint"], cycle["paper"]["input_fingerprint"])
        self.assertEqual(cycle["paper"]["status"], "reference_complete")
        self.assertFalse(cycle["paper"]["actual_fill"])

    def test_two_arms_are_sequential_preassigned(self):
        config = SecondLevelConfig("offline", max_signals=2, max_duration_seconds=100, reconciliation_seconds=3)
        result = self.run_session(config=config)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual([row["arm"] for row in result["cycles"]], ["maker", "taker"])
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit", "entry", "exit"])
        self.assertEqual(result["cycles"][1]["orders"][0]["timeInForce"], "IOC")

    def test_no_signal_reports_finite_zero_sample(self):
        result = self.run_session(Source(self.clock, 0))
        self.assertTrue(result["success"])
        self.assertEqual(result["signal_count"], 0)
        self.assertEqual(self.adapter.posts, [])
        self.assertGreater(result["source_reason_counts"]["below_frozen_threshold"], 0)
        self.assertFalse(result["live_profitability_verified"])

    def test_foreign_account_risk_blocks_all_posts(self):
        self.adapter.foreign_open = True
        result = self.run_session()
        self.assertFalse(result["success"])
        self.assertEqual(self.adapter.posts, [])
        self.assertTrue(result["requires_operator_recovery"])

    def test_maker_unfilled_cancels_after_claim_ttl_and_no_exit(self):
        self.adapter.maker_unfilled = True
        result = self.run_session()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry"])
        self.assertEqual(result["cycles"][0]["entry_terminal_status"], "canceled")
        self.assertGreaterEqual(self.adapter.cancels[0][1] - self.adapter.posts[0][3], 3)
        self.assertTrue(result["cycles"][0]["entry_exchange_accepted"])

    def test_partial_maker_cancel_precedes_actual_size_exit(self):
        self.adapter.entry_partial = True
        result = self.run_session()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(result["cycles"][0]["entry_filled_quantity"], "0.001")
        self.assertEqual(self.adapter.posts[1][2], "0.001")
        self.assertLessEqual(self.adapter.cancels[0][1], self.adapter.posts[1][3])

    def test_partial_ioc_exits_actual_quantity(self):
        self.adapter.entry_partial = True
        source = Source(self.clock)
        source.index = 1
        result = self.run_session(source)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(result["cycles"][0]["arm"], "taker")
        self.assertEqual(result["cycles"][0]["entry_terminal_status"], "expired")
        self.assertEqual(self.adapter.posts[1][2], "0.001")

    def test_unknown_entry_queries_then_protects_without_retry(self):
        self.adapter.unknown_entry = True
        result = self.run_session()
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit"])
        self.assertEqual(self.adapter.position, 0)
        self.assertTrue(result["entries_halted_after_error"])
        self.assertFalse(result["requires_operator_recovery"])

    def test_absent_claim_is_never_reposted_on_recovery(self):
        self.adapter.absent_entry = True
        result = self.run_session()
        self.assertTrue(result["requires_operator_recovery"])
        before = len(self.adapter.posts)
        recovered = self.recover()
        self.assertEqual(len(self.adapter.posts), before)
        self.assertTrue(recovered["requires_operator_recovery"])

    def test_response_validation_error_is_unknown_not_definite_rejected(self):
        self.adapter.malformed_echo = True
        result = self.run_session()
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit"])
        entry = next(row for row in self.ledgers["maker"].orders.values() if not row["reduce_only"])
        self.assertEqual(entry["status"], "filled")
        self.assertTrue(result["entries_halted_after_error"])

    def test_lagging_position_cannot_skip_exit(self):
        self.adapter.stale_position_polls = 1
        result = self.run_session()
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit"])
        self.assertEqual(self.adapter.position, 0)
        self.assertTrue(result["success"], result["errors"])

    def test_unclaimed_exit_recovery_uses_original_immutable_intent(self):
        arm, _entry, old_exit = self.seed(exit_intent=True)
        frozen = deepcopy(old_exit)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(self.adapter.posts[-1][1], old_exit["client_id"])
        self.assertEqual(self.adapter.posts[-1][2], frozen["quantity"])
        for field in ("quantity", "price", "created_ms", "intent_payload"):
            self.assertEqual(self.ledgers[arm].orders[old_exit["client_id"]][field], frozen[field])

    def test_claimed_exit_is_get_only_and_original_quantity_stays(self):
        arm, _entry, old_exit = self.seed(exit_intent=True, exit_claimed=True)
        # Crash after exchange exit execution before local projection.
        self.adapter.submit_market_order(PAIR, "sell", old_exit["quantity"], old_exit["client_id"], reduce_only=True)
        posts = len(self.adapter.posts)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(len(self.adapter.posts), posts)
        self.assertEqual(self.ledgers[arm].orders[old_exit["client_id"]]["quantity"], old_exit["quantity"])

    def test_terminal_partial_exit_uses_new_claimed_remainder_not_repost(self):
        self.adapter.exit_partial_once = True
        result = self.run_session()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit", "exit"])
        self.assertEqual(len({row[1] for row in self.adapter.posts}), 3)
        self.assertEqual(self.adapter.position, 0)

    def test_fee_missing_is_unknown_success_false_and_position_flat(self):
        self.adapter.fee_unknown = True
        result = self.run_session()
        self.assertFalse(result["success"])
        self.assertTrue(result["position_flat"])
        self.assertIsNone(result["cycles"][0]["round_trips"][0]["actual_net_trading_pnl_usdt"])
        self.assertFalse(result["cycles"][0]["fees"]["commission_complete"])

    def test_closed_cycle_missing_fills_is_reconciled_on_recovery(self):
        first = self.run_session()
        self.assertTrue(first["success"])
        for ledger in self.ledgers.values():
            ledger.fills.clear()
        posts = len(self.adapter.posts)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(len(self.adapter.posts), posts)
        self.assertEqual(len(self.ledgers["maker"].fills), 2)
        self.assertEqual(len(result["cycles"]), 1)

    def test_mixed_recovery_protects_unresolved_then_repairs_previous_closed_fills(self):
        self.assertTrue(self.run_session()["success"])
        self.ledgers["maker"].fills.clear()
        self.seed(index=1)
        before = len(self.adapter.posts)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(len(self.adapter.posts), before + 1)
        self.assertEqual([cycle["arm"] for cycle in result["cycles"]], ["taker", "maker"])
        self.assertEqual(len(self.ledgers["maker"].fills), 2)
        self.assertEqual(len(self.ledgers["taker"].fills), 2)

    def test_mixed_unclaimed_recovery_also_repairs_previous_closed_fills(self):
        self.assertTrue(self.run_session()["success"])
        self.ledgers["maker"].fills.clear()
        arm, entry, _ = self.seed(index=1, unclaimed_entry=True)
        before = len(self.adapter.posts)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(len(self.adapter.posts), before)
        self.assertEqual(self.ledgers[arm].orders[entry["client_id"]]["status"], "expired")
        self.assertEqual(len(self.ledgers["maker"].fills), 2)
        self.assertEqual(result["cycles"][0]["arm"], "maker")

    def test_optional_telemetry_budget_cannot_delay_protective_exit(self):
        session = _Session(self.adapter, self.ledgers, None, self.config,
                           self.clock.now, self.clock.sleep, self.clock.mono)
        for _ in range(56):
            session.call(lambda: None, weight=5)
        started = self.clock.mono()
        with self.assertRaises(DemoSafetyError):
            session.call(lambda: self.fail("optional quote must not run"), weight=2,
                         wait_deadline=started + 10)
        self.assertEqual(self.clock.mono() - started, 10)
        session.call(lambda: None, weight=4, protective=True, order_post=True)
        self.assertEqual(self.clock.mono() - started, 10)

    def test_rejected_no_id_entry_recovers_without_post(self):
        self.adapter.reject_entry = True
        self.run_session()
        posts = len(self.adapter.posts)
        result = self.recover()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(len(self.adapter.posts), posts)
        self.assertEqual(result["cycles"][0]["entry_terminal_status"], "rejected")

    def test_post_result_persistence_error_cannot_leave_success_true(self):
        self.ledgers["taker"].fail_result = True
        result = self.run_session()
        self.assertFalse(result["success"])
        self.assertFalse(result["persistence_complete"])
        self.assertNotIn("never-serialize", json.dumps(result))

    def test_delayed_quote_expires_signal_before_post(self):
        self.adapter.quote_delay = 6
        result = self.run_session()
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(result["source_reason_counts"]["signal_expired_during_preflight"], 1)

    def test_stale_demo_quote_never_enters(self):
        self.adapter.quote_stale = True
        result = self.run_session()
        self.assertEqual(self.adapter.posts, [])
        self.assertFalse(result["success"])

    def test_plan_preserves_notional_cap_and_partial_exit_grid(self):
        self.adapter.minimum_notional = "100"
        result = self.run_session()
        self.assertEqual(self.adapter.posts, [])
        self.assertGreater(result["source_reason_counts"]["sizing_or_quote_guard"], 0)
        self.adapter.minimum_notional = "5"
        self.adapter.market_step = "0.002"
        result = self.run_session()
        self.assertEqual(self.adapter.posts, [])

    def test_invalid_namespace_cannot_trigger_recovery_or_network(self):
        self.ledgers["maker"].virtual_account_key = "demo_default"
        result = self.run_session()
        self.assertFalse(result["success"])
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.ledgers["maker"].comparisons, [])

    def test_budget_throttles_all_calls_with_protective_reserve(self):
        session = _Session(self.adapter, self.ledgers, None, self.config, self.clock.now, self.clock.sleep, self.clock.mono)
        marks = []
        for _ in range(70):
            session.call(lambda: marks.append(self.clock.mono()), weight=5)
        for stamp in marks:
            self.assertLessEqual(sum(stamp - 60 < value <= stamp for value in marks) * 5, 280)
        before = self.clock.mono()
        session.call(lambda: None, weight=4, protective=True, order_post=True)
        self.assertEqual(self.clock.mono(), before)

    def test_wall_clock_regression_does_not_extend_monotonic_hold(self):
        method = self.adapter.submit_limit_order
        def regressed(*args, **kwargs):
            self.clock.value -= 600_000
            return method(*args, **kwargs)
        self.adapter.submit_limit_order = regressed
        result = self.run_session()
        self.assertEqual(self.adapter.position, 0)
        self.assertLess(self.adapter.posts[1][3] - self.adapter.posts[0][3], 12)
        self.assertEqual(result["cycles"][0]["paper"]["status"], "reference_unavailable")

    def test_ack_latency_counts_toward_ttl(self):
        self.adapter.maker_unfilled = True
        method = self.adapter.submit_limit_order
        def delayed(*args, **kwargs):
            self.clock.sleep(4)
            return method(*args, **kwargs)
        self.adapter.submit_limit_order = delayed
        result = self.run_session()
        self.assertTrue(result["success"], result["errors"])
        self.assertLessEqual(self.adapter.cancels[0][1] - self.adapter.posts[0][3], 0.01)

    def test_slow_reservation_guard_stops_claimed_entry_before_exchange_post(self):
        ledger = self.ledgers["maker"]
        reserve = ledger.reserve_order
        def delayed(intent):
            value = reserve(intent)
            self.clock.sleep(6)
            return value
        ledger.reserve_order = delayed
        result = self.run_session()
        self.assertEqual(self.adapter.posts, [])
        self.assertTrue(result["entries_halted_after_error"])
        self.assertEqual(next(iter(ledger.orders.values()))["status"], "rejected")
        self.assertFalse(result["cycles"][0]["entry_exchange_accepted"])

    def test_unclaimed_entry_abandoned_on_recovery_stays_unsubmitted(self):
        session = _Session(self.adapter, self.ledgers, None, self.config,
                           self.clock.now, self.clock.sleep, self.clock.mono)
        session.initialize()
        value = signal(self.clock.now())
        plan = build_entry_plan(self.adapter, PAIR, "buy", "maker", 0,
                                self.adapter.fetch_book_ticker(PAIR), session.metadata)
        entry = session.frozen_intent("maker", value, plan, "entry")
        self.ledgers["maker"].reserve_order(entry)
        first = self.recover()
        self.assertTrue(first["success"], first["errors"])
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(self.ledgers["maker"].orders[entry["client_id"]]["status"], "expired")
        second = self.recover()
        self.assertTrue(second["success"], second["errors"])
        self.assertEqual(self.adapter.posts, [])

    def test_persisted_http_time_fields_survive_real_demo_payload_scrubber(self):
        from demo_store import _payload
        result = self.run_session()
        ledger = self.ledgers["maker"]
        timing = next(value for stage, value in ledger.comparisons if stage == "submission_timing")
        saved = _payload(timing)
        self.assertIn("local_http_started_ms", saved)
        self.assertIn("HTTP_elapsed_monotonic_ms", saved)
        cycle = _payload({"cycle": result["cycles"][0]})["cycle"]
        self.assertIn("local_http_started_ms", cycle["paper"]["entry_quote"])
        self.assertIn("local_http_started_ms", next(iter(ledger.orders.values()))["intent_payload"]["plan"]["demo_quote"])
        self.assertIn("frozen_entry_quantity", cycle)

    def test_short_cycle_closes_with_reduce_only_buy(self):
        source = Source(self.clock)
        original = source.latest_signal
        def short(now):
            value = original(now)
            if value:
                value.update(side="sell", obi_5=-0.95)
            return value
        source.latest_signal = short
        result = self.run_session(source)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual(result["cycles"][0]["orders"][1]["side"], "BUY")
        self.assertTrue(result["cycles"][0]["orders"][1]["reduceOnly"])

    def test_after_projection_failure_owned_position_still_closes(self):
        self.ledgers["maker"].fail_projection_once = True
        result = self.run_session()
        self.assertEqual(self.adapter.position, 0)
        self.assertEqual([row[0] for row in self.adapter.posts], ["entry", "exit"])
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
