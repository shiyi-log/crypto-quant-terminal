"""Delayed order-flow outcome contracts without a database dependency."""

import copy
import threading
import unittest
from unittest.mock import Mock, patch

from orderflow_maturity import (
    DEFAULT_HORIZONS,
    FUTURE_INTERVAL_MS,
    HORIZON_CANDLES,
    OrderflowMaturityWorker,
    calculate_maturity,
    mature_surge_observations,
)


BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
START = 1_800_000


def observation(event_id="btc-surge", *, pair=BTC, market="futures", entry=100.0):
    return {
        "event_id": event_id,
        "exchange": "binance",
        "market": market,
        "pair": pair,
        "timeframe": "5m",
        "timestamp": START,
        "close": entry,
        "method_version": "orderflow-volume-surge-v1",
        "data_fingerprint": "original-observation-fingerprint",
        "status": "unknown",
    }


def future_candles(count=48, *, entry=100.0):
    return [
        {
            "timestamp": START + step * FUTURE_INTERVAL_MS,
            "open": entry,
            "high": entry + step + 2.0,
            "low": entry - step / 2.0,
            "close": entry + step,
            "volume": 10.0,
            "extras": {"closed": True},
        }
        for step in range(1, count + 1)
    ]


class MemorySurgeStore:
    """The legacy datastore contract, including immutable retry behavior."""

    def __init__(self, observations, datasets):
        self.observations = copy.deepcopy(observations)
        self.datasets = copy.deepcopy(datasets)
        self.events = {}
        self.statuses = {row["event_id"]: "unknown" for row in observations}
        self.candle_queries = []
        self.status_projections = []

    def list_surge_observations(self, *, status=None, limit=100):
        rows = copy.deepcopy(self.observations)
        return rows if limit is None else rows[:limit]

    def get_candles(self, exchange, market, pair, timeframe, *, candle_type, limit,
                    start, end):
        key = (exchange, market, pair, timeframe, candle_type)
        self.candle_queries.append((key, start, end, limit))
        return copy.deepcopy([
            row for row in self.datasets.get(key, [])
            if start <= row["timestamp"] <= end
        ])

    @staticmethod
    def _stored_content(event):
        # DataStore stores these three fields; rich caller-only metadata does
        # not change the persisted immutable event payload.
        return (event.get("observation_id"), event.get("event_type"),
                event.get("payload", event))

    def append_surge_event(self, event):
        event_id = event["event_id"]
        old = self.events.get(event_id)
        if old is not None:
            if self._stored_content(old) != self._stored_content(event):
                raise ValueError("immutable surge event conflict")
            return False
        self.events[event_id] = copy.deepcopy(event)
        return True

    def project_surge_status(self, event_id, status):
        self.statuses[event_id] = status
        self.status_projections.append((event_id, status))


def dataset_key(row, candle_type=None):
    return (row["exchange"], row["market"], row["pair"], "5m",
            candle_type or row["market"])


