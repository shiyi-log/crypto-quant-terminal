from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from demo_second_level_signals import (
    POLICY_HASH,
    FrozenSecondLevelSignalSource,
    SecondLevelPaperControl,
    frozen_policy,
    latest_closed_snapshot,
)

PAIR = "BTC/USDT:USDT"
NOW = 1_700_000_000_000


def book(timestamp=NOW - 100, *, bid_qty=39, ask_qty=1, sequence=10, received=None):
    return {
        "timestamp": timestamp, "last_update_id": str(sequence),
        "bids": [[str(100 - .01 * (level + 1)), str(bid_qty)] for level in range(20)],
        "asks": [[str(100 + .01 * (level + 1)), str(ask_qty)] for level in range(20)],
        "received_at": timestamp + 10 if received is None else received,
        "extras": {"received_timestamp": timestamp + 5},
    }


class MemoryConnection:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.store.queries.append((sql, params))
        return self

    def fetchone(self):
        return deepcopy(self.store.row)


class MemoryStore:
    def __init__(self, row):
        self.row, self.queries = row, []

    def _connection(self):
        return MemoryConnection(self)


def observation(timestamp=NOW - 100, *, now=NOW, **kwargs):
    return latest_closed_snapshot(MemoryStore(book(timestamp, **kwargs)), PAIR, now)


def signal(*, arm="maker", distance=0):
    source = FrozenSecondLevelSignalSource()
    result = source.observe(observation())
    value = result["signal"]
    value["arm"], value["maker_distance_bp"] = arm, distance
    return value


def quote(*, bid="100", ask="100.02", timestamp=NOW + 1, pair=PAIR):
    raw = {"symbol": "BTCUSDT", "bidPrice": bid, "askPrice": ask}
    if timestamp is not None:
        raw["time"] = timestamp
    return {"pair": pair, "bid_price": bid, "ask_price": ask,
            "timestamp_ms": timestamp, "raw": raw}


class LatestSnapshotTests(unittest.TestCase):
    def test_uses_latest_snapshot_index_and_preserves_real_timestamp_types(self):
        row = book()
        row["received_at"] = datetime.fromtimestamp((NOW - 90) / 1_000, timezone.utc)
        row["received_at"] += timedelta(microseconds=125)
        store = MemoryStore(row)
        result = latest_closed_snapshot(store, PAIR, NOW)
        self.assertEqual(result["quality"], "valid")
        self.assertEqual(result["source_event_ms"], NOW - 100)
        self.assertAlmostEqual(result["database_received_at_ms"], NOW - 89.875, places=3)
        self.assertEqual(result["original_received_at_ms"], NOW - 95)
        self.assertEqual(result["decision_ms"], NOW)
        self.assertAlmostEqual(result["features"]["obi_5"], .95)
        self.assertEqual(result["source_best_bid"], "99.99")
        self.assertEqual(len(result["raw_snapshot_hash"]), 64)
        sql, params = store.queries[0]
        self.assertIn("timestamp <= %s", sql)
        self.assertIn("received_at <= to_timestamp", sql)
        self.assertIn("length(last_update_id) DESC,last_update_id DESC LIMIT 1", sql)
        self.assertEqual(params, ("binance", "futures", PAIR, NOW, NOW))

    def test_stale_snapshot_not_resurrected_by_fresh_database_receipt(self):
        result = observation(NOW - 5_001, received=NOW - 1)
        self.assertEqual(result["reason"], "stale_snapshot")
        self.assertEqual(result["quality"], "unknown")

    def test_future_event_database_receipt_and_original_are_rejected_individually(self):
        for field in ("timestamp", "received_at", "original"):
            with self.subTest(field=field):
                row = book()
                if field == "original":
                    row["extras"]["received_timestamp"] = NOW + .125
                else:
                    row[field] = NOW + 1
                result = latest_closed_snapshot(MemoryStore(row), PAIR, NOW)
                self.assertEqual(result["reason"], "snapshot_not_yet_observable")

    def test_receipt_missing_or_naive_is_unknown_not_replaced_with_exchange_time(self):
        for missing in (None, datetime(2023, 1, 1)):
            row = book()
            row["received_at"] = missing
            result = latest_closed_snapshot(MemoryStore(row), PAIR, NOW)
            self.assertEqual(result["reason"], "invalid_database_received_at_ms")
        row = book()
        row["extras"] = {}
        result = latest_closed_snapshot(MemoryStore(row), PAIR, NOW)
        self.assertEqual(result["quality"], "valid")
        self.assertIsNone(result["original_received_at_ms"])
        self.assertEqual(result["original_receive_time_status"], "unknown")

    def test_book_must_have_valid_twenty_levels_and_supported_identity(self):
        row = book()
        row["asks"] = row["asks"][:5]
        self.assertEqual(latest_closed_snapshot(MemoryStore(row), PAIR, NOW)["reason"], "insufficient_depth")
        row = book()
        row["last_update_id"] = "01"
        self.assertEqual(latest_closed_snapshot(MemoryStore(row), PAIR, NOW)["reason"], "invalid_last_update_id")
        with self.assertRaises(ValueError):
            latest_closed_snapshot(MemoryStore(book()), "SOL/USDT:USDT", NOW)
        self.assertEqual(latest_closed_snapshot(MemoryStore(None), PAIR, NOW)["reason"], "no_observable_snapshot")

    def test_raw_hash_changes_with_evidence_but_not_decision_clock(self):
        first = observation()
        later = observation(now=NOW + 500)
        self.assertEqual(first["raw_snapshot_hash"], later["raw_snapshot_hash"])
        changed = observation(bid_qty=40)
        self.assertNotEqual(first["raw_snapshot_hash"], changed["raw_snapshot_hash"])


