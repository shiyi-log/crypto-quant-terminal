"""Data-only contracts for generation-2 multi-timeframe inputs."""

import unittest
from unittest.mock import Mock

from strategy_data import (
    BREADTH_PAIRS,
    closed_candles,
    load_spec_candles,
    resample_closed_candles,
    strategy_pairs,
    timeframe_milliseconds,
)
from strategy_lab_service import StrategyLabService
from strategy_signals import _history


def candle(timestamp, close=100.0, *, closed=True, extras=None):
    return {"timestamp": timestamp, "open": close - 0.5, "high": close + 1,
            "low": close - 1, "close": close, "volume": 10.0,
            "extras": {"closed": closed, **(extras or {})}}


def candle_store(direct, source, target_timeframe):
    store = Mock()

    def get(*args, **kwargs):
        rows = direct if args[3] == target_timeframe else source
        end = kwargs.get("end")
        eligible = [row for row in rows if end is None or row["timestamp"] <= end]
        return eligible[-kwargs["limit"]:]

    store.get_candles.side_effect = get
    return store


class StrategyDataTests(unittest.TestCase):
    def test_timeframes_and_frozen_universes(self):
        self.assertEqual(timeframe_milliseconds("15m"), 900_000)
        self.assertEqual(timeframe_milliseconds("1h"), 3_600_000)
        self.assertEqual(timeframe_milliseconds("1d"), 86_400_000)
        self.assertEqual(strategy_pairs({"strategy_id": "s13_daily_trend_multi_horizon"}),
                         ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT"))
        self.assertEqual(len(strategy_pairs({"strategy_id": "s19_multi_coin_breadth"})), 20)
        self.assertEqual(strategy_pairs({"strategy_id": "s19_multi_coin_breadth"}), BREADTH_PAIRS)
        self.assertEqual(strategy_pairs({"strategy_id": "s24_cross_sectional_lowvol_Dgen3_100x_maker"}),
                         BREADTH_PAIRS)

    def test_resample_requires_complete_closed_source_bucket(self):
        rows = [candle(index * 300_000, 100 + index, extras={"quote_volume": 2,
                       "taker_buy_base": 3, "num_trades": 4}) for index in range(12)]
        result = resample_closed_candles(rows, "5m", "15m", as_of_ms=3_600_000)
        self.assertEqual([row["timestamp"] for row in result], [0, 900_000, 1_800_000, 2_700_000])
        self.assertEqual(result[0]["open"], 99.5)
        self.assertEqual(result[0]["close"], 102)
        self.assertEqual(result[0]["volume"], 30)
        self.assertEqual(result[0]["extras"]["quote_volume"], 6)
        self.assertEqual(result[0]["extras"]["num_trades"], 12)

        missing = rows[:]
        missing.pop(4)
        incomplete = resample_closed_candles(missing, "5m", "15m", as_of_ms=3_600_000)
        self.assertEqual([row["timestamp"] for row in incomplete], [0, 1_800_000, 2_700_000])

    def test_resample_excludes_partial_target_bucket_and_open_source(self):
        rows = [candle(index * 300_000, 100 + index) for index in range(4)]
        rows[-1]["extras"]["closed"] = False
        self.assertEqual([row["timestamp"] for row in
                          resample_closed_candles(rows, "5m", "15m", as_of_ms=1_200_000)], [0])
        rows[-1]["extras"]["closed"] = True
        self.assertEqual(resample_closed_candles(rows, "5m", "15m", as_of_ms=600_000),
                         [])

    def test_direct_target_data_is_preferred_and_filtered_by_as_of(self):
        store = Mock()
        store.get_candles.return_value = [candle(0), candle(3_600_000), candle(7_200_000)]
        result = load_spec_candles(store, {"strategy_id": "s14_vol_compression_release",
                                           "timeframe": "1h", "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
                                    limit=2, as_of_ms=7_200_000)
        self.assertEqual([row["timestamp"] for row in result["BTC/USDT:USDT"]], [0, 3_600_000])
        # Direct target timeframe is queried before any fallback source query.
        self.assertEqual(store.get_candles.call_args_list[0].args[:4],
                         ("binance", "futures", "BTC/USDT:USDT", "1h"))

    def test_current_native_close_does_not_resample_before_next_native_boundary(self):
        for timeframe in ("15m", "1h", "1d"):
            with self.subTest(timeframe=timeframe):
                interval = timeframe_milliseconds(timeframe)
                direct = [candle(index * interval) for index in range(750)]
                store = candle_store(direct, [], timeframe)
                as_of = 750 * interval + min(interval - 1, 1_800_000)
                rows = load_spec_candles(store, {"timeframe": timeframe,
                                               "universe": ["BTC/USDT:USDT"]},
                                        limit=750, as_of_ms=as_of)["BTC/USDT:USDT"]
                self.assertEqual(len(rows), 750)
                self.assertEqual([call.args[3] for call in store.get_candles.call_args_list],
                                 [timeframe])

    def test_forming_last_row_does_not_reduce_full_native_warmup(self):
        interval = timeframe_milliseconds("1d")
        direct = [candle(index * interval) for index in range(751)]
        direct[-1]["extras"]["closed"] = False
        store = candle_store(direct, [], "1d")
        rows = load_spec_candles(store, {"timeframe": "1d",
                                       "universe": ["BTC/USDT:USDT"]},
                                limit=750, as_of_ms=750 * interval + 1_800_000)["BTC/USDT:USDT"]
        self.assertEqual(len(rows), 750)
        self.assertEqual([call.args[3] for call in store.get_candles.call_args_list], ["1d"])

    def test_complete_daily_history_fetches_only_missing_tail_from_five_minutes(self):
        interval = timeframe_milliseconds("1d")
        ratio = interval // 300_000
        direct = [candle(index * interval) for index in range(750)]
        source = [candle(index * 300_000) for index in range(749 * ratio, 752 * ratio)]
        store = candle_store(direct, source, "1d")
        rows = load_spec_candles(store, {"timeframe": "1d", "universe": ["BTC/USDT:USDT"]},
                                as_of_ms=752 * interval)["BTC/USDT:USDT"]
        self.assertEqual([row["timestamp"] for row in rows],
                         [index * interval for index in range(2, 752)])
        self.assertLess(store.get_candles.call_args_list[-1].kwargs["limit"], 1000)
        continuous, error = _history(rows, 752 * interval, bar_ms=interval)
        self.assertIsNone(error)
        self.assertEqual(len(continuous), 750)

    def test_missing_target_timeframe_falls_back_to_contiguous_lower_timeframe(self):
        store = Mock()
        base = [candle(index * 300_000, 100 + index) for index in range(24)]

        def get(*args, **kwargs):
            return base if args[3] == "5m" else []

        store.get_candles.side_effect = get
        result = load_spec_candles(store, {"strategy_id": "s14_vol_compression_release",
                                           "timeframe": "1h", "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
                                    limit=2, as_of_ms=7_200_000)
        self.assertEqual([row["timestamp"] for row in result["BTC/USDT:USDT"]], [0, 3_600_000])
        self.assertEqual(result["BTC/USDT:USDT"][0]["extras"]["source_timeframe"], "5m")

    def test_stale_direct_history_is_extended_by_a_short_fresh_resampled_tail(self):
        for timeframe, tail_size in (("15m", 120), ("1h", 29)):
            with self.subTest(timeframe=timeframe):
                interval = timeframe_milliseconds(timeframe)
                ratio = interval // 300_000
                direct = [candle(index * interval) for index in range(750)]
                tail_start, tail_end = 740, 740 + tail_size
                source = [candle(index * 300_000)
                          for index in range(tail_start * ratio, tail_end * ratio)]
                as_of = tail_end * interval
                rows = load_spec_candles(candle_store(direct, source, timeframe),
                                        {"timeframe": timeframe, "universe": ["BTC/USDT:USDT"]},
                                        as_of_ms=as_of)["BTC/USDT:USDT"]

                self.assertEqual([row["timestamp"] for row in rows],
                                 [index * interval for index in range(tail_end - 750, tail_end)])
                continuous, error = _history(rows, as_of, bar_ms=interval)
                self.assertIsNone(error)
                self.assertEqual(len(continuous), 750)

    def test_merge_does_not_fill_a_gap_or_replace_direct_with_an_incomplete_bucket(self):
        interval = timeframe_milliseconds("15m")
        direct = [candle(index * interval) for index in range(750)]
        source = [candle(index * 300_000, 200)
                  for index in range(748 * 3, 768 * 3)
                  if index != 748 * 3 + 1 and index // 3 != 750]
        as_of = 768 * interval
        rows = load_spec_candles(candle_store(direct, source, "15m"),
                                {"timeframe": "15m", "universe": ["BTC/USDT:USDT"]},
                                as_of_ms=as_of)["BTC/USDT:USDT"]
        by_stamp = {row["timestamp"]: row for row in rows}

        self.assertEqual(by_stamp[748 * interval]["close"], 100)
        self.assertNotIn(750 * interval, by_stamp)
        continuous, error = _history(rows, as_of, bar_ms=interval)
        self.assertIsNone(error)
        self.assertEqual([row["timestamp"] for row in continuous],
                         [index * interval for index in range(751, 768)])

    def test_fresh_resampled_row_owns_ohlcv_and_extras_without_borrowing_missing_fields(self):
        interval = timeframe_milliseconds("15m")
        old_extras = {"taker_buy_base": 8, "quote_volume": 99, "num_trades": 99,
                      "flow_imbal": 0.9, "live_snapshot_timestamp": 123}
        direct = [candle(index * interval, extras=old_extras) for index in range(2)]
        source = [candle(index * 300_000, 200 + index,
                         extras={"taker_buy_base": 3, "quote_volume": 2, "num_trades": 4})
                  for index in range(3, 9)]
        # Missing one constituent makes the aggregate optional field unavailable.
        source[0]["extras"].pop("taker_buy_base")
        source[0]["extras"].pop("quote_volume")
        rows = load_spec_candles(candle_store(direct, source, "15m"),
                                {"timeframe": "15m", "universe": ["BTC/USDT:USDT"]},
                                limit=3, as_of_ms=3 * interval)["BTC/USDT:USDT"]

        self.assertEqual(rows[0]["extras"]["taker_buy_base"], 8)
        self.assertEqual(rows[1]["close"], 205)
        self.assertEqual(rows[1]["volume"], 30)
        self.assertEqual(rows[1]["extras"], {"closed": True, "source_timeframe": "5m",
                                             "source_bars": 3, "num_trades": 12})
        self.assertEqual(rows[2]["extras"]["taker_buy_base"], 9)
        self.assertEqual(rows[2]["extras"]["quote_volume"], 6)

    def test_forming_direct_row_cannot_suppress_fallback_or_enter_closed_inputs(self):
        interval = timeframe_milliseconds("1h")
        # A mistaken closed flag on the forming candle must not count as history.
        direct = [candle(0), candle(2 * interval), candle(3 * interval)]
        source = [candle(index * 300_000, 200)
                  for index in range(36)]
        source[11]["extras"]["closed"] = False
        store = candle_store(direct, source, "1h")
        as_of = 2 * interval + interval // 2
        rows = load_spec_candles(store, {"timeframe": "1h", "universe": ["BTC/USDT:USDT"]},
                                limit=2, as_of_ms=as_of)["BTC/USDT:USDT"]

        self.assertEqual([call.args[3] for call in store.get_candles.call_args_list],
                         ["1h", "5m"])
        self.assertEqual([row["timestamp"] for row in rows], [0, interval])
        self.assertEqual(rows[0]["close"], 100)
        self.assertEqual(rows[1]["close"], 200)
        self.assertTrue(all(row["timestamp"] + interval <= as_of for row in rows))

    def test_longer_old_resampling_retains_fresher_direct_rows(self):
        interval = timeframe_milliseconds("1h")
        direct = [candle(index * interval, 150,
                         extras={"taker_buy_base": 7, "source": "direct"})
                  for index in (5, 6)]
        source = [candle(index * 300_000, 200,
                         extras={"taker_buy_base": 3}) for index in range(72)]
        rows = load_spec_candles(candle_store(direct, source, "1h"),
                                {"timeframe": "1h", "universe": ["BTC/USDT:USDT"]},
                                limit=7, as_of_ms=7 * interval)["BTC/USDT:USDT"]

        self.assertEqual([row["timestamp"] for row in rows],
                         [index * interval for index in range(7)])
        self.assertEqual(rows[5]["close"], 150)
        self.assertEqual(rows[5]["extras"]["taker_buy_base"], 7)
        self.assertEqual(rows[6]["extras"]["source"], "direct")

    def test_service_native_input_loader_keeps_generation2_data_separate(self):
        store = Mock()
        store.get_candles.return_value = [candle(index * 3_600_000, 100 + index)
                                          for index in range(3)]
        service = StrategyLabService(store, Mock(), [], now_ms=7_200_000)
        spec = {"strategy_id": "s14_vol_compression_release", "timeframe": "1h",
                "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"]}
        loaded = service.load_strategy_inputs(spec, now_ms=7_200_000, limit=2)
        self.assertEqual(list(loaded), ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertIs(service.strategy_history[spec["strategy_id"]], loaded)
        self.assertEqual(service.history, {})

    def test_duplicate_closed_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            closed_candles([candle(0), candle(0)], "5m")


if __name__ == "__main__":
    unittest.main()