class MaturityCalculationTests(unittest.TestCase):
    def test_all_four_horizons_use_observation_close_and_future_window(self):
        source = observation()
        candles = future_candles()
        # An extreme observation candle must never enter future excursions.
        current = {"timestamp": START, "high": 1_000.0, "low": 1.0,
                   "close": 999.0, "extras": {"closed": True}}
        before = copy.deepcopy((source, candles))
        for horizon in DEFAULT_HORIZONS:
            with self.subTest(horizon=horizon):
                count = HORIZON_CANDLES[horizon]
                result = calculate_maturity(source, [current, *reversed(candles)], horizon)
                self.assertIsNotNone(result)
                self.assertEqual(result["entry_price"], 100.0)
                self.assertEqual(result["entry_timestamp"], START)
                self.assertEqual(result["maturity_timestamp"], START + count * FUTURE_INTERVAL_MS)
                self.assertEqual(result["status"], "matured")
                self.assertEqual(result["pair"], BTC)
                self.assertEqual(result["observation_id"], source["event_id"])
                self.assertAlmostEqual(result["return"], count / 100.0)
                self.assertAlmostEqual(result["mfe"], (count + 2.0) / 100.0)
                self.assertAlmostEqual(result["mae"], -count / 200.0)
        self.assertEqual((source, candles), before)

    def test_detector_payload_and_database_payload_wrapper_have_same_result(self):
        flat = observation()
        detector = {key: flat[key] for key in (
            "event_id", "market", "pair", "timeframe", "method_version",
            "data_fingerprint", "status",
        )}
        detector["observation"] = {"timestamp": START, "close": 100.0}
        database_row = {**flat, "payload": detector}
        del database_row["close"]
        expected = calculate_maturity(flat, future_candles(1), "5m")
        self.assertEqual(calculate_maturity(detector, future_candles(1), "5m"), expected)
        self.assertEqual(calculate_maturity(database_row, future_candles(1), "5m"), expected)

    def test_incomplete_or_missing_future_candle_keeps_horizon_unknown(self):
        source = observation()
        complete = future_candles(6)
        cases = {
            "not_yet_complete": complete[:5],
            "missing_middle": complete[:2] + complete[3:],
            "missing_first": complete[1:],
            "no_future": [],
        }
        for name, candles in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(calculate_maturity(source, candles, "30m"))

    def test_open_or_invalid_future_candle_cannot_mature(self):
        source = observation()
        for change in ({"extras": {"closed": False}}, {"closed": False},
                       {"high": float("nan")}, {"low": None}, {"close": 0}):
            with self.subTest(change=change):
                candles = future_candles(6)
                candles[2].update(change)
                self.assertIsNone(calculate_maturity(source, candles, "30m"))

    def test_excursions_include_intermediate_extremes_not_only_final_close(self):
        candles = future_candles(6)
        candles[1]["high"] = 130.0
        candles[2]["low"] = 70.0
        candles[-1]["close"] = 98.0
        result = calculate_maturity(observation(), candles, "30m")
        self.assertAlmostEqual(result["return"], -0.02)
        self.assertAlmostEqual(result["mfe"], 0.30)
        self.assertAlmostEqual(result["mae"], -0.30)

    def test_inconsistent_archived_ohlc_keeps_outcome_unknown(self):
        for change in ({"high": 90.0}, {"low": 110.0},
                       {"high": 101.0, "low": 102.0}, {"open": None}):
            with self.subTest(change=change):
                candles = future_candles(1)
                candles[0].update(change)
                self.assertIsNone(calculate_maturity(observation(), candles, "5m"))

    def test_current_archive_candle_is_unknown_until_interval_closes(self):
        candle = future_candles(1)[0]
        for marked in (False, True):
            with self.subTest(marked=marked):
                candidate = copy.deepcopy(candle)
                if not marked:
                    candidate.pop("extras")
                close_at = candidate["timestamp"] + FUTURE_INTERVAL_MS
                with patch("orderflow_maturity.time.time", return_value=(close_at - 1) / 1000):
                    self.assertIsNone(calculate_maturity(observation(), [candidate], "5m"))
                with patch("orderflow_maturity.time.time", return_value=close_at / 1000):
                    self.assertIsNotNone(calculate_maturity(observation(), [candidate], "5m"))


