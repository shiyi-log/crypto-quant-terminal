"""Closed Demo bucket reference contracts, offline and without credentials."""
import copy
from contextlib import contextmanager
from decimal import Decimal
import unittest

from demo_candle_source import closed_demo_candle


PAIR = "BTC/USDT:USDT"
FIVE = 300_000
TARGET = 900_000
START = TARGET * 100


def candle(timestamp, *, close="100.123456789012345678901234567890", **changes):
    row = {"timestamp": timestamp, "open": Decimal("100.100000000000000000000000000000"),
           "high": Decimal("101.999999999999999999999999999999"),
           "low": Decimal("99.000000000000000000000000000001"), "close": Decimal(close),
           "volume": Decimal("10.000000000000000000000000000001"), "extras": {"closed": True}}
    row.update(changes)
    return row


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return copy.deepcopy(self.rows[0]) if self.rows else None

    def fetchall(self):
        return copy.deepcopy(self.rows)


class MemoryStore:
    def __init__(self):
        self.native, self.source, self.queries = [], [], []
        self.return_all_native = False
        self.return_all_source = False

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, query, params):
        self.queries.append((query, params))
        if "timeframe=%s" in query:
            pair, timeframe, timestamp = params
            if pair != PAIR:
                return Rows([])
            rows = self.native if self.return_all_native else [row for row in self.native
                if row["timestamp"] == timestamp and row.get("extras", {}).get("closed") is True]
            return Rows(rows)
        if "timeframe='5m'" in query:
            pair, start, end = params
            if pair != PAIR:
                return Rows([])
            rows = self.source if self.return_all_source else [row for row in self.source
                if start <= row["timestamp"] < end and row.get("extras", {}).get("closed") is True]
            return Rows(sorted(rows, key=lambda row: row["timestamp"]))
        raise AssertionError("unexpected query")


class CandleSourceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.now = START + TARGET + 50
        self.store.source = [candle(START + index * FIVE) for index in range(3)]

    def read(self, **changes):
        return closed_demo_candle(self.store, PAIR, "15m", now_ms=self.now, **changes)

    def test_latest_native_exact_is_preferred_and_preserves_decimal(self):
        self.store.native = [candle(START)]
        result = self.read()
        self.assertEqual(result, self.store.native[0])
        self.assertEqual(str(result["close"]), "100.123456789012345678901234567890")
        self.assertIsInstance(result["close"], Decimal)
        self.assertEqual(len(self.store.queries), 1)
        self.assertNotIn("resampled_for_demo_reference", result["extras"])

    def test_stale_native_and_fresh_15m_rollup(self):
        self.store.native = [candle(START - 96 * TARGET)]
        result = self.read()
        self.assertEqual(result["timestamp"], START)
        self.assertEqual(result["close"], self.store.source[-1]["close"])
        self.assertEqual(str(result["volume"]), "30.000000000000000000000000000003")
        self.assertEqual(result["extras"]["source_timeframe"], "5m")
        self.assertEqual(result["extras"]["source_bars"], 3)
        self.assertTrue(result["extras"]["resampled_for_demo_reference"])
        self.assertTrue(result["extras"]["closed"])
        self.assertRegex(result["extras"]["source_input_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.store.queries[1][1], (PAIR, START, START + TARGET))

    def test_exact_missing_native_uses_complete_requested_bucket(self):
        self.now += 5 * TARGET
        result = self.read(candle_ms=START)
        self.assertEqual(result["timestamp"], START)
        self.assertIsNone(self.read())

    def test_aggregate_uses_first_open_max_high_min_low_last_close(self):
        self.store.source[1]["high"] = Decimal("120.000000000000000000000000000001")
        self.store.source[1]["low"] = Decimal("80.000000000000000000000000000001")
        self.store.source[2]["close"] = Decimal("101.123456789012345678901234567890")
        result = self.read()
        self.assertEqual(result["open"], self.store.source[0]["open"])
        self.assertEqual(result["high"], self.store.source[1]["high"])
        self.assertEqual(result["low"], self.store.source[1]["low"])
        self.assertEqual(result["close"], self.store.source[2]["close"])

    def test_missing_unclosed_duplicate_or_unaligned_components_never_emit(self):
        original = copy.deepcopy(self.store.source)
        for problem in ("missing", "unclosed", "duplicate", "unaligned"):
            with self.subTest(problem=problem):
                self.store.source = copy.deepcopy(original)
                if problem == "missing":
                    self.store.source.pop(1)
                elif problem == "unclosed":
                    self.store.source[1]["extras"]["closed"] = False
                elif problem == "duplicate":
                    self.store.source[1] = copy.deepcopy(self.store.source[0])
                else:
                    self.store.source[1]["timestamp"] += 1
                self.assertIsNone(self.read())

    def test_gap_never_returns_old_native_or_old_complete_source_bucket(self):
        self.store.native = [candle(START - TARGET)]
        self.store.source = [candle(START - TARGET + index * FIVE) for index in range(3)]
        self.assertIsNone(self.read())
        self.store.return_all_native = True  # Validation also rejects a malformed reader.
        self.store.return_all_source = True
        self.assertIsNone(self.read())

    def test_open_future_negative_unaligned_exact_and_bad_clock_are_refused_without_queries(self):
        for timestamp in (START + TARGET, START + 3 * TARGET, -TARGET, START + 1, True, "0"):
            with self.subTest(timestamp=timestamp):
                self.store.queries.clear()
                self.assertIsNone(self.read(candle_ms=timestamp))
                self.assertEqual(self.store.queries, [])
        for clock in (-1, True, "0", None):
            with self.subTest(clock=clock):
                self.assertIsNone(closed_demo_candle(self.store, PAIR, "15m", now_ms=clock))

    def test_now_inside_new_bucket_only_reads_previous_completed_bucket(self):
        self.now = START + TARGET - 1
        self.assertIsNone(self.read(candle_ms=START))
        self.assertIsNone(self.read())
        self.assertEqual(self.store.queries[-1][1], (PAIR, START - TARGET, START))
        self.now = START + TARGET
        self.assertIsNotNone(self.read())

    def test_invalid_ohlcv_never_emit_even_if_reader_returns_them(self):
        for field, value in (("close", Decimal("NaN")), ("open", Decimal("Infinity")),
                             ("high", Decimal("99")), ("low", Decimal("102")),
                             ("volume", Decimal("-1")), ("close", Decimal("0")),
                             ("volume", True)):
            with self.subTest(field=field, value=value):
                self.store.source = [candle(START + index * FIVE) for index in range(3)]
                self.store.source[1][field] = value
                self.assertIsNone(self.read())

    def test_unclosed_native_cannot_be_used_and_valid_components_can_recover(self):
        self.store.native = [candle(START, extras={"closed": False})]
        self.store.return_all_native = True
        self.assertTrue(self.read()["extras"]["resampled_for_demo_reference"])
        self.store.source[0]["extras"]["closed"] = False
        self.store.return_all_source = True
        self.assertIsNone(self.read())

    def test_one_minute_and_five_minute_require_direct_exact_rows(self):
        for timeframe, interval in (("1m", 60_000), ("5m", FIVE)):
            with self.subTest(timeframe=timeframe):
                now = START + interval
                self.store.queries.clear()
                self.store.native = []
                self.assertIsNone(closed_demo_candle(self.store, PAIR, timeframe, now_ms=now))
                self.assertEqual(len(self.store.queries), 1)
                self.store.native = [candle(START)]
                self.assertEqual(closed_demo_candle(self.store, PAIR, timeframe, now_ms=now)["timestamp"], START)

    def test_unknown_timeframe_or_non_demo_pair_returns_none(self):
        self.assertIsNone(closed_demo_candle(self.store, PAIR, "1w", now_ms=self.now))
        self.assertIsNone(closed_demo_candle(self.store, "BNB/USDT:USDT", "15m", now_ms=self.now))
        self.assertEqual(self.store.queries, [])

    def test_fingerprint_tracks_source_facts_not_mutable_aliases(self):
        original = self.read()
        self.store.source[-1]["close"] = Decimal("100.2")
        changed = self.read()
        self.assertNotEqual(original["extras"]["source_input_fingerprint"],
                            changed["extras"]["source_input_fingerprint"])
        self.assertEqual(original["close"], Decimal("100.123456789012345678901234567890"))


if __name__ == "__main__":
    unittest.main()
