"""Order-flow revisions are verified against an isolated PostgreSQL schema."""

import copy
import hashlib
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from data_store import DataStore, _json_text
from orderflow_detector import VolumeSurgeDetector
from orderflow_maturity import DEFAULT_HORIZONS, mature_surge_observations
from runtime_config import load_environment


load_environment()


def observation(timestamp=300_000, pair="BTC/USDT:USDT"):
    detector = VolumeSurgeDetector(window_size=2)
    detector.seed(pair, [100.0, 100.0])
    event = detector.process(pair, "5m", {
        "timestamp": timestamp, "closed": True, "quote_volume": 800.0,
        "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0,
        "num_trades": 7, "flow_imbal": 0.25,
    }, source_timestamp=timestamp + 300_001)
    event["event_id"] += ":" + uuid.uuid4().hex
    return event


def maturity(event, horizon="1h"):
    return {
        "observation_id": event["event_id"], "pair": event["pair"],
        "horizon": horizon, "entry_timestamp": event["observation"]["timestamp"],
        "entry_price": 100.0, "future_close": 103.0,
        "maturity_timestamp": event["observation"]["timestamp"] + 3_600_000,
        "return": 0.03, "mfe": 0.04, "mae": -0.02,
        "method_version": "orderflow-maturity-v1",
        "candle_fingerprint": "closed-future-candles:" + horizon,
        "status": "matured",
    }


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"),
                     "需要 QUANT_TEST_DATABASE_URL 才运行订单流 PostgreSQL 隔离测试")
class OrderflowStorageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict

        test_config = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production_dsn = os.environ.get("QUANT_DATABASE_URL")
        production_config = conninfo_to_dict(production_dsn) if production_dsn else {}

        def location(config):
            return tuple(config.get(name, "") for name in ("host", "port", "dbname"))

        name = test_config.get("dbname", "").lower()
        if not ("test" in name or name == "quant_ci") or (
            production_config and location(test_config) == location(production_config)
        ):
            raise RuntimeError("QUANT_TEST_DATABASE_URL 必须指向独立测试数据库")
        cls.schema = "quant_orderflow_test_" + uuid.uuid4().hex
        cls.store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema)
        cls.store.initialize()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        cls.store.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.store._connection() as conn:
            conn.execute("TRUNCATE orderflow_surge_events")
            conn.execute("TRUNCATE orderflow_price_candidate_results, orderflow_price_candidates")

    def rows(self, **filters):
        return self.store.read_orderflow_surges(limit=None, **filters)

    @contextmanager
    def legacy_store(self):
        import psycopg
        from psycopg import sql

        schema = "quant_orderflow_migration_test_" + uuid.uuid4().hex
        store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=schema)
        try:
            with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
                yield store, conn
        finally:
            store.close()
            with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_detector_event_roundtrips_and_exact_retry_is_idempotent(self):
        event = observation()
        original = copy.deepcopy(event)
        revision_id = self.store.append_orderflow_surge(event)
        self.assertIsInstance(revision_id, str)
        self.assertEqual(self.store.append_orderflow_surge(copy.deepcopy(event)), revision_id)
        self.assertEqual(event, original)
        rows = self.rows(include_revisions=True)
        self.assertEqual(len(rows), 1)
        stored = rows[0]
        self.assertEqual(stored["event_id"], event["event_id"])
        self.assertEqual(stored["revision_id"], revision_id)
        self.assertEqual(stored["revision_kind"], "observed")
        self.assertEqual(stored["status"], "unknown")
        self.assertEqual(stored["observation"], event["observation"])
        self.assertEqual(stored["timestamp"], event["observation"]["timestamp"])
        self.assertEqual(stored["data_fingerprint"], event["data_fingerprint"])

    def test_empty_online_surges_have_zero_pair_counts_and_unknown_four_window_metrics(self):
        expected_pairs = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
        for selected in (None, "ETH/USDT:USDT"):
            with self.subTest(pair=selected):
                stats = self.store.surge_stats(pair=selected)
                buckets = {row["pair"]: row for row in stats["by_pair"]}
                self.assertEqual(set(buckets), {selected} if selected else expected_pairs)
                for bucket in (stats, *buckets.values()):
                    for count in ("total", "unknown", "matured", "invalid", "known", "sample_count"):
                        self.assertEqual(bucket[count], 0)
                    self.assertEqual(set(bucket["horizons"]), {"5m", "30m", "1h", "4h"})
                    for window in (bucket, *bucket["horizons"].values()):
                        self.assertEqual(window["sample_count"], 0)
                        for metric in ("gross_return_mean", "win_rate", "mfe_mean", "mae_mean",
                                       "required_fee_rate", "actual_fee_rate"):
                            self.assertIsNone(window[metric])

    def test_empty_online_price_spikes_have_zero_pair_band_counts_and_unknown_probabilities(self):
        expected_pairs = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
        for selected in (None, "BTC/USDT:USDT"):
            with self.subTest(pair=selected):
                stats = self.store.price_spike_stats(pair=selected)
                pairs = {selected} if selected else expected_pairs
                self.assertEqual(set(stats["by_pair"]), pairs)
                self.assertEqual(set(stats["profile_by_pair"]), pairs)
                for pair_name, bucket in stats["by_pair"].items():
                    self.assertEqual(bucket["candidate_count"], 0)
                    self.assertEqual(bucket["matured_count"], 0)
                    self.assertEqual(bucket["invalid_count"], 0)
                    self.assertIsNone(bucket["base_p_any"])
                    self.assertIsNone(bucket["first_timestamp"])
                    self.assertIsNone(bucket["last_timestamp"])
                    self.assertEqual(set(bucket["bands"]), set(stats["ratio_bands"]))
                    for band in bucket["bands"].values():
                        for count in ("candidate_count", "matured_count", "invalid_count",
                                      "up_count", "down_count", "any_count"):
                            self.assertEqual(band[count], 0)
                        for probability in ("p_up", "p_down", "p_any", "lift"):
                            self.assertIsNone(band[probability])
                    profile = stats["profile_by_pair"][pair_name]
                    self.assertEqual(set(profile), {"up", "down"})
                    for direction in profile.values():
                        self.assertEqual([point["offset"] for point in direction], list(range(-12, 13)))
                        for point in direction:
                            self.assertEqual(point["sample_count"], 0)
                            self.assertIsNone(point["mean_volume_ratio"])

    def test_price_candidate_is_append_only_includes_low_ratio_bins_and_projects_stats(self):
        detector = VolumeSurgeDetector(window_size=2)
        pair = "BTC/USDT:USDT"
        detector.seed(pair, [10.0, 10.0], timestamps=[0, 300_000])
        detector.process(pair, "5m", {
            "timestamp": 600_000, "closed": True, "quote_volume": 15.0,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        }, source_timestamp=900_000)
        event = detector.last_candidate
        self.assertEqual(event["observation"]["ratio_band"], "1to2")
        self.assertEqual(self.store.append_price_candidate(event), event["event_id"])
        self.assertEqual(self.store.append_price_candidate(copy.deepcopy(event)), event["event_id"])
        with self.assertRaisesRegex(ValueError, "指纹"):
            self.store.append_price_candidate({**copy.deepcopy(event),
                                               "data_fingerprint": "tampered"})
        changed_observation = {
            **event["observation"], "quote_volume": 17.5, "volume_ratio": 1.75,
        }
        with self.assertRaisesRegex(ValueError, "内容冲突"):
            self.store.append_price_candidate({
                **copy.deepcopy(event),
                "observation": changed_observation,
                "data_fingerprint": hashlib.sha256(
                    _json_text(changed_observation).encode("utf-8")
                ).hexdigest(),
            })

        outcome = {
            "status": "matured", "direction": "up", "return_pct": 0.02,
            "data_fingerprint": "outcome-fingerprint",
        }
        first = self.store.append_price_candidate_result(
            event["event_id"], "outcome", outcome,
            method_version=event["method_version"],
        )
        self.assertEqual(self.store.append_price_candidate_result(
            event["event_id"], "outcome", copy.deepcopy(outcome),
            method_version=event["method_version"],
        ), first)
        profile = {"status": "matured", "direction": "up", "points": [
            {"offset": offset, "volume_ratio": 1.5}
            for offset in range(-12, 13)
        ], "data_fingerprint": "profile-fingerprint"}
        self.store.append_price_candidate_result(
            event["event_id"], "profile", profile,
            method_version=event["method_version"],
        )
        projected = self.store.read_price_candidates(
            pair=pair, direction="up", status="matured", limit=10,
        )
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["observation"]["ratio_band"], "1to2")
        self.assertEqual(projected[0]["profile"]["points"][0]["offset"], -12)
        stats = self.store.price_spike_stats(pair)
        self.assertEqual(stats["by_pair"][pair]["matured_count"], 1)
        self.assertEqual(stats["by_pair"][pair]["bands"]["1to2"]["p_up"], 1.0)
        self.assertEqual(stats["profile_by_pair"][pair]["up"][12]["sample_count"], 1)
        self.assertEqual(stats["profile_by_pair"][pair]["up"][12]["offset"], 0)

    def test_zero_quote_volume_is_stored_in_the_lowest_trial_band(self):
        detector = VolumeSurgeDetector(window_size=2)
        pair = "ETH/USDT:USDT"
        detector.seed(pair, [10.0, 10.0], timestamps=[0, 300_000])
        detector.process(pair, "5m", {
            "timestamp": 600_000, "closed": True, "quote_volume": 0,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        }, source_timestamp=900_000)

        event = detector.last_candidate
        self.assertEqual(event["observation"]["ratio_band"], "lt1")
        self.assertEqual(self.store.append_price_candidate(event), event["event_id"])
        rows = self.store.read_price_candidates(pair=pair, direction="any", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(rows[0]["observation"]["quote_volume"], 0)

    def test_same_observation_identity_cannot_overwrite_original_fact(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        for change in (
            {"pair": "ETH/USDT:USDT"},
            {"method_version": "orderflow-volume-surge-v4"},
            {"data_fingerprint": "changed-input"},
            {"observation": {**event["observation"], "quote_volume": 999.0}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.append_orderflow_surge({**copy.deepcopy(event), **change})
        rows = self.rows(include_revisions=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observation"], event["observation"])

    def test_maturity_appends_and_keeps_original_detector_metadata(self):
        event = observation()
        observed_revision = self.store.append_orderflow_surge(event)
        result = maturity(event)
        revision_id = self.store.append_orderflow_surge_result(event["event_id"], result)
        self.assertNotEqual(revision_id, observed_revision)
        self.assertEqual(self.store.append_orderflow_surge_result(
            event["event_id"], copy.deepcopy(result)), revision_id)
        revisions = sorted(self.rows(event_id=event["event_id"], include_revisions=True),
                           key=lambda row: row["revision_no"])
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["status"], "unknown")
        self.assertIsNone(revisions[0]["result"])
        latest = revisions[1]
        self.assertEqual(latest["revision_no"], revisions[0]["revision_no"] + 1)
        self.assertEqual(latest["revision_kind"], "maturity")
        self.assertEqual(latest["status"], "matured")
        self.assertEqual(latest["observation"], event["observation"])
        self.assertEqual(latest["result"], result)
        self.assertEqual(latest["method_version"], event["method_version"])
        self.assertEqual(latest["data_fingerprint"], event["data_fingerprint"])
        self.assertEqual(self.rows(event_id=event["event_id"])[0]["revision_id"], revision_id)
        self.assertEqual(self.rows(event_id=event["event_id"])[0]["status"], "unknown")

    def test_multiple_horizons_append_but_explicit_revision_conflicts_reject(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        first = self.store.append_orderflow_surge_result(event["event_id"], maturity(event, "1h"))
        result = maturity(event, "4h")
        explicit_id = "test-maturity:" + uuid.uuid4().hex
        second = self.store.append_orderflow_surge_result(
            event["event_id"], result, revision_id=explicit_id)
        self.assertEqual(second, explicit_id)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.append_orderflow_surge_result(
            event["event_id"], copy.deepcopy(result), revision_id=explicit_id), explicit_id)
        with self.assertRaises(ValueError):
            self.store.append_orderflow_surge_result(
                event["event_id"], {**result, "return": 0.9}, revision_id=explicit_id)
        self.assertEqual(len(self.rows(include_revisions=True)), 3)
        self.assertEqual(self.rows()[0]["result"]["horizon"], "4h")

    def test_maturity_rejects_missing_observation_and_identity_mismatches(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        with self.assertRaises(ValueError):
            self.store.append_orderflow_surge_result("missing:" + uuid.uuid4().hex, maturity(event))
        for metadata in (
            {"market": "spot"}, {"pair": "ETH/USDT:USDT"}, {"timeframe": "1h"},
            {"timestamp": event["observation"]["timestamp"] + 1},
            {"method_version": "another-detector"}, {"data_fingerprint": "different-input"},
            {"observation": {**event["observation"], "close": 999.0}},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.store.append_orderflow_surge_result(event["event_id"], maturity(event), **metadata)
        self.assertEqual(len(self.rows(include_revisions=True)), 1)

    def test_latest_revision_is_selected_before_status_filter_and_limit(self):
        unknown = observation(300_000)
        mature = observation(600_000)
        self.store.append_orderflow_surge(unknown)
        self.store.append_orderflow_surge(mature)
        for horizon in DEFAULT_HORIZONS:
            self.store.append_orderflow_surge_result(mature["event_id"], maturity(mature, horizon))
        latest = self.store.read_orderflow_surges(pair=mature["pair"], limit=2)
        self.assertEqual({row["event_id"] for row in latest},
                         {unknown["event_id"], mature["event_id"]})
        self.assertEqual([row["event_id"] for row in self.rows(status="unknown")],
                         [unknown["event_id"]])
        self.assertEqual([row["event_id"] for row in self.rows(status="matured")],
                         [mature["event_id"]])

    def test_partial_maturity_stays_unknown_until_every_default_horizon_completes(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        for horizon in DEFAULT_HORIZONS[:-1]:
            self.store.append_orderflow_surge_result(event["event_id"], maturity(event, horizon))
        partial = self.rows()[0]
        self.assertEqual(partial["status"], "unknown")
        self.assertEqual(len(self.rows(status="unknown")), 1)
        self.assertEqual(self.rows(status="matured"), [])
        raw = self.rows(include_revisions=True)
        self.assertEqual([row["status"] for row in raw].count("matured"), 3)
        self.store.append_orderflow_surge_result(event["event_id"], maturity(event, DEFAULT_HORIZONS[-1]))
        self.assertEqual(self.rows()[0]["status"], "matured")
        self.assertEqual(self.rows(status="unknown"), [])
        self.assertEqual(len(self.rows(status="matured")), 1)
        self.assertEqual(len(self.rows(include_revisions=True)), 5)

    def test_repeat_horizon_revisions_do_not_count_as_complete_observation(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        for index in range(4):
            self.store.append_orderflow_surge_result(
                event["event_id"], {**maturity(event), "return": 0.01 * index})
        projected = self.rows()[0]
        self.assertEqual(projected["status"], "unknown")
        self.assertEqual(projected["completed_horizons"], ["1h"])
        self.assertEqual(projected["required_horizons"], list(DEFAULT_HORIZONS))
        self.assertEqual(len(self.rows(include_revisions=True)), 5)

    def test_invalid_horizon_revision_replaces_prior_completion_in_projection(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        for horizon in DEFAULT_HORIZONS:
            self.store.append_orderflow_surge_result(event["event_id"], maturity(event, horizon))
        self.assertEqual(self.rows()[0]["status"], "matured")
        self.store.append_orderflow_surge_result(
            event["event_id"], {**maturity(event, "4h"), "status": "invalid"})
        invalid = self.rows()[0]
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(invalid["revision_status"], "invalid")
        self.assertEqual(invalid["completed_horizons"], list(DEFAULT_HORIZONS[:-1]))
        self.assertEqual(self.rows(status="matured"), [])
        self.assertEqual(len(self.rows(status="invalid")), 1)
        self.store.append_orderflow_surge_result(
            event["event_id"], {**maturity(event, "1h"), "return": 0.05})
        self.assertEqual(self.rows()[0]["status"], "unknown")
        self.store.append_orderflow_surge_result(
            event["event_id"], {**maturity(event, "4h"), "return": 0.05})
        self.assertEqual(self.rows()[0]["status"], "matured")

    def test_statistics_include_partial_horizons_once_and_isolate_pairs(self):
        btc = observation()
        eth = observation(600_000, "ETH/USDT:USDT")
        pending = observation(900_000)
        for event in (btc, eth, pending):
            self.store.append_orderflow_surge(event)
        self.store.append_orderflow_surge_result(btc["event_id"], maturity(btc, "5m"))
        self.store.append_orderflow_surge_result(btc["event_id"], maturity(btc, "1h"))
        # The newer result for a horizon replaces the older sample.
        self.store.append_orderflow_surge_result(
            btc["event_id"], {**maturity(btc, "1h"), "return": 0.05, "mfe": 0.08, "mae": -0.03})
        self.store.append_orderflow_surge_result(
            eth["event_id"], {**maturity(eth, "1h"), "return": -0.01, "mfe": 0.02, "mae": -0.01})
        stats = self.store.surge_stats()
        self.assertEqual((stats["total"], stats["unknown"], stats["matured"]), (3, 3, 0))
        self.assertEqual(stats["statistics_horizon"], "1h")
        self.assertEqual(stats["sample_count"], 2)
        self.assertAlmostEqual(stats["gross_return_mean"], 0.02)
        self.assertAlmostEqual(stats["win_rate"], 0.5)
        self.assertAlmostEqual(stats["mfe_mean"], 0.05)
        self.assertAlmostEqual(stats["mae_mean"], -0.02)
        self.assertEqual(stats["horizons"]["5m"]["sample_count"], 1)
        self.assertIsNone(stats["required_fee_rate"])
        self.assertIsNone(stats["actual_fee_rate"])
        btc_stats = self.store.surge_stats(pair=btc["pair"])
        self.assertEqual(btc_stats["total"], 2)
        self.assertEqual(btc_stats["sample_count"], 1)
        self.assertAlmostEqual(btc_stats["gross_return_mean"], 0.05)
        pairs = {row["pair"]: row for row in stats["by_pair"]}
        self.assertAlmostEqual(pairs[eth["pair"]]["gross_return_mean"], -0.01)
        self.store.append_orderflow_surge_result(
            btc["event_id"], {**maturity(btc, "1h"), "status": "invalid"})
        after = self.store.surge_stats()
        self.assertEqual(after["sample_count"], 1)
        self.assertAlmostEqual(after["gross_return_mean"], -0.01)
        self.assertEqual(after["horizons"]["5m"]["sample_count"], 1)
        self.assertEqual(self.store.surge_stats(pair=btc["pair"])["sample_count"], 0)
        json.dumps(after, allow_nan=False)

    def test_legacy_adapters_share_the_revision_store_and_boolean_retry_contract(self):
        event = observation()
        flat = {**event["observation"], **{
            key: event[key] for key in ("event_id", "method_version", "data_fingerprint",
                                       "market", "pair", "timeframe", "status")
        }}
        self.assertTrue(self.store.insert_surge_observation(flat))
        self.assertFalse(self.store.insert_surge_observation(copy.deepcopy(flat)))
        legacy_maturity = {
            "event_id": "legacy-maturity:" + uuid.uuid4().hex,
            "observation_id": event["event_id"],
            "event_type": "orderflow_volume_surge_matured", "payload": maturity(event),
        }
        self.assertTrue(self.store.append_surge_event(legacy_maturity))
        self.assertFalse(self.store.append_surge_event(copy.deepcopy(legacy_maturity)))
        with self.assertRaises(ValueError):
            self.store.append_surge_event({**legacy_maturity,
                                           "payload": {**legacy_maturity["payload"], "return": 0.5}})
        self.assertEqual(len(self.rows(include_revisions=True)), 2)
        self.assertEqual(len(self.store.list_surge_events(observation_id=event["event_id"])), 2)
        self.assertEqual(len(self.store.list_surge_observations(status="unknown")), 1)
        detail = self.store.get_surge_observation(event["event_id"])
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertEqual(detail["observation"], flat)
        self.assertEqual(detail["status"], "unknown")
        # Per-horizon evidence is visible before the overall four-horizon
        # projection matures the observation.
        self.assertEqual(detail["completed_horizons"], ["1h"])
        self.assertEqual(detail["required_horizons"], ["5m", "30m", "1h", "4h"])
        json.dumps(detail, allow_nan=False)
        nested = observation(900_000)
        self.assertTrue(self.store.insert_surge_observation(nested))
        self.assertFalse(self.store.insert_surge_observation(copy.deepcopy(nested)))
        self.assertEqual(self.rows(event_id=nested["event_id"])[0]["observation"], nested["observation"])

    def test_pair_market_time_bounds_and_event_filters_are_isolated(self):
        btc_old = observation(300_000)
        btc_new = observation(600_000)
        eth = observation(600_000, "ETH/USDT:USDT")
        spot = {**observation(600_000), "market": "spot"}
        for event in (btc_old, btc_new, eth, spot):
            self.store.append_orderflow_surge(event)
        rows = self.rows(market="futures", pair="BTC/USDT:USDT", timeframe="5m",
                         start=600_000, end=600_000)
        self.assertEqual([row["event_id"] for row in rows], [btc_new["event_id"]])
        self.assertEqual([row["event_id"] for row in self.rows(pair=eth["pair"])], [eth["event_id"]])
        self.assertEqual([row["event_id"] for row in self.rows(market="spot")], [spot["event_id"]])
        self.assertEqual([row["event_id"] for row in self.rows(event_id=btc_old["event_id"])],
                         [btc_old["event_id"]])

    def test_invalid_statuses_do_not_create_revisions(self):
        event = observation()
        for status in ("success", "", True):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.append_orderflow_surge({**event, "status": status})
        self.assertEqual(self.rows(include_revisions=True), [])
        self.store.append_orderflow_surge(event)
        with self.assertRaises(ValueError):
            self.store.append_orderflow_surge_result(event["event_id"],
                                                     {**maturity(event), "status": "unknown"})
        with self.assertRaises(ValueError):
            self.rows(status="success")
        self.assertEqual(len(self.rows(include_revisions=True)), 1)

    def test_concurrent_exact_replays_append_only_one_fact(self):
        event = observation()
        with ThreadPoolExecutor(max_workers=4) as workers:
            revision_ids = list(workers.map(self.store.append_orderflow_surge,
                                           [copy.deepcopy(event) for _ in range(8)]))
        self.assertEqual(len(set(revision_ids)), 1)
        self.assertEqual(len(self.rows(include_revisions=True)), 1)

    def test_maturity_runner_uses_stored_observation_shape_and_is_restart_safe(self):
        event = observation()
        self.store.append_orderflow_surge(event)
        timestamp = event["observation"]["timestamp"]
        future = [{
            "timestamp": timestamp + index * 300_000,
            "open": 100.0, "high": 104.0, "low": 98.0,
            "close": 103.0, "volume": 1.0, "extras": {"closed": True},
        } for index in range(1, 13)]
        self.store.upsert_candles("binance", "futures", event["pair"], "5m", "futures", future)
        mature_surge_observations(self.store, horizons=("1h",))
        revisions = self.rows(event_id=event["event_id"], include_revisions=True)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(self.rows()[0]["status"], "unknown")
        self.assertEqual(self.rows()[0]["observation"], event["observation"])
        # Running the durable worker again after a restart can recompute the
        # same future window without appending a duplicate maturity revision.
        mature_surge_observations(self.store, horizons=("1h",))
        self.assertEqual(len(self.rows(include_revisions=True)), 2)

    def test_simple_legacy_event_table_migrates_without_losing_backup(self):
        from psycopg.types.json import Jsonb

        event = observation()
        result = maturity(event)
        maturity_id = "legacy-matured:" + uuid.uuid4().hex
        with self.legacy_store() as (store, conn):
            conn.execute("""CREATE TABLE orderflow_surge_events (
                event_id TEXT PRIMARY KEY, observation_id TEXT, event_type TEXT NOT NULL,
                payload JSONB NOT NULL, source_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sequence_id BIGSERIAL)""")
            conn.execute("CREATE INDEX orderflow_surge_events_type ON orderflow_surge_events (event_type)")
            conn.execute("CREATE INDEX orderflow_surge_events_observation ON orderflow_surge_events (observation_id)")
            # Put maturity first to verify migration orders references by
            # their dependencies rather than legacy ingestion sequence.
            conn.execute("""INSERT INTO orderflow_surge_events
                (event_id,observation_id,event_type,payload,source_id)
                VALUES (%s,%s,%s,%s,%s)""", (maturity_id, event["event_id"],
                "orderflow_volume_surge_matured", Jsonb({"result": result}), "legacy-source"))
            conn.execute("""INSERT INTO orderflow_surge_events
                (event_id,event_type,payload,source_id) VALUES (%s,%s,%s,%s)""",
                (event["event_id"], event["event_type"], Jsonb(event), "legacy-source"))
            # Unrecoverable records remain available in the retained table
            # and receive a visible migration diagnostic.
            invalid_id = "legacy-invalid:" + uuid.uuid4().hex
            conn.execute("""INSERT INTO orderflow_surge_events
                (event_id,event_type,payload) VALUES (%s,%s,%s)""",
                (invalid_id, event["event_type"], Jsonb({"pair": event["pair"]})))
            conn.commit()
            store.initialize()
            rows = store.read_orderflow_surges(limit=None, include_revisions=True)
            self.assertEqual(len(rows), 2)
            latest = store.read_orderflow_surges(event_id=event["event_id"])[0]
            self.assertEqual(latest["revision_id"], maturity_id)
            self.assertEqual(latest["status"], "unknown")
            self.assertEqual(latest["result"], result)
            self.assertEqual(latest["observation"], event["observation"])
            with store._connection() as migrated:
                count = migrated.execute("SELECT COUNT(*) AS n FROM orderflow_surge_events_legacy_v3").fetchone()["n"]
                diagnostic = migrated.execute("""SELECT payload FROM store_metadata
                    WHERE key='orderflow_surge_migration_v3'""").fetchone()["payload"]
            self.assertEqual(count, 3)
            self.assertEqual(diagnostic["imported"], 2)
            self.assertTrue(diagnostic["legacy_tables_retained"])
            self.assertEqual([item["event_id"] for item in diagnostic["skipped"]], [invalid_id])
            store.close()
            store.initialize()
            self.assertEqual(len(store.read_orderflow_surges(limit=None, include_revisions=True)), 2)

    def test_flat_legacy_observation_table_migrates_into_revision_store(self):
        from psycopg.types.json import Jsonb

        event = observation()
        # Older observation rows kept detector facts in payload while the
        # table columns carried identity and validation metadata. Those
        # columns must not be injected into the immutable observation.
        flat = {**event["observation"], "legacy_extra": {"event_type": "keep-me"}}
        with self.legacy_store() as (store, conn):
            conn.execute("""CREATE TABLE orderflow_surge_observations (
                event_id TEXT PRIMARY KEY, market TEXT, pair TEXT, timeframe TEXT,
                timestamp BIGINT, method_version TEXT, data_fingerprint TEXT, payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""INSERT INTO orderflow_surge_observations
                (event_id,market,pair,timeframe,timestamp,method_version,data_fingerprint,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (
                event["event_id"], event["market"], event["pair"], event["timeframe"], event["observation"]["timestamp"],
                event["method_version"], None, Jsonb(flat)))
            conn.commit()
            store.initialize()
            rows = store.read_orderflow_surges(limit=None, include_revisions=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["observation"], flat)
            self.assertEqual(rows[0]["data_fingerprint"],
                             hashlib.sha256(_json_text(flat).encode("utf-8")).hexdigest())
            self.assertNotIn("event_id", rows[0]["observation"])
            self.assertNotIn("event_type", rows[0]["observation"])
            self.assertEqual(rows[0]["status"], "unknown")
            with store._connection() as migrated:
                retained = migrated.execute("SELECT COUNT(*) AS n FROM orderflow_surge_observations").fetchone()["n"]
            self.assertEqual(retained, 1)


if __name__ == "__main__":
    unittest.main()