class MaturityOrchestrationTests(unittest.TestCase):
    def test_complete_archive_appends_all_horizons_and_projects_matured(self):
        row = observation()
        store = MemorySurgeStore([row], {dataset_key(row): future_candles()})
        created = mature_surge_observations(store)
        self.assertEqual(len(created), 4)
        self.assertEqual({event["payload"]["horizon"] for event in created}, set(DEFAULT_HORIZONS))
        self.assertEqual(store.statuses[row["event_id"]], "matured")
        self.assertEqual(store.observations, [row])
        query = store.candle_queries[0]
        self.assertEqual(query[0], ("binance", "futures", BTC, "5m", "futures"))
        self.assertEqual(query[1:3], (START, START + 48 * FUTURE_INTERVAL_MS))

    def test_partial_archive_appends_available_horizon_but_keeps_status_unknown(self):
        row = observation()
        store = MemorySurgeStore([row], {dataset_key(row): future_candles(5)})
        created = mature_surge_observations(store)
        self.assertEqual([event["payload"]["horizon"] for event in created], ["5m"])
        self.assertEqual(store.statuses[row["event_id"]], "unknown")
        self.assertEqual(store.status_projections, [])

    def test_first_future_candle_missing_appends_nothing_and_keeps_unknown(self):
        row = observation()
        store = MemorySurgeStore([row], {dataset_key(row): future_candles()[1:]})
        self.assertEqual(mature_surge_observations(store), [])
        self.assertEqual(store.events, {})
        self.assertEqual(store.statuses[row["event_id"]], "unknown")

    def test_duplicate_run_is_idempotent_when_append_returns_false(self):
        row = observation()
        store = MemorySurgeStore([row], {dataset_key(row): future_candles()})
        first = mature_surge_observations(store)
        snapshot = copy.deepcopy(store.events)
        self.assertEqual(len(first), 4)
        self.assertEqual(mature_surge_observations(store), [])
        self.assertEqual(store.events, snapshot)
        self.assertEqual(len(store.events), 4)

    def test_revised_future_candle_appends_new_event_preserving_old_result(self):
        row = observation()
        key = dataset_key(row)
        store = MemorySurgeStore([row], {key: future_candles()})
        first = mature_surge_observations(store)
        original = copy.deepcopy(store.events)
        # Revising candle 48 changes only the 4h outcome; earlier windows are
        # complete idempotent retries and must not acquire new event IDs.
        store.datasets[key][-1]["close"] = 150.0
        store.datasets[key][-1]["high"] = 155.0
        revised = mature_surge_observations(store)
        self.assertEqual(len(first), 4)
        self.assertEqual(len(revised), 1)
        self.assertEqual(revised[0]["payload"]["horizon"], "4h")
        self.assertNotIn(revised[0]["event_id"], original)
        self.assertAlmostEqual(revised[0]["payload"]["return"], 0.50)
        self.assertEqual(len(store.events), 5)
        for event_id, event in original.items():
            self.assertEqual(store.events[event_id], event)

    def test_btc_eth_and_spot_futures_query_windows_are_isolated(self):
        btc = observation("btc-futures")
        eth = observation("eth-futures", pair=ETH, entry=200.0)
        spot = observation("btc-spot", market="spot", entry=1_000.0)
        btc_candles = future_candles(1)
        eth_candles = future_candles(1, entry=200.0)
        eth_candles[0].update({"close": 190.0, "low": 180.0})
        spot_candles = future_candles(1, entry=1_000.0)
        spot_candles[0].update({"close": 1_100.0, "high": 1_110.0})
        store = MemorySurgeStore([btc, eth, spot], {
            dataset_key(btc): btc_candles,
            dataset_key(eth): eth_candles,
            dataset_key(spot): spot_candles,
        })
        created = mature_surge_observations(store, horizons=("5m",))
        results = {event["observation_id"]: event["payload"] for event in created}
        self.assertAlmostEqual(results["btc-futures"]["return"], 0.01)
        self.assertAlmostEqual(results["eth-futures"]["return"], -0.05)
        self.assertAlmostEqual(results["btc-spot"]["return"], 0.10)
        self.assertEqual({query[0] for query in store.candle_queries}, {
            dataset_key(btc), dataset_key(eth), dataset_key(spot),
        })

    def test_explicit_candle_type_is_used_for_archive_lookup(self):
        row = observation()
        store = MemorySurgeStore([row], {
            dataset_key(row, "custom-archive"): future_candles(1),
        })
        created = mature_surge_observations(store, horizons=("5m",), candle_type="custom-archive")
        self.assertEqual(len(created), 1)
        self.assertEqual(store.candle_queries[0][0][-1], "custom-archive")

    def test_method_version_change_appends_new_revisions(self):
        row = observation()
        store = MemorySurgeStore([row], {dataset_key(row): future_candles()})
        first = mature_surge_observations(store, method_version="maturity-v1")
        second = mature_surge_observations(store, method_version="maturity-v2")
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertTrue({event["event_id"] for event in first}.isdisjoint(
            {event["event_id"] for event in second}))
        self.assertTrue(all(event["method_version"] == row["method_version"]
                            for event in second))
        self.assertTrue(all(event["result"]["method_version"] == "maturity-v2"
                            for event in second))

    def test_canonical_reader_scans_all_markets_and_keeps_detector_payload(self):
        row = observation()
        original = {"timestamp": START, "close": 100.0, "quote_volume": 1_000.0}
        row.update(observation=original, status="unknown")

        class CanonicalStore(MemorySurgeStore):
            def read_orderflow_surges(self, **kwargs):
                self.read_options = kwargs
                return copy.deepcopy(self.observations)

            def append_orderflow_surge_result(self, event_id, result, **metadata):
                self.saved_observation = metadata["observation"]
                return metadata["revision_id"]

        store = CanonicalStore([row], {dataset_key(row): future_candles(1)})
        created = mature_surge_observations(store, horizons=("5m",), limit=None)
        self.assertEqual(len(created), 1)
        self.assertIsNone(store.read_options["market"])
        self.assertIsNone(store.read_options["limit"])
        self.assertEqual(store.saved_observation, original)
        self.assertEqual(store.observations[0]["observation"], original)


