"""Closed Binance 5m candle contracts, without network or database access."""

import unittest

from market_stream import StreamConfig, normalize_message


def kline():
    return {
        "stream": "btcusdt@kline_5m",
        "data": {
            "e": "kline", "E": 1700000400010, "s": "BTCUSDT",
            "k": {
                "t": 1700000100000, "T": 1700000399999, "s": "BTCUSDT",
                "i": "5m", "x": True, "o": "100", "h": "103", "l": "99",
                "c": "102", "v": "12.3400", "q": "1234.50", "n": 42,
                "V": "7.8900", "Q": "789.00",
            },
        },
    }


class KlineNormalizationTests(unittest.TestCase):
    def normalize(self, message):
        return normalize_message(message, StreamConfig(), 1700000400100)

    def test_closed_candle_preserves_source_and_volume_fields(self):
        message = kline()
        event = self.normalize(message)
        self.assertEqual((event["kind"], event["pair"], event["timeframe"]),
                         ("kline", "BTC/USDT:USDT", "5m"))
        candle = event["candle"]
        self.assertTrue(candle["closed"])
        self.assertEqual(candle["timestamp"], message["data"]["k"]["t"])
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"]),
                         (100, 103, 99, 102))
        self.assertEqual((candle["volume"], candle["quote_volume"], candle["num_trades"]),
                         ("12.3400", 1234.5, 42))
        self.assertEqual(candle["taker_buy_base"], "7.8900")
        self.assertAlmostEqual(candle["flow_imbal"], 2 * 789 / 1234.5 - 1)
        self.assertEqual(event["row"]["extras"]["exchange_event_timestamp"],
                         message["data"]["E"])
        self.assertTrue(event["row"]["extras"]["closed"])
        self.assertEqual(event["row"]["extras"]["raw"], message["data"])

    def test_direct_payload_and_spot_candle(self):
        event = self.normalize(kline()["data"])
        self.assertEqual(event["pair"], "BTC/USDT:USDT")
        event = normalize_message(kline(), StreamConfig(pairs=("BTC/USDT",), market="spot"), 100)
        self.assertEqual((event["market"], event["pair"]), ("spot", "BTC/USDT"))

    def test_stream_and_kline_symbols_must_match_payload(self):
        for field, value in (("stream", "ethusdt@kline_5m"), ("s", "ETHUSDT"), ("s", None)):
            with self.subTest(field=field, value=value):
                message = kline()
                if field == "stream":
                    message[field] = value
                elif value is None:
                    del message["data"]["k"][field]
                else:
                    message["data"]["k"][field] = value
                with self.assertRaises(ValueError):
                    self.normalize(message)

    def test_missing_payload_symbol_is_rejected(self):
        message = kline()
        del message["data"]["s"]
        with self.assertRaises(ValueError):
            self.normalize(message)

    def test_open_or_other_interval_candle_is_ignored(self):
        message = kline()
        message["data"]["k"]["x"] = False
        message["data"]["k"]["o"] = "not-an-open-candle-price"
        self.assertIsNone(self.normalize(message))
        message = kline()
        message["data"]["k"]["i"] = "1m"
        self.assertIsNone(self.normalize(message))

    def test_closed_flag_requires_a_boolean(self):
        for value in (None, 0, 1, "true", "false"):
            with self.subTest(value=value):
                message = kline()
                message["data"]["k"]["x"] = value
                with self.assertRaises(ValueError):
                    self.normalize(message)
        message = kline()
        del message["data"]["k"]["x"]
        with self.assertRaises(ValueError):
            self.normalize(message)

    def test_ohlc_bounds_are_enforced(self):
        for changes in ({"h": "101"}, {"l": "101"}, {"h": "98"},
                        {"o": "104"}, {"c": "98"},
                        {"h": "99.99999999999999999", "c": "100"}):
            with self.subTest(changes=changes):
                message = kline()
                message["data"]["k"].update(changes)
                with self.assertRaises(ValueError):
                    self.normalize(message)
        message = kline()
        message["data"]["k"].update(o="100", h="100", l="100", c="100")
        self.assertIsNotNone(self.normalize(message))

    def test_float_fields_reject_nonfinite_and_overflow_values(self):
        for field in ("o", "h", "l", "c", "q", "Q"):
            for value in ("NaN", "Infinity", "-Infinity", "1e1000"):
                with self.subTest(field=field, value=value):
                    message = kline()
                    message["data"]["k"][field] = value
                    with self.assertRaises(ValueError):
                        self.normalize(message)

    def test_prices_reject_zero_negative_and_float_underflow(self):
        for value in ("0", "-1", "1e-1000"):
            message = kline()
            message["data"]["k"]["o"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.normalize(message)

    def test_taker_buy_quote_cannot_exceed_total_before_rounding(self):
        message = kline()
        message["data"]["k"].update(q="1.00000000000000001", Q="1.00000000000000002")
        with self.assertRaises(ValueError):
            self.normalize(message)
        message["data"]["k"].update(q="0", Q="0")
        self.assertNotIn("flow_imbal", self.normalize(message)["candle"])


class KlineSubscriptionTests(unittest.TestCase):
    def test_futures_subscribes_every_pair_on_market_partition(self):
        connections = dict(StreamConfig().connections())
        self.assertIn("btcusdt@kline_5m", connections["trades"])
        self.assertIn("ethusdt@kline_5m", connections["trades"])
        self.assertIn("fstream.binance.com/market/stream?", connections["trades"])
        self.assertNotIn("@kline_5m", connections["orderbooks"])

    def test_spot_subscribes_every_pair(self):
        connections = StreamConfig(pairs=("BTC/USDT", "ETH/USDT"), market="spot").connections()
        self.assertEqual(len(connections), 1)
        url = connections[0][1]
        self.assertIn("stream.binance.com:9443/stream?", url)
        self.assertIn("btcusdt@kline_5m", url)
        self.assertIn("ethusdt@kline_5m", url)


if __name__ == "__main__":
    unittest.main()
