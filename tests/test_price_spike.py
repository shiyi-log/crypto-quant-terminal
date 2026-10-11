"""Online price-spike labels never backfill or skip missing candles."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest

from price_spike import (
    BAR_INTERVAL_MS,
    PRICE_SPIKE_METHOD_VERSION,
    PROFILE_RADIUS,
    mature_price_spike_observations,
)


PAIR = "BTC/USDT:USDT"
START = 300_000 * 100


def candidate(timestamp=START, *, pair=PAIR, close=100.0, ratio=5.0, band="4to8"):
    observation = {
        "timestamp": timestamp, "close": close, "quote_volume": 500.0,
        "baseline_quote_volume": 100.0, "volume_ratio": ratio,
        "ratio_band": band, "baseline_fingerprint": "baseline-hash",
    }
    fingerprint = hashlib.sha256(json.dumps(
        observation, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "event_id": f"price-spike:{pair}:{timestamp}",
        "event_type": "price_spike_candidate_observed",
        "method_version": PRICE_SPIKE_METHOD_VERSION,
        "data_fingerprint": fingerprint,
        "exchange": "binance", "market": "futures", "pair": pair,
        "timeframe": "5m", "timestamp": timestamp,
        "observed_at": timestamp + BAR_INTERVAL_MS, "status": "unknown",
        "observation": observation,
    }


class PriceStore:
    def __init__(self, events=(), candles=(), observations=None):
        self.events = {event["event_id"]: copy.deepcopy(event) for event in events}
        self.candles = list(copy.deepcopy(candles))
        self.observations = observations or {}
        self.results = {}
        self.candle_queries = []

    def read_pending_price_candidates(self, limit=None):
        rows = []
        for event_id, event in self.events.items():
            if (event_id, "outcome") not in self.results:
                rows.append(event)
        return sorted(rows, key=lambda row: row["timestamp"])

    def get_candles(self, exchange, market, pair, timeframe, candle_type,
                    limit=None, start=None, end=None):
        self.candle_queries.append((pair, start, end))
        rows = [copy.deepcopy(row) for row in self.candles
                if row["timestamp"] >= start and (end is None or row["timestamp"] <= end)]
        rows.sort(key=lambda row: row["timestamp"])
        return rows[-limit:] if limit is not None else rows

    def append_price_candidate_result(self, event_id, result_kind, result, **_kwargs):
        self.results[(event_id, result_kind)] = copy.deepcopy(result)
        return f"{event_id}:{result_kind}"

    def read_price_candidates(self, *, pair=None, direction=None, status=None, limit=None):
        rows = []
        for event_id, event in self.events.items():
            if pair is not None and event["pair"] != pair:
                continue
            outcome = self.results.get((event_id, "outcome"))
            profile = self.results.get((event_id, "profile"))
            row = {**event, "outcome": outcome, "profile": profile,
                   "profile_status": profile.get("status") if profile else None,
                   "status": outcome.get("status", "unknown") if outcome else "unknown"}
            if status is not None and row["status"] != status:
                continue
            if direction == "any" and (not outcome or outcome.get("direction") not in {"up", "down"}):
                continue
            if direction in {"up", "down"} and (not outcome or outcome.get("direction") != direction):
                continue
            rows.append(row)
        return sorted(rows, key=lambda row: row["timestamp"], reverse=True)[:limit]

    def read_pending_price_profiles(self, limit=None):
        rows = self.read_price_candidates(status="matured", limit=None)
        return [row for row in rows
                if row.get("outcome", {}).get("direction") in {"up", "down"}
                and row.get("profile") is None][:limit]

    def read_price_observations(self, pair, start, end):
        return [copy.deepcopy(row) for row in self.observations.get(pair, [])
                if start <= row["timestamp"] <= end]


class PriceSpikeMaturityTests(unittest.TestCase):
    def _future(self, close, *, missing=(), through=3):
        return [
            {"timestamp": START + index * BAR_INTERVAL_MS, "close": close,
             "extras": {"live_event_timestamp": START + index * BAR_INTERVAL_MS + BAR_INTERVAL_MS,
                        "closed": True}}
            for index in range(1, through + 1) if index not in missing
        ]

    def test_candidate_is_not_labeled_until_three_exact_future_closes_exist(self):
        event = candidate()
        store = PriceStore([event])

        self.assertEqual(mature_price_spike_observations(store, now_ms=START + 100 * BAR_INTERVAL_MS), [])
        store.candles.extend(self._future(102))
        appended = mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)

        result = store.results[(event["event_id"], "outcome")]
        self.assertEqual(len(appended), 1)
        self.assertEqual(result["status"], "matured")
        self.assertEqual(result["direction"], "up")
        self.assertAlmostEqual(result["return_pct"], 0.02)
        self.assertEqual(result["outcome_window_end_utc"], START + 3 * BAR_INTERVAL_MS)

    def test_close_return_below_threshold_is_a_valid_none_outcome(self):
        event = candidate()
        store = PriceStore([event], self._future(101.49))

        mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)

        result = store.results[(event["event_id"], "outcome")]
        self.assertEqual(result["status"], "matured")
        self.assertEqual(result["direction"], "none")
        self.assertLess(result["return_pct"], 0.015)

    def test_decimal_threshold_boundaries_are_inclusive_without_epsilon(self):
        for anchor, final_close, direction in (
            (100.0, 101.5, "up"),
            (100.0, 98.5, "down"),
            (110.0, 111.65, "up"),
            (110.0, 108.35, "down"),
            (100.0, math.nextafter(101.5, 0.0), "none"),
            (100.0, math.nextafter(98.5, math.inf), "none"),
        ):
            with self.subTest(anchor=anchor, final_close=final_close):
                event = candidate(close=anchor)
                store = PriceStore([event], self._future(final_close))
                mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)
                result = store.results[(event["event_id"], "outcome")]
                self.assertEqual(result["status"], "matured")
                self.assertEqual(result["direction"], direction)
                self.assertEqual(result["method_version"], PRICE_SPIKE_METHOD_VERSION)
                self.assertAlmostEqual(result["return_pct"], final_close / anchor - 1)

    def test_intermediate_moves_do_not_replace_the_third_close_anchor(self):
        event = candidate()
        candles = self._future(100.0)
        candles[0]["close"] = 110.0
        candles[1]["close"] = 90.0
        store = PriceStore([event], candles)
        mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)
        result = store.results[(event["event_id"], "outcome")]
        self.assertEqual(result["direction"], "none")
        self.assertEqual(result["return_pct"], 0.0)

    def test_missing_future_candle_is_invalid_not_a_no_spike(self):
        event = candidate()
        store = PriceStore([event], self._future(105, missing=(2, 3)))

        before_deadline = mature_price_spike_observations(store, now_ms=START + 100 * BAR_INTERVAL_MS)
        store.candles.extend(self._future(105)[-1:])
        after_deadline = mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)

        self.assertEqual(before_deadline, [])
        result = store.results[(event["event_id"], "outcome")]
        self.assertEqual(after_deadline[0]["result_kind"], "outcome")
        self.assertEqual(result["status"], "invalid")
        self.assertIn(START + 2 * BAR_INTERVAL_MS, result["missing_timestamps"])

    def test_full_profile_is_appended_only_after_plus_twelve_is_available(self):
        event = candidate()
        store = PriceStore([event], self._future(102))
        ratios = []
        for offset in range(-PROFILE_RADIUS, PROFILE_RADIUS + 1):
            if offset > 3:
                continue
            ts = START + offset * BAR_INTERVAL_MS
            ratios.append({"event_id": f"candidate:{ts}", "timestamp": ts,
                           "observation": {"volume_ratio": float(offset + 20)}})
        store.observations[PAIR] = ratios

        mature_price_spike_observations(store, now_ms=START + 4 * BAR_INTERVAL_MS)
        self.assertNotIn((event["event_id"], "profile"), store.results)
        store.candles.extend(self._future(102, through=PROFILE_RADIUS)[3:])
        for offset in range(4, PROFILE_RADIUS + 1):
            ts = START + offset * BAR_INTERVAL_MS
            ratios.append({"event_id": f"candidate:{ts}", "timestamp": ts,
                           "observation": {"volume_ratio": float(offset + 20)}})
        appended = mature_price_spike_observations(
            store, now_ms=START + (PROFILE_RADIUS + 1) * BAR_INTERVAL_MS
        )

        profile = store.results[(event["event_id"], "profile")]
        self.assertEqual(appended[0]["result_kind"], "profile")
        self.assertEqual(profile["status"], "matured")
        self.assertEqual(len(profile["points"]), 25)
        self.assertEqual(profile["points"][0]["offset"], -12)
        self.assertEqual(profile["points"][-1]["offset"], 12)

    def test_profile_gap_is_explicitly_invalid(self):
        event = candidate()
        store = PriceStore([event], self._future(102, through=PROFILE_RADIUS))
        store.results[(event["event_id"], "outcome")] = {
            "status": "matured", "direction": "up",
        }
        store.observations[PAIR] = [{
            "timestamp": START + offset * BAR_INTERVAL_MS,
            "observation": {"volume_ratio": 2.0},
        } for offset in range(-PROFILE_RADIUS, PROFILE_RADIUS + 1) if offset != -4]

        mature_price_spike_observations(
            store, now_ms=START + (PROFILE_RADIUS + 1) * BAR_INTERVAL_MS
        )

        profile = store.results[(event["event_id"], "profile")]
        self.assertEqual(profile["status"], "invalid")
        self.assertIn(START - 4 * BAR_INTERVAL_MS, profile["missing_timestamps"])
        self.assertNotIn(-4, {point["offset"] for point in profile["points"]})


if __name__ == "__main__":
    unittest.main()