class FrozenSignalTests(unittest.TestCase):
    def test_entry_rearm_and_frozen_schedule_are_independent_of_results(self):
        source = FrozenSecondLevelSignalSource()
        signals = []
        for index in range(10):
            now = NOW + 2_000 * index
            value = observation(now - 100, now=now, sequence=index * 2 + 10,
                                bid_qty=39 if index % 2 == 0 else 1,
                                ask_qty=1 if index % 2 == 0 else 39)
            result = source.observe(value)
            signals.append(result["signal"])
            # Even the opposite extreme cannot rearm without a low-OBI input.
            waiting = source.observe(observation(now + 400, now=now + 1_000,
                                                  sequence=index * 2 + 11, bid_qty=1, ask_qty=1))
            self.assertEqual(waiting["reason"], "obi_rearmed")
        self.assertEqual([(s["arm"], s["maker_distance_bp"]) for s in signals],
                         [("maker", 0), ("taker", None), ("maker", 1), ("taker", None),
                          ("maker", 2), ("taker", None), ("maker", 5), ("taker", None),
                          ("maker", 0), ("taker", None)])
        self.assertEqual(signals[1]["side"], "sell")
        self.assertEqual(signals[0]["signal_ms"], NOW)
        self.assertNotIn("candle_ms", signals[0])
        self.assertEqual(signals[0]["policy_hash"], POLICY_HASH)
        self.assertFalse(signals[0]["live_launch_allowed"])

    def test_opposite_extreme_neither_rearms_nor_repeats_entry(self):
        source = FrozenSecondLevelSignalSource()
        self.assertIsNotNone(source.observe(observation())["signal"])
        result = source.observe(observation(NOW + 900, now=NOW + 1_000,
                                           sequence=11, bid_qty=1, ask_qty=39))
        self.assertIsNone(result["signal"])
        self.assertEqual(result["reason"], "waiting_obi_rearm")
        self.assertEqual(source.state["eligible_signal_count"], 1)

    def test_duplicates_backwards_and_poll_rate_cannot_advance_arm_index(self):
        source = FrozenSecondLevelSignalSource()
        self.assertIsNotNone(source.observe(observation())["signal"])
        self.assertEqual(source.observe(observation(), decision_ms=NOW + 100)["reason"], "poll_interval_not_elapsed")
        self.assertEqual(source.observe(observation(), decision_ms=NOW + 1_000)["reason"], "duplicate_snapshot")
        result = source.observe(observation(NOW - 101, now=NOW + 2_000, sequence=11))
        self.assertEqual(result["reason"], "backward_snapshot")
        result = source.observe(observation(NOW + 2_900, now=NOW + 3_000, sequence=9))
        self.assertEqual(result["reason"], "backward_snapshot")
        self.assertEqual(source.state["eligible_signal_count"], 1)
        self.assertEqual(source.observe(observation(), decision_ms=NOW + 2_999)["reason"], "backward_decision_clock")

    def test_stale_valid_marker_and_future_original_cannot_bypass_source_checks(self):
        source = FrozenSecondLevelSignalSource()
        value = observation()
        value["source_event_ms"] = NOW - 5_001
        self.assertEqual(source.observe(value)["reason"], "stale_snapshot")
        value = observation(NOW + 900, now=NOW + 1_000, sequence=11)
        value["original_received_at_ms"] = NOW + 1_000.125
        self.assertEqual(source.observe(value)["reason"], "snapshot_not_yet_observable")
        self.assertEqual(source.state["eligible_signal_count"], 0)

    def test_session_cutoff_and_recovery_preserve_consumption_and_arm_assignment(self):
        source = FrozenSecondLevelSignalSource(started_ms=NOW)
        self.assertEqual(source.observe(observation())["reason"], "snapshot_precedes_session")
        first = source.observe(observation(NOW + 900, now=NOW + 1_000, sequence=11))["signal"]
        restarted = FrozenSecondLevelSignalSource(state=source.state)
        self.assertEqual(restarted.observe(observation(NOW + 900, now=NOW + 2_000, sequence=11))["reason"], "duplicate_snapshot")
        restarted.observe(observation(NOW + 2_900, now=NOW + 3_000, sequence=12, bid_qty=1, ask_qty=1))
        second = restarted.observe(observation(NOW + 3_900, now=NOW + 4_000, sequence=13))["signal"]
        self.assertEqual(first["arm"], "maker")
        self.assertEqual(second["arm"], "taker")
        wrong = source.state
        wrong["policy_hash"] = "wrong"
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            FrozenSecondLevelSignalSource(state=wrong)

    def test_recovery_preserves_start_even_before_any_valid_snapshot(self):
        source = FrozenSecondLevelSignalSource(started_ms=NOW)
        self.assertEqual(source.observe(observation())["reason"], "snapshot_precedes_session")
        recovered = FrozenSecondLevelSignalSource(state=source.state)
        result = recovered.observe(observation(now=NOW + 1_000))
        self.assertEqual(result["reason"], "snapshot_precedes_session")
        self.assertIsNone(result["signal"])
        self.assertEqual(recovered.started_ms, NOW)
        with self.assertRaisesRegex(ValueError, "start_mismatch"):
            FrozenSecondLevelSignalSource(state=source.state, started_ms=NOW + 1)

    def test_callable_fetches_latest_and_unknown_input_does_not_trigger(self):
        store = MemoryStore(None)
        source = FrozenSecondLevelSignalSource(store)
        self.assertIsNone(source(NOW))
        self.assertEqual(source.last_decision["reason"], "no_observable_snapshot")
        store.row = book(NOW + 900, sequence=11)
        self.assertIsNotNone(source.latest_signal(NOW + 1_000))
        policy = frozen_policy()
        policy["entry_abs_obi5"] = 0
        self.assertEqual(frozen_policy()["entry_abs_obi5"], .9)

    def test_trigger_and_rejection_counts_survive_recovery_without_assuming_independence(self):
        source = FrozenSecondLevelSignalSource()
        source.observe(observation())
        source.observe(observation(), decision_ms=NOW + 1_000)
        source.observe(observation(NOW + 1_900, now=NOW + 2_000, sequence=11))
        recovered = FrozenSecondLevelSignalSource(state=source.state)
        self.assertEqual(recovered.metrics, source.metrics)
        self.assertEqual(recovered.metrics["decision_count"], 3)
        self.assertEqual(recovered.metrics["consumed_snapshot_count"], 2)
        self.assertEqual(recovered.metrics["eligible_signal_count"], 1)
        self.assertEqual(recovered.metrics["reason_counts"], {
            "obi_entry_signal": 1, "duplicate_snapshot": 1, "waiting_obi_rearm": 1})
        self.assertEqual(recovered.metrics["observed_trigger_rate_per_minute"], 30)
        self.assertFalse(recovered.metrics["trigger_count_is_independent_completed_trade_count"])
        corrupted = source.state
        corrupted["decision_count"] = 100
        with self.assertRaisesRegex(ValueError, "reason_counts"):
            FrozenSecondLevelSignalSource(state=corrupted)


