import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orderbook_features import (HOUR_MS, compute_hour_features,
                               load_published_feature, snapshot_features)
from orderbook_feature_service import (FeatureInstanceLock, OrderbookFeatureService,
                                      fetch_hour_rows, publish_feature)

PAIR = "BTC/USDT:USDT"
OPEN = HOUR_MS * 100
CLOSE = OPEN + HOUR_MS


def book(timestamp, *, mid=100.0, bid_qty=3, ask_qty=1, sequence=None, received=None):
    return {"timestamp": timestamp, "last_update_id": str(sequence if sequence is not None else timestamp),
            "bids": [[mid - .01 * (i + 1), bid_qty] for i in range(20)],
            "asks": [[mid + .01 * (i + 1), ask_qty] for i in range(20)],
            "received_at": timestamp + 5 if received is None else received,
            "received_timestamp": timestamp + 5}


def hour_books(*, moving=False):
    return [book(t, mid=100 + ((t - OPEN) // 1000) * .0001 if moving else 100)
            for t in range(OPEN, CLOSE, 1000)]


def calculate(rows, **kwargs):
    return compute_hour_features(rows, pair=PAIR, bar_open_ms=OPEN,
                                 observed_at_ms=CLOSE + 15_000, **kwargs)


class CursorResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class MemoryConnection:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.store.queries.append((sql, params))
        if "FROM orderbooks" in sql:
            return CursorResult(self.store.rows[:params[-1]])
        if "INSERT INTO documents" in sql:
            key, payload = params
            if key in self.store.documents:
                return CursorResult([])
            self.store.documents[key] = copy.deepcopy(payload)
            return CursorResult([{"key": key}])
        if "SELECT payload FROM documents" in sql:
            return CursorResult([{"payload": self.store.documents[params[0]]}])
        raise AssertionError(sql)


class MemoryStore:
    def __init__(self, rows=()):
        self.documents, self.rows, self.queries, self.status_writes = {}, list(rows), [], []

    def _connection(self):
        return MemoryConnection(self)

    def _json(self, value):
        json.dumps(value, allow_nan=False)
        return value

    def get_document(self, key, default=None):
        return copy.deepcopy(self.documents.get(key, default))

    def put_document(self, key, value):
        self.status_writes.append(key)
        self.documents[key] = copy.deepcopy(value)


class OrderbookFeatureTests(unittest.TestCase):
    def test_twenty_level_math_and_normalized_depth_slope(self):
        feature = snapshot_features(book(OPEN))
        for depth in (1, 5, 20):
            self.assertEqual(feature[f"obi_{depth}"], .5)
        self.assertAlmostEqual(feature["relative_spread"], .0002)
        self.assertAlmostEqual(feature["depth_slope_bid"], 500)
        self.assertAlmostEqual(feature["depth_slope_ask"], 500)
        self.assertAlmostEqual(snapshot_features(book(OPEN, mid=1000))["relative_spread"], .00002)

    def test_invalid_books_reject_crossing_nonpositive_nonfinite_and_order(self):
        cases = []
        row = book(OPEN)
        row["bids"][0][0] = row["asks"][0][0]
        cases.append(row)
        row = book(OPEN)
        row["asks"][3][1] = 0
        cases.append(row)
        row = book(OPEN)
        row["bids"][1][0] = float("nan")
        cases.append(row)
        row = book(OPEN)
        row["asks"][2][0] = row["asks"][1][0]
        cases.append(row)
        row = book(OPEN)
        row["bids"].pop()
        cases.append(row)
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    snapshot_features(row)

    def test_full_hour_ready_and_history_preview_never_forward(self):
        result = calculate(hour_books(moving=True))
        self.assertTrue(result["quality"]["eligible"])
        self.assertEqual(result["quality"]["covered_seconds"], 3600)
        self.assertEqual(result["quality"]["minute_bins"], 60)
        self.assertFalse(result["forward_eligible"])
        self.assertEqual(result["features"]["lagged_obi_mid_directional_consistency"], 1)
        self.assertLessEqual(result["quality"]["directional_comparisons"], 3599)
        self.assertEqual(result["quality"]["independent_research_samples"], 0)

    def test_time_weighting_does_not_weight_dense_burst_more_than_equal_duration(self):
        rows = [book(OPEN + t, bid_qty=3, ask_qty=1) for t in range(0, 1000, 100)]
        rows.append(book(OPEN + 1000, bid_qty=1, ask_qty=3))
        result = calculate(rows)
        # +0.5 for 1 second, then -0.5 for capped 3 seconds. Snapshot-count
        # mean would incorrectly be strongly positive (10 positive vs 1 negative).
        self.assertAlmostEqual(result["features"]["obi_5_time_mean"], -.25)
        self.assertFalse(result["quality"]["eligible"])
        self.assertEqual(result["quality"]["covered_seconds"], 4)

    def test_snapshot_gap_cannot_be_filled_by_unlimited_quote_hold(self):
        rows = [r for r in hour_books() if not OPEN + 600_000 <= r["timestamp"] < OPEN + 720_000]
        result = calculate(rows)
        self.assertFalse(result["quality"]["eligible"])
        self.assertIn("collector_gap", result["quality"]["reasons"])
        self.assertLess(result["quality"]["covered_seconds"], 3500)

    def test_close_event_and_late_db_or_original_receive_are_not_inputs(self):
        rows = [book(OPEN), book(CLOSE), book(OPEN + 1000, received=CLOSE + 1), book(OPEN + 2000)]
        rows[-1]["received_timestamp"] = CLOSE + 1
        result = calculate(rows)
        self.assertEqual(result["quality"]["valid_snapshots"], 1)
        self.assertEqual(result["quality"]["rejected_by_reason"], {
            "outside_bar": 1, "received_after_cutoff": 2})

    def test_missing_receive_is_unknown_and_timezone_receive_is_supported(self):
        missing = book(OPEN)
        missing.pop("received_at")
        dated = book(OPEN + 1000)
        dated["received_at"] = datetime.fromtimestamp((OPEN + 1005) / 1000, tz=timezone.utc)
        result = calculate([missing, dated])
        self.assertEqual(result["quality"]["valid_snapshots"], 1)
        self.assertEqual(result["quality"]["rejected_by_reason"]["receive_time_unknown"], 1)

    def test_receive_microseconds_after_cutoff_cannot_round_into_bar(self):
        row = book(OPEN)
        row["received_at"] = datetime.fromtimestamp(CLOSE / 1000, tz=timezone.utc) + timedelta(microseconds=1)
        result = calculate([row])
        self.assertEqual(result["quality"]["valid_snapshots"], 0)
        self.assertEqual(result["quality"]["rejected_by_reason"], {"received_after_cutoff": 1})

    def test_independent_exchange_and_receive_clocks_use_cutoff_not_each_other(self):
        row = book(OPEN + 3000, received=OPEN + 1000)
        row["received_timestamp"] = OPEN + 900
        result = calculate([row])
        self.assertEqual(result["quality"]["valid_snapshots"], 1)
        self.assertEqual(result["quality"]["positive_event_receive_skew_count"], 1)
        self.assertEqual(result["quality"]["max_positive_event_receive_skew_ms"], 2100)

    def test_malformed_sequence_is_unknown(self):
        rows = [book(OPEN + i * 1000, sequence=seq) for i, seq in enumerate((None, "garbage", -1))]
        rows[0]["last_update_id"] = None
        result = calculate(rows)
        self.assertEqual(result["quality"]["valid_snapshots"], 0)
        self.assertEqual(result["quality"]["rejected_by_reason"], {"sequence_unknown": 3})

    def test_empty_input_is_unknown_and_json_safe(self):
        result = calculate([])
        self.assertEqual(result["quality"]["status"], "unknown")
        self.assertTrue(result["quality"]["stale"])
        self.assertIsNone(result["features"]["obi_5_time_mean"])
        json.dumps(result, allow_nan=False)

    def test_forming_hour_unsupported_pair_and_unaligned_bar_rejected(self):
        for params in ({"observed_at_ms": CLOSE - 1}, {"pair": "SOL/USDT:USDT"}, {"bar_open_ms": OPEN + 1}):
            inputs = {"pair": PAIR, "bar_open_ms": OPEN, "observed_at_ms": CLOSE}
            inputs.update(params)
            with self.assertRaises(ValueError):
                compute_hour_features([], **inputs)

    def test_deterministic_source_hash_and_new_hash_for_correction(self):
        rows = [book(OPEN), book(OPEN + 1000)]
        first = calculate(rows)
        self.assertEqual(first["source_hash"], calculate(list(reversed(rows)))["source_hash"])
        corrected = copy.deepcopy(rows)
        corrected[0]["bids"][0][1] = 100
        self.assertNotEqual(first["source_hash"], calculate(corrected)["source_hash"])

    def test_duplicate_millisecond_chooses_highest_sequence_no_duration_inflation(self):
        row = book(OPEN, sequence=1)
        later = book(OPEN, sequence=2, bid_qty=1, ask_qty=3)
        result = calculate([row, later, later])
        self.assertEqual(result["quality"]["valid_snapshots"], 1)
        self.assertEqual(result["quality"]["rejected_by_reason"]["duplicate_snapshot"], 1)
        self.assertEqual(result["features"]["obi_5_time_mean"], -.5)
        self.assertEqual(result["input_sequence"]["last_update_id"], "2")

    def test_flat_mid_consistency_unknown_rather_than_zero_evidence(self):
        result = calculate(hour_books())
        self.assertIsNone(result["features"]["lagged_obi_mid_directional_consistency"])
        self.assertEqual(result["quality"]["directional_comparisons"], 0)

    def test_query_truncation_is_fail_closed_even_with_complete_coverage(self):
        result = calculate(hour_books(), query_truncated=True)
        self.assertFalse(result["quality"]["eligible"])
        self.assertIn("query_truncated", result["quality"]["reasons"])


class FeaturePublicationTests(unittest.TestCase):
    def test_first_accepted_input_remains_immutable_after_correction(self):
        store = MemoryStore()
        first = calculate([book(OPEN)], forward_start_ms=OPEN)
        result = publish_feature(store, first, published_at_ms=CLOSE + 16_000)
        self.assertTrue(result["first_acceptance"])
        self.assertEqual(load_published_feature(store, PAIR, OPEN)["source_hash"], first["source_hash"])
        correction = calculate([book(OPEN, bid_qty=8)], forward_start_ms=OPEN)
        result = publish_feature(store, correction, published_at_ms=CLOSE + 20_000)
        self.assertFalse(result["first_acceptance"])
        self.assertEqual(len(store.documents), 3)
        self.assertEqual(load_published_feature(store, PAIR, OPEN)["features"], first["features"])
        result = publish_feature(store, first, published_at_ms=CLOSE + 30_000)
        self.assertFalse(result["first_acceptance"])
        self.assertEqual(load_published_feature(store, PAIR, OPEN)["published_at_ms"], CLOSE + 16_000)
        self.assertTrue(all("DO NOTHING" in sql for sql, _ in store.queries if "INSERT INTO" in sql))

    def test_history_late_or_before_observation_publication_writes_nothing(self):
        store = MemoryStore()
        for record, published in ((calculate([]), CLOSE + 20_000),
                                  (calculate([], forward_start_ms=OPEN), CLOSE + 90_001),
                                  (calculate([], forward_start_ms=OPEN), CLOSE + 14_999)):
            self.assertFalse(publish_feature(store, record, published_at_ms=published)["published"])
        self.assertEqual(store.documents, {})

    def test_selector_payload_mismatch_is_fail_closed(self):
        store = MemoryStore()
        record = calculate([], forward_start_ms=OPEN)
        publish_feature(store, record, published_at_ms=CLOSE + 20_000)
        store.documents[record["feature_key"]]["pair"] = "ETH/USDT:USDT"
        self.assertIsNone(load_published_feature(store, PAIR, OPEN))

    def test_tampered_history_boolean_cutoff_or_selector_cannot_publish(self):
        store = MemoryStore()
        record = calculate([], forward_start_ms=CLOSE)
        record["forward_eligible"] = True
        self.assertFalse(publish_feature(store, record, published_at_ms=CLOSE + 20_000)["published"])
        record = calculate([], forward_start_ms=OPEN)
        for field, value in (("cutoff_ms", CLOSE + 1), ("received_cutoff_ms", CLOSE + 1),
                             ("market", "spot"), ("feature_key", "different/key"), ("method_spec", {})):
            altered = copy.deepcopy(record)
            altered[field] = value
            self.assertFalse(publish_feature(store, altered, published_at_ms=CLOSE + 20_000)["published"])
        self.assertEqual(store.documents, {})

    def test_indexed_query_has_pair_and_half_open_hour_range_and_bounded_limit(self):
        store = MemoryStore([book(OPEN)] * 101)
        rows, truncated = fetch_hour_rows(store, PAIR, OPEN, max_rows=100)
        self.assertEqual(len(rows), 100)
        self.assertTrue(truncated)
        sql, params = store.queries[0]
        self.assertIn("timestamp >= %s AND timestamp < %s", sql)
        self.assertIn("received_at", sql)
        self.assertEqual(params, ("binance", "futures", PAIR, OPEN, CLOSE, 101))


class FeatureServiceTests(unittest.TestCase):
    def test_boot_partial_hour_does_not_query_or_publish_previous_hours(self):
        store = MemoryStore()
        now = [OPEN + 30_000]
        service = OrderbookFeatureService(store, apply=True, started_at_ms=now[0], clock=lambda: now[0])
        self.assertEqual(service.forward_start_ms, CLOSE)
        self.assertEqual(service.step(), [])
        self.assertEqual(store.queries, [])
        now[0] = CLOSE + 15_000
        self.assertEqual(service.step(), [])
        self.assertEqual(store.queries, [])

    def test_after_future_complete_hour_one_publish_per_pair_and_no_replay(self):
        store = MemoryStore([])
        now = [OPEN]
        service = OrderbookFeatureService(store, pairs=(PAIR,), apply=True, started_at_ms=OPEN,
                                         clock=lambda: now[0])
        now[0] = CLOSE + 14_999
        self.assertEqual(service.step(), [])
        self.assertEqual(store.queries, [])
        now[0] = CLOSE + 15_000
        with tempfile.TemporaryDirectory() as folder:
            service.status_path = Path(folder) / "status.json"
            self.assertEqual(len(service.step()), 1)
            self.assertEqual(service.status["published_count"], 1)
            count = len(store.queries)
            self.assertEqual(service.step(), [])
            self.assertEqual(len(store.queries), count)
            now[0] = CLOSE + 100_000
            self.assertEqual(service.step(), [])
            self.assertEqual(len(store.queries), count)
            now[0] = CLOSE + 10 * HOUR_MS + 15_000
            self.assertEqual(len(service.step()), 1)
            queries = [params for sql, params in store.queries if "FROM orderbooks" in sql]
            self.assertEqual([q[3] for q in queries], [OPEN, OPEN + 10 * HOUR_MS])

    def test_preview_has_no_database_documents_status_files_or_lock(self):
        store = MemoryStore([])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            service = OrderbookFeatureService(store, pairs=(PAIR,), started_at_ms=OPEN,
                                             clock=lambda: CLOSE + 20_000, status_path=path)
            self.assertEqual(len(service.preview()), 1)
            service.step()
            service.write_status(force=True)
            self.assertEqual(store.documents, {})
            self.assertEqual(store.status_writes, [])
            self.assertFalse(path.exists())

    def test_independent_lock_excludes_only_same_publisher(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "features.lock"
            with FeatureInstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with FeatureInstanceLock(path):
                        pass
                with FeatureInstanceLock(Path(folder) / "collector.lock"):
                    pass
            with FeatureInstanceLock(path):
                pass

    def test_only_three_supported_pairs_and_max_rows_bound(self):
        for pairs in ((), ("SOL/USDT:USDT",)):
            with self.assertRaises(ValueError):
                OrderbookFeatureService(MemoryStore(), pairs=pairs)
        with self.assertRaises(ValueError):
            OrderbookFeatureService(MemoryStore(), max_rows=500_001)

    def test_status_write_failures_propagate_and_are_not_fake_success(self):
        store = MemoryStore()
        service = OrderbookFeatureService(store, apply=True, clock=lambda: CLOSE)
        with patch.object(store, "put_document", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                service.write_status(force=True)


class FeaturePublicationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from runtime_config import load_environment
        load_environment()
        test_dsn = os.getenv("QUANT_TEST_DATABASE_URL")
        if not test_dsn or test_dsn == os.getenv("QUANT_DATABASE_URL"):
            raise unittest.SkipTest("an isolated QUANT_TEST_DATABASE_URL is required")
        from data_store import DataStore
        cls.store = DataStore(dsn=test_dsn, max_connections=1)
        cls.store.initialize()
        with cls.store._connection() as conn:
            name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if "test" not in name.lower():
            cls.store.close()
            raise unittest.SkipTest("refusing publication test outside an isolated test database")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_postgres_atomic_first_acceptance_idempotence_and_correction(self):
        opening = OPEN + os.getpid() * HOUR_MS
        cutoff = opening + HOUR_MS
        first = compute_hour_features([book(opening)], pair=PAIR, bar_open_ms=opening,
                                      observed_at_ms=cutoff + 15_000, forward_start_ms=opening)
        correction = compute_hour_features([book(opening, bid_qty=8)], pair=PAIR, bar_open_ms=opening,
                                           observed_at_ms=cutoff + 20_000, forward_start_ms=opening)
        keys = [first["feature_key"], correction["feature_key"],
                first["feature_key"].rsplit("/", 1)[0] + "/accepted.json"]
        try:
            self.assertTrue(publish_feature(self.store, first, published_at_ms=cutoff + 16_000)["first_acceptance"])
            self.assertFalse(publish_feature(self.store, correction, published_at_ms=cutoff + 21_000)["first_acceptance"])
            self.assertFalse(publish_feature(self.store, first, published_at_ms=cutoff + 22_000)["first_acceptance"])
            observed = load_published_feature(self.store, PAIR, opening)
            self.assertEqual(observed["features"], first["features"])
            self.assertEqual(observed["published_at_ms"], cutoff + 16_000)
            self.assertIsNotNone(self.store.get_document(correction["feature_key"]))
        finally:
            with self.store._connection() as conn:
                conn.execute("DELETE FROM documents WHERE key=ANY(%s)", (keys,))


if __name__ == "__main__":
    unittest.main()
