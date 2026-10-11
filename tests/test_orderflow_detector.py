import copy
import unittest

from orderflow_detector import VolumeSurgeDetector


def candle(timestamp, volume, *, closed=True):
    return {"timestamp": timestamp, "quote_volume": volume, "closed": closed}


class VolumeSurgeDetectorTests(unittest.TestCase):
    def test_uses_previous_window_only_and_assigns_threshold_band(self):
        seen = []
        detector = VolumeSurgeDetector(window_size=3, on_surge=seen.append)
        for timestamp in range(3):
            self.assertIsNone(detector.process("BTC/USDT:USDT", "5m", candle(timestamp, 10)))
        event = detector.process("BTC/USDT:USDT", "5m", candle(3, 45))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 10)
        self.assertEqual(event["observation"]["volume_ratio"], 4.5)
        self.assertEqual(event["observation"]["threshold_band"], "4x")
        self.assertEqual(event["event_id"], seen[0]["event_id"])

    def test_open_candle_and_warmup_do_not_emit_or_advance(self):
        detector = VolumeSurgeDetector(window_size=2)
        self.assertIsNone(detector.process("ETH/USDT:USDT", "5m", candle(0, 10, closed=False)))
        self.assertIsNone(detector.process("ETH/USDT:USDT", "5m", candle(0, 10)))
        self.assertIsNone(detector.process("ETH/USDT:USDT", "5m", candle(1, 10)))
        self.assertIsNotNone(detector.process("ETH/USDT:USDT", "5m", candle(2, 25)))

    def test_duplicate_and_unsupported_candles_are_ignored(self):
        detector = VolumeSurgeDetector(window_size=1)
        self.assertIsNone(detector.process("SOL/USDT:USDT", "5m", candle(0, 100)))
        event = detector.process("BTC/USDT:USDT", "5m", candle(0, 10))
        self.assertIsNone(event)
        event = detector.process("BTC/USDT:USDT", "5m", candle(1, 25))
        self.assertIsNotNone(event)
        self.assertIsNone(detector.process("BTC/USDT:USDT", "5m", candle(1, 25)))
        self.assertIsNone(detector.process("BTC/USDT:USDT", "1m", candle(2, 100)))

    def test_btc_and_eth_windows_are_isolated_and_seed_is_bounded(self):
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed("BTC/USDT:USDT", [1, 2, 3])
        self.assertIsNotNone(detector.process("BTC/USDT:USDT", "5m", candle(10, 8)))
        self.assertIsNone(detector.process("ETH/USDT:USDT", "5m", candle(10, 8)))
        self.assertIsNone(detector.process("ETH/USDT:USDT", "5m", candle(11, 8)))
        self.assertIsNotNone(detector.process("ETH/USDT:USDT", "5m", candle(12, 20)))

    def test_shift_one_baseline_is_previous_median_and_excludes_current_candle(self):
        detector = VolumeSurgeDetector(window_size=3)
        pair = "BTC/USDT:USDT"
        for timestamp, volume in enumerate((10, 20, 100)):
            self.assertIsNone(detector.process(pair, "5m", candle(timestamp, volume)))

        # The preceding median is 20. Including the current 40 would produce
        # a median of 30 and incorrectly miss the 2x event.
        event = detector.process(pair, "5m", candle(3, 40))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 20.0)
        self.assertEqual(event["observation"]["volume_ratio"], 2.0)
        self.assertEqual(event["observation"]["threshold_band"], "2x")

    def test_live_baseline_rolls_past_the_window_limit(self):
        detector = VolumeSurgeDetector(window_size=3)
        pair = "BTC/USDT:USDT"
        for timestamp, volume in enumerate((100, 100, 10, 10, 10)):
            self.assertIsNone(detector.process(pair, "5m", candle(timestamp, volume)))

        event = detector.process(pair, "5m", candle(5, 20))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 10)
        self.assertEqual(event["observation"]["volume_ratio"], 2)
        self.assertEqual(len(detector._volumes[("futures", pair)]), 3)

    def test_threshold_boundaries_are_inclusive(self):
        pair = "BTC/USDT:USDT"
        for threshold, band in ((2.0, "2x"), (4.0, "4x"), (8.0, "8x"), (20.0, "20x")):
            with self.subTest(threshold=threshold):
                detector = VolumeSurgeDetector(window_size=2)
                detector.seed(pair, [10, 10])
                event = detector.process(pair, "5m", candle(100, 10 * threshold))
                self.assertIsNotNone(event)
                self.assertEqual(event["observation"]["threshold"], threshold)
                self.assertEqual(event["observation"]["threshold_band"], band)
                self.assertAlmostEqual(event["observation"]["volume_ratio"], threshold)

    def test_zero_missing_and_malformed_volumes_do_not_advance_state(self):
        pair = "ETH/USDT:USDT"
        invalid_values = (None, True, False, "", "not-a-number", -1, float("nan"))
        for invalid in invalid_values:
            with self.subTest(invalid=repr(invalid)):
                detector = VolumeSurgeDetector(window_size=2)
                self.assertIsNone(detector.process(pair, "5m", candle(0, 10)))
                # Invalid input does not claim the timestamp, so a corrected
                # close for that same timestamp remains observable.
                self.assertIsNone(detector.process(pair, "5m", candle(1, invalid)))
                self.assertIsNone(detector.process(pair, "5m", candle(1, 20)))
                event = detector.process(pair, "5m", candle(2, 30))
                self.assertIsNotNone(event)
                self.assertEqual(event["observation"]["baseline_quote_volume"], 15.0)

        detector = VolumeSurgeDetector(window_size=2)
        self.assertIsNone(detector.process(pair, "5m", {"timestamp": 0, "closed": True}))
        self.assertIsNone(detector.process(pair, "5m", candle(0, 10)))

    def test_zero_volume_is_a_valid_trial_and_advances_the_baseline(self):
        pair = "BTC/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed(pair, [10, 10], timestamps=[0, 300_000])

        self.assertIsNone(detector.process(pair, "5m", candle(600_000, 0)))
        candidate = detector.last_candidate
        self.assertEqual(candidate["observation"]["volume_ratio"], 0)
        self.assertEqual(candidate["observation"]["ratio_band"], "lt1")

        event = detector.process(pair, "5m", candle(900_000, 10))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 5)

    def test_duplicate_timestamp_does_not_change_baseline(self):
        detector = VolumeSurgeDetector(window_size=2)
        pair = "BTC/USDT:USDT"
        self.assertIsNone(detector.process(pair, "5m", candle(0, 10)))
        self.assertIsNone(detector.process(pair, "5m", candle(1, 20)))
        self.assertIsNone(detector.process(pair, "5m", candle(1, 100)))
        event = detector.process(pair, "5m", candle(2, 30))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 15.0)

    def test_emitted_observation_is_immutable_after_source_changes(self):
        seen = []
        detector = VolumeSurgeDetector(window_size=1, on_surge=seen.append)
        detector.seed("BTC/USDT:USDT", [10])
        source = candle(1, 30)
        source.update({"num_trades": 7, "flow_imbal": 0.25})
        event = detector.process("BTC/USDT:USDT", "5m", source)
        snapshot = copy.deepcopy(event)
        source.update({"quote_volume": 9999, "num_trades": 999, "flow_imbal": -1})
        self.assertEqual(event, snapshot)
        self.assertEqual(seen[0], snapshot)

    def test_custom_method_version_is_part_of_event_identity(self):
        pair = "BTC/USDT:USDT"
        default = VolumeSurgeDetector(window_size=1)
        custom = VolumeSurgeDetector(window_size=1, method_version="orderflow-volume-surge-v4")
        default.seed(pair, [10])
        custom.seed(pair, [10])
        default_event = default.process(pair, "5m", candle(1, 30))
        custom_event = custom.process(pair, "5m", candle(1, 30))
        self.assertEqual(custom_event["method_version"], "orderflow-volume-surge-v4")
        self.assertNotEqual(default_event["event_id"], custom_event["event_id"])

    def test_default_288_window_drops_old_history_and_excludes_current_close(self):
        detector = VolumeSurgeDetector()
        pair = "ETH/USDT:USDT"
        for index in range(288):
            self.assertIsNone(detector.process(pair, "5m", candle(index * 300_000, 10)))

        # Replacing the entire preceding day must remove all low-volume history.
        # An expanding-history median would remain 15 instead of the latest 20.
        for index in range(288, 576):
            detector.process(pair, "5m", candle(index * 300_000, 20))
        event = detector.process(pair, "5m", candle(576 * 300_000, 40))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 20.0)
        self.assertEqual(event["observation"]["volume_ratio"], 2.0)
        self.assertEqual(event["observation"]["threshold_band"], "2x")
        self.assertEqual(event["status"], "unknown")

    def test_seed_rejects_booleans_without_claiming_warmup(self):
        detector = VolumeSurgeDetector(window_size=2)
        pair = "BTC/USDT:USDT"
        detector.seed(pair, [True, False, 10])
        self.assertIsNone(detector.process(pair, "5m", candle(0, 10)))
        event = detector.process(pair, "5m", candle(300_000, 20))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 10.0)

    def test_restored_event_id_suppresses_restart_replay(self):
        pair = "BTC/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=1)
        detector.seed(pair, [10])
        event = detector.process(pair, "5m", candle(300_000, 30))
        restarted = VolumeSurgeDetector(window_size=1)
        restarted.seed(pair, [10])
        restarted.restore_event_ids([None, event["event_id"]])
        self.assertIsNone(restarted.process(pair, "5m", candle(300_000, 30)))

    def test_seeded_timestamps_suppress_restart_replay_without_changing_baseline(self):
        pair = "BTC/USDT:USDT"
        restarted = VolumeSurgeDetector(window_size=2)
        restarted.seed(pair, [10, 20], timestamps=[0, 300_000])

        self.assertIsNone(restarted.process(pair, "5m", candle(300_000, 999)))
        self.assertIsNone(restarted.process(pair, "5m", candle(150_000, 999)))
        event = restarted.process(pair, "5m", candle(600_000, 30))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 15.0)
        self.assertEqual(event["observation"]["volume_ratio"], 2.0)

    def test_candidate_trial_records_sub_two_x_ratio_without_emitting_surge(self):
        pair = "ETH/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed(pair, [10, 10], timestamps=[0, 300_000])

        event = detector.process(pair, "5m", candle(600_000, 15), source_timestamp=900_000)

        self.assertIsNone(event)
        candidate = detector.last_candidate
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["event_type"], "price_spike_candidate_observed")
        self.assertEqual(candidate["observation"]["volume_ratio"], 1.5)
        self.assertEqual(candidate["observation"]["ratio_band"], "1to2")
        self.assertEqual(candidate["observed_at"], 900_000)
        self.assertEqual(candidate["observation"]["baseline_start_timestamp"], 0)
        self.assertEqual(candidate["observation"]["baseline_end_timestamp"], 300_000)
        self.assertEqual(candidate["observation"]["baseline_source"],
                         "binance_websocket_closed_kline")

    def test_seed_uses_only_contiguous_trailing_bars(self):
        pair = "BTC/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed(pair, [10, 20, 100, 100],
                      timestamps=[0, 300_000, 900_000, 1_200_000])

        self.assertEqual(list(detector._volumes[("futures", pair)]), [100.0, 100.0])
        event = detector.process(pair, "5m", candle(1_500_000, 200))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 100.0)

    def test_live_gap_clears_stale_baseline_until_a_full_window_reforms(self):
        pair = "BTC/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=2)
        self.assertIsNone(detector.process(pair, "5m", candle(0, 10)))
        self.assertIsNone(detector.process(pair, "5m", candle(300_000, 10)))
        self.assertIsNone(detector.process(pair, "5m", candle(900_000, 100)))
        self.assertIsNone(detector.process(pair, "5m", candle(1_200_000, 100)))

        event = detector.process(pair, "5m", candle(1_500_000, 200))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 100.0)

    def test_timestamp_seed_keeps_valid_rows_aligned_and_orders_closed_history(self):
        pair = "ETH/USDT:USDT"
        detector = VolumeSurgeDetector(window_size=2)
        detector.seed(pair, [20, "invalid", 10, 999],
                      timestamps=[300_000, 600_000, 0, True])

        event = detector.process(pair, "5m", candle(600_000, 30))
        self.assertIsNotNone(event)
        self.assertEqual(event["observation"]["baseline_quote_volume"], 15.0)
        with self.assertRaisesRegex(ValueError, "same length"):
            detector.seed(pair, [10, 20], timestamps=[0])


if __name__ == "__main__":
    unittest.main()