class PaperControlTests(unittest.TestCase):
    def test_crossing_control_uses_next_demo_quote_and_actual_horizon_not_source_mid(self):
        control = SecondLevelPaperControl(signal(), "0.5", {
            "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0004"})
        self.assertIsNone(control.result["hypothetical_crossing_entry_price"])
        result = control.observe_book_ticker(quote(bid="101", ask="101.02"),
                                             observed_ms=NOW + 100, request_started_ms=NOW + 1)
        self.assertEqual(result["hypothetical_crossing_entry_price"], "101.02")
        result = control.observe_book_ticker(quote(bid="102", ask="102.02", timestamp=NOW + 10_000),
                                             observed_ms=NOW + 10_100, request_started_ms=NOW + 10_000)
        self.assertTrue(control.terminal)
        self.assertEqual(result["hypothetical_crossing_gross_pnl_usdt"], "0.490")
        self.assertEqual(Decimal(result["hypothetical_crossing_fee_usdt"]), Decimal("0.040604"))
        self.assertEqual(Decimal(result["hypothetical_crossing_net_pnl_usdt"]), Decimal("0.449396"))
        self.assertEqual(result["maker_fill_status"], "unknown")
        self.assertIsNone(result["maker_queue_or_nonfill_probability"])
        self.assertFalse(result["actual_fill"])
        self.assertIsNone(result["actual_commission"])
        self.assertEqual(result["horizon_quote"]["horizon_lag_ms"], 100)

    def test_sell_reference_crossing_math_and_missing_fee_remain_unknown(self):
        value = signal(arm="taker", distance=None)
        value["side"] = "sell"
        control = SecondLevelPaperControl(value, "2")
        control.observe_book_ticker(quote(bid="100", ask="100.02"),
                                    observed_ms=NOW + 20, request_started_ms=NOW + 1)
        result = control.observe_book_ticker(quote(bid="99", ask="99.02", timestamp=None),
                                             observed_ms=NOW + 10_100, request_started_ms=NOW + 10_000)
        self.assertEqual(result["hypothetical_crossing_entry_price"], "100")
        self.assertEqual(result["hypothetical_crossing_exit_price"], "99.02")
        self.assertEqual(result["hypothetical_crossing_gross_pnl_usdt"], "1.96")
        self.assertIsNone(result["hypothetical_crossing_net_pnl_usdt"])
        self.assertIsNone(result["horizon_quote"]["exchange_event_ms"])
        self.assertEqual(result["horizon_quote"]["exchange_time_status"], "unknown")

    def test_predecision_quote_and_request_start_crossing_horizon_are_not_lookahead_inputs(self):
        control = SecondLevelPaperControl(signal(), "1")
        result = control.observe_book_ticker(quote(), observed_ms=NOW + 10, request_started_ms=NOW)
        self.assertEqual(result["last_reason"], "quote_not_strictly_after_decision")
        self.assertIsNone(result["entry_quote"])
        control.observe_book_ticker(quote(), observed_ms=NOW + 100, request_started_ms=NOW + 1)
        # Response after the horizon is insufficient if the request began before it.
        result = control.observe_book_ticker(quote(timestamp=NOW + 10_000), observed_ms=NOW + 10_100,
                                             request_started_ms=NOW + 9_999)
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["horizon_quote"])
        result = control.observe_book_ticker(quote(timestamp=NOW + 10_150), observed_ms=NOW + 10_200,
                                             request_started_ms=NOW + 10_150)
        self.assertTrue(result["terminal"])

    def test_quote_touch_never_fills_maker_or_derives_nonfill_rate(self):
        control = SecondLevelPaperControl(signal(distance=5), "1")
        result = control.observe_book_ticker(quote(), observed_ms=NOW + 100, request_started_ms=NOW + 1)
        self.assertEqual(result["maker_limit_price_unrounded"], "99.9500")
        control.observe_book_ticker(quote(bid="99", ask="99.01"), observed_ms=NOW + 500,
                                    request_started_ms=NOW + 400)
        result = control.observe_book_ticker(quote(bid="99", ask="99.01", timestamp=NOW + 10_000), observed_ms=NOW + 10_100,
                                             request_started_ms=NOW + 10_000)
        self.assertEqual(result["maker_fill_status"], "unknown")
        self.assertIsNone(result["maker_queue_or_nonfill_probability"])
        self.assertFalse(result["actual_fill"])

    def test_duplicate_backward_wrong_pair_and_invalid_quotes_cannot_supply_reference(self):
        control = SecondLevelPaperControl(signal(), "1")
        for value, expected in ((quote(pair="ETH/USDT:USDT"), "quote_pair_mismatch"),
                                (quote(bid="100", ask="100"), "quote_crossed_or_locked"),
                                (quote(bid="nan"), "invalid_quote_bid")):
            result = control.observe_book_ticker(value, observed_ms=NOW + 100, request_started_ms=NOW + 1)
            self.assertEqual(result["last_reason"], expected)
            self.assertIsNone(result["entry_quote"])
        control.observe_book_ticker(quote(), observed_ms=NOW + 100, request_started_ms=NOW + 1)
        result = control.observe_book_ticker(quote(), observed_ms=NOW + 99, request_started_ms=NOW + 2)
        self.assertEqual(result["last_reason"], "duplicate_or_backward_quote_observation")
        result = control.observe_book_ticker(quote(), observed_ms=NOW + 200, request_started_ms=NOW + 1)
        self.assertEqual(result["last_reason"], "duplicate_or_backward_quote_observation")

    def test_missing_entry_at_horizon_finishes_with_unavailable_reference(self):
        control = SecondLevelPaperControl(signal(), "1")
        result = control.observe_book_ticker(quote(timestamp=NOW + 10_000), observed_ms=NOW + 10_100,
                                             request_started_ms=NOW + 9_999)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "reference_unavailable")
        self.assertIsNone(result["hypothetical_crossing_gross_pnl_usdt"])
        self.assertIsNone(result["hypothetical_crossing_net_pnl_usdt"])

    def test_zero_quote_rate_is_distinct_from_absent_or_bad_rate(self):
        for rate, expected in (("0", Decimal("0")), (None, None), ("NaN", None), ("-0.1", None)):
            with self.subTest(rate=rate):
                control = SecondLevelPaperControl(signal(), "1", {"takerCommissionRate": rate})
                control.observe_book_ticker(quote(), observed_ms=NOW + 100, request_started_ms=NOW + 1)
                result = control.observe_book_ticker(quote(bid="101", ask="101.02", timestamp=NOW + 10_000),
                                                     observed_ms=NOW + 10_100, request_started_ms=NOW + 10_000)
                fees = result["hypothetical_crossing_fee_usdt"]
                self.assertEqual(None if fees is None else Decimal(fees), expected)

    def test_known_stale_and_backward_exchange_quotes_cannot_create_a_price_benchmark(self):
        control = SecondLevelPaperControl(signal(), "1")
        result = control.observe_book_ticker(quote(timestamp=NOW - 60_000), observed_ms=NOW + 100,
                                             request_started_ms=NOW + 1)
        self.assertEqual(result["last_reason"], "quote_exchange_clock_outside_frozen_window")
        self.assertIsNone(result["entry_quote"])
        control.observe_book_ticker(quote(timestamp=NOW + 10), observed_ms=NOW + 100,
                                    request_started_ms=NOW + 1)
        result = control.observe_book_ticker(quote(timestamp=NOW + 9), observed_ms=NOW + 200,
                                             request_started_ms=NOW + 101)
        self.assertEqual(result["last_reason"], "backward_quote_exchange_time")
        result = control.observe_book_ticker(quote(timestamp=NOW - 120_000), observed_ms=NOW + 10_100,
                                             request_started_ms=NOW + 10_000)
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["hypothetical_crossing_net_pnl_usdt"])

    def test_missing_exchange_time_remains_unknown_and_late_horizon_is_unavailable(self):
        control = SecondLevelPaperControl(signal(), "1")
        result = control.observe_book_ticker(quote(timestamp=None), observed_ms=NOW + 100,
                                             request_started_ms=NOW + 1)
        self.assertEqual(result["entry_quote"]["exchange_freshness_status"], "unknown")
        self.assertIsNone(result["entry_quote"]["local_receipt_minus_exchange_ms"])
        result = control.observe_book_ticker(quote(timestamp=NOW + 16_000), observed_ms=NOW + 16_100,
                                             request_started_ms=NOW + 16_000)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "reference_unavailable")
        self.assertEqual(result["last_reason"], "horizon_reference_window_missed")
        self.assertIsNone(result["hypothetical_crossing_gross_pnl_usdt"])


if __name__ == "__main__":
    unittest.main()