class MaturityWorkerTests(unittest.TestCase):
    def test_notify_during_scan_is_not_lost_and_owned_store_closes_on_worker(self):
        initial, release, notified = threading.Event(), threading.Event(), threading.Event()
        threads, calls = {}, []
        store = Mock()
        store.close.side_effect = lambda: threads.update(close=threading.get_ident())

        def factory():
            threads["factory"] = threading.get_ident()
            return store

        def scan(actual_store, **kwargs):
            self.assertIs(actual_store, store)
            calls.append(kwargs)
            threads["scan"] = threading.get_ident()
            if len(calls) == 1:
                initial.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release initial scan")
            else:
                notified.set()
            return [{}]

        worker = OrderflowMaturityWorker(factory, interval=60)
        with patch("orderflow_maturity.mature_surge_observations", side_effect=scan):
            try:
                worker.start()
                worker.start()
                self.assertTrue(initial.wait(2))
                worker.notify()
                release.set()
                self.assertTrue(notified.wait(2))
            finally:
                release.set()
                self.assertTrue(worker.close(2))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["limit"] is None for call in calls))
        self.assertEqual(worker.status["scans"], 2)
        self.assertEqual(worker.status["appended"], 2)
        self.assertFalse(worker.status["running"])
        self.assertTrue(worker.status["closed"])
        self.assertEqual(threads["factory"], threads["scan"])
        self.assertEqual(threads["factory"], threads["close"])
        self.assertNotEqual(threads["factory"], threading.get_ident())
        store.close.assert_called_once()

    def test_periodic_scan_recovers_from_error_without_logging_exception_text(self):
        completed = threading.Event()
        attempts = []
        store = Mock()

        def scan(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("private-database-credential")
            completed.set()
            return []

        worker = OrderflowMaturityWorker(lambda: store, interval=0.01)
        with patch("orderflow_maturity.mature_surge_observations", side_effect=scan), \
                self.assertLogs("orderflow_maturity", level="WARNING") as logs:
            try:
                worker.start()
                self.assertTrue(completed.wait(2))
            finally:
                self.assertTrue(worker.close(2))
        self.assertEqual(worker.status["failures"], 1)
        self.assertIsNone(worker.status["last_error"])
        self.assertGreaterEqual(worker.status["scans"], 1)
        self.assertIsNotNone(worker.status["last_scan_at"])
        self.assertNotIn("private-database-credential", " ".join(logs.output))
        store.close.assert_called_once()

    def test_close_timeout_reports_running_scan_then_releases_store(self):
        started, release = threading.Event(), threading.Event()
        store = Mock()

        def scan(*args, **kwargs):
            started.set()
            release.wait(2)
            return []

        worker = OrderflowMaturityWorker(lambda: store, interval=60)
        with patch("orderflow_maturity.mature_surge_observations", side_effect=scan):
            try:
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(worker.close(0.001))
                self.assertTrue(worker.status["running"])
                store.close.assert_not_called()
            finally:
                release.set()
                self.assertTrue(worker.close(2))
        store.close.assert_called_once()

    def test_closed_worker_does_not_start_or_create_store(self):
        factory = Mock()
        worker = OrderflowMaturityWorker(factory)
        self.assertTrue(worker.close())
        worker.start()
        worker.notify()
        factory.assert_not_called()
        self.assertFalse(worker.status["running"])


if __name__ == "__main__":
    unittest.main()
