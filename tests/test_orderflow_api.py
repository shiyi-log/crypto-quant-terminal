"""Order-flow detail, statistics, and older-store HTTP contracts."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
LIST_PATHS = (
    "/api/locals/orderflow/surges",
    "/api/locals/database/orderflow-surges",
)
STATS_PATHS = (
    "/api/locals/orderflow/surge-stats",
    "/api/locals/database/orderflow-surges/stats",
)
EVENT_ID = "volume-surge:test"
PRICE_SPIKE_PATH = "/api/locals/orderflow/price-spikes"


class OrderflowApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="quant_orderflow_api_")
        cls.addClassCleanup(cls.temp_dir.cleanup)
        spec = importlib.util.spec_from_file_location(
            "quant_orderflow_api_contract_test", ROOT / "auth_service.py"
        )
        assert spec is not None and spec.loader is not None
        cls.auth = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temp_dir.name}):
            spec.loader.exec_module(cls.auth)

    def _handler(self, path):
        handler = object.__new__(self.auth.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        handler._read_body = Mock(return_value=b"")
        handler._bearer = Mock(return_value="test-only-token")
        handler._send = Mock(side_effect=lambda code, payload: (code, payload))
        return handler

    def _get(self, path, store=None, *, authenticated=True, error=None):
        handler = self._handler(path)
        with patch.object(self.auth, "verify_token", return_value=("admin" if authenticated else None)), \
             patch.object(self.auth, "get_store", return_value=store, side_effect=error) as get_store:
            result = handler.do_GET()
        return result, get_store

    def _observation(self, **changes):
        return {
            "event_id": EVENT_ID,
            "market": "futures",
            "pair": "BTC/USDT:USDT",
            "timeframe": "5m",
            "timestamp": 100,
            "status": "unknown",
            **changes,
        }

    def test_all_routes_require_auth_before_reading_store(self):
        paths = (*LIST_PATHS, *STATS_PATHS, PRICE_SPIKE_PATH,
                 *(path + "/" + EVENT_ID for path in LIST_PATHS))
        for path in paths:
            with self.subTest(path=path):
                (code, _payload), get_store = self._get(path, authenticated=False)
                self.assertEqual(code, 401)
                get_store.assert_not_called()

    def test_list_aliases_share_the_canonical_query(self):
        for path in LIST_PATHS:
            with self.subTest(path=path):
                read = Mock(return_value=[self._observation()])
                (code, payload), _get_store = self._get(path, SimpleNamespace(read_orderflow_surges=read))
                self.assertEqual(code, 200)
                self.assertEqual(payload["items"][0]["event_id"], EVENT_ID)
                read.assert_called_once_with(
                    market="futures", pair=None, timeframe="5m", status=None,
                    limit=100, include_revisions=False,
                )

    def test_detail_prefers_overall_projection_and_preserves_revision_facts(self):
        revision = self._observation(status="matured", revision_no=2, result={"horizon": "5m"})
        projection = self._observation(revisions=[revision], completed_horizons=["5m"])
        for path in LIST_PATHS:
            with self.subTest(path=path):
                get = Mock(return_value=projection)
                read = Mock(side_effect=AssertionError("detail should use the overall projection"))
                (code, payload), _get_store = self._get(
                    path + "/" + EVENT_ID,
                    SimpleNamespace(get_surge_observation=get, read_orderflow_surges=read),
                )
                self.assertEqual(code, 200)
                self.assertEqual(payload["observation"]["status"], "unknown")
                self.assertEqual(payload["item"], payload["observation"])
                self.assertEqual(payload["events"], [revision])
                self.assertEqual(payload["revisions"], payload["events"])
                self.assertNotIn("revisions", payload["observation"])
                self.assertEqual(projection["revisions"], [revision])
                get.assert_called_once_with(EVENT_ID)
                read.assert_not_called()

    def test_detail_reads_by_event_id_when_projection_adapter_is_unavailable(self):
        original = self._observation(revision_no=1)
        latest = self._observation(status="matured", revision_no=2)
        read = Mock(return_value=[latest, original, {"event_id": "other", "revision_no": 9}])
        (code, payload), _get_store = self._get(
            LIST_PATHS[0] + "/" + EVENT_ID, SimpleNamespace(read_orderflow_surges=read)
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["item"]["status"], "unknown")
        self.assertEqual(payload["item"]["completed_horizons"], [])
        self.assertEqual(payload["item"]["required_horizons"], ["5m", "30m", "1h", "4h"])
        self.assertEqual(payload["item"]["event_id"], EVENT_ID)
        self.assertEqual(payload["revisions"], [original, latest])
        read.assert_called_once_with(
            market="futures", pair=None, timeframe="5m", event_id=EVENT_ID,
            limit=None, include_revisions=True,
        )

    def test_unknown_event_returns_404(self):
        for path in LIST_PATHS:
            with self.subTest(path=path):
                store = SimpleNamespace(get_surge_observation=Mock(return_value=None))
                (code, payload), _get_store = self._get(path + "/missing", store)
                self.assertEqual(code, 404)
                self.assertIn("未找到", payload["detail"])

    def test_detail_rejects_invalid_id_before_reading_store(self):
        for event_id in ("", "%2Fetc%2Fpasswd", "test/child", "a%00b", "a" * 257):
            with self.subTest(event_id=event_id):
                (code, _payload), get_store = self._get(LIST_PATHS[0] + "/" + event_id)
                self.assertEqual(code, 400)
                get_store.assert_not_called()

    def test_detail_respects_pair_filter_for_projection_adapter(self):
        store = SimpleNamespace(get_surge_observation=Mock(return_value=self._observation()))
        (code, _payload), _get_store = self._get(
            LIST_PATHS[0] + "/" + EVENT_ID + "?pair=ETH%2FUSDT%3AUSDT", store
        )
        self.assertEqual(code, 404)

    def test_detail_and_stats_reject_invalid_or_duplicate_filters(self):
        for path in (LIST_PATHS[0] + "/" + EVENT_ID, *STATS_PATHS):
            for query in ("?market=spot", "?timeframe=1h", "?pair=SOL%2FUSDT%3AUSDT", "?market=futures&market=futures"):
                with self.subTest(path=path, query=query):
                    (code, _payload), get_store = self._get(path + query)
                    self.assertEqual(code, 400)
                    get_store.assert_not_called()

    def test_stats_aliases_preserve_horizon_metrics_and_unknown_counts(self):
        bucket = {"pair": "BTC/USDT:USDT", "total": 3, "unknown": 2, "matured": 1, "known": 1}
        raw = {"total": 3, "unknown": 2, "matured": 1, "known": 1,
               "statistics_horizon": "1h", "gross_return_mean": 0.02,
               "actual_fee_rate": None, "by_pair": [bucket]}
        for path in STATS_PATHS:
            with self.subTest(path=path):
                stats = Mock(return_value=raw)
                (code, payload), _get_store = self._get(path, SimpleNamespace(surge_stats=stats))
                self.assertEqual(code, 200)
                self.assertEqual(payload["summary"]["unknown"], 2)
                self.assertEqual(payload["statistics_horizon"], "1h")
                self.assertEqual(payload["gross_return_mean"], 0.02)
                self.assertIsNone(payload["actual_fee_rate"])
                self.assertEqual(payload["pairs"]["BTC/USDT:USDT"], bucket)
                self.assertIn("各窗口 unknown", payload["limitations"][0])
                stats.assert_called_once_with(pair=None, timeframe="5m")

    def test_fallback_stats_excludes_unknown_and_invalid_from_known_samples(self):
        rows = [self._observation(status=status) for status in ("unknown", "unknown", "matured", "invalid")]
        read = Mock(return_value=rows)
        (code, payload), _get_store = self._get(
            STATS_PATHS[0], SimpleNamespace(read_orderflow_surges=read)
        )
        self.assertEqual(code, 200)
        self.assertEqual({key: payload[key] for key in ("total", "unknown", "matured", "invalid", "known")},
                         {"total": 4, "unknown": 2, "matured": 1, "invalid": 1, "known": 1})
        read.assert_called_once_with(
            market="futures", pair=None, timeframe="5m", limit=None, include_revisions=False,
        )

    def test_zero_sample_stats_have_pair_counts_unknown_metrics_and_live_warmup(self):
        warmup = {
            "BTC/USDT:USDT": {"valid_bars": 93, "required_bars": 288, "ready": False},
            "ETH/USDT:USDT": {"valid_bars": 93, "required_bars": 288, "ready": False},
        }
        for path in STATS_PATHS:
            with self.subTest(path=path):
                raw = {"total": 0, "by_pair": [], "horizons": {}}
                stats = Mock(return_value=raw)
                read_status = Mock(return_value={"surge_warmup": warmup})
                (code, payload), _get_store = self._get(
                    path, SimpleNamespace(surge_stats=stats, get_document=read_status),
                )
                self.assertEqual(code, 200)
                self.assertEqual(payload["warmup"], warmup)
                self.assertEqual(set(payload["pairs"]), set(warmup))
                self.assertIsInstance(payload["by_pair"], list)
                for bucket in (payload["summary"], *payload["pairs"].values()):
                    self.assertEqual(bucket["total"], 0)
                    self.assertEqual(bucket["sample_count"], 0)
                    self.assertIsNone(bucket["gross_return_mean"])
                    self.assertEqual(set(bucket["horizons"]), {"5m", "30m", "1h", "4h"})
                    for window in bucket["horizons"].values():
                        self.assertEqual(window["sample_count"], 0)
                        for metric in ("gross_return_mean", "win_rate", "mfe_mean", "mae_mean",
                                       "required_fee_rate", "actual_fee_rate"):
                            self.assertIsNone(window[metric])
                self.assertEqual(raw, {"total": 0, "by_pair": [], "horizons": {}})
                read_status.assert_called_once_with("market_stream_status.json", {})

    def test_empty_old_stats_store_and_fallback_support_a_single_pair(self):
        pair = "ETH/USDT:USDT"
        for store in (
            SimpleNamespace(surge_stats=Mock(return_value={"total": 0, "by_pair": {}})),
            SimpleNamespace(read_orderflow_surges=Mock(return_value=[])),
        ):
            with self.subTest(store=store):
                (code, payload), _get_store = self._get(
                    STATS_PATHS[0] + "?pair=ETH%2FUSDT%3AUSDT", store,
                )
                self.assertEqual(code, 200)
                self.assertEqual(set(payload["pairs"]), {pair})
                self.assertEqual(payload["pairs"][pair]["total"], 0)
                self.assertIsNone(payload["pairs"][pair]["gross_return_mean"])
                self.assertEqual(payload["warmup"], {})

    def test_stats_pair_filter_also_filters_warmup(self):
        pair = "ETH/USDT:USDT"
        stats = Mock(return_value={"total": 0, "by_pair": []})
        warmup = {
            "BTC/USDT:USDT": {"valid_bars": 93},
            pair: {"valid_bars": 94},
        }
        (code, payload), _get_store = self._get(
            STATS_PATHS[0] + "?pair=ETH%2FUSDT%3AUSDT",
            SimpleNamespace(surge_stats=stats,
                            get_document=Mock(return_value={"surge_warmup": warmup})),
        )
        self.assertEqual(code, 200)
        self.assertEqual(set(payload["pairs"]), {pair})
        self.assertEqual(payload["warmup"], {pair: warmup[pair]})
        stats.assert_called_once_with(pair=pair, timeframe="5m")

    def test_old_reader_applies_bounds_before_public_limit(self):
        calls = []

        def old_read(market, pair, timeframe, status, limit, include_revisions):
            calls.append(limit)
            rows = [self._observation(event_id=str(ts), timestamp=ts) for ts in (300, 200, 100)]
            return rows if limit is None else rows[:limit]

        (code, payload), _get_store = self._get(
            LIST_PATHS[0] + "?start=100&end=100&limit=1",
            SimpleNamespace(read_orderflow_surges=old_read),
        )
        self.assertEqual(code, 200)
        self.assertEqual([row["timestamp"] for row in payload["items"]], [100])
        self.assertEqual(calls, [None])

    def test_old_flat_store_applies_bounds_before_public_limit(self):
        calls = []

        def old_list(pair, timeframe, status, limit, start=None, end=None):
            calls.append(limit)
            rows = [self._observation(event_id=str(ts), timestamp=ts) for ts in (300, 200, 100)]
            rows = rows if limit is None else rows[:limit]
            return [row for row in rows if (start is None or row["timestamp"] >= start)
                    and (end is None or row["timestamp"] <= end)]

        (code, payload), _get_store = self._get(
            LIST_PATHS[1] + "?start=100&end=100&limit=1",
            SimpleNamespace(list_surge_observations=old_list),
        )
        self.assertEqual(code, 200)
        self.assertEqual([row["timestamp"] for row in payload["items"]], [100])
        self.assertEqual(calls, [None])

    def test_internal_reader_type_error_is_not_retried_as_legacy_signature(self):
        read = Mock(side_effect=TypeError("private internal failure"))
        with self.assertRaisesRegex(TypeError, "private internal failure"):
            self._get(LIST_PATHS[0] + "?start=1", SimpleNamespace(read_orderflow_surges=read))
        self.assertEqual(read.call_count, 1)

    def test_out_of_range_bound_returns_400_without_database_access(self):
        for value in ("9007199254740993", "1" + "0" * 400):
            with self.subTest(value=value):
                (code, _payload), get_store = self._get(LIST_PATHS[0] + "?start=" + value)
                self.assertEqual(code, 400)
                get_store.assert_not_called()

    def test_database_failures_return_503_without_error_details(self):
        for path in (*LIST_PATHS, *STATS_PATHS, LIST_PATHS[0] + "/" + EVENT_ID):
            with self.subTest(path=path):
                (code, payload), _get_store = self._get(
                    path, error=self.auth.psycopg.OperationalError("private DSN")
                )
                self.assertEqual(code, 503)
                self.assertNotIn("private", payload["detail"])

    def test_query_database_failures_return_503(self):
        for path, method in ((LIST_PATHS[0], "read_orderflow_surges"),
                             (STATS_PATHS[0], "surge_stats"),
                             (LIST_PATHS[0] + "/" + EVENT_ID, "get_surge_observation")):
            with self.subTest(path=path):
                store = SimpleNamespace(**{method: Mock(side_effect=self.auth.psycopg.OperationalError("offline"))})
                (code, _payload), _get_store = self._get(path, store)
                self.assertEqual(code, 503)

    def test_price_spike_route_returns_online_trials_and_per_pair_stats(self):
        items = [{"event_id": "price-spike:test", "pair": "BTC/USDT:USDT",
                  "outcome": {"direction": "up"}}]
        summary = {"sample_scope": "live_online_only", "by_pair": {"BTC/USDT:USDT": {}}}
        warmup = {"BTC/USDT:USDT": {"valid_bars": 288, "required_bars": 288,
                                     "ready": True}}
        read = Mock(return_value=items)
        stats = Mock(return_value=summary)
        read_status = Mock(return_value={"surge_warmup": warmup})
        (code, payload), _get_store = self._get(
            PRICE_SPIKE_PATH + "?pair=BTC%2FUSDT%3AUSDT&direction=up&limit=25",
            SimpleNamespace(read_price_candidates=read, price_spike_stats=stats,
                            get_document=read_status),
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["items"], items)
        self.assertEqual(payload["stats"], summary)
        self.assertEqual(payload["warmup"], warmup)
        self.assertEqual(payload["sample_scope"], "live_online_only")
        read.assert_called_once_with(pair="BTC/USDT:USDT", direction="up",
                                     status=None, limit=25)
        stats.assert_called_once_with(pair="BTC/USDT:USDT")
        read_status.assert_called_once_with("market_stream_status.json", {})

    def test_price_spike_route_keeps_pending_trials_visible(self):
        items = [{"event_id": "price-spike:pending", "status": "unknown"}]
        read = Mock(return_value=items)
        stats = Mock(return_value={"sample_scope": "live_online_only"})
        (code, payload), _get_store = self._get(
            PRICE_SPIKE_PATH,
            SimpleNamespace(read_price_candidates=read, price_spike_stats=stats),
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["items"], items)
        read.assert_called_once_with(pair=None, direction="any", status=None, limit=100)

    def test_price_spike_route_rejects_bad_direction_before_database_access(self):
        (code, payload), get_store = self._get(PRICE_SPIKE_PATH + "?direction=long")
        self.assertEqual(code, 400)
        self.assertIn("direction", payload["detail"])
        get_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
